"""Сопоставление текста объявления с эталонным каталогом книг.

Правила (сознательно консервативные, ложные срабатывания дороже пропусков):

* название должно присутствовать в тексте целиком (набор лемм, нечётко);
* «слабые» названия («Письма», «Опыты», «Политика») требуют автора;
* год издания в том же сегменте текста повышает уверенность до strong;
  чужое переиздание (1969, 1979, 1985, 2007…) — причина отклонить лот;
* издательство/серия дают medium, только название — weak; автор — лишь
  подтверждение произведения (у переиздания тот же автор), не издания;
* уведомляется только совпадение с доказательством издания (год,
  издательство/серия, примета издания);
* лот об одной книге разбирается по всему тексту: год из любого абзаца —
  её год издания (переиздание отсекает лот, целевой год подтверждает);
  в лоте из нескольких книг строки склеиваются по позициям, чтобы год
  одной книги не относился к соседней;
* негативные термины (репринт, факсимиле…) отклоняют лот, но «не репринт» — нет;
* дубли названий («Слово о полку Игореве» 1950 и 1934) разрешаются годом
  или дискриминатором (Палех/Голиков); если не разрешаются — оба варианта
  уходят с пометкой «уточнить год».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from books.catalog import Book, get_catalog
from books.normalize import (
    bad_term_hit,
    contains_key,
    find_positions,
    has_negation_before,
    tokenize,
)

STRONG = "strong"
MEDIUM = "medium"
WEAK = "weak"

CONFIDENCE_LABELS = {
    STRONG: "совпадение",
    MEDIUM: "вероятное совпадение",
    WEAK: "⚠️ Требует проверки",
}

# Сколько токенов допускаем между названием и годом/автором.
_YEAR_PROXIMITY = 12
# Год объявления (текущий и прошлый) не считаем годом издания.
_CURRENT_YEAR = datetime.now().year

_SEGMENT_RE = re.compile(r"[\n;•·|]+")

# Короткие слова, которые могут стоять рядом с годом в отдельной строке.
_YEAR_WORDS = frozenset({"г", "год", "года", "гг"})


def _is_year_only(tokens: list[str]) -> bool:
    """Сегмент — отдельная строка с годом: «1950», «1950 г.», «1950-1960»."""
    if not tokens or len(tokens) > 3:
        return False
    has_year = any(t.isdigit() and len(t) == 4 for t in tokens)
    return has_year and all(
        (t.isdigit() and len(t) == 4) or t in _YEAR_WORDS for t in tokens
    )


def _merge_year_segments(segments: list[list[str]]) -> list[list[str]]:
    """Приклеить строку-год к соседнему описанию.

    «Повесть временных лет» и «1950 г.» на разных строках — это одна
    позиция. Приклеиваем к предыдущему сегменту (обычный порядок —
    название, затем год), а если год стоит первым — к следующему.
    Так год не «перетекает» в чужую книгу из соседней строки.
    """
    merged: list[list[str]] = []
    leading: list[str] = []
    for tokens in segments:
        if _is_year_only(tokens):
            if merged:
                merged[-1].extend(tokens)
            else:
                leading.extend(tokens)
            continue
        if leading:
            tokens = leading + tokens
            leading = []
        merged.append(tokens)
    if leading:
        merged.append(leading)
    return merged


# Максимальная длина строки (в токенах), которую ещё считаем частью позиции,
# а не отдельным абзацем описания.
_MAX_GLUE_TOKENS = 8


def _has_year_token(tokens: list[str]) -> bool:
    return any(t.isdigit() and len(t) == 4 for t in tokens)


def _segment_books(tokens: list[str], catalog: tuple[Book, ...]) -> set[int]:
    """Книги каталога, название или автор которых упомянуты в сегменте."""
    books: set[int] = set()
    for book in catalog:
        if any(find_positions(tokens, key)
               for key in book.title_keys + book.loose_keys):
            books.add(book.index)
        elif any(contains_key(tokens, key) for key in book.author_keys):
            books.add(book.index)
    return books


def _merge_key_segments(
    segments: list[list[str]], catalog: tuple[Book, ...],
) -> list[list[str]]:
    """Склеить соседние строки одной позиции: автор + название (+ год).

    В объявлениях позиция часто разбита по строкам:

        Дефо
        Робинзон Крузо
        1929

    Строку-год уже приклеивает `_merge_year_segments`. Здесь склеиваем
    строки, где есть только автор или только название из каталога, с
    соседней строкой той же книги. Чтобы год чужой книги не «перетекал»
    через границу строки, строка с годом принимается только тогда, когда в
    ней есть автор/название той же книги (например, «Цезарь. 1948»).
    """
    info = [
        {
            "tokens": tokens,
            "year": _has_year_token(tokens),
            "books": _segment_books(tokens, catalog),
        }
        for tokens in segments
    ]
    used = [False] * len(info)
    merged: list[list[str]] = []

    for i, seg in enumerate(info):
        if used[i]:
            continue
        if (len(seg["tokens"]) > _MAX_GLUE_TOKENS
                or not seg["books"] or seg["year"]):
            merged.append(seg["tokens"])
            continue

        target_pos = None
        for j in (i - 1, i + 1):
            if j < 0 or j >= len(info) or used[j]:
                continue
            neighbor = info[j]
            if len(neighbor["tokens"]) > _MAX_GLUE_TOKENS:
                continue
            # Сосед может упоминать только книги текущей позиции.
            if not neighbor["books"] <= seg["books"]:
                continue
            # Чужой год без привязки к книге — не склеиваем (защита от
            # перетекания года из соседней позиции).
            if neighbor["year"] and not neighbor["books"]:
                continue
            target_pos = j
            break

        if target_pos is None:
            merged.append(seg["tokens"])
            continue

        used[i] = True
        used[target_pos] = True
        neighbor = info[target_pos]
        if target_pos == i - 1:
            # Сосед мог уже уйти в результат — забираем его обратно.
            if merged and merged[-1] is neighbor["tokens"]:
                merged.pop()
            merged.append(neighbor["tokens"] + seg["tokens"])
        else:
            merged.append(seg["tokens"] + neighbor["tokens"])

    return merged


_PUBLISHER_TERMS = {
    "lp": (
        # «Литературные памятники» — название серии, само по себе достаточно.
        ("литературный", "памятник"),
        ("академия", "наук"),
        # Однословные термины слишком общие: «наука» — это и издательство,
        # и «общество наук». Засчитываются только рядом с маркером издателя.
        ("наука",),
        ("памятник",),
        ("ссср",),
    ),
    "academia": (
        ("академия",),
        ("ленинград",),
        ("петроград",),
    ),
}

# Маркеры издателя: без них общее слово («наука», «академия»,
# «ленинград») не считается указанием на издательство.
_PUBLISHER_MARKERS: tuple[tuple[str, ...], ...] = (
    ("издательство",), ("издатель",), ("изд",), ("выд",),
    ("выпуск",), ("типография",), ("книгоиздательство",), ("publisher",),
)
# Насколько далеко от термина ещё считается маркер издателя.
_PUBLISHER_MARKER_DISTANCE = 8

# «Собрание сочинений», «полный комплект» — общий сборник, а не книга.
_COLLECTION_MARKERS: tuple[tuple[str, ...], ...] = (
    ("полный", "собрание"),
    ("собрание", "сочинение"),
    ("собрание", "творение"),
    ("сборник", "сочинение"),
    ("полный", "комплект"),
    ("комплект", "книга"),
)

# «Халиф на час. Из тысячи и одной ночи» — цитата из чужого произведения.
_CITATION_PREFIXES = ("из",)

# Общие стоп-маркеры подделок и перепечаток.
_GLOBAL_BAD_TERMS: tuple[tuple[str, ...], ...] = (
    ("репринт",), ("репринтный",), ("факсимиле",), ("переиздание",),
    ("репродукция",), ("аудиокнига",),
)


def _publisher_evidence(
    tokens: list[str], terms: tuple[tuple[str, ...], ...],
) -> bool:
    """Издательство засчитано, только если термин ж��т в той же позиции.

    Однословные термины («наука», «академия», «ленинград») слишком общие:
    они принимаются только рядом с маркером издателя («издательство
    Академия», «изд. Наука»). Многословные («литературные памятники»)
    самодостаточны.
    """
    if not terms:
        return False
    marker_positions: list[int] = []
    for marker in _PUBLISHER_MARKERS:
        marker_positions.extend(find_positions(tokens, marker))
    for term in terms:
        positions = find_positions(tokens, term)
        if not positions:
            continue
        if len(term) > 1:
            return True
        if any(
            abs(pos - marker_pos) <= _PUBLISHER_MARKER_DISTANCE
            for pos in positions
            for marker_pos in marker_positions
        ):
            return True
    return False


def _has_collection_marker(tokens: list[str]) -> bool:
    """В позиции назван общий сборник («собрание сочинений», «комплект»)."""
    return any(contains_key(tokens, marker) for marker in _COLLECTION_MARKERS)


def _is_collection_book(book: Book) -> bool:
    """Каталожная позиция сама является собранием («Собрание сочинений»)."""
    for key in book.title_keys:
        key_set = set(key)
        if any(set(marker) <= key_set for marker in _COLLECTION_MARKERS):
            return True
    return False


def _is_cited_work(tokens: list[str], positions: list[int]) -> bool:
    """Название упомянуто как цитата: «Из тысячи и одной ночи»."""
    return any(
        pos > 0 and tokens[pos - 1] in _CITATION_PREFIXES
        for pos in positions
    )


@dataclass(frozen=True)
class BookMatch:
    book: Book
    confidence: str
    year_near: bool = False
    discriminator: bool = False
    needs_year: bool = False
    evidence: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return CONFIDENCE_LABELS.get(self.confidence, self.confidence)


@dataclass
class MatchReport:
    matches: list[BookMatch] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.matches

    @property
    def needs_year(self) -> bool:
        return any(m.needs_year for m in self.matches)

    @property
    def confidence(self) -> str:
        rank = {STRONG: 2, MEDIUM: 1, WEAK: 0}
        return max((m.confidence for m in self.matches), key=lambda c: rank.get(c, 0), default=WEAK)

    def confirmed(self) -> "MatchReport":
        """Оставить только совпадения, подтверждённые доказательством издания.

        У современного переиздания тот же заголовок («Робинзон Крузо») и тот
        же автор, поэтому название без года, издательства/серии или приметы
        издания — не повод для уведомления. Неразрешённый дубль издания
        (`needs_year`) тоже не подтверждается: год мог не написать продавец,
        а издание может оказаться переизданием — такие лоты уходят в
        LLM-очередь.
        """
        return MatchReport(matches=[
            m for m in self.matches
            if m.evidence and m.confidence != WEAK and not m.needs_year
        ])

    @property
    def ambiguous(self) -> list[BookMatch]:
        """Кандидаты на LLM-проверку: доказательства издания нет."""
        return [
            m for m in self.matches
            if m.needs_year or not m.evidence or m.confidence == WEAK
        ]

    def estimate(self) -> tuple[int, int]:
        """Суммарная оценка по позициям; дубли-варианты считаются один раз."""
        total_min = 0
        total_max = 0
        seen_groups: set[str] = set()
        for match in self.matches:
            book = match.book
            # У изданий без цен (позиции 51–68) оценки нет — не считаем.
            if book.price_min is None or book.price_max is None:
                continue
            if book.group:
                if book.group in seen_groups:
                    continue
                seen_groups.add(book.group)
                variants = [
                    m.book for m in self.matches
                    if m.book.group == book.group
                    and m.book.price_min is not None and m.book.price_max is not None
                ]
                total_min += min(b.price_min for b in variants)
                total_max += max(b.price_max for b in variants)
            else:
                total_min += book.price_min
                total_max += book.price_max
        return total_min, total_max


def _has_bad_term(tokens: list[str], terms: tuple[tuple[str, ...], ...]) -> bool:
    for term in terms:
        for idx, token in enumerate(tokens):
            if bad_term_hit(token, term) and not has_negation_before(tokens, idx):
                return True
    return False


def _linked_years(
    tokens: list[str], positions: list[int], year_range: tuple[int, int],
) -> tuple[bool, bool]:
    """(год рядом с ожидаемым, найдено только чужое переиздание)."""
    low, high = year_range
    years: list[int] = []
    for idx, token in enumerate(tokens):
        if not token.isdigit() or len(token) != 4:
            continue
        year = int(token)
        # Диапазон с 1800 года: дореволюционные издания (Брокгауз, журналы).
        if not (1800 <= year <= _CURRENT_YEAR):
            continue
        if year >= _CURRENT_YEAR - 1:
            # «продаю с 2024» — это не год издания.
            continue
        if any(abs(idx - pos) <= _YEAR_PROXIMITY for pos in positions):
            years.append(year)
    if not years:
        return False, False
    # У диапазона (многотомник, журнал) подходит любой год внутри него.
    year_near = any(low - 1 <= year <= high + 1 for year in years)
    # Более позднее издание (даже на 2–4 года) — уже не та книга.
    reprint = not year_near and all(year > high + 1 for year in years)
    return year_near, reprint


def _match_book(
    book: Book,
    segment_tokens: list[str],
    all_tokens: list[str],
    publisher_terms: tuple[tuple[str, ...], ...],
) -> BookMatch | None:
    strict_positions: list[int] = []
    for key in book.title_keys:
        strict_positions.extend(find_positions(segment_tokens, key))

    if not strict_positions:
        # Слабое название («Махабхарата», «Эдда») — только с подтверждением.
        loose_positions: list[int] = []
        for key in book.loose_keys:
            loose_positions.extend(find_positions(segment_tokens, key))
        if not loose_positions:
            return None
    else:
        loose_positions = []

    positions = strict_positions or loose_positions
    year_near, reprint = _linked_years(segment_tokens, positions, book.year_range)

    if reprint:
        return None

    if _has_bad_term(segment_tokens, _GLOBAL_BAD_TERMS + book.bad_terms):
        return None

    author_present = any(
        contains_key(segment_tokens, key) or contains_key(all_tokens, key)
        for key in book.author_keys
    )
    if book.needs_author and not author_present:
        return None

    publisher_present = _publisher_evidence(segment_tokens, publisher_terms)
    discriminator = any(contains_key(segment_tokens, term) for term in book.discriminators)

    if loose_positions and not (year_near or author_present or publisher_present or discriminator):
        return None

    # «Халиф на час. Из тысячи и одной ночи» — цитата из чужого
    # произведения, а не «Книга тысячи и одной ночи».
    if _is_cited_work(segment_tokens, positions):
        return BookMatch(book=book, confidence=WEAK, evidence=())

    if year_near or discriminator:
        confidence = STRONG
    elif publisher_present:
        confidence = MEDIUM
    else:
        confidence = WEAK

    # «Сборник статей о Старшей Эдде» — это не сама Эдда: оставляем, но
    # помечаем как требующее проверки, даже если год совпал.
    if confidence != WEAK and contains_key(segment_tokens, ("сборник",)) \
            and contains_key(segment_tokens, ("статья",)):
        confidence = WEAK

    # «Полное собрание сочинений» — сборник, а не отдельная книга.
    if not _is_collection_book(book) and _has_collection_marker(segment_tokens):
        return BookMatch(book=book, confidence=WEAK, evidence=())

    evidence: list[str] = []
    if year_near:
        evidence.append(f"год {book.year_text or book.year}")
    if publisher_present:
        evidence.append("издательство/серия")
    if discriminator:
        evidence.append("примета издания")

    return BookMatch(
        book=book,
        confidence=confidence,
        year_near=year_near,
        discriminator=discriminator,
        evidence=tuple(evidence),
    )


def _resolve_groups(matches: dict[int, BookMatch]) -> list[BookMatch]:
    grouped: dict[str, list[BookMatch]] = {}
    for match in matches.values():
        if match.book.group:
            grouped.setdefault(match.book.group, []).append(match)

    dropped: set[int] = set()
    resolved: dict[int, BookMatch] = {}
    for _group, variants in grouped.items():
        if len(variants) < 2:
            continue
        by_discriminator = [m for m in variants if m.discriminator]
        by_year = [m for m in variants if m.year_near]
        if by_discriminator:
            keep = by_discriminator
        elif by_year:
            keep = by_year
        else:
            # Ни год, ни примета не разрешили дубль — отправляем оба варианта.
            for match in variants:
                resolved[match.book.index] = BookMatch(
                    book=match.book,
                    confidence=match.confidence,
                    year_near=match.year_near,
                    discriminator=match.discriminator,
                    needs_year=True,
                    evidence=match.evidence,
                )
            continue
        keep_indexes = {m.book.index for m in keep}
        for match in variants:
            if match.book.index not in keep_indexes:
                dropped.add(match.book.index)

    result: list[BookMatch] = []
    for index, match in matches.items():
        if index in dropped:
            continue
        result.append(resolved.get(index, match))
    return sorted(result, key=lambda m: m.book.index)


def _refine_single_book_match(match: BookMatch, all_tokens: list[str]) -> BookMatch | None:
    """Уточнить совпадение по всему тексту лота (только год и плохие термины).

    Лот об одной книге: год издания может стоять в другом абзаце. Если он
    указывает на более позднее переиздание — лот отбрасывается; если совпадает
    с целевым годом — совпадение подтверждается. Издательство и автор при
    этом НЕ переносятся: они относятся к позиции, а не ко всему лоту.
    """
    book = match.book
    positions: list[int] = []
    for key in book.title_keys:
        positions.extend(find_positions(all_tokens, key))
    if not positions:
        for key in book.loose_keys:
            positions.extend(find_positions(all_tokens, key))
    if not positions:
        return match

    year_near, reprint = _linked_years(all_tokens, positions, book.year_range)
    if reprint:
        return None
    if _has_bad_term(all_tokens, _GLOBAL_BAD_TERMS + book.bad_terms):
        return None
    if year_near and not match.year_near:
        evidence = list(match.evidence)
        year_evidence = f"год {book.year_text or book.year}"
        if year_evidence not in evidence:
            evidence.insert(0, year_evidence)
        return BookMatch(
            book=book,
            confidence=STRONG,
            year_near=True,
            discriminator=match.discriminator,
            needs_year=match.needs_year,
            evidence=tuple(evidence),
        )
    return match


def match_text(text: str, catalog: tuple[Book, ...] | None = None) -> MatchReport:
    """Найти в тексте объявления все книги эталонного списка."""
    if not text or not text.strip():
        return MatchReport()

    catalog = catalog or get_catalog()
    all_tokens = tokenize(text)
    segments = [tokenize(part) for part in _SEGMENT_RE.split(text)]
    segments = [tokens for tokens in segments if tokens]
    segments = _merge_year_segments(segments)
    segments = _merge_key_segments(segments, catalog)

    matches: dict[int, BookMatch] = {}
    for segment_tokens in segments:
        for book in catalog:
            found = _match_book(
                book,
                segment_tokens,
                all_tokens,
                _PUBLISHER_TERMS.get(book.series, ()),
            )
            if found is None:
                continue
            previous = matches.get(book.index)
            if previous is None or _rank(found.confidence) > _rank(previous.confidence):
                matches[book.index] = found
            elif found.year_near and not previous.year_near:
                matches[book.index] = found

    # Лот об одной книге: всё описание относится к ней одной, поэтому год
    # ищем по всему тексту — год из дальнего абзаца либо подтверждает
    # целевое издание, либо указывает на переиздание. Издательство и автор
    # при этом НЕ переносятся: они принадлежат позиции, а не всему лоту.
    if matches and len({m.book.group or m.book.index for m in matches.values()}) == 1:
        for index, match in list(matches.items()):
            refined = _refine_single_book_match(match, all_tokens)
            if refined is None:
                del matches[index]
            else:
                matches[index] = refined

    return MatchReport(matches=_resolve_groups(matches))


def _rank(confidence: str) -> int:
    return {STRONG: 2, MEDIUM: 1, WEAK: 0}.get(confidence, 0)
