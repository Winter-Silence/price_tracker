import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from parsers import get_parser
from utils.display import ensure_xvfb, stop_xvfb


def _parse_args(argv):
    """Accept either positional URL, or --search "title" + URL for search-cheapest mode."""
    args = [a for a in argv[1:] if a not in ("-h", "--help")]
    title_filter = None
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--search" and i + 1 < len(args):
            title_filter = args[i + 1]
            i += 2
            continue
        filtered.append(args[i])
        i += 1
    return title_filter, filtered


async def main():
    title_filter, pos_args = _parse_args(sys.argv)
    if not pos_args:
        print("Usage:")
        print("  python scripts/test_parsers.py <url>")
        print("  python scripts/test_parsers.py --search \"title filter\" <search_url>")
        sys.exit(1)

    load_dotenv()
    xvfb_proc = ensure_xvfb()

    url = pos_args[0]
    parser = get_parser(url)

    if not parser:
        print(f"No parser found for {url}")
        sys.exit(1)

    print(f"Parser: {parser.__class__.__name__}")
    print(f"Marketplace: {parser.marketplace}")
    print(f"URL: {url}")
    if title_filter is not None:
        print(f"Mode: SEARCH (title_filter={title_filter!r})")
    else:
        print("Mode: PRODUCT (get_price_tiers)")

    try:
        if title_filter is not None:
            result = await parser.get_cheapest_from_search(url, title_filter)
            if result:
                print()
                print(f"✅ Cheapest match:")
                print(f"  Title: {result.product_title}")
                print(f"  Price: {result.price:.2f}₽")
                print(f"  URL:   {result.product_url}")
                if result.tiers:
                    print(f"  Tiers: {result.tiers}")
            else:
                print()
                print("❌ No matching card found (or parser returned None)")
        else:
            tiers = await parser.get_price_tiers(url)
            print()
            if tiers:
                print(f"Prices: {tiers}")
                for t, p in tiers.items():
                    print(f"  {t}: {p:.2f}₽")
            else:
                print("Price: not available")
    finally:
        await parser.end_session()
        stop_xvfb(xvfb_proc)


if __name__ == "__main__":
    asyncio.run(main())
