# Установка

> [Что за проект](PROJECT.md) · **Как установить** · [Как работать](USAGE.md)

## Требования

- asdf-vm **>= 0.16** (более старые версии не поддерживают `asdf install` без дополнительного плагина)
- Python 3.11.9 (управляется через asdf, зафиксирован в `.tool-versions`)

## Автоматическая установка на сервер (Ubuntu)

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

Скрипт установит Google Chrome, Xvfb, создаст виртуальное окружение,
настроит systemd-сервисы (`xvfb.service` и `price-tracker.service`).

## Локально (Arch Linux)

```bash
# Установка asdf и плагина python
sudo pacman -S asdf-vm
asdf plugin add python

# Установка Python и зависимостей
asdf install
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## На сервере (Ubuntu) — вручную

```bash
# Установка asdf >= 0.16
sudo apt update && sudo apt install -y curl git
git clone https://github.com/asdf-vm/asdf.git ~/.asdf --branch v0.16.0
echo '. "$HOME/.asdf/asdf.sh"' >> ~/.bashrc
echo '. "$HOME/.asdf/completions/asdf.bash"' >> ~/.bashrc
source ~/.bashrc
asdf plugin add python

# Python и окружение
asdf install
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Настройка

### 1. Скопируйте `.env.example` в `.env`

```bash
cp .env.example .env
```

### 2. Заполните переменные в `.env`

```env
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
DB_PATH=./data/prices.db
POLL_INTERVAL_MINUTES=60
OZON_PROXY=socks5://user:pass@host:port
```

### 3. Создайте директорию для БД

```bash
mkdir -p data
```

### 4. Получите токен бота

- Откройте [@BotFather](https://t.me/BotFather) в Telegram
- Отправьте `/newbot`, придумайте имя и username
- Скопируйте полученный токен в `.env`

## Переменные окружения

| Переменная | Описание | По умолчанию |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather | — (обязательно) |
| `DB_PATH` | Путь к SQLite-файлу | `./data/prices.db` |
| `POLL_INTERVAL_MINUTES` | Интервал опроса цен (в минутах) | `60` |
| `OZON_PROXY` | Прокси для Ozon (Ozon банит серверные IP). Формат: `socks5://user:pass@host:port` или `http://host:port` | — |
