import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from parsers import get_parser
from utils.display import ensure_xvfb, stop_xvfb


async def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_parsers.py <url>")
        sys.exit(1)

    load_dotenv()
    xvfb_proc = ensure_xvfb()

    url = sys.argv[1]
    parser = get_parser(url)

    if not parser:
        print(f"No parser found for {url}")
        sys.exit(1)

    print(f"Parser: {parser.__class__.__name__}")
    print(f"Marketplace: {parser.marketplace}")
    print(f"URL: {url}")

    try:
        price = await parser.get_price(url)

        if price is not None:
            print(f"Price: {price:.2f}")
        else:
            print("Price: not available")
    finally:
        await parser.end_session()
        stop_xvfb(xvfb_proc)


if __name__ == "__main__":
    asyncio.run(main())
