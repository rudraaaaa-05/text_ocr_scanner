#!/usr/bin/env bash
# Creates a private virtualenv on first run, installs dependencies, starts OCR Scanner.
set -e
cd "$(dirname "$(readlink -f "$0")")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
if [ ! -f .venv/.deps_ok ] || [ requirements.txt -nt .venv/.deps_ok ]; then
    pip install -r requirements.txt
    touch .venv/.deps_ok
fi
# Global hotkeys on X11 / macOS need pynput. Optional, and unused on Wayland (Hyprland).
if [ -z "$WAYLAND_DISPLAY" ] && ! python -c "import pynput" 2>/dev/null; then
    pip install "pynput>=1.7.6,<1.8" >/dev/null 2>&1 || echo "note: could not install pynput; in-app hotkeys disabled"
fi
exec python ocr_scanner.py "$@"
