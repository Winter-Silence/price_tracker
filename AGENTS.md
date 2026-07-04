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
│   └── models.py           # dataclass-модели (Product, PriceRecord, Alert, User)
├── parsers/
│   ├── base.py             # абстрактный BaseParser
│   ├── __init__.py         # PARSERS = [...]; get_parser(url) -> BaseParser | None
│   └── wildberries.py      # один файл = один маркетплейс
├── scheduler/
│   └── jobs.py             # poll_prices() + start_scheduler()
├── bot/
│   ├── handlers.py         # ConversationHandler и команды /start /add /link /threshold /list /delete /history
│   └── notifications.py    # send_alert_notification()
├── utils/
│   └── logger.py           # единый логгер проекта
└── scripts/
    └── test_parsers.py     # ручной запуск парсера по URL из CLI
```

## Команды

```bash
# Автоматическая установка на сервер (Ubuntu)
chmod +x scripts/deploy.sh && ./scripts/deploy.sh

# Ручная установка
asdf install                                  # требуется asdf >= 0.16
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Запуск
python main.py

# Ручной тест парсера
python scripts/test_parsers.py "https://www.wildberries.ru/catalog/12345678/detail.aspx"
```

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Приветствие и список команд |
| `/help` | Справка |
| `/add` | Добавить новый товар: название + ссылка + порог цены |
| `/link` | Привязать ссылку к существующему товару (для отслеживания на нескольких маркетплейсах) |
| `/threshold` | Изменить/отключить пороговую цену уведомления (0 = отключить) |
| `/list` | Список твоих товаров с текущими ценами |
| `/delete` | Удалить товар (с подтверждением) |
| `/history` | История цен товара |

## Переменные окружения

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather |
| `DB_PATH` | Путь к SQLite-файлу, например `./data/prices.db` |
| `POLL_INTERVAL_MINUTES` | Интервал опроса цен (default: 60) |

Загружать через `python-dotenv` только в `main.py`, дальше передавать явно.

## Правила кодирования

### Async везде
- Все функции БД — `async def` через `aiosqlite`
- Все обработчики бота — `async def`
- Задача планировщика `poll_prices()` — `async def`
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
- Зарегистрировать в `parsers/__init__.py` → список `PARSERS`
- Браузерная автоматизация через `nodriver` (настоящий Chrome)
- Ozon: переход с главной страницы на товар (прямой URL блокируется антиботом)

### Telegram-бот
- Использовать `ConversationHandler` для многошаговых диалогов (`/add`)
- Пользователю — дружелюбные сообщения без технических деталей
- Все сообщения с эмодзи для наглядности (🔔 📦 💰 🏪 🔗)
- Inline-кнопки для деструктивных действий (удаление с подтверждением)
