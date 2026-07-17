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
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env   # заполните TELEGRAM_BOT_TOKEN
python main.py
```

**Создание venv** зависит от shell (подробности в [docs/INSTALL.md](docs/INSTALL.md)):

```bash
# bash / zsh:
asdf exec python -m venv venv
source venv/bin/activate

# fish (нужен virtualenv — он генерирует activate.fish):
asdf exec python -m pip install --user virtualenv
asdf exec python -m virtualenv venv
source venv/bin/activate.fish
```

> Python 3.11.9 управляется через asdf и фиксируется в `.tool-versions`.
> Браузерная автоматизация — на `nodriver` + настоящем Google Chrome
> (на production ставится `scripts/deploy.sh`).

## Деплой на сервер

Деплой выполняется через [Fabric](https://www.fabfile.org/) (Python-аналог capistrano):
одна команда `fab deploy` делает push → SSH → pull → пересоздание venv → restart → логи.

### Первичная установка сервера

На сервере должен быть клонирован репозиторий, SSH-доступ по ключу, и
пользователь с passwordless sudo для `systemctl`.

```bash
# На сервере:
git clone <repo-url> ~/price_tracker
cd ~/price_tracker
bash scripts/deploy.sh     # ставит Chrome, asdf, venv, systemd-юниты, стартует
```

### Настройка деплоя с локальной машины

```bash
# 1. Fabric ставится вместе с остальными зависимостями:
pip install -r requirements.txt

# 2. Конфиг деплоя (один раз):
cp .deploy.env.example .deploy.env
$EDITOR .deploy.env        # заполнить DEPLOY_HOST, DEPLOY_USER, DEPLOY_PATH

# 3. Проверка связи:
fab check
```

### Деплой

```bash
git add ... && git commit && git push
fab deploy                 # push + SSH + pull + пересоздание venv + restart + логи

# Если push уже сделан вручную:
fab deploy --no-push

# Деплой другой ветки:
fab deploy --branch dev
```

### Управление сервисом

| Команда | Описание |
|---|---|
| `fab status` | Статус всех трёх systemd-сервисов (xvfb, fluxbox, bot) |
| `fab logs` | Последние 50 строк лога бота |
| `fab logs --lines 200` | Последние N строк лога |
| `fab restart` | Перезапустить бота (без деплоя) |
| `fab stop` / `fab start` | Остановить / запустить |
| `fab rollback` | Откатить сервер на `HEAD~1` и перезапустить (экстренно) |
| `fab rollback --steps 3` | Откат на N коммитов |
| `fab setup` | Повторно запустить `scripts/deploy.sh` на сервере |

Миграции БД применяются автоматически при старте `main.py` (`init_db()`),
отдельная команда не нужна — рестарт сервиса = миграции выполнены.

## Поддерживаемые маркетплейсы

Wildberries · Ozon · Citilink

## Лицензия

MIT
