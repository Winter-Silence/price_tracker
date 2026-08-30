import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from parsers.avito import AvitoParser
from utils.display import ensure_xvfb, stop_xvfb


URLS = [
    "https://www.avito.ru/astrahan/tovary_dlya_kompyutera/komplektuyuschie/protsessory/amd-ASgBAgICA0TGB~pm7gniZ~LNE4q85hE?d=1&q=amd+5700x&s=104",
    "https://www.avito.ru/astrahan/tovary_dlya_kompyutera/komplektuyuschie/protsessory/amd-ASgBAgICA0TGB~pm7gniZ~LNE4q85hE?context=H4sIAAAAAAAA_wEmANn_YToxOntzOjE6InkiO3M6MTY6Iklvb2tKRnphTHZkOGxZcmYiO30JWQWTJgAAAA&d=1&f=ASgBAgECA0TGB~pm7gniZ~LNE4q85hEBRcaaDBl7ImZyb20iOjEwMDAwLCJ0byI6MTEwMDB9&q=amd+5700x&s=104",
]


async def main():
    load_dotenv()
    xvfb_proc = ensure_xvfb()
    parser = AvitoParser()
    try:
        for url in URLS:
            print("=" * 60)
            print("URL:", url)
            items = await parser.get_all_items_from_search(url)
            if items is None:
                print("RESULT: None (blocked/captcha)")
            else:
                print(f"RESULT: {len(items)} items")
                for i, it in enumerate(items[:5], start=1):
                    print(f"  {i}. {it.title} | {it.price}₽ | {it.url}")
    finally:
        await parser.end_session()
        stop_xvfb(xvfb_proc)


if __name__ == "__main__":
    asyncio.run(main())
