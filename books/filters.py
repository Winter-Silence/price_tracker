"""Фильтры объявления: дата публикации и отзывы продавца.

Avito показывает дату относительными формулировками («сегодня в 14:35»,
«вчера», «2 часа назад») или «8 ноября». Считаем всё по Москве: сервер
может жить в UTC, а объявления датированы местным временем.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
    # На случай, если Avito отдаст именительный падеж.
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11,
    "декабрь": 12,
}

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
_RELATIVE_RE = re.compile(
    r"(\d+)\s*(минут|час|день|дн|сут|недел|месяц)\w*\s*назад"
)
_REVIEWS_RE = re.compile(r"(\d[\d\s]*)\s*отзыв")


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(MOSCOW)
    if now.tzinfo is None:
        return now.replace(tzinfo=MOSCOW)
    return now.astimezone(MOSCOW)


def _at(date: datetime, hour: int, minute: int) -> datetime:
    return date.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_posting_date(text: str, now: datetime | None = None) -> datetime | None:
    """Разобрать строку даты Avito. None — дата не распознана."""
    if not text:
        return None
    current = _now(now)
    value = " ".join(text.lower().replace("ё", "е").split())

    # JSON-LD отдаёт дату в ISO-виде (datePosted), иногда со смещением.
    iso = re.match(
        r"(\d{4})-(\d{2})-(\d{2})[t ](\d{2}):(\d{2})(?::\d{2})?"
        r"(z|[+-]\d{2}:?\d{2})?",
        value,
    )
    if iso:
        tz = MOSCOW
        raw_tz = iso.group(6)
        if raw_tz:
            if raw_tz == "z":
                tz = timezone.utc
            else:
                sign = 1 if raw_tz[0] == "+" else -1
                digits = raw_tz[1:].replace(":", "")
                tz = timezone(
                    sign * timedelta(
                        hours=int(digits[:2]), minutes=int(digits[2:4])
                    )
                )
        try:
            parsed = datetime(
                int(iso.group(1)), int(iso.group(2)), int(iso.group(3)),
                int(iso.group(4)), int(iso.group(5)), tzinfo=tz,
            )
        except ValueError:
            return None
        return parsed.astimezone(MOSCOW)

    relative = _RELATIVE_RE.search(value)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit.startswith("минут"):
            return current - timedelta(minutes=amount)
        if unit.startswith("час"):
            return current - timedelta(hours=amount)
        if unit.startswith(("день", "дн", "сут")):
            return current - timedelta(days=amount)
        if unit.startswith("недел"):
            return current - timedelta(weeks=amount)
        if unit.startswith("месяц"):
            return current - timedelta(days=30 * amount)

    time_match = _TIME_RE.search(value)
    hour = int(time_match.group(1)) if time_match else 0
    minute = int(time_match.group(2)) if time_match else 0
    has_time = time_match is not None

    if "позавчера" in value:
        return _at(current - timedelta(days=2), hour, minute)
    if "вчера" in value:
        return _at(current - timedelta(days=1), hour, minute)
    if "сегодня" in value:
        return _at(current, hour, minute) if has_time else current

    day_month = re.search(
        r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")", value
    )
    if day_month:
        day = int(day_month.group(1))
        month = _MONTHS[day_month.group(2)]
        year_match = re.search(
            r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})", value
        )
        year = int(year_match.group(3)) if year_match else current.year
        try:
            parsed = datetime(year, month, day, hour, minute, tzinfo=MOSCOW)
        except ValueError:
            return None
        if parsed > current + timedelta(days=1):
            try:
                parsed = parsed.replace(year=year - 1)
            except ValueError:
                return None
        return parsed

    return None


def parse_reviews_count(text: str) -> int | None:
    """Количество отзывов продавца. None — счётчик не найден."""
    if not text:
        return None
    value = text.lower().replace("ё", "е")
    if "нет отзыв" in value:
        return 0
    match = _REVIEWS_RE.search(value)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else 0


def is_wholesale_seller(reviews_count: int | None,
                        limit: int = 50) -> bool:
    """Продавец с 50+ отзывами — оптовик, такие лоты пропускаем."""
    return reviews_count is not None and reviews_count >= limit


def is_fresh(published_at: datetime | None, now: datetime | None = None,
             max_age_hours: int = 24) -> bool:
    """Свежее ли объявление. Неизвестная дата считается свежей."""
    if published_at is None:
        return True
    current = _now(now)
    return current - published_at <= timedelta(hours=max_age_hours)
