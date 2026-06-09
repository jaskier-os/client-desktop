#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "[desktop-client] Creating venv..."
  python3 -m venv venv
fi

echo "[desktop-client] Installing dependencies..."
venv/bin/python -m pip install -q -r requirements.txt

echo "[desktop-client] Starting..."
exec venv/bin/python main.py --config config.ini
