#!/usr/bin/env bash
# Entry point: prepares a Python venv, installs deps, runs magic_snipping.py
# Reads the deck URL from deck_link.txt (override with --input <file>).

set -euo pipefail

cd "$(dirname "$0")"

VENV="${VENV:-.venv}"
PYTHON="${PYTHON:-python3}"

if [ ! -d "$VENV" ]; then
    echo "[run.sh] Creating virtualenv in $VENV"
    "$PYTHON" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "[run.sh] Installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ ! -f deck_link.txt ]; then
    echo "[run.sh] deck_link.txt not found. Create it with the deck URL on the first line." >&2
    exit 1
fi

echo "[run.sh] Running magic_snipping"
python magic_snipping.py "$@"
