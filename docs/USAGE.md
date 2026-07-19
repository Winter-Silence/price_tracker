# Работа с проектом

> [Что за проект](PROJECT.md) · [Как установить](INSTALL.md) · **Как работать**

## Запуск

```bash
# bash / zsh:
source venv/bin/activate
# fish:
source venv/bin/activate.fish

python main.py
```

Бот и планировщик работают в одном event loop. При старте:

1. Инициализируется БД (создаются таблицы)
2. Запускается планировщик — раз в `POLL_INTERVAL_MINUTES` минут опрашивает цены всех активных товаров
3. Запускается Telegram-бот — реагирует на команды пользователей

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Приветствие и краткая инструкция |
| `/help` | Подробная помощь по командам |
| `/add` | Добавить товар для отслеживания (многошаговый диалог) |
| `/list` | Список отслеживаемых товаров с текущими ценами |
| `/delete` | Удалить товар (с подтверждением через inline-кнопки) |
| `/history` | История цен для товара |

### Добавление товара (`/add`)

1. `/add` — бот просит ссылку на товар
2. Отправьте URL товара (Wildberries, Ozon, Citilink)
3. Введите название товара (для удобства, отображается в `/list`)
4. Введите пороговую цену — бот уведомит, когда цена упадёт ниже этого значения. Введите `0`, чтобы просто отслеживать цену без уведомлений
5. Товар добавлен — бот будет проверять цену каждый час (или другой интервал из `POLL_INTERVAL_MINUTES`)

### Удаление товара (`/delete`)

1. `/delete` — бот показывает inline-кнопки со всеми товарами
2. Выберите товар — бот запросит подтверждение
3. Подтвердите — товар деактивируется (мягкое удаление, `is_active = 0`)

### История цен (`/history`)

1. `/history` — бот показывает inline-кнопки со всеми товарами
2. Выберите товар — бот покажет последние 10 записей цен с датами

## Уведомления

Когда цена товара опускается ниже пороговой, бот отправляет сообщение:

```
🔔 Цена упала!

📦 Пылесос Kärcher WD 3
🏪 ozon
💰 Текущая цена: 5490.00₽
🎯 Твоя цель: 6000.00₽

🔗 Перейти к товару
```

После срабатывания алерт деактивируется (повторных уведомлений не будет — нужно поставить новую цель через `/add`).

## Ручной тест парсера

Скрипт для проверки парсеров без запуска бота:

```bash
python scripts/test_parsers.py "https://www.wildberries.ru/catalog/12345678/detail.aspx"
python scripts/test_parsers.py "https://www.ozon.ru/product/nazvanie-833683323/"
```

Вывод при успехе:

```
Parser: OzonParser
Marketplace: ozon
URL: https://www.ozon.ru/product/...
Price: 5490.00
```

Если цена не получена (капча, ошибка, нет в наличии):

```
Price: not available
```

## Скриншоты при ошибках

При ошибках парсинга (капча, таймаут, не найден элемент цены) парсер автоматически делает full-page скриншот и сохраняет его в `screenshots/`:

```
screenshots/
├── ozon_captcha_20260626_161318.png
├── ozon_error_20260626_161318.png
└── wildberries_error_20260626_161307.png
```

Формат имени: `<marketplace>_<label>_<дата>_<время>.png`

Метки:

- `captcha` — обнаружена капча
- `error` — неперехваченное исключение
- `no_price_element` — не найден элемент цены на странице
- `no_connection` — страница «нет соединения» (Ozon)

## Добавление нового маркетплейса

1. Создайте файл в `parsers/` (например, `dns.py`)
2. Наследуйтесь от `BaseParser`:

```python
from parsers.base import BaseParser
from utils.logger import logger


class DnsParser(BaseParser):
    marketplace = "dns"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "dns-shop.ru" in url

    async def get_price(self, url: str) -> float | None:
        await self._random_delay()
        page = await self._get_page(url)
        try:
            price_text = await self._eval(page, '''
                (() => {
                    let el = document.querySelector("YOUR_SELECTOR");
                    return el ? el.innerText.trim() : null;
                })()
            ''')
            if not price_text:
                return None
            # логика парсинга цены из price_text
            price = ...
            return float(price)
        except Exception as exc:
            logger.error("DNS parser failed for %s: %s", url, exc)
            await self._take_screenshot(page, "error")
            return None
        finally:
            await self._close()
```

3. Зарегистрируйте в `parsers/__init__.py` — добавьте класс в список `PARSERS` и импорт
4. Протестируйте: `python scripts/test_parsers.py "<url>"`

> **Stealth работает автоматически** — `_inject_stealth()` вызывается в `BaseParser.start_session()`
> и инжектит антидетект-скрипт через CDP до загрузки страницы. Дополнительных действий
> от нового парсера не требуется.

Если маркетплейс отдаёт цену по API (как Wildberries), переопределите `start_session()` / `end_session()` для использования `aiohttp.ClientSession` вместо браузера — см. `parsers/wildberries.py` как пример.

## Лицензия

MIT
