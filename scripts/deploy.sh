#!/usr/bin/env bash
# Первичная установка Price Tracker на сервер (Ubuntu).
# Повторный деплой выполняется через `fab deploy` из fabfile.py.
set -euo pipefail

echo "=== Price Tracker — Initial Setup ==="

USER_NAME="$(whoami)"
PROJECT_DIR="/home/${USER_NAME}/price_tracker"

# 1. System deps
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3 python3-venv xvfb wget fluxbox build-essential \
  libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
  libffi-dev liblzma-dev libncursesw5-dev

# 2. Google Chrome
if ! command -v google-chrome-stable &>/dev/null; then
  echo ">>> Installing Google Chrome..."
  wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo dpkg -i /tmp/chrome.deb || sudo apt-get -f install -y -qq
  rm -f /tmp/chrome.deb
  echo ">>> Chrome installed: $(google-chrome-stable --version)"
else
  echo ">>> Chrome already installed: $(google-chrome-stable --version)"
fi

# 3. Project
cd "${PROJECT_DIR}"

# 4. Python via asdf
if ! command -v asdf &>/dev/null; then
  echo ">>> Installing asdf..."
  git clone https://github.com/asdf-vm/asdf.git ~/.asdf --branch v0.16.0
  echo '. "$HOME/.asdf/asdf.sh"' >> ~/.bashrc
  . "$HOME/.asdf/asdf.sh"
fi
. "$HOME/.asdf/asdf.sh"
asdf plugin add python 2>/dev/null || true
asdf install

# 5. Python venv (remove stale venv from previous failed runs)
rm -rf venv
asdf exec python -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
echo ">>> Python version: $(python --version)"
pip install --upgrade pip
pip install -r requirements.txt

# 6. .env
if [ ! -f .env ]; then
  cp .env.example .env
  echo ">>> Edit ${PROJECT_DIR}/.env and set TELEGRAM_BOT_TOKEN <<<"
  exit 1
fi

# 7. Install systemd units from scripts/*.service files
echo ">>> Installing systemd units..."
sudo install -m 644 "${PROJECT_DIR}/scripts/price-tracker-xvfb.service"   /etc/systemd/system/price-tracker-xvfb.service
sudo install -m 644 "${PROJECT_DIR}/scripts/price-tracker-fluxbox.service" /etc/systemd/system/price-tracker-fluxbox.service
sudo install -m 644 "${PROJECT_DIR}/scripts/price-tracker.service"        /etc/systemd/system/price-tracker.service

# Templates содержат User=yury и пути /home/yury/price_tracker.
# Если текущий пользователь отличается — патчим на лету.
if [ "${USER_NAME}" != "yury" ]; then
  for svc in price-tracker-xvfb price-tracker-fluxbox price-tracker; do
    sudo sed -i "s/^User=yury$/User=${USER_NAME}/" "/etc/systemd/system/${svc}.service"
    sudo sed -i "s|/home/yury/price_tracker|${PROJECT_DIR}|g" "/etc/systemd/system/${svc}.service"
  done
fi

# 8. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now price-tracker-xvfb.service
sleep 2
sudo systemctl enable --now price-tracker-fluxbox.service
sleep 1
sudo systemctl enable --now price-tracker.service

echo "=== Setup complete ==="
echo "Xvfb:   systemctl status price-tracker-xvfb"
echo "WM:     systemctl status price-tracker-fluxbox"
echo "Bot:    systemctl status price-tracker"
echo "Logs:   journalctl -u price-tracker -f"
echo
echo "Next time use: fab deploy"
