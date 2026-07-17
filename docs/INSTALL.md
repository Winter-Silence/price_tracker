# Установка

> [Что за проект](PROJECT.md) · **Как установить** · [Как работать](USAGE.md)

## Требования

- asdf-vm **>= 0.16** (более старые версии не поддерживают `asdf install` без дополнительного плагина)
- Python 3.11.9 (управляется через asdf, зафиксирован в `.tool-versions`)
- Build deps для сборки Python из исходников (ставятся автоматически `scripts/deploy.sh` на сервере; вручную — см. ниже)
- **bash или fish**. Команды активации venv даны для обоих; на fish нужен `virtualenv` вместо стандартного `python -m venv` (см. примечание ниже)

> ⚠ **Windows-checkout / `git config --global core.autocrlf=true`:**
> если репозиторий клонируется на WSL/Linux с включённым autocrlf, asdf-плагины
> ломаются (`'bash\r': No such file or directory`). Перед установкой asdf-плагина
> python проверь: `git config --get core.autocrlf` должен вернуть `false` или
> `input` для этого клона. Если вернул `true` — выполни в репозитории:
> `git config --local core.autocrlf false && rm -rf ~/.asdf/plugins/python && asdf plugin add python`
> и при необходимости `find ~/.asdf/plugins/python -type f -exec sed -i 's/\r$//' {} +`.

## Автоматическая установка на сервер (Ubuntu)

`scripts/deploy.sh` выполняет **первичную** установку: ставит Google Chrome,
Xvfb, fluxbox, asdf, создаёт venv, копирует systemd-юниты из `scripts/*.service`
в `/etc/systemd/system/` и запускает сервисы.

```bash
# 1. На сервере склонируйте репозиторий:
git clone <repo-url> ~/price_tracker
cd ~/price_tracker

# 2. Запустите первичную установку:
bash scripts/deploy.sh
```

Создаются три systemd-юнита: `price-tracker-xvfb`, `price-tracker-fluxbox`,
`price-tracker`. После `deploy.sh` бот сам стартует; проверьте статус:

```bash
systemctl status price-tracker.service
journalctl -u price-tracker.service -f
```

Если username на сервере отличается от `yury`, `deploy.sh` автоматически
пропатчит `User=` и пути в скопированных юнитах.

## Локально (Arch Linux / WSL Ubuntu)

```bash
# 1. Установка asdf и плагина python
#    Arch:
sudo pacman -S asdf-vm
#    WSL Ubuntu / Debian: см. https://asdf-vm.com/guide/getting-started.html
asdf plugin add python

# 2. Установка системных deps для сборки Python 3.11.9
#    Arch:
sudo pacman -S --needed base-devel openssl zlib bzip2 readline sqlite ncurses xz libffi
#    Ubuntu/Debian:
sudo apt install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
  libreadline-dev libsqlite3-dev libffi-dev liblzma-dev libncursesw5-dev

# 3. Установка Python 3.11.9 — версия берётся из .tool-versions:
cd ~/price_tracker     # asdf подхватит .tool-versions в этой директории
asdf install

# 4. Создание виртуального окружения.
#    Это место зависит от shell — см. ниже.

# — bash / zsh:
asdf exec python -m venv venv
source venv/bin/activate

# — fish (нужен virtualenv, т.к. python -m venv не генерирует activate.fish):
asdf exec python -m pip install --user virtualenv
asdf exec python -m virtualenv venv
source venv/bin/activate.fish

# 5. Установка зависимостей проекта
pip install --upgrade pip
pip install -r requirements.txt
```

> **Почему для fish нужен `virtualenv`?**
> Стандартный `python -m venv` создаёт только `activate` (bash-синтаксис).
> fish его не понимает (`Unsupported use of '='`). Пакет `virtualenv`
> генерирует шесть активаторов, включая `activate.fish`.

## На сервере (Ubuntu) — вручную

> На production повторный деплой выполняется через `fab deploy` (см. раздел
> «Деплой на production (Fabric)» ниже). Этот раздел — для первичной ручной
> установки без `scripts/deploy.sh` или для отладки venv вручную.

```bash
# Установка asdf >= 0.16
sudo apt update && sudo apt install -y curl git
#    Arch / WSL — см. инструкцию выше.
git clone https://github.com/asdf-vm/asdf.git ~/.asdf --branch v0.16.0
# Для bash:
echo '. "$HOME/.asdf/asdf.sh"' >> ~/.bashrc
echo '. "$HOME/.asdf/completions/asdf.bash"' >> ~/.bashrc
source ~/.bashrc
# Для fish (если используешь fish как login shell):
#   см. https://asdf-vm.com/guide/getting-started.html#fish — добавить в
#   ~/.config/fish/config.fish блок инициализации asdf
asdf plugin add python

# Build deps для Python 3.11.9
sudo apt install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
  libreadline-dev libsqlite3-dev libffi-dev liblzma-dev libncursesw5-dev

# Python
asdf install

# Создание venv (выбери shell-секцию):

# bash / zsh:
asdf exec python -m venv venv
source venv/bin/activate

# fish:
asdf exec python -m pip install --user virtualenv
asdf exec python -m virtualenv venv
source venv/bin/activate.fish

# Зависимости (как обычно)
pip install --upgrade pip
pip install -r requirements.txt
```

> Серверный бот запускается через systemd-юнит `price-tracker.service`,
> который использует `bash`-нотацию абсолютных путей к `venv/bin/python` —
> выбор shell на сервере не влияет на systemd-запуск, влияет только на
> интерактивную отладку вручную.

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

## Деплой на production (Fabric)

Деплой выполняется с **локальной** машины через [Fabric](https://www.fabfile.org/) —
Python-аналог capistrano. Одна команда `fab deploy` делает:
`git push` → SSH на сервер → `git reset --hard origin/<branch>` →
`asdf install` (если сменилась версия Python) → пересоздание `venv` →
`pip install -r requirements.txt` → `systemctl restart price-tracker.service` →
`systemctl is-active` (проверка) → `journalctl -n 20` (хвост логов для контроля).

Миграции БД накатываются **автоматически** при старте бота (`init_db()` в
`main.py` применяет `MIGRATIONS` на запуске). Отдельной команды для миграций нет:
рестарт сервиса = миграции выполнены.

### Требования к серверу

- Ubuntu (тестировалось на 22.04+)
- SSH-доступ по ключу (без пароля)
- Пользователь с **passwordless sudo** для `systemctl` (требуется для `fab restart`)
- Склонированный `~/price_tracker` (см. «Автоматическая установка» выше)
- `~/price_tracker/.env` с заполненным `TELEGRAM_BOT_TOKEN`

### Настройка деплоя (один раз, локально)

`fabric` уже включён в `requirements.txt`, так что достаточно создать конфиг
деплоя из шаблона:

```bash
cp .deploy.env.example .deploy.env
$EDITOR .deploy.env        # минимум: DEPLOY_HOST, DEPLOY_USER, DEPLOY_PATH
```

Переменные конфига деплоя (`.deploy.env`, не в git):

| Переменная | Описание | По умолчанию |
|---|---|---|
| `DEPLOY_HOST` | SSH-хост или IP сервера | — (обязательно) |
| `DEPLOY_USER` | SSH-пользователь | `yury` |
| `DEPLOY_PORT` | SSH-порт | `22` |
| `DEPLOY_PATH` | Абсолютный путь к проекту на сервере | `/home/yury/price_tracker` |
| `DEPLOY_BRANCH` | Ветка для деплоя | `main` |
| `DEPLOY_SSH_KEY` | Путь к SSH-ключу (если нестандартный) | `~/.ssh/...` (ssh-agent) |

Проверка связи:

```bash
fab check
```

### Команды Fabric

| Команда | Что делает |
|---|---|
| `fab deploy` | Push + SSH + pull + пересоздание venv + restart + хвост логов |
| `fab deploy --no-push` | То же, но без локального `git push` (если push уже сделан) |
| `fab deploy --branch dev` | Деплой произвольной ветки |
| `fab status` | Статус всех трёх systemd-сервисов (xvfb, fluxbox, bot) |
| `fab logs` | Последние 50 строк лога бота |
| `fab logs --lines 200` | Последние N строк лога |
| `fab restart` | Перезапуск бота без деплоя (например после ручной правки `.env`) |
| `fab stop` / `fab start` | Остановить / запустить бота |
| `fab rollback` | Экстренный откат сервера на `HEAD~1` + restart |
| `fab rollback --steps 3` | Откат на N коммитов назад |
| `fab setup` | Повторно запустить `scripts/deploy.sh` на сервере (если нужно обновить системные пакеты или systemd-юниты) |
| `fab check` | Проверка `.deploy.env` и SSH-связности с сервером |

### Типичный сценарий релиза

```bash
# 1. Локально: коммит + push
git add ... && git commit -m "feat: ..."
git push

# 2. Деплой
fab deploy
#    Fabric сам сделает push, pull на сервере, обновит venv, перезапустит бота
#    и покажет последние 20 строк лога для визуального контроля старта.

# 3. Если в логах видно что всё ок — готово.
#    Если что-то не так:
fab logs --lines 200       # подробнее
fab rollback                # вернуться на предыдущий коммит
```

Если сломалось серьёзно и хочешь откатить через revert (с сохранением истории, а не reset --hard, как делает `fab rollback`):

```bash
git revert <commit> && git push
fab deploy --no-push        # не делаем повторный push, мы уже запушили revert
```

### Что НЕ делает `fab deploy`

- Не обновляет Google Chrome на сервере (ставится один раз `scripts/deploy.sh`).
- Не `apt upgrade` системных пакетов.
- Не меняет systemd-юниты. Если правка `.service` файла попала в репозиторий —
  выполните `fab setup` (полный re-run `scripts/deploy.sh`) или вручную:
  ```bash
  fab deploy --no-push        # код и venv
  # затем единоразово для юнитов:
  fab setup                   # или: sudo install -m 644 scripts/price-tracker-*.service /etc/systemd/system/ && sudo systemctl daemon-reload && fab restart
  ```
- Не откатывает автоматически БД. `init_db()` применяет только «вперёд»
  (`MIGRATIONS`), необратимые миграции — через новый коммит-миграцию.
