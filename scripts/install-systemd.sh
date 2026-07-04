#!/usr/bin/env bash
set -euo pipefail

sudo cp /home/yury/price-tracker/scripts/xvfb.service /etc/systemd/system/xvfb.service
sudo cp /home/yury/price-tracker/scripts/price-tracker.service /etc/systemd/system/price-tracker.service
sudo systemctl daemon-reload
sudo systemctl enable --now xvfb.service
sudo systemctl enable --now price-tracker.service

echo "Services installed and started:"
sudo systemctl status xvfb.service --no-pager
sudo systemctl status price-tracker.service --no-pager
