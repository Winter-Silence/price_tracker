#!/usr/bin/env bash
set -euo pipefail

echo "=== Price Tracker — Deploy Script ==="

# 1. System deps
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv xvfb wget fluxbox build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev liblzma-dev libncursesw5-dev

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

# 3. Project (assuming it's already cloned/copied)
cd /home/$(whoami)/price_tracker

# 4. Python via asdf
if ! command -v asdf &>/dev/null; then
    echo ">>> Installing asdf..."
    git clone https://github.com/asdf-vm/asdf.git ~/.asdf --branch v0.16.0
    echo '. "$HOME/.asdf/asdf.sh"' >> ~/.bashrc
    . "$HOME/.asdf/asdf.sh"
fi
. "$HOME/.asdf/asdf.sh"
asdf plugin add python
asdf install

# 5. Python venv
asdf exec python -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 6. .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo ">>> Edit .env and set TELEGRAM_BOT_TOKEN <<<"
    exit 1
fi

# 7. Xvfb systemd service
sudo tee /etc/systemd/system/price-tracker-xvfb.service > /dev/null <<EOF
[Unit]
Description=X Virtual Framebuffer for Price Tracker
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24 -ac
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 8. fluxbox systemd service (requires running Xvfb)
sudo tee /etc/systemd/system/price-tracker-fluxbox.service > /dev/null <<EOF
[Unit]
Description=fluxbox window manager for Price Tracker
After=price-tracker-xvfb.service
Requires=price-tracker-xvfb.service
BindsTo=price-tracker-xvfb.service

[Service]
Type=simple
User=$(whoami)
ExecStartPre=/usr/bin/sleep 1
ExecStart=/usr/bin/fluxbox
Restart=always
RestartSec=5
Environment=DISPLAY=:99

[Install]
WantedBy=multi-user.target
EOF

# 9. Bot systemd service
sudo tee /etc/systemd/system/price-tracker.service > /dev/null <<EOF
[Unit]
Description=Price Tracker Telegram Bot
After=network.target price-tracker-xvfb.service price-tracker-fluxbox.service
Requires=price-tracker-xvfb.service price-tracker-fluxbox.service

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=/home/$(whoami)/price_tracker
Environment=DISPLAY=:99
EnvironmentFile=-/home/$(whoami)/price_tracker/.env
ExecStart=/home/$(whoami)/price_tracker/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 10. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now price-tracker-xvfb.service
sleep 2
sudo systemctl enable --now price-tracker-fluxbox.service
sleep 1
sudo systemctl enable --now price-tracker.service

echo "=== Done ==="
echo "Xvfb:   systemctl status price-tracker-xvfb"
echo "WM:     systemctl status price-tracker-fluxbox"
echo "Bot:    systemctl status price-tracker"
echo "Logs:   journalctl -u price-tracker -f"
