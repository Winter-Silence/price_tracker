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
    year: int
    publisher: str
    series: str  # "lp" — Литературные памятники, "academia" — Academia
    price_min: int
    price_max: int
    price_top: int
    title_keys: tuple[tuple[str, ...], ...]
    loose_keys: tuple[tuple[str, ...], ...]
    author_keys: tuple[tuple[str, ...], ...]
    discriminators: tuple[tuple[str, ...], ...]
    bad_terms: tuple[tuple[str, ...], ...]
    needs_author: bool
    group: str
    note: str
    query: str

    @property
    def label(self) -> str:
        parts = [f"{self.author}. {self.title}" if self.author not in ("—", "") else self.title,
                 f"{self.year}, {self.publisher}"]
        return " — ".join(parts)


def _book_from_dict(data: dict) -> Book:
    return Book(
        index=data["index"],
        author=data["author"],
        title=data["title"],
        year=data["year"],
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
    )


@lru_cache(maxsize=1)
def get_catalog(path: str | None = None) -> tuple[Book, ...]:
    """Вернуть каталог (кэшируется на процесс)."""
    source = Path(path) if path else CATALOG_PATH
    raw = json.loads(source.read_text(encoding="utf-8"))
    return tuple(_book_from_dict(item) for item in raw)
