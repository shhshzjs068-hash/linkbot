#!/usr/bin/env bash
# Run from inside the project folder on an Ubuntu/Debian VPS:  bash install_vps.sh
set -euo pipefail
APP=/opt/linkbot

sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

id linkbot &>/dev/null || sudo useradd --system --home "$APP" --shell /usr/sbin/nologin linkbot
sudo mkdir -p "$APP"
sudo cp bot.py gen_session.py requirements.txt "$APP"/
[ -f "$APP/.env" ] || sudo cp .env.example "$APP/.env"

sudo python3 -m venv "$APP/venv"
sudo "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

sudo chown -R linkbot:linkbot "$APP"
sudo chmod 600 "$APP/.env"
sudo cp linkbot.service /etc/systemd/system/linkbot.service
sudo systemctl daemon-reload

cat << MSG

Installed. Next:
 1) Edit the config:        sudo nano $APP/.env
      (BOT_TOKEN, API_ID, API_HASH, ADMIN_IDS, PHONE; leave SESSION_STRING empty)
 2) First login (one time, asks for the Telegram code):
      cd $APP && sudo -u linkbot ./venv/bin/python bot.py
      -> enter the code, wait for "Checker account: ...", then press Ctrl+C
 3) Start it for good:      sudo systemctl enable --now linkbot
 4) Watch the logs:         journalctl -u linkbot -f
MSG
