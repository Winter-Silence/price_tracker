"""Тесты пагинации выдачи Avito в книжном режиме (/books)."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from parsers.avito import build_page_url
from scheduler import jobs


BASE_URL = (
    "https://www.avito.ru/all/knigi_i_zhurnaly/knigi/knigi-ASgBAgICAUTOAuoK"
    "?cd=1&context=H4sIAAAAAAAA&f=ASgBAgICBUTGB&localPriority=1"
    "&q=knigi&s=104"
)


def _item(lot_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        url=f"https://www.avito.ru/moskva/kniga_{lot_id}",
        title="Книга",
        price=100.0,
        posted_at_text="сегодня",
    )


class _FakeParser:
    """Парсер-заглушка: отдаёт заранее заданные страницы выдачи."""

    def __init__(self, pages: dict[int, list] | None = None):
        self.pages = pages or {}
        self.calls: list[int] = []
        self._captcha_detected = False
        self._profile_blocked = False

    async def get_all_items_from_search(self, url: str, page: int = 1):
        self.calls.append(page)
        return self.pages.get(page)

    async def _random_delay(self) -> None:
        return None


class BuildPageUrlTest(unittest.TestCase):
    def test_page_one_removes_p(self):
        assert build_page_url(BASE_URL + "&p=3", 1) == BASE_URL

    def test_page_two_adds_p(self):
        assert build_page_url(BASE_URL, 2) == BASE_URL + "&p=2"

    def test_page_replaces_existing_p(self):
        assert build_page_url(BASE_URL + "&p=2", 3) == BASE_URL + "&p=3"

    def test_other_params_preserved(self):
        url = build_page_url(BASE_URL, 4)
        for marker in ("s=104", "f=ASgBAgICBUTGB", "context=H4sIAAAAAAAA",
                       "cd=1", "localPriority=1", "q=knigi"):
            assert marker in url, marker

    def test_order_preserved(self):
        url = build_page_url(BASE_URL, 2)
        query = url.split("?", 1)[1]
        names = [part.split("=", 1)[0] for part in query.split("&")]
        assert names == ["cd", "context", "f", "localPriority", "q", "s", "p"]


class WalkPagesTest(unittest.IsolatedAsyncioTestCase):
    """Правила обхода страниц: стоп по фронту, бюджет, курсор глубины."""

    async def asyncSetUp(self):
        self._env = mock.patch.dict(os.environ, {
            "AVITO_BOOKS_MAX_PAGES": "5",
            "AVITO_BOOKS_PAGE_DELAY_MIN": "0",
            "AVITO_BOOKS_PAGE_DELAY_MAX": "0",
        })
        self._env.start()
        self.link = {"id": 11, "search_url": BASE_URL}

    async def asyncTearDown(self):
        self._env.stop()

    async def test_stops_on_page_with_only_known_lots(self):
        parser = _FakeParser({
            1: [_item(1)],
            2: [_item(2)],
            3: [_item(1)],  # уже известный лот
        })
        items, status, visited, covered, _deep = await jobs._walk_avito_pages(
            parser, self.link, {1}, [5],
        )
        assert status == jobs.SCAN_FRONTIER
        assert parser.calls == [1, 2, 3]
        assert covered == 3
        # Лот 1 встретился на стр. 1 и 3 — в visited он один.
        assert len(visited) == 2

    async def test_page_with_known_and_unknown_lot_does_not_stop(self):
        parser = _FakeParser({
            1: [_item(1)],
            2: [_item(1), _item(2)],
            3: [_item(3)],
            4: [_item(1)],
        })
        _items, status, _visited, _covered, _deep = await jobs._walk_avito_pages(
            parser, self.link, {1}, [5],
        )
        assert status == jobs.SCAN_FRONTIER
        # Стр. 2 дала новый лот — обход продолжился до стр. 4.
        assert parser.calls == [1, 2, 3, 4]

    async def test_page_budget_limits_walk(self):
        parser = _FakeParser({
            1: [_item(1)],
            2: [_item(2)],
            3: [_item(3)],
        })
        _items, status, _visited, covered, _deep = await jobs._walk_avito_pages(
            parser, self.link, set(), [2],
        )
        assert status == jobs.SCAN_BUDGET
        assert parser.calls == [1, 2]
        assert covered == 2

    async def test_retry_charges_page_budget(self):
        parser = _FakeParser({
            1: [_item(1), _item(2), _item(3), _item(4)],
            2: [_item(5)],  # короткая страница → один повтор
            3: [],           # ещё один запрос, но повтор уже не влезает
        })
        budget = [4]

        _items, status, _visited, _covered, _deep = await jobs._walk_avito_pages(
            parser, self.link, set(), budget,
        )

        assert status == jobs.SCAN_INCOMPLETE
        # Считаются реальные запросы страниц, включая повтор, а не логические страницы.
        assert parser.calls == [1, 2, 2, 3]
        assert len(parser.calls) == 4
        assert budget[0] == 0

    async def test_coverage_uses_cursor_and_skips_covered_pages(self):
        parser = _FakeParser({
            1: [_item(1)],
            4: [_item(4)],
            5: [_item(5)],
        })
        _items, status, _visited, covered, _deep = await jobs._walk_avito_pages(
            parser, self.link, set(), [2],
            covered_page=3, coverage=True,
        )
        assert status == jobs.SCAN_BUDGET
        # Стр. 1 + продолжение с курсора (стр. 4), а не с 2.
        assert parser.calls == [1, 4]
        assert covered == 4

    async def test_empty_first_page_is_incomplete(self):
        parser = _FakeParser({1: [], 2: []})
        items, status, _visited, _covered, _deep = await jobs._walk_avito_pages(
            parser, self.link, set(), [5],
        )
        assert items is None
        assert status == jobs.SCAN_INCOMPLETE

    async def test_listing_ended_after_full_page(self):
        parser = _FakeParser({
            1: [_item(1)],
            2: [_item(2)],
            3: [],
        })
        _items, status, _visited, _covered, _deep = await jobs._walk_avito_pages(
            parser, self.link, set(), [5],
        )
        assert status == jobs.SCAN_FRONTIER
        # Стр. 3 пуста — страница перезапрошена один раз.
        assert parser.calls == [1, 2, 3, 3]


class LoadListingTest(unittest.IsolatedAsyncioTestCase):
    """Книжный режим: базлайн, курсор глубины, пропуск mark_missing."""

    async def asyncSetUp(self):
        self._env = mock.patch.dict(os.environ, {
            "AVITO_BOOKS_MAX_PAGES": "2",
            "AVITO_BOOKS_PAGE_DELAY_MIN": "0",
            "AVITO_BOOKS_PAGE_DELAY_MAX": "0",
        })
        self._env.start()
        self.link = {
            "id": 11,
            "search_url": BASE_URL,
            "deep_scan_done": 0,
            "deep_scan_page": 1,
        }
        self.parser = _FakeParser({1: [_item(1)], 2: [_item(2)]})

    async def asyncTearDown(self):
        self._env.stop()

    def _patches(self, state=None):
        return [
            mock.patch.object(jobs, "get_avito_item_state",
                              mock.AsyncMock(return_value=state or {})),
            mock.patch.object(jobs, "sync_avito_items", mock.AsyncMock(
                return_value={it.url: {"id": idx, "notified_at": None}
                              for idx, it in enumerate(self.parser.pages.get(1, []), 1)})),
            mock.patch.object(jobs, "mark_missing_avito_items",
                              mock.AsyncMock(return_value=0)),
            mock.patch.object(jobs, "mark_avito_items_notified",
                              mock.AsyncMock(return_value=None)),
            mock.patch.object(jobs, "mark_avito_deep_scan_done",
                              mock.AsyncMock(return_value=None)),
            mock.patch.object(jobs, "set_avito_deep_scan_page",
                              mock.AsyncMock(return_value=None)),
        ]

    async def test_baseline_marks_lots_notified_without_notifications(self):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            payload, _captcha = await jobs._load_avito_listing(
                self.parser, self.link, False,
                deep_scan=True, page_budget=[3],
            )
            assert payload is not None
            assert payload["baseline"] is True
            jobs.mark_avito_items_notified.assert_awaited()
            jobs.mark_missing_avito_items.assert_not_awaited()
        finally:
            for p in patches:
                p.stop()

    async def test_second_scan_is_not_baseline(self):
        self.link["deep_scan_done"] = 1
        known = {1: {
            "id": 1, "is_active": 1, "notified_at": "2026-01-01 00:00:00",
            "miss_streak": 0, "price": 100.0,
        }}
        patches = self._patches(state=known)
        for p in patches:
            p.start()
        try:
            payload, _captcha = await jobs._load_avito_listing(
                self.parser, self.link, False,
                deep_scan=True, page_budget=[3],
            )
            assert payload is not None
            assert payload["baseline"] is False
            jobs.mark_avito_items_notified.assert_not_awaited()
            jobs.mark_missing_avito_items.assert_not_awaited()
        finally:
            for p in patches:
                p.stop()

    async def test_cursor_saved_when_deeper_page_visited(self):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            await jobs._load_avito_listing(
                self.parser, self.link, False,
                deep_scan=True, page_budget=[3],
            )
            jobs.set_avito_deep_scan_page.assert_awaited_with(11, 2)
        finally:
            for p in patches:
                p.stop()

    async def test_coverage_marks_only_deep_lots_notified(self):
        # Первичное покрытие — базлайн для исторической части выдачи:
        # лоты со страниц глубже первой помечаются уведомлёнными, а свежие
        # лоты с первой страницы остаются на обычную проверку.
        known = {1: {
            "id": 1, "is_active": 1, "notified_at": "2026-01-01 00:00:00",
            "miss_streak": 0, "price": 100.0,
        }}
        synced = {
            "https://www.avito.ru/moskva/kniga_1": {"id": 1, "notified_at": None},
            "https://www.avito.ru/moskva/kniga_2": {"id": 2, "notified_at": None},
        }
        patches = self._patches(state=known)
        patches[1] = mock.patch.object(
            jobs, "sync_avito_items", mock.AsyncMock(return_value=synced))
        for p in patches:
            p.start()
        try:
            payload, _captcha = await jobs._load_avito_listing(
                self.parser, self.link, False,
                deep_scan=True, page_budget=[3],
            )
            assert payload is not None
            assert payload["baseline"] is True
            jobs.mark_avito_items_notified.assert_awaited_once_with([2])
        finally:
            for p in patches:
                p.stop()


class _FakeParserClass:
    instance = None

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "avito.ru" in url

    def __init__(self, profile_dir=None, profile_markers=None):
        self._profile_dir = profile_dir
        self._profile_markers = profile_markers
        self._captcha_detected = False
        self._profile_blocked = False
        type(self).instance = self

    async def start_session(self):
        return None

    async def end_session(self):
        return None


class PageBudgetTest(unittest.IsolatedAsyncioTestCase):
    """Бюджет страниц на домен — один счётчик на все книжные ссылки."""

    async def test_page_budget_shared_between_book_links(self):
        links = [
            {"id": 11, "search_url": BASE_URL, "track_mode": "books",
             "deep_scan_done": 1, "deep_scan_page": 1},
            {"id": 12, "search_url": BASE_URL + "&cd=1", "track_mode": "books",
             "deep_scan_done": 1, "deep_scan_page": 1},
        ]
        budgets: list[list[int]] = []

        async def fake_poll(parser, link, captcha_hit, budget, page_budget, browser):
            # Остаток страниц переходит следующей ссылке домена.
            budgets.append([id(page_budget), page_budget[0]])
            page_budget[0] -= 1
            return False

        with mock.patch.object(jobs, "get_active_avito_search_links",
                               mock.AsyncMock(return_value=links)), \
             mock.patch.object(jobs, "PARSERS", [_FakeParserClass]), \
             mock.patch.object(jobs, "_poll_books_link", side_effect=fake_poll), \
             mock.patch.object(jobs, "_get_avito_books_pages_per_cycle",
                               return_value=4), \
             mock.patch.object(jobs.asyncio, "sleep", mock.AsyncMock()):
            await jobs.poll_avito_books()

        assert budgets[0][1] == 4
        # Счётчик тот же самый объект, остаток — 3 у второй ссылки.
        assert budgets[1] == [budgets[0][0], 3]


if __name__ == "__main__":
    unittest.main()
