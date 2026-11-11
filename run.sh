#!/usr/bin/env bash
# Creates a private virtualenv on first run, installs dependencies, starts OCR Scanner.
set -e
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$SCRIPT_DIR"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
if [ ! -f .venv/.deps_ok ] || [ requirements.txt -nt .venv/.deps_ok ]; then
    python -m pip install -r requirements.txt
    touch .venv/.deps_ok
fi
exec python ocr_scanner.py "$@"
