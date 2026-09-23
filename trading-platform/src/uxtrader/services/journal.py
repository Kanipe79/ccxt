"""SQLite journal: equity, fills and operator-visible events, persisted.

Single-box deployments get history that survives restarts (the dashboard's equity
curve, fills and event log) without running Postgres. It is a *record*, not a source of
truth: positions are always rebuilt from the venue on start, never from this file.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from ..bus import Bus
from ..events import Control, FeedRecovered, PortfolioSnapshot, StaleFeed
from ..types import Fill, RiskDecision

SCHEMA = """
CREATE TABLE IF NOT EXISTS equity (ts TEXT, seq INTEGER, equity REAL, peak REAL);
CREATE TABLE IF NOT EXISTS fills (ts TEXT, strategy TEXT, venue TEXT, symbol TEXT, side TEXT,
    amount REAL, price REAL, fee REAL, slippage_bps REAL, maker INTEGER, coid TEXT);
CREATE TABLE IF NOT EXISTS events (ts TEXT, severity TEXT, kind TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS equity_ts ON equity (ts);
CREATE INDEX IF NOT EXISTS fills_ts ON fills (ts);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


class Journal:
    def __init__(self, path: str | Path, *, equity_every_s: float = 2.0) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        self.equity_every_s = equity_every_s
        self._last_equity_write = 0.0
        self._last_event: tuple[str, str] | None = None

    async def start(self, bus: Bus) -> None:
        await bus.subscribe('portfolio.snapshot', self._on_snapshot)
        await bus.subscribe('fill.>', self._on_fill)
        await bus.subscribe('control.>', self._on_control)
        await bus.subscribe('risk.veto', self._on_veto)
        await bus.subscribe('feed.stale', self._on_feed)
        await bus.subscribe('feed.recovered', self._on_feed)

    def event(self, severity: str, kind: str, text: str, ts: str | None = None) -> None:
        # Collapse immediate repeats (a killed strategy retrying every bar).
        if self._last_event == (kind, text):
            return
        self._last_event = (kind, text)
        self.db.execute('INSERT INTO events VALUES (?,?,?,?)',
                        (ts or _now(), severity, kind, text))
        self.db.commit()

    async def _on_snapshot(self, _s: str, snap: Any) -> None:
        if not isinstance(snap, PortfolioSnapshot):
            return
        now = time.monotonic()
        if now - self._last_equity_write < self.equity_every_s:
            return
        self._last_equity_write = now
        self.db.execute('INSERT INTO equity VALUES (?,?,?,?)',
                        (snap.ts.isoformat(), snap.seq, float(snap.equity), float(snap.peak_equity)))
        self.db.commit()

    async def _on_fill(self, _s: str, f: Any) -> None:
        if not isinstance(f, Fill):
            return
        self.db.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?)', (
            f.ts.isoformat(), f.strategy, f.venue, f.symbol, f.side, float(f.amount),
            float(f.price), float(f.fee), f.slippage_bps, int(f.is_maker), f.client_order_id))
        self.db.commit()

    async def _on_control(self, _s: str, c: Any) -> None:
        if isinstance(c, Control):
            scope = c.strategy or c.venue or 'all'
            self.event('CRIT' if c.command == 'kill' else 'WARN', c.command,
                       f'{c.command.upper()} ({scope}): {c.reason}', c.ts.isoformat())

    async def _on_veto(self, _s: str, d: Any) -> None:
        if isinstance(d, RiskDecision) and not d.approved:
            limit = d.breaches[0].limit if d.breaches else 'veto'
            self.event('WARN', 'veto', f'risk veto — {limit}: {d.note}')

    async def _on_feed(self, _s: str, m: Any) -> None:
        if isinstance(m, StaleFeed):
            self.event('WARN', 'feed', f'feed stale: {m.symbol} ({m.age_s:.0f}s)', m.ts.isoformat())
        elif isinstance(m, FeedRecovered):
            self.event('INFO', 'feed', f'feed recovered: {m.symbol}', m.ts.isoformat())

    # -- reads -----------------------------------------------------------------

    def equity(self, limit: int = 2000) -> list[tuple[str, float]]:
        rows = self.db.execute('SELECT ts, equity FROM equity ORDER BY rowid DESC LIMIT ?',
                               (limit,)).fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]

    def fills(self, limit: int = 200) -> list[dict[str, Any]]:
        cols = ['ts', 'strategy', 'venue', 'symbol', 'side', 'amount', 'price', 'fee',
                'slippage_bps', 'is_maker', 'client_order_id']
        rows = self.db.execute('SELECT * FROM fills ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute('SELECT ts, severity, kind, text FROM events ORDER BY rowid DESC LIMIT ?',
                               (limit,)).fetchall()
        return [{'ts': r[0], 'severity': r[1], 'kind': r[2], 'text': r[3]} for r in rows]

    def close(self) -> None:
        self.db.close()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
