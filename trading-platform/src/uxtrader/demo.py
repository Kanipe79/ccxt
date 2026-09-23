"""Kept for compatibility: ``python -m uxtrader.demo`` is now ``ux demo``.

Starts the dashboard and immediately launches a demo run (synthetic market, simulated
fills, no keys, no network). The demo strategy lives in ``uxtrader.app``.
"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == '__main__':
    raise SystemExit(main(['demo', *sys.argv[1:]]))
