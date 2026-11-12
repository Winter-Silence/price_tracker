#!/usr/bin/env python3
"""Собрать data/books_catalog.json из docs/books/02_price_table.md.

Автор, название, год, издательство и цены берутся из таблицы. Всё, что
касается распознавания (ключи названий, авторы, дискриминаторы, негативные
термины), задаётся в OVERRIDES ниже: таблица — источник фактов, этот файл —
источник правил матчинга.

Запуск: ./venv/bin/python scripts/build_books_catalog.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from books.normalize import key_tokens  # noqa: E402

SOURCE = ROOT / "docs" / "books" / "02_price_table.md"
TARGET = ROOT / "data" / "books_catalog.json"

# Ключи названий задаются в исходном виде — builder сам приводит их к леммам.
# loose_keys срабатывают только при подтверждении годом/автором.
OVERRIDES: dict[int, dict] = {
    1: {"title_keys": ["Записки о Галльской войне", "Записки о Галльской"],
        "author_keys": ["Цезарь"]},
    2: {"title_keys": ["Хожение за три моря", "Хождение за три моря"],
        "author_keys": ["Афанасий Никитин", "Никитин"]},
    3: {"title_keys": ["Сиасет-намэ", "Сиасет наме", "Сиасет намэ"],
        "author_keys": ["Низам ал-Мульк", "Низам аль-Мульк", "Низам"]},
    4: {"title_keys": ["Письма к Аттику", "Письма Аттику"],
        "author_keys": ["Цицерон"]},
    5: {"title_keys": ["Земледелие"], "author_keys": ["Катон"],
        "needs_author": True},
    6: {"title_keys": ["Адипарва"], "loose_keys": ["Махабхарата"]},
    7: {"title_keys": ["Письма"], "author_keys": ["Плиний"],
        "needs_author": True},
    8: {"title_keys": ["Повесть временных лет", "ПВЛ"]},
    9: {"title_keys": ["Слово о полку Игореве"], "group": "слово_о_полку"},
    10: {"title_keys": ["Послания Ивана Грозного", "Послания"],
         "author_keys": ["Иван Грозный", "Грозный"], "needs_author": True},
    11: {"title_keys": ["Анабасис"], "author_keys": ["Ксенофонт"]},
    12: {"title_keys": ["Фарсалия"], "author_keys": ["Лукан"]},
    13: {"title_keys": ["Книга о скудости и богатстве", "О скудости и богатстве"],
         "author_keys": ["Посошков"]},
    14: {"title_keys": ["Временник"], "author_keys": ["Иван Тимофеев", "Тимофеев"],
         "needs_author": True},
    15: {"title_keys": ["Рассуждение о добровольном рабстве"],
         "author_keys": ["Ла Боэси", "Боэси"]},
    16: {"title_keys": ["Кабус-намэ", "Кабус наме", "Кабус намэ"]},
    17: {"title_keys": ["Избранные сочинения"],
         "author_keys": ["Симеон Полоцкий", "Полоцкий"], "needs_author": True},
    18: {"title_keys": ["Опыты"], "author_keys": ["Монтень"], "needs_author": True},
    19: {"title_keys": ["Шицзин"]},
    20: {"title_keys": ["Артхашастра"]},
    21: {"title_keys": ["Дигенис Акрит"]},
    22: {"title_keys": ["Книга моего деда Коркута", "Книга деда Коркута", "Коркут"]},
    23: {"title_keys": ["Сабхапарва"], "loose_keys": ["Махабхарата"]},
    24: {"title_keys": ["Старшая Эдда"], "loose_keys": ["Эдда"],
         "bad_terms": ["младшая"]},
    25: {"title_keys": ["Эпос сербского народа", "Сербский эпос"]},
    26: {"title_keys": ["Политика"], "author_keys": ["Крижанич"],
         "needs_author": True},
    27: {"title_keys": ["Полное собрание творений"],
         "author_keys": ["Платон"], "needs_author": True},
    28: {"title_keys": ["Проблема стихотворного языка"], "author_keys": ["Тынянов"]},
    29: {"title_keys": ["Я и Оно"], "author_keys": ["Фрейд"], "needs_author": True},
    30: {"title_keys": ["Собрание сочинений"],
         "author_keys": ["Анри де Ренье", "Ренье"], "needs_author": True},
    31: {"title_keys": ["Поэтика"], "author_keys": ["Аристотель"],
         "needs_author": True},
    32: {"title_keys": ["Декамерон"], "author_keys": ["Боккаччо"]},
    33: {"title_keys": ["Морфология сказки"], "author_keys": ["Пропп"]},
    34: {"title_keys": ["Путешествия Гулливера", "Гулливер"], "author_keys": ["Свифт"]},
    35: {"title_keys": ["Золотой осел", "Метаморфозы"], "author_keys": ["Апулей"]},
    36: {"title_keys": ["Книга лирики"], "author_keys": ["Катулл"],
         "needs_author": True},
    37: {"title_keys": ["Дон Кихот"], "author_keys": ["Сервантес"]},
    38: {"title_keys": ["Робинзон Крузо"], "author_keys": ["Дефо"]},
    39: {"title_keys": ["Книга тысячи и одной ночи", "Тысяча и одна ночь", "1001 ночь"]},
    40: {"title_keys": ["Книга комедий", "Комедии Аристофана"],
         "author_keys": ["Аристофан"], "needs_author": True},
    41: {"title_keys": ["Похвальное слово глупости", "Похвала глупости"],
         "author_keys": ["Эразм", "Роттердамский"]},
    42: {"title_keys": ["Жизнь Бенвенуто Челлини", "Челлини"],
         "author_keys": ["Челлини"]},
    43: {"title_keys": ["Эфиопика"], "author_keys": ["Гелиодор"]},
    44: {"title_keys": ["Манон Леско"], "author_keys": ["Прево"]},
    45: {"title_keys": ["Русская крестьянская игрушка", "Крестьянская игрушка"]},
    46: {"title_keys": ["Слово о полку Игореве"], "group": "слово_о_полку",
         "discriminators": ["Палех", "Голиков"]},
    47: {"title_keys": ["Фацетии"], "author_keys": ["Поджо Браччолини", "Браччолини"]},
    48: {"title_keys": ["Сасанидский металл", "Сасанидское серебро"]},
    49: {"title_keys": ["Фаюмский портрет", "Фаюмские портреты"]},
    50: {"title_keys": ["Основания новой науки об общей природе наций",
                       "Основания новой науки"],
         "author_keys": ["Вико"]},
}

TITLE_GROUPS = {"слово_о_полку": "Слово о полку Игореве"}


def parse_price(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def parse_range(text: str) -> tuple[int, int]:
    parts = re.split(r"[–—-]", text)
    if len(parts) < 2:
        value = parse_price(text)
        return value, value
    return parse_price(parts[0]), parse_price(parts[1])


def parse_rows() -> list[dict]:
    rows: list[dict] = []
    for line in SOURCE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or "Автор" in line:
            continue
        parts = [cell.strip() for cell in line.strip("|").split("|")]
        if len(parts) < 8:
            continue
        author, title, year, publisher, avg, top, note, query = parts[:8]
        rows.append({
            "author": author,
            "title": title,
            "year": int(year),
            "publisher": publisher,
            "avg": avg,
            "top": top,
            "note": note,
            "query": query.strip("`"),
        })
    return rows


def build() -> list[dict]:
    rows = parse_rows()
    if len(rows) != 50:
        raise SystemExit(f"Ожидалось 50 строк, получено {len(rows)}")

    catalog: list[dict] = []
    for index, row in enumerate(rows, start=1):
        override = OVERRIDES.get(index, {})
        series = "academia" if "academia" in row["publisher"].lower() else "lp"
        price_min, price_max = parse_range(row["avg"])
        title_keys = [key_tokens(text) for text in override.get("title_keys", [row["title"]])]
        loose_keys = [key_tokens(text) for text in override.get("loose_keys", [])]
        author_keys = [key_tokens(text) for text in override.get("author_keys", [])]
        title_keys = [tokens for tokens in title_keys if tokens]
        loose_keys = [tokens for tokens in loose_keys if tokens]
        author_keys = [tokens for tokens in author_keys if tokens]
        if not title_keys and not loose_keys:
            raise SystemExit(f"Позиция {index}: пустые ключи названия")

        catalog.append({
            "index": index,
            "author": row["author"],
            "title": row["title"],
            "year": row["year"],
            "publisher": row["publisher"],
            "series": series,
            "price_min": price_min,
            "price_max": price_max,
            "price_top": parse_price(row["top"]),
            "title_keys": title_keys,
            "loose_keys": loose_keys,
            "author_keys": author_keys,
            "discriminators": [key_tokens(t) for t in override.get("discriminators", [])],
            "bad_terms": [key_tokens(t) for t in override.get("bad_terms", [])],
            "needs_author": bool(override.get("needs_author", False)),
            "group": override.get("group", ""),
            "note": row["note"],
            "query": row["query"],
        })

    return catalog


def main() -> None:
    catalog = build()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Записано {len(catalog)} позиций в {TARGET}")


if __name__ == "__main__":
    main()
