#!/usr/bin/env python3
"""Собрать data/books_catalog.json из docs/books/02_price_table.md и 04_avito_catalog_ext.md.

Автор, название, год, издательство и цены берутся из таблиц. Всё, что
касается распознавания (ключи названий, авторы, дискриминаторы, негативные
термины), задаётся в OVERRIDES ниже: таблица — источник фактов, этот файл —
источник правил матчинга.

Позиции 1–50 — «Литературные памятники» и Academia с ценами; 51–68 —
издания без цен (только распознавание и приметы издания).

Запуск: ./venv/bin/python scripts/build_books_catalog.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from books.normalize import key_tokens, tokenize  # noqa: E402

SOURCE = ROOT / "docs" / "books" / "02_price_table.md"
EXT_SOURCE = ROOT / "docs" / "books" / "04_avito_catalog_ext.md"
TARGET = ROOT / "data" / "books_catalog.json"

# Позиции 1–50 — с ценами, 51–68 — издания без цен.
EXT_FIRST_INDEX = 51
EXT_COUNT = 18

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

# Дополнение к названиям, которые из таблицы 04 не выводятся автоматически.
NEW_OVERRIDES: dict[int, dict] = {
    51: {"extra_keys": ["Супрематический сказ про два квадрата"]},
    62: {"extra_keys": ["Брокгауз и Ефрон"]},
}

# Три поисковых запроса на позицию. В таблице 04 приведён только первый
# (для чтения), остальные живут здесь: URL поиска по ним не строятся.
EXT_QUERIES: dict[int, list[str]] = {
    51: ["Лисицкий Супрематический сказ про два квадрата 1922",
         "Лисицкий Скифы 1922 два квадрата",
         "Супрематический сказ про два квадрата в 6 постройках"],
    52: ["Маяковский Для голоса 1923 Лисицкий",
         "Для голоса Маяковского Берлин 1923",
         "Маяковский Лисицкий конструктор книги"],
    53: ["Маршак Цирк 1925 Лебедев Радуга", "Маршак Цирк 1925",
         "Маршак Лебедев Цирк издательство Радуга"],
    54: ["Маяковский Про это 1923 Родченко", "Про это Маяковский 1923",
         "Маяковский Родченко Про это оформление"],
    55: ["Толстой Аэлита 1923 Закат Марса", "Толстой Аэлита первое издание 1923",
         "Аэлита Толстой Ладыжников 1923"],
    56: ["Беляев Человек-амфибия первое издание 1928", "Беляев Человек-амфибия 1929",
         "Человек-амфибия Беляев отдельное издание"],
    57: ["Чуковский Мойдодыр 1923 Анненков Радуга",
         "Мойдодыр Чуковского 1923 первое издание", "Чуковский Мойдодыр Анненков"],
    58: ["Хармс Иван Иваныч Самовар 1929", "Хармс Самовар Ермолаева",
         "Иван Иваныч Самовар 1929 Государственное издательство"],
    59: ["Беляев Голова профессора Доуэля 1926 Земля и Фабрика",
         "Голова профессора Доуэля Беляев первое издание 1926",
         "Беляев Доуэля Земля и Фабрика"],
    60: ["Золотое руно журнал 1906", "Золотое руно Рябушинский 1909",
         "журнал Золотое руно 1906 1907 1908 1909"],
    61: ["Мир искусства журнал 1899", "Мир искусства Дягилев Санкт-Петербург",
         "журнал Мир искусства 1900 1901 1902 1903 1904"],
    62: ["Брокгауз Ефрон Энциклопедический словарь 1890",
         "Энциклопедический словарь Брокгауза и Ефрона том",
         "Брокгауз Ефрон СПб 1890 1907"],
    63: ["Даль Толковый словарь живого великорусского языка 1880",
         "Даль словарь 1880 1881 1882 Вольф", "Толковый словарь Даля второе издание том"],
    64: ["Брем Жизнь животных 10 томов иллюстрированное",
         "Брем Жизнь животных хромолитографии политипажи",
         "Жизнь животных Брема antique edition"],
    65: ["журнал Аполлон 1909", "Аполлон Маковский журнал 1910 1911 1912",
         "журнал Аполлон Санкт-Петербург 1909 1917"],
    66: ["журнал Зодчий архитектурный 1900", "Зодчий журнал Санкт-Петербург архитекторов",
         "журнал Зодчий 1910 1912 1913"],
    67: ["Маршак Багаж 1926 Лебедев", "Багаж Маршака Радуга 1926",
         "Маршак Багаж Лебедев иллюстрации"],
    68: ["Толстой Гиперболоид инженера Гарина 1927",
         "Гиперболоид Гарина Толстого первое издание 1927",
         "Толстой Гиперболоид 1927 отдельное издание"],
}

# Негативные термины: тот же общий список, что books.matcher._GLOBAL_BAD_TERMS.
BAD_TERMS = ["репринт", "репринтный", "факсимиле", "переиздание",
             "репродукция", "аудиокнига"]

# Источник задаёт год словами: берём ориентир, иначе позиция без года.
YEAR_TEXT_FALLBACK = {"конец XIX века": (1890, 1900)}


def parse_year_text(text: str) -> tuple[int, int]:
    """«1922» → (1922, 1922); «1906–1909» → (1906, 1909)."""
    years = [int(year) for year in re.findall(r"\d{4}", text)]
    if years:
        return min(years), max(years)
    if text.strip() in YEAR_TEXT_FALLBACK:
        return YEAR_TEXT_FALLBACK[text.strip()]
    raise SystemExit(f"Не разобрать год: {text}")


def title_keys_for(title: str, extra: list[str]) -> list[list[str]]:
    """Ключи названия: полное, часть до подзаголовка, плюс варианты из NEW_OVERRIDES."""
    keys: list[list[str]] = []
    # «Про это» состоит только из служебных слов — ключ из них же; шум гасит confirmed().
    for text in (title, re.split(r"[.(]", title, maxsplit=1)[0], *extra):
        tokens = key_tokens(text) or tokenize(text)
        if tokens and tokens not in keys:
            keys.append(tokens)
    return keys


def author_keys_for(author: str) -> list[list[str]]:
    """Ключи автора: имя до пояснения в скобках и отдельно фамилия."""
    name = author.split("(")[0].strip()
    if name in ("", "—"):
        return []
    tokens = key_tokens(name)
    if not tokens:
        return []
    return [tokens, tokens[-1:]] if len(tokens) > 1 else [tokens]


def discriminator_keys(*texts: str) -> list[list[str]]:
    """Приметы издания из места, художника, тома и издательства (без короткого шума)."""
    keys: list[list[str]] = []
    for text in texts:
        # Пояснение в скобках — не примета издания («(2-е издание)»).
        tokens = [token for token in key_tokens(text.split("(")[0]) if len(token) >= 3]
        if tokens and tokens not in keys:
            keys.append(tokens)
    return keys


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


def parse_ext_rows() -> list[dict]:
    """Строки таблицы 04 (18 изданий без цен)."""
    columns = ("author", "title", "year_text", "publisher", "place",
               "artist", "volume", "note", "query")
    rows: list[dict] = []
    for line in EXT_SOURCE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or "Автор" in line:
            continue
        parts = [cell.strip() for cell in line.strip("|").split("|")]
        if len(parts) != len(columns):
            raise SystemExit(f"Ожидалось {len(columns)} колонок в {EXT_SOURCE.name}: {line}")
        rows.append(dict(zip(columns, parts)))
    return rows


def build_main(rows: list[dict]) -> list[dict]:
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


def build_ext(rows: list[dict]) -> list[dict]:
    catalog: list[dict] = []
    for index, row in enumerate(rows, start=EXT_FIRST_INDEX):
        override = NEW_OVERRIDES.get(index, {})
        year_from, year_to = parse_year_text(row["year_text"])
        title_keys = title_keys_for(row["title"], override.get("extra_keys", []))
        if not title_keys:
            raise SystemExit(f"Позиция {index}: пустые ключи названия")

        catalog.append({
            "index": index,
            "author": row["author"],
            "title": row["title"],
            # Единственный год — в year, диапазон — только в year_from/year_to.
            "year": year_from if year_from == year_to else None,
            "publisher": row["publisher"],
            "series": "",
            "price_min": None,
            "price_max": None,
            "price_top": None,
            "title_keys": title_keys,
            "loose_keys": [],
            "author_keys": author_keys_for(row["author"]),
            "discriminators": discriminator_keys(
                row["place"], row["artist"], row["volume"], row["publisher"]),
            "bad_terms": [key_tokens(term) for term in BAD_TERMS],
            "needs_author": False,
            "group": "",
            "note": row["note"],
            "query": row["query"],
            "year_from": year_from,
            "year_to": year_to,
            "year_text": row["year_text"],
            "place": row["place"],
            "artist": row["artist"],
            "volume": row["volume"],
            "queries": EXT_QUERIES[index],
        })

    return catalog


def build() -> list[dict]:
    rows = parse_rows()
    ext_rows = parse_ext_rows()
    if len(rows) != 50 or len(ext_rows) != EXT_COUNT:
        raise SystemExit(
            f"Ожидалось 50 строк в {SOURCE.name} и {EXT_COUNT} в {EXT_SOURCE.name}, "
            f"получено {len(rows)} и {len(ext_rows)}"
        )

    catalog = build_main(rows) + build_ext(ext_rows)
    indexes = [row["index"] for row in catalog]
    if indexes != list(range(1, 50 + EXT_COUNT + 1)):
        raise SystemExit(f"Индексы не непрерывны: {indexes}")
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
