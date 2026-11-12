# Price Tracker

Telegram-бот для отслеживания цен на товары на российских маркетплейсах.
Пользователь добавляет конкретный URL товара + пороговую цену. Бот периодически
парсит страницы и присылает уведомление, когда цена опускается ниже порога.

## Стек

- Python (версия зафиксирована в `.tool-versions` через asdf)
- `nodriver` — браузерная автоматизация на базе настоящего Chrome (обход антибота)
- `aiosqlite` — async-доступ к SQLite
- `apscheduler` (AsyncIOScheduler) — планировщик опросов цен
- `python-telegram-bot` v20+ — Telegram-бот (async API)
- `python-dotenv` — переменные окружения из `.env`
- `fabric` — деплой на production через `fab deploy` (см. `fabfile.py`)

## Структура проекта

```
price-tracker/
├── .tool-versions          # asdf: фиксированная версия Python
├── .env                    # секреты (не в git)
├── .env.example            # шаблон
├── requirements.txt
├── fabfile.py               # Fabric tasks: fab deploy / rollback / status / logs / restart / stop / start / setup / sync-profile / check
├── .deploy.env              # конфиг деплоя (не в git; см. .deploy.env.example)
├── .deploy.env.example
├── main.py                 # точка входа: бот + планировщик в одном event loop
├── db/
│   ├── database.py         # init_db() и все функции запросов
│   └── models.py           # dataclass-модели (Product, PriceRecord, Alert, User)
├── parsers/
│   ├── base.py             # абстрактный BaseParser
│   ├── __init__.py         # PARSERS = [...]; get_parser(url) -> BaseParser | None
│   ├── wildberries.py      # Wildberries
│   ├── ozon.py             # Ozon
│   ├── citilink.py         # Citilink
│   └── avito.py            # Avito (новые объявления + карточка лота)
├── books/
│   ├── normalize.py        # лемматизация pymorphy3 + нечёткое сравнение rapidfuzz
│   ├── catalog.py          # загрузка data/books_catalog.json
│   ├── matcher.py          # сопоставление текста лота с эталонным списком
│   └── filters.py          # дата публикации (МСК) и отзывы продавца
├── data/
│   └── books_catalog.json  # сгенерированный каталог 50 книг (в git)
├── tests/
│   └── test_book_matcher.py
├── scheduler/
│   └── jobs.py             # poll_prices() + poll_search_prices() + poll_avito_search() + start_scheduler()
├── bot/
│   ├── handlers.py         # ConversationHandler и команды /start /add /link /threshold /list /delete /history /cancel
│   ├── notifications.py    # send_alert_notification() + send_search_alert_notification() + send_avito_new_item_notification()
│   ├── bot_instance.py     # глобальный синглтон: set_bot() / get_bot()
│   └── admin_notify.py     # send_admin_notification() для служебных уведомлений
├── utils/
│   ├── logger.py           # единый логгер проекта
│   └── display.py          # ensure_xvfb() / stop_xvfb() — управление виртуальным дисплеем
├── scripts/
│   ├── deploy.sh               # первичная установка на сервер (ставит Chrome, asdf, venv, systemd-юниты)
│   ├── install-systemd.sh      # устаревший альтернативный путь установки systemd-юнитов
│   ├── price-tracker-xvfb.service    # systemd-юнит Xvfb
│   ├── price-tracker-fluxbox.service # systemd-юнит fluxbox (WM поверх Xvfb)
│   ├── price-tracker.service         # systemd-юнит бота
│   ├── test_parsers.py         # ручной запуск парсера по URL из CLI
│   ├── test_fingerprint.py     # ручной тест отпечатков браузера
│   ├── test_avito_search.py    # ручной тест поиска Avito
│   ├── test_avito_item.py      # ручной тест карточки объявления Avito
│   └── build_books_catalog.py  # генерация data/books_catalog.json из docs/books/
└── docs/
    ├── PROJECT.md          # архитектура, парсеры, stealth, схема БД
    ├── INSTALL.md          # инструкция по установке
    ├── USAGE.md            # команды бота, уведомления, добавление маркетплейсов
    └── books/              # эталонный список 50 книг и правила отбора
```

## Команды

```bash
# Автоматическая установка на сервер (Ubuntu)
chmod +x scripts/deploy.sh && ./scripts/deploy.sh

# Ручная установка
asdf install                                  # требуется asdf >= 0.16
# bash/zsh:
asdf exec python -m venv venv && source venv/bin/activate
# fish — см. docs/INSTALL.md
pip install -r requirements.txt

# Запуск
python main.py

# Ручной тест парсера
python scripts/test_parsers.py "https://www.wildberries.ru/catalog/12345678/detail.aspx"

# Деплой на production (один раз настроить .deploy.env из .deploy.env.example)
fab check          # проверка соединения с сервером
fab deploy         # push + SSH + pull + пересоздание venv + restart + логи
fab deploy --no-push   # если push уже сделан
fab deploy --branch <name>  # деплой конкретной ветки
fab status         # статус systemd-сервисов
fab logs           # последние 50 строк лога бота
fab logs --lines 200   # последние N строк
fab restart        # перезапуск бота без деплоя
fab stop           # остановка бота
fab start          # запуск бота
fab sync-profile   # загрузить локальный chrome_profile_avito/ на сервер (Avito, разово)
fab rollback       # экстренный откат на HEAD~1
fab rollback --steps 3  # откат на N коммитов назад
```

### First-time Avito setup

Avito требует доверенную сессию (обход капчи). На headless-сервере негде открыть
браузер и решить капчу, поэтому доверенная сессия готовится один раз на
dev-машине и загружается на сервер:

```bash
# 1. На dev-машине (с GUI) открыть https://www.avito.ru в Chrome-профиле бота
#    (`chrome_profile_avito/`) и один раз вручную решить капчу / подтвердить доступ.
#    Быстрее всего: `python scripts/test_avito_search.py <url>` и решить капчу,
#    когда бот откроет окно Chrome.

# 2. Загрузить доверенный профиль на сервер (остановит бота, бэкапит старый,
#    загрузит новый профиль, перезапустит бота):
fab sync-profile
```

Профиль `chrome_profile_avito/` (gitignored) хранит доверенную Avito-сессию
между деплоями — `fab deploy` его не трогает. Парсеры цен (WB/Ozon/Citilink)
используют отдельный `chrome_profile/`, а Avito — `chrome_profile_avito/`,
чтобы их 5-минутный и часовой циклы не конфликтовали по `SingletonLock`.
Файл замков (`SingletonLock`/`SingletonCookie`/`SingletonSocket`) может стать
stale, если профиль синхронизирован с dev-машины с другим hostname — в таком
случае `_check_and_reset_chrome_profile` удаляет только замки (куки/сессию
сохраняет). Повторный `fab sync-profile` не нужен, пока сессия жива;
при повторной капче — просто повторить оба шага.


> **Shell-совместимость:** инструкции активации venv даны для bash
> (`source venv/bin/activate`). Для fish используйте `activate.fish` —
> для этого venv создаётся через `virtualenv` (не `python -m venv`):
> `asdf exec python -m pip install --user virtualenv &&
>  asdf exec python -m virtualenv venv &&
>  source venv/bin/activate.fish`. См. `docs/INSTALL.md`.
>
> **Windows-checkout warning:** если репозиторий клонируется на Linux с
> `git config --global core.autocrlf=true`, asdf-плагин python ломается
> (`'bash\r': No such file or directory`). Перед `asdf install` убедись,
> что `git config --get core.autocrlf` возвращает `false` или `input`
> для этого клона.

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Приветствие и список команд |
| `/help` | Справка |
| `/add` | Добавить новый товар: название + ссылка + порог цены |
| `/link` | Привязать ссылку к существующему товару (для отслеживания на нескольких маркетплейсах) |
| `/threshold` | Изменить/отключить пороговую цену уведомления (0 = отключить) |
| `/privileges` | Настроить привилегии на маркетплейсе (скидка по карте, подписка) |
| `/books` | Добавить поиск Avito по книгам: новые лоты проверяются по эталонному списку 50 книг |
| `/list` | Список твоих товаров с текущими ценами |
| `/delete` | Удалить товар (с подтверждением) |
| `/history` | История цен товара (с фильтром по типу цены) |

## Переменные окружения

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather |
| `DB_PATH` | Путь к SQLite-файлу, например `./data/prices.db` |
| `POLL_INTERVAL_MINUTES` | Интервал опроса цен (default: 120) |
| `AVITO_POLL_INTERVAL_MINUTES` | Интервал опроса поисковых страниц Avito (default: 5) |
| `AVITO_PROFILE_DIR` | Путь к Chrome-профилю для Avito (default: `./chrome_profile_avito`) |
| `AVITO_BLOCK_RETRY_MINUTES` | Пауза до следующей проверки после заглушки «Доступ ограничен», в минутах (default: 5) |
| `AVITO_BLOCK_MAX_ATTEMPTS` | Сколько подряд блокировок терпеть перед уведомлением админу (default: 3) |
| `AVITO_BOOKS_MAX_CHECKS` | Сколько карточек новых лотов открывать за цикл книжного режима (default: 15) |
| `AVITO_BOOKS_DRY_RUN` | `1` — только логировать совпадения книг, без уведомлений |
| `PROXY_URL` | SOCKS5-прокси для Telegram-бота (опционально, формат: `socks5://login:pass@host:port`) |
| `ADMIN_TELEGRAM_ID` | Telegram ID администратора для служебных уведомлений (опционально) |

Загружать через `python-dotenv` только в `main.py`, дальше передавать явно.

## Правила кодирования

### Язык общения
- Общаться с пользователем **только на русском языке** (все ответы, комментарии к коду, сообщения в Telegram-боте)
- Размышлять (internal reasoning) можно на любом языке

### Async везде
- Все функции БД — `async def` через `aiosqlite`
- Все обработчики бота — `async def`
- Задачи планировщика — `async def`: `poll_prices()`, `poll_search_prices()`, `poll_avito_search()`
- Никакого `asyncio.run()` внутри модулей — только в `main.py`

### Обработка ошибок
- **Никогда** не давать ошибке парсера уронить цикл планировщика
- Каждый вызов парсера — в `try/except`, логировать и идти дальше
- Если страница содержит `captcha` — `return None` с WARNING-логом, не ретраить
- Если товар не в наличии — `return None`, это не ошибка

### Логирование
```python
from utils.logger import logger

logger.debug("Parsed price: %s from %s", price, url)
logger.info("Saved price %.2f for link_id=%d", price, link_id)
logger.warning("Captcha detected at %s", url)
logger.error("Parser failed for %s: %s", url, exc)
```
Никогда не использовать `print()` в модулях.

### База данных
- Все SQL-функции в `db/database.py`, модели в `db/models.py`
- Мягкие удаления: `is_active = 0`, никогда `DELETE`
- Всегда указывать имена столбцов в `INSERT`
- Передавать соединение явным параметром или через контекстный менеджер

### Парсеры
- Один маркетплейс = один файл в `parsers/`
- Наследовать от `BaseParser`
- Обязательно реализовать:
  - `can_handle(url: str) -> bool` (classmethod, проверка по домену)
  - `get_price(url: str) -> float | None` (async)
  - `get_price_tiers(url: str) -> dict[str, float] | None` (async) — возвращает все варианты цен (standard, card, premium, wb_club). По умолчанию обёртка над `get_price()`.
  - `get_cheapest_from_search(search_url: str, title_filter: str) -> SearchResult | None` (async) — ищет самый дешёвый товар на поисковой странице, фильтрует по `title_filter`. Возвращает `SearchResult(price, product_url, product_title, tiers)`.
- Доступные типы цен: `standard`, `card`, `premium`, `wb_club`. Словарь `TIER_LABELS` в `parsers/base.py`.
- Зарегистрировать в `parsers/__init__.py` → список `PARSERS`
- Добавить типы привилегий маркетплейса в словрь `MARKETPLACE_TIERS` в `parsers/__init__.py`
- Браузерная автоматизация через `nodriver` (настоящий Chrome, non-headless + Xvfb + fluxbox)
- **Stealth-маскировка обязательна** — nodriver инжектит `STEALTH_JS` через CDP
  `Page.addScriptToEvaluateOnNewDocument` в `_inject_stealth()` (вызывается в `start_session()`).
  Скрипт маскирует: `navigator.webdriver`, `chrome.runtime`, `cdc_*` переменные,
  индикаторы автоматизации (13 свойств), `navigator.plugins/languages/hardwareConcurrency/deviceMemory`.
  Chrome-флаги: `--disable-blink-features=AutomationControlled`, nodriver-флаги перезаписываются
  через `uc.Config._default_browser_args` (только `--no-first-run`).
  При добавлении нового парсера stealth работает автоматически — наследуется от `BaseParser`.
- Ozon: переход с главной страницы на товар (прямой URL блокируется антиботом)
- Avito (`parsers/avito.py`): отслеживает не цену товара, а **появление новых объявлений**
  на поисковой странице. `get_all_items_from_search()` возвращает список всех текущих
  объявлений (url/title/price). Первый опрос по ссылке только запоминает текущую выдачу,
  уведомлений нет. Дальше новые объявления определяются по `notified_at = NULL` в
  `avito_search_items` и присылаются пользователю с ценой и ссылкой. Уведомление
  отправляется, только если оно реально доставлено: объявления помечаются `notified_at`
  ПОСЛЕ успешной отправки, поэтому при сбое Telegram уведомление повторится в следующем
  цикле. Объявление, пропавшее из выдачи на `AVITO_MISS_THRESHOLD` (=2) успешных опроса
  подряд, помечается `is_active = 0`; когда оно возвращается, `notified_at` сбрасывается,
  но повторное уведомление приходит только при снижении цены ниже прежней (иначе
  объявление помечается уведомлённым молча). Если за один опрос приходит вдвое меньше объявлений, чем
  отслеживается, пропажа не фиксируется (защита от частично отрендеренной страницы).
  Схема `avito_search_items`: `item_url`, `item_title`, `price`, `first_seen_at`,
  `last_seen_at`, `notified_at`, `miss_streak`, `is_active`.
   Заглушка «Доступ ограничен» означает, что маркетплейс заблокировал сам профиль
   (cookies), а не IP: тогда `end_session()` пересоздаёт профиль с нуля — старый
   каталог уходит в `chrome_profile_avito.blocked.<timestamp>` (хранится только
   последний), авто-сброс не чаще `PROFILE_RESET_MIN_INTERVAL` (30 минут). Ссылки
   Avito обрабатывает только `poll_avito_search()`: `poll_prices()` и
   `poll_search_prices()` их пропускают, иначе две джобы поднимают `AvitoParser`
   на одном профиле и падают на `SingletonLock`.
   Avito требует доверенную сессию: caches решаются вручную один раз в
   Chrome-профиле бота (`chrome_profile_avito/`), дальше профиль должен
   сохраняться (см. `_check_and_reset_chrome_profile`). Парсеры цен
   (WB/Ozon/Citilink) используют отдельный `chrome_profile/` — между
   ними нет конфликта замков.
 - Chrome-релиабильность: обязательно включать `--no-sandbox` в
   `CHROME_ARGS` (`no_sandbox=True` у nodriver на части систем не
   добавляет флаг). `_check_and_reset_chrome_profile` удаляет только
   стале-к SingletonLock/Cookie/Socket (куки/сессию сохраняет), а
   именно весь профиль — только если живой Chrome сам не умирает
   после SIGTERM. Особый случай: если профиль синхронизирован с
   dev-машины (`chrome_profile_avito/` с другого хоста), `SingletonLock`
   будет ссылаться на чужой hostname — Chrome блокирует запуск;
   `_check_and_reset_chrome_profile` распознаёт это и починяет без
   потери данных. Повторный `fab sync-profile` не нужен, пока сессия
   жива; при повторной капче — просто повторить оба шага.
### Книжный режим Avito (`/books`)
- `search_links.track_mode = 'books'`; обычный `/add` создаёт `'price'`, его поведение не меняется.
- Новые лоты проверяются в `_poll_books_link`: **заголовочный фильтр** → дата публикации (МСК) → отзывы продавца → `books.matcher.match_text(title + description)`.
- Заголовочный фильтр `books/prefilter.py::lot_title_interesting(title)` решает, открывать ли карточку вообще (массовый скрапинг карточек ведёт к бану Avito). Лот интересен, если:
  - в заголовке есть книга из списка (полный `match_text` по заголовку, специфичное название, автор из списка);
  - есть серийный/издательский маркер: «Литературные памятники», «Academia»/«Академия» рядом с книжным словом;
  - есть антикварный/коллекционный маркер: «антикварные», «старинные», «букинистические», «дореволюционные», «винтажные», «редкие книги», «из домашней/личной библиотеки», «коллекция/подборка/много книг», «одним лотом», «всё вместе»;
  - год целевой эпохи (1922–1937 или 1948–1965) стоит рядом с книжным словом.
- Если признаков нет («Марафон похудения», «Жириновский. Собрание сочинений», «Учебник по физике»), карточка **не открывается**: лот сохраняется в `avito_lot_checks` со статусом `skipped` и `skip_reason='title_gate'` и помечается уведомлённым.
- Год в заголовке, противоречащий книге (позднее переиздание, например «Слово о полку Игореве. 1985»), снимает её название с проверки — такой лот тоже не открывается.
- Первый опрос книжной ссылки только запоминает выдачу, карточки не открываются.
- Глобальный дедуп по `avito_lot_checks.item_url`; ошибка разбора карточки — до 3 попыток.
- Продавец с 50+ отзывами пропускается; объявление старше 24 ч — пропускается; неизвестная дата — уходит с пометкой «Дата не определена». Счётчик отзывов берётся из `[data-marker="rating-caption/rating"]` (запасные пути: ссылка `a[href="#open-reviews-list"]` и текст блока `item-view/seller-info`) — старый `seller-info/reviews` Avito больше не отдаёт.
- Совпадение «только по названию» или «название + автор» (без года, издательства/серии или приметы издания) не уведомляется: у современного переиздания тот же заголовок и тот же автор. Автор подтверждает произведение, но не издание. Такие лоты сохраняются как `skipped`/`no_evidence`. Исключение — неразрешённый дубль издания (`needs_year`), он уходит с пометкой «уточнить год».
- Соседние строки одной позиции склеиваются перед матчингом (`_merge_year_segments` + `_merge_key_segments`): строка-год, строка только с автором или только с названием из каталога. Примеры: «Робинзон Крузо» + «Дефо. 1929» → сильное совпадение; «Записки о Галльской войне» + «Цезарь. 1948» → сильное. Строка с годом склеивается с соседом только если содержит автора/название той же книги — иначе год из чужой позиции не перетекает.
- Если в лоте нашлась одна книга (или один дубль названия), склейка не нужна: всё описание относится к ней, поэтому совпадение пересчитывается по всему тексту. Тогда год из дальнего абзаца подтверждает целевое издание или отсекает лот как переиздание («Робинзон Крузо» + «Москва: Эксмо, 2023» → лот отброшен). В лоте из нескольких книг год остаётся привязан к своей позиции.
- Каталог: `docs/books/02_price_table.md` → `scripts/build_books_catalog.py` → `data/books_catalog.json` (файл в git).
- Эталон и правила отбора: `docs/books/01_reference_list.md`, `docs/books/03_lots_and_checklist.md`.

### Telegram-бот
- Использовать `ConversationHandler` для многошаговых диалогов (`/add`)
- Пользователю — дружелюбные сообщения без технических деталей
- Все сообщения с эмодзи для наглядности (🔔 📦 💰 🏪 🔗)
- Inline-кнопки для деструктивных действий (удаление с подтверждением)
- Синглтон `bot_instance.py`: `set_bot()` / `get_bot()` — доступ к Bot из планировщика/уведомлений
- `admin_notify.py`: `send_admin_notification()` — служебные уведомления администратору (если задан `ADMIN_TELEGRAM_ID`)
