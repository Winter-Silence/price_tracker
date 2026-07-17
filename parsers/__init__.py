from parsers.base import BaseParser
from parsers.wildberries import WildberriesParser
from parsers.ozon import OzonParser
from parsers.citilink import CitilinkParser

PARSERS: list[type[BaseParser]] = [
    WildberriesParser,
    OzonParser,
    CitilinkParser,
]

MARKETPLACE_TIERS: dict[str, list[str]] = {
    "wildberries": ["card", "wb_club"],
    "ozon": ["card", "premium"],
    "citilink": ["card"],
}


def get_parser(url: str) -> BaseParser | None:
    for parser_cls in PARSERS:
        if parser_cls.can_handle(url):
            return parser_cls()
    return None