#!/usr/bin/env bash
# Entry point: prepares a Python venv, installs deps, runs magic_snipping.py
# Reads the deck URL from deck_link.txt (override with --input <file>).
# Works on Linux/macOS and on Git Bash / MSYS on Windows.

set -euo pipefail

cd "$(dirname "$0")"

VENV="${VENV:-.venv}"

# Pick a Python interpreter that exists on this system
pick_python() {
    if [ -n "${PYTHON:-}" ] && command -v "$PYTHON" >/dev/null 2>&1; then
        echo "$PYTHON"; return
    fi
    for cand in python3 python py; do
        if command -v "$cand" >/dev/null 2>&1; then
            echo "$cand"; return
        fi
    done
    echo "[run.sh] No python interpreter found (tried python3, python, py)." >&2
    exit 1
}
PY="$(pick_python)"

if [ ! -d "$VENV" ]; then
    echo "[run.sh] Creating virtualenv in $VENV using $PY"
    "$PY" -m venv "$VENV"
fi

# venv layout differs between Unix (.venv/bin) and Windows (.venv/Scripts)
if [ -f "$VENV/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    VENV_PY="$VENV/bin/python"
elif [ -f "$VENV/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV/Scripts/activate"
    VENV_PY="$VENV/Scripts/python"
else
    echo "[run.sh] Could not find venv activate script under $VENV" >&2
    exit 1
fi

echo "[run.sh] Installing dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r requirements.txt

if [ ! -f deck_link.txt ]; then
    echo "[run.sh] deck_link.txt not found. Create it with the deck URL on the first line." >&2
    exit 1
fi

echo "[run.sh] Running magic_snipping"
"$VENV_PY" magic_snipping.py "$@"
