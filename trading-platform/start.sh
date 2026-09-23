#!/usr/bin/env bash
# One command from a fresh clone to a running dashboard:
#   ./start.sh            dashboard; launch the bot from the browser
#   ./start.sh demo       dashboard + a demo run started for you
#   ./start.sh doctor     check this machine
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"

if [ ! -x .venv/bin/ux ]; then
  echo "ux-trader: first run — creating .venv and installing (a minute or two)…"
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ required")'
  "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e ..        # this fork's ccxt, not the PyPI build
  .venv/bin/pip install -q -e .
fi
exec .venv/bin/ux "$@"
