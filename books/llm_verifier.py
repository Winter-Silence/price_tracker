"""Проверка спорного книжного лота бесплатной LLM через локальный OpenCode CLI.

Сеть отсюда не ходим: единственный транспорт — установленный `opencode`,
он сам держит авторизацию провайдеров. Цепочка из трёх бесплатных моделей,
следующая вызывается только если предыдущая ответила "unclear" или сломалась.
Это не кворум: сравнивать ответы моделей нельзя.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field

from utils.logger import logger

PROMPT_VERSION = "book-verify-v1"
API_PATH = "/api/experimental/generate/text"
DEFAULT_TIMEOUT = 90.0

# providerID + modelID: префикс провайдера в modelID не дублируется (иначе ModelNotFoundError)
PROVIDER_CHAIN: tuple[tuple[str, str], ...] = (
    ("kilo", "baidu/ernie-5.5-flash-preview:free"),
    ("nvidia", "minimaxai/minimax-m3.1-flash"),
    ("opencode", "minimax-m3.1-flash-free"),
)

RATE_LIMIT_MARKERS = ("rate_limit", "Free usage limit reached", "429", "Too Many Requests")
RETRY_AFTER_RE = re.compile(r"retry[_-]?after\D{0,20}(\d+)", re.IGNORECASE)

VERDICTS = ("match", "reject", "unclear")
CONFIDENCES = ("strong", "medium", "weak")
CORRECTION = "\n\nYour previous answer was not valid JSON. Answer with JSON only."

SERIES_LABELS = {"lp": "Литературные памятники", "academia": "Academia"}


@dataclass
class Candidate:
    """Кандидат из каталога: подмножество полей Book, без импорта books.catalog."""

    index: int
    author: str = ""
    title: str = ""
    year: str = ""
    publisher: str = ""
    series: str = ""
    place: str = ""
    artist: str = ""
    volume: str = ""
    note: str = ""

    @classmethod
    def from_book(cls, book) -> Candidate:
        """Строится из Book или из dict с теми же полями."""
        get = book.get if isinstance(book, dict) else lambda key, default=None: getattr(book, key, default)
        fields = ("author", "title", "year", "publisher", "series", "place", "artist", "volume", "note")
        data = {name: str(get(name) or "") for name in fields}
        if not data["year"]:
            # У части изданий год — диапазон («1906–1909») или не указан.
            data["year"] = str(get("year_text") or "")
        return cls(index=int(get("index")), **data)


@dataclass
class BookVerdict:
    index: int
    confidence: str
    is_target_edition: bool
    evidence: list[str] = field(default_factory=list)


@dataclass
class VerdictResult:
    verdict: str
    books: list[BookVerdict]
    reason: str
    provider: str
    model: str
    prompt_version: str
    raw: str


class LlmUnavailable(Exception):
    """Модель не ответила: транспорт, таймаут или невалидный ответ."""


class LlmRateLimited(LlmUnavailable):
    """Бесплатная квота провайдера исчерпана. Повторять в цикле нельзя."""

    def __init__(self, provider: str, model: str, retry_after_s: int | None = None):
        super().__init__(f"{provider}/{model}: бесплатная квота исчерпана (rate_limit)")
        self.provider = provider
        self.model = model
        self.retry_after_s = retry_after_s


PROMPT_TEMPLATE = f"""Ты эксперт по русской букинистике. Реши, является ли лот объявления именно тем изданием из каталога.
Версия промпта: {PROMPT_VERSION}

Правила:
- is_target_edition = false, если лот — современное переиздание или общая подборка без конкретного издания.
- Приметы издания — год, издательство, серия, художник, том, место. Ищи их в тексте лота.
- confidence: strong — издание совпадает по прямым приметам; medium — совпала часть примет; weak — примет нет.
- verdict: match — хотя бы одна книга лота совпала с целевым изданием; reject — ни одна не совпала; unclear — текста не хватает.

Ответь СТРОГО одним JSON-объектом, без пояснений и без markdown:
{{"verdict": "match|reject|unclear", "books": [{{"index": 1, "confidence": "strong|medium|weak", "is_target_edition": true, "evidence": ["..."]}}], "reason": "..."}}
"""


def _candidate_lines(candidates: list[Candidate]) -> str:
    lines = []
    for candidate in candidates:
        parts = [f"[{candidate.index}]"]
        if candidate.author:
            parts.append(f"Автор: {candidate.author}.")
        if candidate.title:
            parts.append(f"Название: {candidate.title}.")
        if candidate.year:
            parts.append(f"Год: {candidate.year}.")
        if candidate.publisher:
            parts.append(f"Издательство: {candidate.publisher}.")
        if candidate.series:
            parts.append(f"Серия: {SERIES_LABELS.get(candidate.series, candidate.series)}.")
        if candidate.place:
            parts.append(f"Место: {candidate.place}.")
        if candidate.artist:
            parts.append(f"Художник: {candidate.artist}.")
        if candidate.volume:
            parts.append(f"Том: {candidate.volume}.")
        if candidate.note:
            parts.append(f"Примечание: {candidate.note}.")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def build_prompt(candidates: list[Candidate], title: str, description: str) -> str:
    return (
        f"Кандидаты из каталога:\n{_candidate_lines(candidates)}\n\n"
        f"Лот:\nНазвание: {title}\nОписание: {description or '(пусто)'}\n\n"
        f"{PROMPT_TEMPLATE}"
    )


def _extract_json_object(text: str) -> dict:
    """Первый валидный JSON-объект из ответа: модель оборачивает его в прозу и ```json."""
    decoder = json.JSONDecoder()
    for position, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[position:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("в ответе модели нет JSON-объекта")


def parse_answer(text: str, provider: str = "", model: str = "") -> VerdictResult:
    """Разбор и проверка схемы ответа. ValueError — невалидный ответ."""
    data = _extract_json_object(text)
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError(f"недопустимый verdict: {verdict!r}")
    raw_books = data.get("books")
    if not isinstance(raw_books, list):
        raise ValueError("books должен быть списком")
    books: list[BookVerdict] = []
    for item in raw_books:
        if not isinstance(item, dict):
            raise ValueError("элемент books должен быть объектом")
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError(f"index должен быть int: {index!r}")
        confidence = item.get("confidence")
        if confidence not in CONFIDENCES:
            raise ValueError(f"недопустимый confidence: {confidence!r}")
        is_target = item.get("is_target_edition")
        if not isinstance(is_target, bool):
            raise ValueError(f"is_target_edition должен быть bool: {is_target!r}")
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError("evidence должен быть списком")
        books.append(BookVerdict(index, confidence, is_target, [str(e) for e in evidence]))
    reason = data.get("reason")
    return VerdictResult(
        verdict=verdict,
        books=books,
        reason=reason if isinstance(reason, str) else "",
        provider=provider,
        model=model,
        prompt_version=PROMPT_VERSION,
        raw=text,
    )


def _rate_limit(output: str) -> tuple[bool, int | None]:
    """(есть ли признак исчерпанной квоты, retry_after в секундах или None)."""
    if not any(marker.lower() in output.lower() for marker in RATE_LIMIT_MARKERS):
        return False, None
    match = RETRY_AFTER_RE.search(output)
    return True, int(match.group(1)) if match else None


async def _run_cli(argv: list[str], provider: str, model: str, timeout: float) -> str:
    if shutil.which("opencode") is None:
        raise LlmUnavailable("opencode не найден в PATH")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise LlmUnavailable(f"{provider}/{model}: таймаут {timeout:.0f} с") from None
    out = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")

    limited, retry_after = _rate_limit(out + "\n" + err)
    if limited or (proc.returncode or 0) == 429:
        raise LlmRateLimited(provider, model, retry_after)
    if proc.returncode != 0:
        raise LlmUnavailable(f"{provider}/{model}: код {proc.returncode}: {err.strip()[:300]}")
    return out


async def call_http(prompt: str, provider: str, model: str, timeout: float) -> str:
    """Транспорт `opencode api post`: stdout = {"data": {"text": "..."}}."""
    body = json.dumps(
        {"prompt": prompt, "model": {"providerID": provider, "modelID": model, "id": model}},
        ensure_ascii=False,
    )
    out = await _run_cli(
        ["opencode", "api", "post", API_PATH, "--data", body], provider, model, timeout
    )
    try:
        payload = json.loads(out)
    except ValueError:
        raise LlmUnavailable(f"{provider}/{model}: api вернул не JSON: {out.strip()[:200]}") from None
    data = payload.get("data") if isinstance(payload, dict) else None
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise LlmUnavailable(f"{provider}/{model}: api вернул пустой текст")
    return text


async def call_opencode(prompt: str, provider: str, model: str, timeout: float) -> str:
    """Запасной транспорт `opencode run --format json`: JSONL-поток событий."""
    out = await _run_cli(
        ["opencode", "run", "--model", f"{provider}/{model}", "--format", "json", prompt],
        provider,
        model,
        timeout,
    )
    parts: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        text = event.get("text") or part.get("text")
        if isinstance(text, str):
            parts.append(text)
    text = "".join(parts).strip()
    if not text:
        raise LlmUnavailable(f"{provider}/{model}: пустой ответ opencode run")
    return text


async def call_model(prompt: str, provider: str, model: str, timeout: float) -> str:
    """api-транспорт, при его поломке — `opencode run`. Rate limit не обходим."""
    try:
        return await call_http(prompt, provider, model, timeout)
    except LlmRateLimited:
        raise
    except LlmUnavailable as exc:
        logger.warning("api-транспорт %s/%s не сработал: %s", provider, model, exc)
    return await call_opencode(prompt, provider, model, timeout)


async def _ask_model(
    prompt: str, provider: str, model: str, timeout: float
) -> VerdictResult:
    """Один вызов модели; при невалидном JSON — ровно один корректирующий повтор."""
    last_error: Exception | None = None
    for attempt in (1, 2):
        text = await call_model(prompt if attempt == 1 else prompt + CORRECTION, provider, model, timeout)
        try:
            return parse_answer(text, provider, model)
        except ValueError as exc:
            last_error = exc
            logger.warning("Модель %s/%s дала невалидный ответ: %s", provider, model, exc)
    raise LlmUnavailable(f"{provider}/{model}: невалидный ответ: {last_error}")


async def verify_lot(
    candidates: list[Candidate],
    title: str,
    description: str,
    providers: list[tuple[str, str]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> VerdictResult:
    """Вердикт по лоту: первая уверенная модель побеждает, все "unclear" — verdict="unclear"."""
    if not candidates:
        raise LlmUnavailable("нет кандидатов каталога для проверки")
    prompt = build_prompt(candidates, title, description)
    last_unclear: VerdictResult | None = None
    last_error: LlmUnavailable | None = None

    for provider, model in providers or PROVIDER_CHAIN:
        try:
            result = await _ask_model(prompt, provider, model, timeout)
        except LlmRateLimited:
            raise
        except LlmUnavailable as exc:
            logger.warning("Модель %s/%s не ответила: %s", provider, model, exc)
            last_error = exc
            continue
        logger.info("Модель %s/%s: вердикт %s", provider, model, result.verdict)
        if result.verdict in ("match", "reject"):
            return result
        last_unclear = result

    if last_unclear is not None:
        return last_unclear
    raise last_error or LlmUnavailable("ни одна модель не ответила")
