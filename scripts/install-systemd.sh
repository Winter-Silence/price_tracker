#!/usr/bin/env bash
# Устаревший альтернативный путь установки systemd-юнитов.
# Для первичной установки используйте scripts/deploy.sh (он сам поставит юниты).
# Здесь — только ручная установка юнитов без остального setup-процесса.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/price_tracker}"

sudo install -m 644 "${PROJECT_DIR}/scripts/price-tracker-xvfb.service"   /etc/systemd/system/price-tracker-xvfb.service
sudo install -m 644 "${PROJECT_DIR}/scripts/price-tracker-fluxbox.service" /etc/systemd/system/price-tracker-fluxbox.service
sudo install -m 644 "${PROJECT_DIR}/scripts/price-tracker.service"         /etc/systemd/system/price-tracker.service
sudo systemctl daemon-reload
sudo systemctl enable --now price-tracker-xvfb.service
sleep 2
sudo systemctl enable --now price-tracker-fluxbox.service
sleep 1
sudo systemctl enable --now price-tracker.service

echo "Services installed and started:"
sudo systemctl status price-tracker-xvfb.service   --no-pager || true
sudo systemctl status price-tracker-fluxbox.service --no-pager || true
sudo systemctl status price-tracker.service        --no-pager || true
