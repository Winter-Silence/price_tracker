"""Тесты изоляции режимов Avito: ценовой опрос не ждёт книжный обход."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from parsers.avito import AvitoItemDetails
from parsers import base as parser_base
from parsers.base import BaseParser
from scheduler import jobs
from utils import chrome_cleanup


PRICE_URL = "https://www.avito.ru/astrahan/bytovaya_elektronika?q=seasonic&s=104"
BOOKS_URL = "https://m.avito.ru/all/knigi_i_zhurnaly?q=knigi&s=104"
OLD_URL = "https://www.avito.ru/moskva/lot_1"


def _price_link(link_id: int) -> dict:
    return {
        "id": link_id, "search_url": PRICE_URL, "track_mode": "price",
        "title_filter": "", "product_name": "Цена", "user_id": 1,
    }


def _books_link(link_id: int, url: str = BOOKS_URL) -> dict:
    return {
        "id": link_id, "search_url": url, "track_mode": "books",
        "deep_scan_done": 1, "deep_scan_page": 1, "title_filter": "",
        "user_id": 1,
    }


def _lot(lot_id: int, domain: str) -> SimpleNamespace:
    return SimpleNamespace(
        url=f"https://{domain}/moskva/lot_{lot_id}",
        title="Литературные памятники",
        price=100.0,
        posted_at_text="сегодня",
    )


class _FakeAvito:
    """Парсер Avito: поведение задаётся тестом через атрибуты класса."""

    pages = None      # async callable(url, page, parser) -> list | None
    details = None    # async callable(url, parser) -> детали | None
    events: list = []
    end_blocked = None

    def __init__(self, profile_dir=None, profile_markers=None):
        self._profile_dir = Path(profile_dir).resolve() if profile_dir else None
        self._profile_markers = profile_markers
        self._captcha_detected = False
        self._profile_blocked = False

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "avito.ru" in url

    async def start_session(self):
        type(self).events.append("session_start")

    async def end_session(self):
        # Как BaseParser: по флагу решает, пересоздавать ли профиль, и сбрасывает его.
        type(self).end_blocked = self._profile_blocked
        self._profile_blocked = False
        type(self).events.append("session_end")

    async def sweep_profile_chrome(self):
        type(self).events.append(f"sweep:{self._profile_dir}")

    async def _random_delay(self):
        return None

    async def get_all_items_from_search(self, url, page=1):
        return await type(self).pages(url, page, self)

    async def get_item_details(self, url):
        return await type(self).details(url, self)


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class _PartialBrowserParser(BaseParser):
    """Парсер, у которого Chrome успел появиться до сбоя запуска."""

    marketplace = "avito"
    instance = None
    events: list = []

    def __init__(self, profile_dir=None, profile_markers=None):
        super().__init__(
            profile_dir=profile_dir, profile_markers=profile_markers)
        self._browser = None
        self._session_active = False
        type(self).instance = self

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "avito.ru" in url

    async def start_session(self):
        self.created_browser = _FakeBrowser()
        self._browser = self.created_browser
        raise RuntimeError("verification failed")

    async def sweep_profile_chrome(self):
        type(self).events.append(f"sweep:{self._profile_dir}")

    async def get_price(self, url: str) -> float | None:
        return None

    async def get_cheapest_from_search(
        self, url: str, title_filter: str,
    ):
        return None


class _AvitoTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            prefix="avito-mode-", dir="/tmp/opencode")
        tmp_path = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "AVITO_BOOKS_MAX_PAGES": "2",
            "AVITO_BOOKS_PAGE_DELAY_MIN": "0",
            "AVITO_BOOKS_PAGE_DELAY_MAX": "0",
            "AVITO_PROFILE_DIR": str(tmp_path / "price"),
            "AVITO_BOOKS_PROFILE_DIR": str(tmp_path / "books"),
        })
        self._env.start()
        _FakeAvito.events = []
        _FakeAvito.end_blocked = None
        jobs._books_rotation.clear()

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _jobs_stack(self, links, pages, details=None) -> ExitStack:
        _FakeAvito.pages = pages
        _FakeAvito.details = details
        stack = ExitStack()
        stack.enter_context(mock.patch.object(
            jobs, "get_active_avito_search_links",
            mock.AsyncMock(return_value=links)))
        stack.enter_context(mock.patch.object(jobs, "PARSERS", [_FakeAvito]))
        stack.enter_context(mock.patch.object(
            jobs, "AVITO_PAGE_DELAY_SECONDS", 0))
        return stack

    def _price_db_stack(self, state, synced) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(mock.patch.object(
            jobs, "get_avito_item_state",
            mock.AsyncMock(return_value=state)))
        stack.enter_context(mock.patch.object(
            jobs, "sync_avito_items", mock.AsyncMock(return_value=synced)))
        stack.enter_context(mock.patch.object(
            jobs, "mark_missing_avito_items", mock.AsyncMock(return_value=0)))
        stack.enter_context(mock.patch.object(
            jobs, "mark_avito_items_notified", mock.AsyncMock(return_value=None)))

        async def notify(*args, **kwargs):
            _FakeAvito.events.append("price_notified")
            return True

        stack.enter_context(mock.patch.object(
            jobs, "send_avito_new_item_notification", side_effect=notify))
        return stack

    @staticmethod
    def _price_state() -> tuple[dict, dict]:
        state = {1: {
            "id": 1, "is_active": 1, "notified_at": "2026-01-01 00:00:00",
            "miss_streak": 0, "price": 100.0,
        }}
        synced = {
            "https://www.avito.ru/moskva/lot_2": {
                "id": 2, "notified_at": None},
        }
        return state, synced


class ParallelProfileTest(_AvitoTestBase):
    """Ценовой и книжный режимы используют разные профили и не ждут друг друга."""

    async def test_price_pass_does_not_wait_for_book_page(self):
        page_started = asyncio.Event()
        release_page = asyncio.Event()

        async def pages(url, page, parser):
            if "knigi" in url:
                _FakeAvito.events.append(f"book_page{page}")
                if page == 1:
                    page_started.set()
                    await release_page.wait()
                return [_lot(7, "m.avito.ru")] if page == 1 else None
            _FakeAvito.events.append("price_page")
            return [_lot(2, "www.avito.ru")]

        state, synced = self._price_state()
        links = [_books_link(11), _price_link(16)]
        with self._jobs_stack(links, pages), self._price_db_stack(state, synced):
            books_task = asyncio.create_task(jobs.poll_avito_books())
            await page_started.wait()
            await asyncio.wait_for(jobs.poll_avito_search(), timeout=1)
            assert "price_notified" in _FakeAvito.events, _FakeAvito.events
            release_page.set()
            await asyncio.wait_for(books_task, timeout=5)

    async def test_price_block_does_not_break_books_pass(self):
        async def pages(url, page, parser):
            if "knigi" in url:
                _FakeAvito.events.append(f"book_page{page}")
                return [_lot(7, "m.avito.ru")] if page == 1 else None
            parser._captcha_detected = True
            parser._profile_blocked = True
            return None

        async def details(url, parser):
            _FakeAvito.events.append("book_card")
            return AvitoItemDetails(
                url=url, title="Литературные памятники", description="",
                posted_at_text="сегодня", reviews_text="5 отзывов",
                price=100.0,
            )

        state, synced = self._price_state()
        synced[_lot(7, "m.avito.ru").url] = {"id": 7, "notified_at": None}
        links = [_books_link(11), _price_link(16)]
        with self._jobs_stack(links, pages, details), \
             self._price_db_stack(state, synced), \
             mock.patch.object(jobs, "lot_title_interesting",
                               return_value=(True, "test")), \
             mock.patch.object(jobs, "get_avito_lot_check",
                               mock.AsyncMock(return_value=None)), \
             mock.patch.object(jobs, "save_avito_lot_check",
                               mock.AsyncMock(return_value=None)):
            blocked = await jobs.poll_avito_search()
            await asyncio.wait_for(jobs.poll_avito_books(), timeout=5)

        assert blocked is True
        assert "book_card" in _FakeAvito.events, _FakeAvito.events


class BlockIsolationTest(_AvitoTestBase):
    """Блокировка в одном режиме не подавляет другой режим."""

    async def test_book_block_does_not_suppress_price_links(self):
        async def pages(url, page, parser):
            if "knigi" in url:
                parser._captcha_detected = True
                return None
            return [_lot(2, "www.avito.ru")]

        state, synced = self._price_state()
        links = [_books_link(11), _price_link(16)]
        with self._jobs_stack(links, pages), self._price_db_stack(state, synced):
            await asyncio.wait_for(jobs.poll_avito_books(), timeout=5)
            await asyncio.wait_for(jobs.poll_avito_search(), timeout=5)

        assert "price_notified" in _FakeAvito.events, _FakeAvito.events

    async def test_each_task_only_polls_its_own_links(self):
        price_links: list[int] = []
        book_links: list[int] = []

        async def fake_price(parser, link, captcha_hit, browser=None):
            price_links.append(link["id"])
            return False

        async def fake_books(parser, link, captcha_hit, budget, page_budget, browser):
            book_links.append(link["id"])
            return False

        links = [_books_link(11), _price_link(16)]
        with self._jobs_stack(links, None), \
             mock.patch.object(jobs, "_poll_avito_link", side_effect=fake_price), \
             mock.patch.object(jobs, "_poll_books_link", side_effect=fake_books):
            await jobs.poll_avito_search()
            await jobs.poll_avito_books()

        assert price_links == [16]
        assert book_links == [11]


class BooksBudgetRotationTest(_AvitoTestBase):
    """Общий бюджет домена достаётся всем книжным ссылкам по кругу."""

    async def test_page_budget_rotates_between_book_links(self):
        links = [
            _books_link(idx, f"https://www.avito.ru/knigi?p={idx}")
            for idx in range(1, 6)
        ]
        firsts: list[int] = []

        async def fake_poll(parser, link, captcha_hit, budget, page_budget, browser):
            # Первая ссылка с ненулевым остатком съедает весь бюджет страниц.
            if page_budget[0] > 0:
                firsts.append(link["id"])
                page_budget[0] = 0
            return False

        with self._jobs_stack(links, None), \
             mock.patch.object(jobs, "_poll_books_link", side_effect=fake_poll), \
             mock.patch.object(jobs, "_get_avito_books_pages_per_cycle",
                               return_value=4):
            for _ in range(5):
                await jobs.poll_avito_books()

        # Пятая ссылка не осталась голодной: очередь сдвигается каждый цикл.
        assert firsts == [1, 2, 3, 4, 5]


class TimeoutIsolationTest(_AvitoTestBase):
    """Зависший книжный запрос прерывается по таймауту и не держит профиль."""

    async def test_hung_book_request_does_not_block_price(self):
        book_started = asyncio.Event()

        async def pages(url, page, parser):
            if "knigi" in url:
                _FakeAvito.events.append("book_page")
                book_started.set()
                await asyncio.Event().wait()  # висит, пока не сработает таймаут
            return [_lot(2, "www.avito.ru")]

        state, synced = self._price_state()
        links = [_books_link(11), _price_link(16)]
        books_profile = jobs._resolve_avito_profile_dir(books=True)
        browser = jobs._get_avito_browser(books_profile)
        with self._jobs_stack(links, pages), self._price_db_stack(state, synced), \
             mock.patch.object(jobs, "AVITO_REQUEST_TIMEOUT_SECONDS", 0.05), \
             mock.patch.object(jobs, "send_admin_notification",
                               mock.AsyncMock()):
            books_task = asyncio.create_task(jobs.poll_avito_books())
            await book_started.wait()
            await asyncio.wait_for(jobs.poll_avito_search(), timeout=5)
            await asyncio.wait_for(books_task, timeout=5)

        assert "price_notified" in _FakeAvito.events, _FakeAvito.events
        # Блокировка отпущена, сессия закрыта — следующий цикл начнётся чисто.
        assert browser._lock.locked() is False
        assert browser._users == 0
        assert browser._parser is None

    async def test_non_cooperative_book_request_is_abandoned_and_price_proceeds(self):
        book_started = asyncio.Event()
        release_book = asyncio.Event()

        async def pages(url, page, parser):
            if "knigi" in url:
                _FakeAvito.events.append("book_page")
                book_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # Игнорируем отмену: стандартный wait_for ждал бы её вечно.
                    _FakeAvito.events.append("book_ignored_cancel")
                    await release_book.wait()
            return [_lot(2, "www.avito.ru")]

        state, synced = self._price_state()
        links = [_books_link(11), _price_link(16)]
        books_profile = jobs._resolve_avito_profile_dir(books=True)
        browser = jobs._get_avito_browser(books_profile)
        with self._jobs_stack(links, pages), self._price_db_stack(state, synced), \
             mock.patch.object(jobs, "AVITO_REQUEST_TIMEOUT_SECONDS", 0.05), \
             mock.patch.object(jobs, "send_admin_notification",
                               mock.AsyncMock()):
            books_task = asyncio.create_task(jobs.poll_avito_books())
            await book_started.wait()
            await asyncio.wait_for(jobs.poll_avito_search(), timeout=1)
            await asyncio.wait_for(books_task, timeout=1)

            background = list(browser._background_tasks)
            release_book.set()
            await asyncio.gather(*background, return_exceptions=True)

        assert "book_ignored_cancel" in _FakeAvito.events
        assert "price_notified" in _FakeAvito.events
        assert browser._lock.locked() is False


class SessionCleanupTest(_AvitoTestBase):
    """Отмена задачи не должна оставлять счётчик ссылок и живой Chrome."""

    async def test_cancelled_session_releases_refcount_and_chrome(self):
        browser = jobs._get_avito_browser()
        in_body = asyncio.Event()

        async def body():
            async with browser.session(_FakeAvito):
                in_body.set()
                await asyncio.sleep(3600)

        task = asyncio.create_task(body())
        await in_body.wait()
        assert browser._users == 1
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        assert browser._users == 0
        assert browser._parser is None
        assert _FakeAvito.events.count("session_end") == 1, _FakeAvito.events

    async def test_failed_start_session_closes_partial_chrome(self):
        browser = jobs._get_avito_browser()

        class Broken(_FakeAvito):
            async def start_session(self):
                type(self).events.append("session_start")
                raise RuntimeError("chrome failed")

        with self.assertRaises(RuntimeError):
            async with browser.session(Broken):
                pass

        assert browser._users == 0
        assert browser._parser is None
        assert _FakeAvito.events.count("session_end") == 1, _FakeAvito.events

    async def test_failed_start_closes_browser_created_before_error(self):
        browser = jobs._get_avito_browser()

        with self.assertRaises(RuntimeError):
            async with browser.session(_PartialBrowserParser):
                pass

        parser = _PartialBrowserParser.instance
        assert parser.created_browser.closed is True
        assert parser._browser is None
        assert browser._users == 0
        assert browser._parser is None
        assert browser._lock.locked() is False

    async def test_hung_session_start_cannot_delay_price_beyond_bound(self):
        books_profile = jobs._resolve_avito_profile_dir(books=True)
        browser = jobs._get_avito_browser(books_profile)
        start_entered = asyncio.Event()
        release_start = asyncio.Event()

        class SlowStart(_FakeAvito):
            async def start_session(self):
                type(self).events.append("slow_start")
                start_entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    type(self).events.append("slow_start_ignored_cancel")
                    await release_start.wait()

        async def books_session():
            async with browser.session(SlowStart):
                pass

        async def pages(url, page, parser):
            return [_lot(2, "www.avito.ru")]

        state, synced = self._price_state()
        links = [_price_link(16)]
        with self._jobs_stack(links, pages), self._price_db_stack(state, synced), \
             mock.patch.object(jobs, "AVITO_REQUEST_TIMEOUT_SECONDS", 0.05):
            books_task = asyncio.create_task(books_session())
            await start_entered.wait()
            started_at = asyncio.get_running_loop().time()
            await asyncio.wait_for(jobs.poll_avito_search(), timeout=1)
            elapsed = asyncio.get_running_loop().time() - started_at

            with self.assertRaises(asyncio.TimeoutError):
                await books_task
            background = list(browser._background_tasks)
            release_start.set()
            await asyncio.gather(*background, return_exceptions=True)

        assert elapsed < 1
        assert "price_notified" in _FakeAvito.events
        assert browser._lock.locked() is False


class ProfileRegistryTest(_AvitoTestBase):
    """Каждый режим получает отдельный профиль, менеджер и блокировки."""

    async def test_price_and_books_use_different_profiles_and_locks(self):
        price_profile = jobs._resolve_avito_profile_dir(books=False)
        books_profile = jobs._resolve_avito_profile_dir(books=True)
        assert price_profile != books_profile
        assert price_profile.is_absolute()
        assert books_profile.is_absolute()

        price_browser = jobs._get_avito_browser(price_profile)
        books_browser = jobs._get_avito_browser(books_profile)
        assert price_browser is not books_browser
        assert price_browser.profile_dir == price_profile
        assert books_browser.profile_dir == books_profile
        assert price_browser._lock is not books_browser._lock
        assert price_browser._lifecycle_lock is not books_browser._lifecycle_lock

    async def test_books_session_start_does_not_wait_for_price_profile(self):
        price_profile = jobs._resolve_avito_profile_dir(books=False)
        books_profile = jobs._resolve_avito_profile_dir(books=True)
        price_browser = jobs._get_avito_browser(price_profile)
        books_browser = jobs._get_avito_browser(books_profile)

        await price_browser._lifecycle_lock.acquire()
        try:
            async with books_browser.session(_FakeAvito):
                pass
        finally:
            price_browser._lifecycle_lock.release()

        assert "session_start" in _FakeAvito.events


class QuarantineTest(_AvitoTestBase):
    """Таймаут и сбой запуска изолированы одним профилем."""

    async def test_timeout_quarantines_only_books_profile(self):
        price_profile = jobs._resolve_avito_profile_dir(books=False)
        books_profile = jobs._resolve_avito_profile_dir(books=True)
        price_browser = jobs._get_avito_browser(price_profile)
        books_browser = jobs._get_avito_browser(books_profile)

        async def hung_call():
            await asyncio.Event().wait()

        async with price_browser.session(_FakeAvito):
            async with books_browser.session(_FakeAvito):
                with mock.patch.object(
                        jobs, "AVITO_REQUEST_TIMEOUT_SECONDS", 0.01):
                    with self.assertRaises(jobs.AvitoQuarantined):
                        await books_browser.books_call(hung_call)

        assert f"sweep:{books_profile}" in _FakeAvito.events
        assert f"sweep:{price_profile}" not in _FakeAvito.events
        assert books_profile not in jobs._avito_browsers
        assert price_profile in jobs._avito_browsers
        assert jobs._get_avito_browser(books_profile) is not books_browser

    async def test_failed_start_sweeps_only_failed_profile(self):
        books_profile = jobs._resolve_avito_profile_dir(books=True)
        browser = jobs._get_avito_browser(books_profile)

        class Broken(_FakeAvito):
            async def start_session(self):
                type(self).events.append("session_start")
                raise RuntimeError("chrome failed")

        with self.assertRaises(RuntimeError):
            async with browser.session(Broken):
                pass

        assert f"sweep:{books_profile}" in _FakeAvito.events
        assert books_profile not in jobs._avito_browsers

    async def test_base_failed_start_sweeps_profile_before_retry(self):
        profile = jobs._resolve_avito_profile_dir(books=True)

        class BrokenParser(BaseParser):
            marketplace = "avito"

            @classmethod
            def can_handle(cls, url: str) -> bool:
                return "avito.ru" in url

            async def get_price(self, url: str) -> float | None:
                return None

            async def get_cheapest_from_search(
                self, search_url: str, title_filter: str,
            ):
                return None

        parser = BrokenParser(profile_dir=profile)
        sweep = mock.AsyncMock(return_value=0)
        with mock.patch.object(
                parser_base.uc, "start", side_effect=RuntimeError("spawn failed")), \
             mock.patch.object(parser_base, "async_sweep_chrome_tree", sweep), \
             mock.patch.object(parser_base.asyncio, "sleep", mock.AsyncMock()):
            with self.assertRaises(RuntimeError):
                await parser.start_session()

        assert sweep.await_count == 3
        assert all(
            call.args[0] == profile and call.args[1]
            for call in sweep.await_args_list)


class ChromeProcessScopeTest(_AvitoTestBase):
    """Убийство Chrome ограничено точным user-data-dir."""

    def test_sweep_kills_only_matching_profile(self):
        price_profile = self._tmp.name + "/price profile"
        books_profile = self._tmp.name + "/books"
        cmdlines = [
            (101, ["/usr/bin/google-chrome", f"--user-data-dir={price_profile}"]),
            (202, ["/usr/bin/google-chrome", f"--user-data-dir={books_profile}"]),
            (303, ["/usr/bin/google-chrome", f"--user-data-dir={price_profile}-other"]),
        ]
        killed: list[int] = []
        with mock.patch.object(
                chrome_cleanup, "_iter_proc_cmdlines", return_value=iter(cmdlines)), \
             mock.patch.object(chrome_cleanup.time, "sleep"), \
             mock.patch.object(
                 chrome_cleanup, "_safe_kill",
                 side_effect=lambda pid, sig: killed.append(pid)):
            count = chrome_cleanup.sweep_chrome_tree(price_profile)

        assert count == 1
        assert set(killed) == {101}

    def test_sweep_kills_legacy_relative_profile(self):
        """Chrome старой версии запускался с относительным путём профиля."""
        cmdlines = [
            (101, ["/usr/bin/google-chrome",
                   "--user-data-dir=chrome_profile_avito"]),
            (202, ["/usr/bin/google-chrome",
                   "--user-data-dir=chrome_profile_avito_other"]),
        ]
        killed: list[int] = []
        with mock.patch.object(
                chrome_cleanup, "_iter_proc_cmdlines", return_value=iter(cmdlines)), \
             mock.patch.object(chrome_cleanup.time, "sleep"), \
             mock.patch.object(
                 chrome_cleanup, "_safe_kill",
                 side_effect=lambda pid, sig: killed.append(pid)):
            count = chrome_cleanup.sweep_chrome_tree(
                "/srv/app/chrome_profile_avito", ("chrome_profile_avito",))

        assert count == 1
        assert set(killed) == {101}

    def test_two_token_user_data_dir_form_is_matched(self):
        """Форма ``--user-data-dir <path>`` (как в README) тоже распознаётся."""
        profile = "/srv/app/chrome_profile_avito"
        assert chrome_cleanup.profile_matches_args(
            ["/usr/bin/google-chrome", "--user-data-dir", profile], profile)
        assert not chrome_cleanup.profile_matches_args(
            ["/usr/bin/google-chrome", "--user-data-dir", profile + "-other"],
            profile)

    def test_chromium_binary_is_matched_too(self):
        profile = "/srv/app/chrome_profile_avito"
        assert chrome_cleanup.profile_matches_args(
            ["/usr/bin/chromium", f"--user-data-dir={profile}"], profile)

    def test_profile_markers_include_absolute_and_legacy_tokens(self):
        markers = chrome_cleanup.profile_marker_set(
            "/srv/app/chrome_profile_avito", ("chrome_profile_avito",))
        assert "/srv/app/chrome_profile_avito" in markers
        assert "chrome_profile_avito" in markers

    def test_scheduler_markers_keep_configured_relative_token(self):
        with mock.patch.dict(
                os.environ, {"AVITO_PROFILE_DIR": "./chrome_profile_avito"}):
            profile = jobs._resolve_avito_profile_dir(books=False)
            markers = jobs._avito_profile_markers(profile)

        assert str(profile) in markers
        assert "chrome_profile_avito" in markers


class ProfileLockCleanupTest(_AvitoTestBase):
    """Перед удалением lock-файлов живой Chrome этого профиля убивается."""

    async def test_foreign_lock_kills_live_chrome_before_removing_lock(self):
        profile = jobs._resolve_avito_profile_dir(books=False)
        profile.mkdir(parents=True, exist_ok=True)
        lock = profile / "SingletonLock"
        lock.symlink_to("otherhost-4242")

        sweep = mock.AsyncMock(return_value=1)
        with mock.patch.object(parser_base, "async_sweep_chrome_tree", sweep):
            parser = _PartialBrowserParser(profile_dir=profile)
            await parser._check_and_reset_chrome_profile()

        assert sweep.await_count == 1
        assert sweep.await_args.args[0] == profile
        assert lock.exists() is False


class PriceTimeoutTest(_AvitoTestBase):
    """Зависший ценовой запрос ограничен таймаутом и карантинится."""

    async def test_hung_price_request_quarantines_profile_and_aborts_pass(self):
        release = asyncio.Event()
        fetched: list[str] = []

        async def pages(url, page, parser):
            fetched.append(url)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return None

        links = [_price_link(16), _price_link(17)]
        price_profile = jobs._resolve_avito_profile_dir(books=False)
        browser = jobs._get_avito_browser(price_profile)
        with self._jobs_stack(links, pages), self._price_db_stack({}, {}), \
             mock.patch.object(jobs, "AVITO_REQUEST_TIMEOUT_SECONDS", 0.05), \
             mock.patch.object(jobs, "send_admin_notification",
                               mock.AsyncMock()):
            await asyncio.wait_for(jobs.poll_avito_search(), timeout=5)
            background = list(browser._background_tasks)
            release.set()
            await asyncio.gather(*background, return_exceptions=True)

        # Вторая ссылка не опрашивалась: проход прерван после карантина.
        assert fetched == [PRICE_URL]
        assert f"sweep:{price_profile}" in _FakeAvito.events
        assert price_profile not in jobs._avito_browsers
        assert browser._lock.locked() is False


class ProfileConflictTest(_AvitoTestBase):
    """Ценовой и книжный профили Avito не могут совпадать."""

    async def test_books_polling_refuses_when_profiles_are_equal(self):
        with mock.patch.dict(
                os.environ,
                {"AVITO_BOOKS_PROFILE_DIR": os.environ["AVITO_PROFILE_DIR"]},
        ), mock.patch.object(
                jobs, "send_admin_notification", mock.AsyncMock()) as notify:
            assert await jobs.poll_avito_books() is False

        notify.assert_awaited()

    async def test_scheduler_skips_books_task_when_profiles_are_equal(self):
        with mock.patch.dict(
                os.environ,
                {"AVITO_BOOKS_PROFILE_DIR": os.environ["AVITO_PROFILE_DIR"]},
        ), mock.patch.object(
                jobs, "send_admin_notification", mock.AsyncMock()):
            tasks = jobs.start_scheduler()
        coros = {t.get_coro().__qualname__ for t in tasks}
        for task in tasks:
            task.cancel()

        assert "_run_avito_books_periodic" not in coros

    async def test_scheduler_starts_books_task_when_profiles_differ(self):
        with mock.patch.object(
                jobs, "send_admin_notification", mock.AsyncMock()):
            tasks = jobs.start_scheduler()
        coros = {t.get_coro().__qualname__ for t in tasks}
        for task in tasks:
            task.cancel()

        assert "_run_avito_books_periodic" in coros


class PriceIdentityTest(_AvitoTestBase):
    """Ценовой режим помнит лот по ID, даже если Avito сменил slug."""

    async def test_reslugged_returning_lot_is_not_notified_without_price_drop(self):
        new_url = "https://www.avito.ru/moskva/renamed_lot_2"
        item = _lot(2, "www.avito.ru")
        item.url = new_url
        state = {2: {
            "id": 2, "is_active": 0, "notified_at": "2026-01-01 00:00:00",
            "miss_streak": 2, "price": 100.0,
        }}
        synced = {new_url: {"id": 2, "notified_at": None}}

        async def pages(url, page, parser):
            return [item]

        with self._jobs_stack([_price_link(16)], pages), \
             self._price_db_stack(state, synced):
            await asyncio.wait_for(jobs.poll_avito_search(), timeout=5)
            jobs.mark_avito_items_notified.assert_awaited_with([2])

        assert "price_notified" not in _FakeAvito.events

    async def test_reslugged_returning_lot_is_notified_after_price_drop(self):
        new_url = "https://www.avito.ru/moskva/renamed_lot_2"
        item = _lot(2, "www.avito.ru")
        item.url = new_url
        item.price = 90.0
        state = {2: {
            "id": 2, "is_active": 0, "notified_at": "2026-01-01 00:00:00",
            "miss_streak": 2, "price": 100.0,
        }}
        synced = {new_url: {"id": 2, "notified_at": None}}

        async def pages(url, page, parser):
            return [item]

        with self._jobs_stack([_price_link(16)], pages), \
             self._price_db_stack(state, synced):
            await asyncio.wait_for(jobs.poll_avito_search(), timeout=5)

        assert "price_notified" in _FakeAvito.events


class StickyBlockTest(_AvitoTestBase):
    """Признак блокировки хранится в сессии, а не в общем парсере."""

    async def test_block_survives_other_mode_request(self):
        browser = jobs._get_avito_browser()
        async with browser.session(_FakeAvito) as parser:
            parser._profile_blocked = True
            # _poll_avito_links видит флаг и запоминает блокировку в сессии.
            browser.mark_blocked()
            # Другой режим успел сбросить флаг общего парсера до закрытия.
            parser._profile_blocked = False

        assert _FakeAvito.end_blocked is True


if __name__ == "__main__":
    unittest.main()
