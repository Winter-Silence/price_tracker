# Price Tracker

Telegram-бот для отслеживания цен на товары на российских маркетплейсах.
Пользователь добавляет URL товара + пороговую цену. Бот периодически
парсит страницы и присылает уведомление, когда цена опускается ниже порога.

## Документация

- [Что за проект](docs/PROJECT.md) — стек, структура, поддерживаемые маркетплейсы, архитектура
- [Как установить](docs/INSTALL.md) — требования, установка, настройка `.env`, переменные окружения
- [Как работать](docs/USAGE.md) — запуск, команды бота, уведомления, тест парсеров, добавление маркетплейса

## Быстрый старт

```bash
asdf install
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # заполните TELEGRAM_BOT_TOKEN
python main.py
```

## Поддерживаемые маркетплейсы

Wildberries · Ozon · Citilink

## Лицензия

MIT
