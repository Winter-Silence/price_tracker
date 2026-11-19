"""Загрузка эталонного каталога книг (data/books_catalog.json)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "books_catalog.json"


@dataclass(frozen=True)
class Book:
    index: int
    author: str
    title: str
    year: int | None  # единственный год издания; у диапазона — None
    publisher: str
    series: str  # "lp" — Литературные памятники, "academia" — Academia
    price_min: int | None
    price_max: int | None
    price_top: int | None
    title_keys: tuple[tuple[str, ...], ...]
    loose_keys: tuple[tuple[str, ...], ...]
    author_keys: tuple[tuple[str, ...], ...]
    discriminators: tuple[tuple[str, ...], ...]
    bad_terms: tuple[tuple[str, ...], ...]
    needs_author: bool
    group: str
    note: str
    query: str
    # Поля изданий без цен (позиции 51–68); у старых записей пустые.
    year_from: int | None = None
    year_to: int | None = None
    year_text: str = ""
    place: str = ""
    artist: str = ""
    volume: str = ""
    queries: tuple[str, ...] = ()

    @property
    def year_range(self) -> tuple[int, int]:
        """Диапазон годов издания: (lo, hi)."""
        if self.year_from is not None and self.year_to is not None:
            return self.year_from, self.year_to
        year = self.year if self.year is not None else 0
        return year, year

    @property
    def label(self) -> str:
        year = self.year_text or (self.year if self.year is not None else "год не указан")
        parts = [f"{self.author}. {self.title}" if self.author not in ("—", "") else self.title,
                 f"{year}, {self.publisher}"]
        return " — ".join(parts)


def _book_from_dict(data: dict) -> Book:
    return Book(
        index=data["index"],
        author=data["author"],
        title=data["title"],
        year=data.get("year"),
        publisher=data["publisher"],
        series=data["series"],
        price_min=data["price_min"],
        price_max=data["price_max"],
        price_top=data["price_top"],
        title_keys=tuple(tuple(k) for k in data["title_keys"]),
        loose_keys=tuple(tuple(k) for k in data["loose_keys"]),
        author_keys=tuple(tuple(k) for k in data["author_keys"]),
        discriminators=tuple(tuple(k) for k in data["discriminators"]),
        bad_terms=tuple(tuple(k) for k in data["bad_terms"]),
        needs_author=bool(data["needs_author"]),
        group=data.get("group", ""),
        note=data.get("note", ""),
        query=data.get("query", ""),
        year_from=data.get("year_from"),
        year_to=data.get("year_to"),
        year_text=data.get("year_text", ""),
        place=data.get("place", ""),
        artist=data.get("artist", ""),
        volume=data.get("volume", ""),
        queries=tuple(data.get("queries", ())),
    )


@lru_cache(maxsize=1)
def get_catalog(path: str | None = None) -> tuple[Book, ...]:
    """Вернуть каталог (кэшируется на процесс)."""
    source = Path(path) if path else CATALOG_PATH
    raw = json.loads(source.read_text(encoding="utf-8"))
    return tuple(_book_from_dict(item) for item in raw)
