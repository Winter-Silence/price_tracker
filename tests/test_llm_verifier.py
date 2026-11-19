"""Тесты LLM-проверки книжного лота: транспорт подменён, сеть и subprocess не нужны."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from books.llm_verifier import (
    CORRECTION,
    PROMPT_VERSION,
    Candidate,
    LlmRateLimited,
    LlmUnavailable,
    parse_answer,
    verify_lot,
)

PROVIDERS = [("kilo", "m-one"), ("nvidia", "m-two"), ("opencode", "m-three")]


def answer(verdict: str, index: int = 1, **book) -> str:
    payload = {
        "verdict": verdict,
        "books": [
            {
                "index": index,
                "confidence": book.get("confidence", "strong"),
                "is_target_edition": book.get("is_target_edition", verdict == "match"),
                "evidence": book.get("evidence", ["1948, Наука"]),
            }
        ],
        "reason": book.get("reason", "год и издательство совпали"),
    }
    return json.dumps(payload, ensure_ascii=False)


def fake_model(answers: dict, calls: list):
    """answers[(provider, model)] -> str | Exception | list[str|Exception] (по одному на вызов)."""

    async def call(prompt: str, provider: str, model: str, timeout: float) -> str:
        calls.append({"provider": provider, "model": model, "prompt": prompt})
        item = answers[(provider, model)]
        if isinstance(item, list):
            item = item.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return call


def candidates() -> list[Candidate]:
    return [Candidate.from_book({"index": 1, "author": "Цезарь", "title": "Записки о Галльской войне",
                                 "year": 1948, "publisher": "Наука", "series": "lp"})]


class VerdictParsingTest(unittest.IsolatedAsyncioTestCase):
    async def test_match_is_parsed(self):
        calls: list = []
        with patch("books.llm_verifier.call_model", new=fake_model({PROVIDERS[0]: answer("match")}, calls)):
            result = await verify_lot(candidates(), "Цезарь. 1948", "Наука, 1948", PROVIDERS)

        self.assertEqual(result.verdict, "match")
        self.assertEqual(result.provider, "kilo")
        self.assertEqual(result.model, "m-one")
        self.assertEqual(result.prompt_version, PROMPT_VERSION)
        self.assertEqual(len(result.books), 1)
        self.assertEqual(result.books[0].index, 1)
        self.assertEqual(result.books[0].confidence, "strong")
        self.assertTrue(result.books[0].is_target_edition)
        self.assertEqual(result.books[0].evidence, ["1948, Наука"])
        self.assertEqual(result.raw, answer("match"))
        self.assertEqual(len(calls), 1)

    async def test_reject_is_parsed(self):
        reject = answer("reject", is_target_edition=False, confidence="weak", evidence=["Эксмо, 2023"])
        calls: list = []
        with patch("books.llm_verifier.call_model", new=fake_model({PROVIDERS[0]: reject}, calls)):
            result = await verify_lot(candidates(), "Цезарь", "Москва: Эксмо, 2023", PROVIDERS)

        self.assertEqual(result.verdict, "reject")
        self.assertFalse(result.books[0].is_target_edition)

    async def test_prose_and_json_fence_is_parsed(self):
        prose = "Вот мой ответ:\n```json\n" + answer("match") + "\n```\nГотово."
        calls: list = []
        with patch("books.llm_verifier.call_model", new=fake_model({PROVIDERS[0]: prose}, calls)):
            result = await verify_lot(candidates(), "Цезарь", "1948", PROVIDERS)

        self.assertEqual(result.verdict, "match")
        self.assertEqual(len(calls), 1)

    async def test_invalid_schema_raises(self):
        with self.assertRaises(ValueError):
            parse_answer('{"verdict": "maybe", "books": [], "reason": ""}')
        with self.assertRaises(ValueError):
            parse_answer('{"verdict": "match", "books": [{"index": "1"}], "reason": ""}')
        with self.assertRaises(ValueError):
            parse_answer("совсем не JSON")


class ChainTest(unittest.IsolatedAsyncioTestCase):
    async def test_unclear_from_first_model_triggers_second(self):
        calls: list = []
        fake = fake_model(
            {PROVIDERS[0]: answer("unclear", confidence="weak", is_target_edition=False), PROVIDERS[1]: answer("reject")},
            calls,
        )
        with patch("books.llm_verifier.call_model", new=fake):
            result = await verify_lot(candidates(), "Цезарь", "без года", PROVIDERS)

        self.assertEqual(result.verdict, "reject")
        self.assertEqual(result.provider, "nvidia")
        self.assertEqual([c["provider"] for c in calls], ["kilo", "nvidia"])

    async def test_rate_limit_raises_without_retry(self):
        calls: list = []
        limited = LlmRateLimited("kilo", PROVIDERS[0][1], retry_after_s=38400)
        with patch("books.llm_verifier.call_model", new=fake_model({PROVIDERS[0]: limited}, calls)):
            with self.assertRaises(LlmRateLimited) as ctx:
                await verify_lot(candidates(), "Цезарь", "1948", PROVIDERS)

        self.assertEqual(ctx.exception.provider, "kilo")
        self.assertEqual(ctx.exception.model, PROVIDERS[0][1])
        self.assertEqual(ctx.exception.retry_after_s, 38400)
        self.assertIsInstance(ctx.exception, LlmUnavailable)
        self.assertEqual(len(calls), 1)

    async def test_parse_failure_retries_once_then_falls_through(self):
        calls: list = []
        fake = fake_model(
            {PROVIDERS[0]: ["не JSON", "опять не JSON"], PROVIDERS[1]: answer("match")}, calls
        )
        with patch("books.llm_verifier.call_model", new=fake):
            result = await verify_lot(candidates(), "Цезарь", "1948", PROVIDERS)

        self.assertEqual(result.verdict, "match")
        self.assertEqual(result.provider, "nvidia")
        self.assertEqual([c["provider"] for c in calls], ["kilo", "kilo", "nvidia"])
        self.assertTrue(calls[1]["prompt"].endswith(CORRECTION), "второй запрос — корректирующий")
        self.assertFalse(calls[2]["prompt"].endswith(CORRECTION), "новой модели — чистый промпт")

    async def test_all_unclear_returns_unclear(self):
        calls: list = []
        fake = fake_model(
            {(provider, model): answer("unclear", confidence="weak", is_target_edition=False)
             for provider, model in PROVIDERS},
            calls,
        )
        with patch("books.llm_verifier.call_model", new=fake):
            result = await verify_lot(candidates(), "Цезарь", "без года", PROVIDERS)

        self.assertEqual(result.verdict, "unclear")
        self.assertEqual(result.provider, "opencode")
        self.assertEqual([c["provider"] for c in calls], ["kilo", "nvidia", "opencode"])

    async def test_all_transport_errors_raise_unavailable(self):
        calls: list = []
        fake = fake_model({(provider, model): LlmUnavailable("нет связи") for provider, model in PROVIDERS}, calls)
        with patch("books.llm_verifier.call_model", new=fake):
            with self.assertRaises(LlmUnavailable):
                await verify_lot(candidates(), "Цезарь", "1948", PROVIDERS)

        self.assertEqual(len(calls), 3)

    async def test_default_chain_order(self):
        calls: list = []
        fake = fake_model(
            {
                ("kilo", "baidu/ernie-5.5-flash-preview:free"): answer("unclear", is_target_edition=False),
                ("nvidia", "minimaxai/minimax-m3.1-flash"): answer("unclear", is_target_edition=False),
                ("opencode", "minimax-m3.1-flash-free"): answer("match"),
            },
            calls,
        )
        with patch("books.llm_verifier.call_model", new=fake):
            result = await verify_lot(candidates(), "Цезарь", "1948")

        self.assertEqual(result.verdict, "match")
        self.assertEqual(
            [(c["provider"], c["model"]) for c in calls],
            [
                ("kilo", "baidu/ernie-5.5-flash-preview:free"),
                ("nvidia", "minimaxai/minimax-m3.1-flash"),
                ("opencode", "minimax-m3.1-flash-free"),
            ],
        )

    async def test_prompt_carries_candidates_and_lot(self):
        calls: list = []
        with patch("books.llm_verifier.call_model", new=fake_model({PROVIDERS[0]: answer("match")}, calls)):
            await verify_lot(candidates(), "Лот: Цезарь", "Описание лота", PROVIDERS)

        prompt = calls[0]["prompt"]
        self.assertIn("book-verify-v1", prompt)
        self.assertIn("Записки о Галльской войне", prompt)
        self.assertIn("Литературные памятники", prompt)
        self.assertIn("Лот: Цезарь", prompt)
        self.assertIn("Описание лота", prompt)


class TransportFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_api_failure_falls_back_to_run(self):
        async def broken_http(prompt, provider, model, timeout):
            raise LlmUnavailable("api вернул не JSON")

        async def run_cli(prompt, provider, model, timeout):
            return answer("match")

        from books.llm_verifier import call_model

        with patch("books.llm_verifier.call_http", new=broken_http), \
                patch("books.llm_verifier.call_opencode", new=run_cli):
            text = await call_model("промпт", "kilo", "m-one", 5.0)

        self.assertEqual(json.loads(text)["verdict"], "match")

    async def test_rate_limit_is_not_retried_on_run(self):
        async def limited_http(prompt, provider, model, timeout):
            raise LlmRateLimited("kilo", "m-one", 38400)

        async def unexpected_run(prompt, provider, model, timeout):
            raise AssertionError("opencode run не должен вызываться после rate_limit")

        from books.llm_verifier import call_model

        with patch("books.llm_verifier.call_http", new=limited_http), \
                patch("books.llm_verifier.call_opencode", new=unexpected_run):
            with self.assertRaises(LlmRateLimited):
                await call_model("промпт", "kilo", "m-one", 5.0)


if __name__ == "__main__":
    unittest.main()
