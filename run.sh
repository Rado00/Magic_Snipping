#!/usr/bin/env bash
# Entry point: prepares a Python venv, installs deps, runs magic_snipping.py
# Reads the deck URL from deck_link.txt (override with --input <file>).
# Works on Linux/macOS and on Git Bash / MSYS on Windows.

set -euo pipefail

cd "$(dirname "$0")"

VENV="${VENV:-.venv}"

# Pick a Python interpreter that ACTUALLY runs (not the Windows Store stub).
# We probe each candidate by running `--version`; the Store stub prints to
# stderr and exits non-zero, so it gets skipped.
pick_python() {
    local candidates=()
    if [ -n "${PYTHON:-}" ]; then
        candidates+=("$PYTHON")
    fi
    # On Windows prefer `py` (the official launcher) and `python` over `python3`
    # (which is usually the Microsoft Store alias).
    case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*) candidates+=(py python python3) ;;
        *)                    candidates+=(python3 python py) ;;
    esac
    for cand in "${candidates[@]}"; do
        if "$cand" --version >/dev/null 2>&1; then
            echo "$cand"; return
        fi
    done
    echo "[run.sh] No working python interpreter found (tried: ${candidates[*]})." >&2
    echo "[run.sh] Tip: open 'App execution aliases' in Windows Settings and turn off the python.exe / python3.exe Store aliases, or set PYTHON=/c/Users/<you>/AppData/Local/Programs/Python/Python313/python.exe" >&2
    exit 1
}
PY="$(pick_python)"
echo "[run.sh] Using Python: $($PY --version 2>&1)"

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
