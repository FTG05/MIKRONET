#!/bin/sh
# Runs the merged MikroNet + Lending + Bibi Payment app on this machine.
# Usage: ./start-local.sh
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "First run: creating venv and installing requirements..."
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
fi

port=$(python3 -c "import json;print(json.load(open('config.json'))['port'])")
echo "Starting on http://127.0.0.1:$port  (Ctrl+C to stop)"
./venv/bin/python app.py
