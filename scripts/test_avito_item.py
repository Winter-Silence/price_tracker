#!/usr/bin/env python3
"""Ручная проверка карточки объявления Avito (книжный режим).

Запуск: ./venv/bin/python scripts/test_avito_item.py "<url объявления>"
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from books.filters import is_fresh, parse_posting_date, parse_reviews_count
from books.matcher import match_text
from parsers.avito import AvitoParser
from utils.display import ensure_xvfb, stop_xvfb


async def main() -> None:
    if len(sys.argv) < 2:
        print("Укажи URL объявления Avito")
        return

    url = sys.argv[1]
    load_dotenv()
    xvfb_proc = ensure_xvfb()
    parser = AvitoParser()
    try:
        details = await parser.get_item_details(url)
        if details is None:
            print("RESULT: None (капча/блокировка/не удалось разобрать)")
            return

        print("=" * 60)
        print("URL:", details.url)
        print("TITLE:", details.title)
        print("PRICE:", details.price)
        print("DATE:", repr(details.posted_at_text))
        print("REVIEWS:", repr(details.reviews_text))
        print("DESCRIPTION:", details.description[:1000])

        posted_at = parse_posting_date(details.posted_at_text)
        reviews = parse_reviews_count(details.reviews_text)
        print("-" * 60)
        print("posted_at:", posted_at)
        print("fresh (<24h):", is_fresh(posted_at))
        print("reviews:", reviews)

        report = match_text(f"{details.title}\n{details.description}")
        print("-" * 60)
        print("matches:", len(report.matches))
        for match in report.matches:
            print(
                f"  #{match.book.index} {match.book.label} "
                f"| {match.confidence} | needs_year={match.needs_year} "
                f"| evidence={match.evidence}"
            )
        if report.matches:
            low, high = report.estimate()
            print("estimate:", low, "-", high)
    finally:
        await parser.end_session()
        stop_xvfb(xvfb_proc)


if __name__ == "__main__":
    asyncio.run(main())
