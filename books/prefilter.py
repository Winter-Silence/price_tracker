"""Заголовочный фильтр: стоит ли вообще открывать карточку объявления.

Книжный режим не должен открывать каждый новый лот — массовый скрапинг
карточек быстро приводит к бану. По заголовку в выдаче решаем, есть ли в
лоте хоть один признак интересной книги:

* название/автор из эталонного списка (полный матчер по одному заголовку);
* специфичное название из двух и более слов («Слово о полку Игореве»);
* серия/издательство: «Литературные памятники», «Academia», «Академия»;
* антикварные / коллекционные маркеры: «антикварные», «старинные»,
  «из домашней библиотеки», «подборка книг», «одним лотом» и т. п.;
* год целевой эпохи (1922–1937 или 1948–1965) рядом с книжным словом.

Если ни одного признака нет («Марафон похудения», «Жириновский. Собрание
сочинений», «Учебник по физике»), карточка не открывается вообще.
"""

from __future__ import annotations

from datetime import datetime

from books.catalog import Book, get_catalog
from books.matcher import match_text
from books.normalize import contains_key, normalize_text, tokenize

_CURRENT_YEAR = datetime.now().year

# Целевые годы изданий: «Литературные памятники» и Academia.
ERA_RANGES = ((1922, 1937), (1948, 1965))

# Слова, подтверждающие, что речь про книгу (для маркера «год эпохи»).
_BOOK_WORDS = (
    ("книга",), ("том",), ("издание",), ("собрание",), ("серия",),
    ("обложка",), ("переплет",), ("ссср",), ("букинист",),
)

# Пары/тройки лемм, каждая из которых должна встретиться в заголовке.
_MARKER_KEYS: tuple[tuple[str, ...], ...] = (
    ("литературный", "памятник"),
    ("антикварный",),
    ("старинный",),
    ("букинистический",),
    ("дореволюционный",),
    ("винтажный",),
    ("редкий", "книга"),
    ("коллекция", "книга"),
    ("личный", "коллекция"),
    ("домашний", "библиотека"),
    ("личный", "библиотека"),
    ("из", "библиотека"),
    ("подборка", "книга"),
    ("лот", "книга"),
    ("один", "лот"),
    ("пачка", "книга"),
    ("много", "книга"),
    ("все", "вместе"),
    ("собрание", "книга"),
    ("академия", "книга"),
)

# Подстроки в нормализованном тексте (леммы тут не нужны).
_MARKER_SUBSTRINGS = (
    "academia",
    "xix",
    "19 век",
)

# Маркеры «коллекционности» в заголовке: много книг одним лотом.
COLLECTION_REASONS = frozenset({"series", "antique", "collection"})


def _era_years(tokens: list[str]) -> list[int]:
    years = []
    for token in tokens:
        if not token.isdigit() or len(token) != 4:
            continue
        year = int(token)
        if any(low <= year <= high for low, high in ERA_RANGES):
            years.append(year)
    return years


def _year_matches(book: Book, years: list[int]) -> bool:
    """Год в заголовке не противоречит книге (или года нет вовсе)."""
    if not years:
        return True
    low, high = book.year_range
    return any(low - 1 <= year <= high + 1 for year in years)


def lot_title_interesting(
    title: str, catalog: tuple[Book, ...] | None = None,
) -> tuple[bool, str]:
    """Решить по заголовку лота, открывать ли карточку.

    Возвращает (интересен, причина). Причина нужна для логов и dry-run:
    book_title / title_key / author / series / antique / collection / era_year
    либо пустая строка.
    """
    if not title or not title.strip():
        return False, ""

    catalog = catalog or get_catalog()
    tokens = tokenize(title)

    # 1. Полноценный матчер по заголовку: название + год/автор/серия.
    if not match_text(title, catalog).is_empty:
        return True, "book_title"

    years = [int(t) for t in tokens
             if t.isdigit() and len(t) == 4 and 1900 <= int(t) <= _CURRENT_YEAR]

    # 2. Специфичное название из нескольких слов или «слабое» название.
    #    Книги, которым нужен автор («Собрание сочинений», «Письма»),
    #    пропускаем: сам заголовок без автора ничего не доказывает,
    #    а автора из списка поймает шаг 3.
    for book in catalog:
        if book.needs_author or not _year_matches(book, years):
            continue
        for key in book.title_keys:
            if len(key) >= 2 and contains_key(tokens, key):
                return True, "title_key"
        for key in book.loose_keys:
            if contains_key(tokens, key):
                return True, "title_key"

    # 3. Автор из списка (в сборных лотах авторы перечислены в заголовке).
    for book in catalog:
        if not _year_matches(book, years):
            continue
        for key in book.author_keys:
            if contains_key(tokens, key):
                return True, "author"

    # 4. Серия, издательство, антикварные и коллекционные маркеры.
    for key in _MARKER_KEYS:
        if contains_key(tokens, key):
            if key[0] == "литературный" or key[0] == "академия":
                return True, "series"
            if key[0] in ("антикварный", "старинный", "букинистический",
                          "дореволюционный", "винтажный"):
                return True, "antique"
            return True, "collection"

    text = normalize_text(title)
    for needle in _MARKER_SUBSTRINGS:
        if needle in text:
            return True, "series" if needle in ("academia",) else "antique"

    # 5. Год целевой эпохи — только если рядом есть книжное слово.
    era = _era_years(tokens)
    if era and any(contains_key(tokens, word) for word in _BOOK_WORDS):
        return True, "era_year"

    return False, ""
