"""Нормализация русского текста для сопоставления книжных лотов.

Морфология — pymorphy3, нечёткое сравнение токенов — rapidfuzz.
Все ключи каталога и токены объявления приводятся к леммам, поэтому
"Галльской", "галльская" и "ГАЛЛЬСКОЙ" для матчера одно и то же.
"""

from __future__ import annotations

import re
from functools import lru_cache

import pymorphy3
from rapidfuzz import fuzz

_MORPH = pymorphy3.MorphAnalyzer()

# Служебные слова, которые не несут смысла в названии книги.
STOPWORDS = frozenset({
    "а", "без", "бы", "в", "во", "вот", "все", "всё", "вы", "где", "да",
    "для", "до", "его", "ее", "её", "если", "есть", "же", "за", "и", "из",
    "или", "их", "к", "как", "ко", "когда", "кто", "ли", "мне", "на", "над",
    "не", "но", "о", "об", "он", "она", "они", "от", "по", "под", "при",
    "про", "с", "со", "так", "то", "у", "что", "это", "я",
    # Библиографический шум.
    "г", "год", "года", "году", "изд", "издание", "издательство", "кн",
    "книга", "книги", "книгу", "пер", "ред", "серия", "собр", "соч", "стр",
    "т", "том", "тома", "часть", "ч", "худ", "экз", "тираж",
})

_TOKEN_RE = re.compile(r"[a-zа-я0-9]+")


def normalize_text(text: str) -> str:
    """Привести текст к нижнему регистру и единому виду."""
    if not text:
        return ""
    text = text.lower().replace("ё", "е")
    # Латиница в объявлениях почти всегда означает издательство Academia.
    text = text.replace("academia", "академия")
    return text


@lru_cache(maxsize=20000)
def lemma(word: str) -> str:
    """Лемма одного слова (с кэшем — матчер вызывает её тысячи раз)."""
    word = normalize_text(word)
    if not word:
        return ""
    if word.isdigit():
        return word
    return _MORPH.parse(word)[0].normal_form


def tokenize(text: str) -> list[str]:
    """Токены-леммы в порядке появления (служебные слова сохраняются)."""
    return [lemma(tok) for tok in _TOKEN_RE.findall(normalize_text(text))]


def key_tokens(text: str) -> list[str]:
    """Токены-леммы для ключа каталога: без служебных слов и номеров томов."""
    result = []
    for tok in tokenize(text):
        if not tok or tok in STOPWORDS:
            continue
        if tok.isdigit() and len(tok) <= 2:
            # "т. 1", "кн. 2", "ч. 1" — номер тома не должен быть обязателен.
            continue
        result.append(tok)
    return result


def token_matches(left: str, right: str) -> bool:
    """Нечёткое равенство лемм: опечатки и словоформы-исключения."""
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 4:
        return False
    if left[0] != right[0]:
        return False
    return fuzz.ratio(left, right) >= 85


def find_positions(haystack: list[str], needle: list[str]) -> list[int]:
    """Позиции вхождения ключа (bag of words) в список токенов.

    Возвращает позиции первого токена ключа для каждого полного вхождения.
    """
    if not needle:
        return []
    positions: list[int] = []
    for start, tok in enumerate(haystack):
        if not token_matches(tok, needle[0]):
            continue
        used = {start}
        matched = True
        for part in needle[1:]:
            found = None
            for idx, candidate in enumerate(haystack):
                if idx in used:
                    continue
                if token_matches(candidate, part):
                    found = idx
                    break
            if found is None:
                matched = False
                break
            used.add(found)
        if matched:
            positions.append(start)
    return positions


def contains_key(haystack: list[str], needle: list[str]) -> bool:
    return bool(find_positions(haystack, needle))


def has_negation_before(tokens: list[str], position: int, window: int = 3) -> bool:
    """Есть ли "не" в трёх токенах перед позицией (учёт "не репринт")."""
    start = max(0, position - window)
    return "не" in tokens[start:position]


def bad_term_hit(token: str, term: list[str]) -> bool:
    """Совпадает ли токен с негативным термином (с учётом словоформ)."""
    if not term:
        return False
    head = term[0]
    if token == head:
        return True
    if token.startswith(head) or head.startswith(token):
        return True
    if min(len(token), len(head)) >= 5 and token[0] == head[0]:
        return fuzz.ratio(token, head) >= 88
    return False
