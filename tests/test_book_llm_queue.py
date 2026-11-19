"""Тесты очереди LLM-вердиктов для книжного режима Avito."""

from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from books.llm_verifier import (
    BookVerdict,
    LlmRateLimited,
    LlmUnavailable,
    VerdictResult,
)
from scheduler import jobs

URL = "https://www.avito.ru/all/knigi_i_zhurnaly/knigi/kniga_8414522966"


def _row(**kwargs) -> dict:
    row = {
        "id": 7,
        "search_link_id": 11,
        "item_url": URL,
        "lot_id": 8414522966,
        "result_json": json.dumps({
            "title": "Слово о полку Игореве",
            "description": "Слово о полку Игореве, издание Academia",
            "price": 1500,
            "posted_at_text": "сегодня",
            "reviews_text": "нет отзыв",
            "candidates": [9, 46],
        }),
        "attempts": 0,
        "search_url": "https://www.avito.ru/all/knigi_i_zhurnaly/knigi",
        "user_id": 3,
    }
    row.update(kwargs)
    return row


def _verdict(verdict: str, *, target: bool = True,
             index: int = 46) -> VerdictResult:
    return VerdictResult(
        verdict=verdict,
        books=[BookVerdict(
            index=index,
            confidence="strong",
            is_target_edition=target,
            evidence=["издательство Academia"],
        )],
        reason="тестовый вердикт",
        provider="kilo",
        model="baidu/ernie-5.5-flash-preview:free",
        prompt_version="book-verify-v1",
        raw="{}",
    )


class LlmQueueTest(unittest.IsolatedAsyncioTestCase):
    """Проверки обработки очереди llm_pending."""

    def setUp(self):
        jobs._llm_rate_limited_until = 0.0
        patcher = mock.patch.dict(
            os.environ, {"AVITO_BOOKS_LLM_ENABLED": "1"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, jobs, "_llm_rate_limited_until", 0.0)

    async def _run(self, rows, verdict=None, exc=None,
                   sent=True, notify_status=None):
        """Прогнать очередь с подменёнными зависимостями."""
        updates: list[tuple] = []
        notified: list[tuple] = []

        async def _update(check_id, status, skip_reason=None, confidence=None,
                          result_json=None, last_error=None,
                          bump_attempts=False):
            updates.append((check_id, status, skip_reason, bump_attempts))

        async def _mark_notified(search_link_id, item_url, lot_id=None):
            notified.append((search_link_id, item_url, lot_id))
            return True

        async def _send(*args, **kwargs):
            return sent

        async def _status(user_id, text):
            if notify_status is None:
                return True
            notify_status.append((user_id, text))
            return True

        async def _verify(candidates, title, description, **kwargs):
            if exc is not None:
                raise exc
            return verdict

        with mock.patch.object(
            jobs, "get_pending_avito_llm_checks",
            new=mock.AsyncMock(return_value=rows),
        ), mock.patch.object(
            jobs, "update_avito_lot_check", new=_update,
        ), mock.patch.object(
            jobs, "mark_avito_item_notified", new=_mark_notified,
        ), mock.patch.object(
            jobs, "send_book_lot_notification", new=_send,
        ), mock.patch.object(
            jobs, "send_book_llm_status_notification", new=_status,
        ), mock.patch.object(
            jobs, "verify_lot", new=_verify,
        ):
            processed = await jobs.poll_book_llm_queue()

        return processed, updates, notified

    async def test_match_sends_notification_and_marks_matched(self):
        processed, updates, notified = await self._run(
            [_row()], verdict=_verdict("match"))

        self.assertEqual(processed, 1)
        self.assertEqual(updates[0][0], 7)
        self.assertEqual(updates[0][1], "matched")
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0][0], 11)
        self.assertEqual(notified[0][1], URL)

    async def test_reject_skips_lot_without_notification(self):
        processed, updates, notified = await self._run(
            [_row()], verdict=_verdict("reject"))

        self.assertEqual(processed, 1)
        self.assertEqual(updates[0][1], "skipped")
        self.assertEqual(updates[0][2], "llm_reject")
        self.assertEqual(len(notified), 1)

    async def test_match_without_target_edition_is_skipped(self):
        processed, updates, notified = await self._run(
            [_row()], verdict=_verdict("match", target=False))

        self.assertEqual(processed, 1)
        self.assertEqual(updates[0][1], "skipped")
        self.assertEqual(updates[0][2], "llm_reject")
        self.assertEqual(len(notified), 1)

    async def test_rate_limit_notifies_user_and_keeps_lot_pending(self):
        messages: list[tuple] = []
        exc = LlmRateLimited("kilo", "baidu/ernie-5.5-flash-preview:free", 600)
        processed, updates, notified = await self._run(
            [_row()], exc=exc, notify_status=messages)

        self.assertEqual(processed, 0)
        # Лот остаётся в очереди — статус не меняем.
        self.assertEqual(updates, [])
        self.assertEqual(notified, [])
        self.assertEqual(len(messages), 1)
        self.assertIn("kilo", messages[0][1])
        self.assertIn("baidu/ernie-5.5-flash-preview:free", messages[0][1])
        self.assertGreater(jobs._llm_rate_limited_until, 0)

    async def test_unclear_increments_attempts(self):
        processed, updates, notified = await self._run(
            [_row()], verdict=_verdict("unclear"))

        self.assertEqual(processed, 1)
        self.assertEqual(updates[0][1], "llm_pending")
        self.assertTrue(updates[0][3])  # bump_attempts
        self.assertEqual(notified, [])

    async def test_unclear_after_three_attempts_is_skipped(self):
        processed, updates, notified = await self._run(
            [_row(attempts=2)], verdict=_verdict("unclear"))

        self.assertEqual(processed, 1)
        self.assertEqual(updates[0][1], "skipped")
        self.assertEqual(updates[0][2], "llm_unclear")
        self.assertEqual(len(notified), 1)

    async def test_unavailable_after_three_attempts_is_skipped(self):
        processed, updates, notified = await self._run(
            [_row(attempts=2)], exc=LlmUnavailable("модели не ответили"))

        self.assertEqual(processed, 0)
        self.assertEqual(updates[0][1], "skipped")
        self.assertEqual(updates[0][2], "llm_unavailable")
        self.assertEqual(len(notified), 1)

    async def test_row_without_candidates_is_skipped(self):
        row = _row(result_json=json.dumps({
            "title": "Слово о полку Игореве",
            "description": "",
            "candidates": [],
        }))
        processed, updates, notified = await self._run([row])

        self.assertEqual(processed, 0)
        self.assertEqual(updates[0][1], "skipped")
        self.assertEqual(updates[0][2], "llm_no_candidates")
        self.assertEqual(len(notified), 1)

    async def test_failed_notification_keeps_lot_pending(self):
        processed, updates, notified = await self._run(
            [_row()], verdict=_verdict("match"), sent=False)

        self.assertEqual(processed, 1)
        self.assertEqual(updates[0][1], "llm_pending")
        self.assertEqual(notified, [])

    async def test_disabled_llm_does_nothing(self):
        with mock.patch.dict(
            os.environ, {"AVITO_BOOKS_LLM_ENABLED": "0"}, clear=False,
        ), mock.patch.object(
            jobs, "get_pending_avito_llm_checks",
            new=mock.AsyncMock(return_value=[_row()]),
        ) as pending:
            self.assertEqual(await jobs.poll_book_llm_queue(), 0)
            pending.assert_not_awaited()

    async def test_cooldown_blocks_new_requests(self):
        jobs._llm_rate_limited_until = jobs.time.monotonic() + 3600
        with mock.patch.object(
            jobs, "get_pending_avito_llm_checks",
            new=mock.AsyncMock(return_value=[_row()]),
        ) as pending:
            self.assertEqual(await jobs.poll_book_llm_queue(), 0)
            pending.assert_not_awaited()


class AmbiguousLotQueuedTest(unittest.IsolatedAsyncioTestCase):
    """Спорный лот уходит в очередь LLM, а не уведомляется сразу."""

    async def test_ambiguous_lot_is_queued(self):
        item = SimpleNamespace(
            url=URL,
            title="Слово о полку Игореве",
            price=1500,
            posted_at_text="сегодня",
        )
        details = SimpleNamespace(
            url=URL,
            title="Слово о полку Игореве",
            description="Слово о полку Игореве, издание Academia",
            posted_at_text="сегодня",
            reviews_text="нет отзыв",
            price=1500,
        )

        class _Parser:
            async def get_item_details(self, url):
                return details

        prepared = {
            "items": [item],
            "synced": {URL: {"id": 5, "notified_at": None}},
            "baseline": False,
            "first_run": False,
            "state_before": {},
        }
        link = {
            "id": 11,
            "search_url": "https://www.avito.ru/all/knigi_i_zhurnaly/knigi",
            "user_id": 3,
        }

        with mock.patch.object(
            jobs, "_load_avito_listing",
            new=mock.AsyncMock(return_value=(prepared, False)),
        ), mock.patch.object(
            jobs, "get_avito_lot_check", new=mock.AsyncMock(return_value=None),
        ), mock.patch.object(
            jobs, "save_avito_lot_check", new=mock.AsyncMock(),
        ) as save_check, mock.patch.object(
            jobs, "mark_avito_items_notified", new=mock.AsyncMock(),
        ) as mark_notified, mock.patch.object(
            jobs, "send_book_lot_notification", new=mock.AsyncMock(),
        ) as notify:
            await jobs._poll_books_link(
                _Parser(), link, False, [5], [2])

        notify.assert_not_awaited()
        save_check.assert_awaited_once()
        args = save_check.await_args.args
        self.assertEqual(args[0], 11)
        self.assertEqual(args[1], URL)
        self.assertEqual(args[2], "llm_pending")
        # Кандидаты для LLM — оба издания группы «Слово о полку Игореве».
        payload = json.loads(save_check.await_args.kwargs["result_json"])
        self.assertEqual(payload["candidates"], [9, 46])
        mark_notified.assert_awaited_once_with([5])


if __name__ == "__main__":
    unittest.main()
