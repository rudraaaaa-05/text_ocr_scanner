#!/usr/bin/env bash
# Runs the OCR Scanner background daemon commands inside the same private virtualenv as run.sh.
#   ./run_daemon.sh start | stop | status | run
#   start launches the daemon plus a hidden tray GUI
#   ./run_daemon.sh autostart enable | disable | status      (systemd user service)
#   ./run_daemon.sh service start | stop | restart | status
set -e
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$SCRIPT_DIR"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
if [ ! -f .venv/.deps_ok ] || [ requirements.txt -nt .venv/.deps_ok ]; then
    python -m pip install -r requirements.txt
    touch .venv/.deps_ok
fi
exec python ocr_daemon.py "$@"
