from dataclasses import dataclass
from datetime import datetime


@dataclass
class User:
    id: int
    telegram_id: int
    created_at: datetime


@dataclass
class Product:
    id: int
    name: str
    created_by: int
    created_at: datetime


@dataclass
class MarketplaceLink:
    id: int
    product_id: int
    marketplace: str
    url: str
    last_price: float | None
    last_checked_at: datetime | None
    is_active: bool


@dataclass
class PriceRecord:
    id: int
    link_id: int
    price: float
    privilege_type: str
    recorded_at: datetime


@dataclass
class Alert:
    id: int
    user_id: int
    link_id: int
    threshold_price: float
    is_active: bool
    triggered_at: datetime | None
    created_at: datetime


@dataclass
class UserPrivilege:
    id: int
    user_id: int
    marketplace: str
    privilege_type: str


@dataclass
class SearchLink:
    id: int
    product_id: int
    marketplace: str
    search_url: str
    title_filter: str
    last_price: float | None
    last_resolved_url: str | None
    last_resolved_title: str | None
    last_checked_at: datetime | None
    is_active: bool


@dataclass
class SearchPriceRecord:
    id: int
    search_link_id: int
    price: float
    resolved_url: str | None
    resolved_title: str | None
    recorded_at: datetime
