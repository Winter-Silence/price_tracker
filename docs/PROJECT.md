# О проекте

> [Что за проект](PROJECT.md) · [Как установить](INSTALL.md) · [Как работать](USAGE.md)

Telegram-бот для отслеживания цен на товары на российских маркетплейсах.
Пользователь добавляет конкретный URL товара + пороговую цену. Бот периодически
парсит страницы и присылает уведомление, когда цена опускается ниже порога.

## Стек

- **Python** 3.11.9 (версия зафиксирована в `.tool-versions` через asdf)
- **playwright** 1.44.0 — браузерная автоматизация для парсинга SPA-страниц
- **aiohttp** — HTTP-клиент для API-запросов (Wildberries)
- **aiosqlite** 0.20.0 — async-доступ к SQLite
- **apscheduler** 3.10.4 (AsyncIOScheduler) — планировщик опросов цен
- **python-telegram-bot** 20.7 — Telegram-бот (async API)
- **python-dotenv** 1.0.1 — переменные окружения из `.env`
- **curl_cffi** 0.15.0 — TLS-имитация для обхода антибота

## Структура проекта

```
price-tracker/
├── .tool-versions          # asdf: фиксированная версия Python
├── .env                    # секреты (не в git)
├── .env.example            # шаблон
├── requirements.txt
├── main.py                 # точка входа: бот + планировщик в одном event loop
├── db/
│   ├── database.py         # init_db() и все функции запросов
│   └── models.py           # dataclass-модели (User, Product, MarketplaceLink, PriceRecord, Alert)
├── parsers/
│   ├── base.py             # абстрактный BaseParser + _take_screenshot()
│   ├── __init__.py         # PARSERS = [...]; get_parser(url) -> BaseParser | None
│   ├── wildberries.py      # Wildberries (API через aiohttp, без Playwright)
│   ├── ozon.py             # Ozon (Playwright, SPA)
│   └── citilink.py         # Citilink (Playwright, SPA)
├── scheduler/
│   └── jobs.py             # poll_prices() + start_scheduler()
├── bot/
│   ├── handlers.py         # ConversationHandler и команды /start /add /list /delete /history
│   └── notifications.py    # send_alert_notification()
├── utils/
│   └── logger.py           # единый логгер проекта
├── scripts/
│   └── test_parsers.py     # ручной запуск парсера по URL из CLI
├── screenshots/            # автоскриншоты при ошибках парсеров
└── data/                   # SQLite-база
```

## Поддерживаемые маркетплейсы

| Маркетплейс | Домены | Метод | Примечания |
|---|---|---|---|
| Wildberries | wildberries.ru, wildberries.by | API (aiohttp) | Цена через `card.wb.ru/cards/detail` |
| Ozon | ozon.ru | Playwright | SPA, селектор `[data-widget="webPrice"]` |
| Citilink | citilink.ru | Playwright | SPA, селектор `[data-meta="price"]` или `.product-price__value` |

## Архитектура

### Парсеры

- Все парсеры наследуются от `BaseParser`
- `can_handle(url)` — classmethod, определяет подходящий парсер по домену
- `get_price(url)` — async, возвращает `float` или `None`
- `None` = ошибка/капча/нет в наличии (не крашит планировщик)
- Wildberries использует `aiohttp` для API-запросов (без браузера), Ozon и Citilink — Playwright
- Перед Playwright-запросом — случайная задержка 1–3 сек (Ozon: 5–12 сек) для антибота
- При ошибках — автоматический скриншот страницы (`screenshots/`)

### База данных

- SQLite через `aiosqlite`, все функции — `async def`
- Мягкие удаления: `is_active = 0`, никогда `DELETE`
- Схема: `users` → `products` → `marketplace_links` → `price_history` + `alerts`
- Соединение через контекстный менеджер `db_connection()`

### Планировщик

- `AsyncIOScheduler` опрашивает все активные ссылки раз в `POLL_INTERVAL_MINUTES`
- Ссылки группируются по домену — одна браузерная сессия на домен
- Ошибка одного парсера не прерывает цикл — логируется и идёт дальше
- При обнаружении капчи на домене — остальные ссылки этого домена пропускаются
- Межзапросные задержки: по умолчанию 2–5 сек, для ozon.ru — 15–30 сек
- При снижении цены ниже порога — отправляется уведомление пользователю

### Логирование

```python
from utils.logger import logger

logger.debug("Parsed price: %s from %s", price, url)
logger.info("Saved price %.2f for link_id=%d", price, link_id)
logger.warning("Captcha detected at %s", url)
logger.error("Parser failed for %s: %s", url, exc)
```

Никогда не использовать `print()` в модулях.
