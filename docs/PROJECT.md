# О проекте

> [Что за проект](PROJECT.md) · [Как установить](INSTALL.md) · [Как работать](USAGE.md)

Telegram-бот для отслеживания цен на товары на российских маркетплейсах.
Пользователь добавляет конкретный URL товара + пороговую цену. Бот периодически
парсит страницы и присылает уведомление, когда цена опускается ниже порога.

## Стек

- **Python** 3.11.9 (версия зафиксирована в `.tool-versions` через asdf)
- **nodriver** — браузерная автоматизация на базе настоящего Chrome (обход антибота)
- **aiosqlite** 0.20.0 — async-доступ к SQLite
- **apscheduler** 3.10.4 (AsyncIOScheduler) — планировщик опросов цен
- **python-telegram-bot** 20.7 — Telegram-бот (async API)
- **python-dotenv** 1.0.1 — переменные окружения из `.env`
- **fabric** — деплой на production через `fab deploy`

## Структура проекта

```
price-tracker/
├── .tool-versions          # asdf: фиксированная версия Python
├── .env                    # секреты (не в git)
├── .env.example            # шаблон
├── requirements.txt
├── fabfile.py               # Fabric tasks: fab deploy / rollback / status / logs / restart / stop / start / setup / check
├── .deploy.env              # конфиг деплоя (не в git; см. .deploy.env.example)
├── .deploy.env.example
├── main.py                 # точка входа: бот + планировщик в одном event loop
├── db/
│   ├── database.py         # init_db() и все функции запросов
│   └── models.py           # dataclass-модели (User, Product, MarketplaceLink, PriceRecord, Alert)
├── parsers/
│   ├── base.py             # абстрактный BaseParser, stealth-инжект, _take_screenshot()
│   ├── __init__.py         # PARSERS = [...]; get_parser(url) -> BaseParser | None
│   ├── wildberries.py      # Wildberries (API через aiohttp)
│   ├── ozon.py             # Ozon (nodriver, SPA)
│   └── citilink.py         # Citilink (nodriver, SPA)
├── scheduler/
│   └── jobs.py             # poll_prices() + start_scheduler()
├── bot/
│   ├── handlers.py         # ConversationHandler и команды /start /add /list /delete /history
│   └── notifications.py    # send_alert_notification()
├── utils/
│   ├── logger.py           # единый логгер проекта
│   └── display.py          # Xvfb + fluxbox (виртуальный дисплей для nodriver)
├── scripts/
│   ├── test_parsers.py     # ручной запуск парсера по URL из CLI
│   ├── test_fingerprint.py # диагностика отпечатков браузера (Playwright)
│   └── deploy.sh           # первичная установка на сервер
├── screenshots/            # автоскриншоты при ошибках парсеров
└── data/                   # SQLite-база
```

## Поддерживаемые маркетплейсы

| Маркетплейс | Домены | Метод | Примечания |
|---|---|---|---|
| Wildberries | wildberries.ru, wildberries.by | API (aiohttp) | Цена через `card.wb.ru/cards/detail` |
| Ozon | ozon.ru | nodriver (Chrome) | SPA, stealth-маскировка, `[data-widget="webPrice"]` |
| Citilink | citilink.ru | nodriver (Chrome) | SPA, `[data-meta="price"]` или `.product-price__value` |

## Архитектура

### Парсеры

- Все парсеры наследуются от `BaseParser`
- `can_handle(url)` — classmethod, определяет подходящий парсер по домену
- `get_price(url)` — async, возвращает `float` или `None`
- `None` = ошибка/капча/нет в наличии (не крашит планировщик)
- Wildberries использует `aiohttp` для API-запросов (без браузера), Ozon и Citilink — nodriver
- Перед запросом — случайная задержка (Ozon: 5–12 сек) для имитации поведения человека
- При ошибках — автоматический скриншот страницы (`screenshots/`)

### Браузерная автоматизация и антибот

Проект использует `nodriver` (обёртка над CDP) с **настоящим Chrome** в non-headless
режиме внутри Xvfb. Это ключевое отличие от headless-режима, который легко детектируется.

**Инфраструктурный слой:**

- Xvfb — виртуальный дисплей 1920x1080x24
- fluxbox — оконный менеджер (нужен для `document.hasFocus()`, без него Ozon показывает капчу)
- Настоящий `google-chrome-stable` (не Chromium)
- `headless=False` — Chrome работает в полном графическом режиме

**Stealth-механизмы** (`parsers/base.py`, константа `STEALTH_JS`):

Скрипт инжектится через CDP `Page.addScriptToEvaluateOnNewDocument` до выполнения
любых скриптов страницы. Маскирует следующие сигнатуры:

| Сигнатура | Описание |
|---|---|
| `navigator.webdriver` | Удаляется с прототипа — Ozon проверяет эту переменную |
| `chrome.runtime` | Создаётся фейковый объект как в настоящем Chrome |
| `__webdriver_evaluate`, `__selenium_unwrapped` и др. (13 свойств) | Индикаторы автоматизации ChromeDriver/Selenium |
| `cdc_*` переменные на `window` | Переменные, которые ChromeDriver инжектит при запуске |
| `navigator.plugins` | 3 плагина как в настоящем Chrome (headless имеет 0) |
| `navigator.languages` | `['ru-RU', 'ru', 'en-US', 'en']` |
| `navigator.hardwareConcurrency` / `deviceMemory` | 8 / 8 — типичные значения десктопа |
| `Permissions.prototype.query` | Корректный ответ для `notifications` |

**Chrome-флаги:**

- nodriver по умолчанию добавляет подозрительные флаги (`--remote-allow-origins=*`,
  `--disable-infobars` и др.) — они перезаписываются через `uc.Config._default_browser_args`
- Добавлен `--disable-blink-features=AutomationControlled` — отключает внутренний
  механизм Chrome, помечающий браузер как управляемый автоматизацией
- Флаг `--window-size=1920,1080` — совпадает с разрешением Xvfb

**Запуск сессии** (`BaseParser.start_session()`):

1. `uc.start()` — запуск Chrome с минимальными флагами
2. `_inject_stealth()` — CDP `Page.enable()` + `add_script_to_evaluate_on_new_document(STEALTH_JS)`
3. Если задан `_root_url` — переход на главную страницу маркетплейса (прогрев кук/сессии)
4. Случайная задержка 2–4 сек на главной странице

**Дополнительные средства:**

- `scripts/test_fingerprint.py` — диагностика отпечатков браузера (сравнение vanilla Chromium,
  Chromium+stealth, настоящий Chrome). Используется при разработке, не в продакшене.

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

### Книжный режим Avito

Отдельный сценарий для поиска 50 редких книг (серии «Литературные памятники»
1948–1965 и Academia 1922–1937). Пользователь добавляет свои поисковые ссылки
Avito через `/books`; обычный `/add` для Avito работает как раньше.

- `search_links.track_mode = 'books'` отличает книжные ссылки от ценовых.
- Первый опрос ссылки запоминает текущую выдачу и не открывает карточки.
- Заголовочный фильтр (`books/prefilter.py::lot_title_interesting`) отсеивает
  лоты до открытия карточки: массовый скрапинг карточек ведёт к бану Avito.
  Лот интересен, если в заголовке есть книга/автор из каталога, серия
  «Литературные памятники», издательство Academia/«Академия» рядом с книжным
  словом, антикварный или коллекционный маркер («антикварные», «из домашней
  библиотеки», «подборка книг», «одним лотом» и т. п.) либо год целевой эпохи
  (1922–1937/1948–1965) рядом с книжным словом. Иначе лот сохраняется как
  `skipped`/`title_gate` и не открывается.
- Новые прошедшие фильтр лоты (`avito_search_items.notified_at IS NULL`)
  открываются по одному с задержкой 6–12 с, не более `AVITO_BOOKS_MAX_CHECKS`
  за цикл.
- `parsers/avito.py::get_item_details()` достаёт описание, дату публикации и
  отзывы продавца из DOM, при необходимости — из JSON-LD.
- `books/filters.py` отбрасывает объявления старше 24 ч и продавцов с 50+
  отзывами; неизвестная дата не отбрасывается, а помечается в уведомлении.
  Счётчик отзывов читается из `[data-marker="rating-caption/rating"]`
  (Avito больше не отдаёт старый `seller-info/reviews`).
- Уведомление уходит только при подтверждении совпадения (год, издательство/серия,
  примета издания): `MatchReport.confirmed()`. Автор подтверждает только
  произведение, но не издание, поэтому «название + автор» без года и
  издательства сохраняется как `skipped`/`no_evidence`; неразрешённый дубль
  изданий (`needs_year`) уходит с пометкой «уточнить год».
- Сегменты описания (строки) склеиваются в одну позицию: строка-год
  («1950 г.», «1950»), а также строка только с автором или только с названием
  из каталога («Робинзон Крузо» + «Дефо. 1929», «Записки о Галльской войне» +
  «Цезарь. 1948»). Строка с годом принимается соседом только если в ней есть
  автор/название той же книги — чтобы год не переносился на чужую книгу в
  многострочном лоте.
- Лот об одной книге (или об одном дубле названия) склейки не требует — всё
  описание относится к ней. Поэтому совпадение пересчитывается по всему тексту:
  год из дальнего абзаца подтверждает целевое издание или отсекает лот как
  переиздание («Робинзон Крузо» + «Москва: Эксмо, 2023» → совпадений нет).
  В лоте из нескольких книг год остаётся привязан к своей позиции.
- `books/matcher.py` сопоставляет название и описание с каталогом: леммы
  pymorphy3, нечёткие токены rapidfuzz, обязательный автор для общих названий,
  проверка года/издательства/серии, негативные термины и дубли изданий.
- `avito_lot_checks` хранит итог проверки по `item_url` глобально (дедуп между
  поисковыми ссылками) и до 3 попыток разбора карточки.
- Оценка стоимости — диапазон из `docs/books/02_price_table.md`, справочная.

### Логирование

```python
from utils.logger import logger

logger.debug("Parsed price: %s from %s", price, url)
logger.info("Saved price %.2f for link_id=%d", price, link_id)
logger.warning("Captcha detected at %s", url)
logger.error("Parser failed for %s: %s", url, exc)
```

Никогда не использовать `print()` в модулях.
