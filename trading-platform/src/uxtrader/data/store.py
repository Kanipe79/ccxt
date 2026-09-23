"""Research storage: immutable, versioned Parquet.

Two rules, both from docs/03:

* **Immutable builds.** A dataset is written once under a build id and never
  rewritten. Venues occasionally revise recent candles; re-fetching history to
  "refresh" a dataset silently changes the inputs of every backtest you have already
  run. A new fetch is a new build.
* **Point-in-time columns.** Every row carries ``available_ts`` — the earliest moment
  a live system could have known it. Research filters on that, never on ``ts``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

OHLCV_COLUMNS = ['ts', 'open', 'high', 'low', 'close', 'volume', 'available_ts']
FUNDING_COLUMNS = ['ts', 'rate', 'mark_price', 'interval_hours', 'available_ts']


def _safe(symbol: str) -> str:
    return symbol.replace('/', '-').replace(':', '_')


class ParquetStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _dir(self, kind: str, venue: str, symbol: str, extra: str = '') -> Path:
        parts = [kind, venue, _safe(symbol)] + ([extra] if extra else [])
        return self.root.joinpath(*parts)

    def write(self, kind: str, venue: str, symbol: str, df: pd.DataFrame, *,
              build: str, extra: str = '', meta: dict[str, Any] | None = None) -> Path:
        """Write a new immutable build. Refuses to overwrite an existing one."""
        directory = self._dir(kind, venue, symbol, extra)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'{build}.parquet'
        if path.exists():
            raise FileExistsError(f'{path} exists — builds are immutable; use a new build id')
        df.to_parquet(path, index=False)
        info = {'build': build, 'rows': int(len(df)),
                'written_at': datetime.now(timezone.utc).isoformat(), **(meta or {})}
        if len(df):
            info['first_ts'] = str(df['ts'].min())
            info['last_ts'] = str(df['ts'].max())
        (directory / f'{build}.json').write_text(json.dumps(info, indent=2, default=str))
        return path

    def builds(self, kind: str, venue: str, symbol: str, extra: str = '') -> list[str]:
        directory = self._dir(kind, venue, symbol, extra)
        return sorted(p.stem for p in directory.glob('*.parquet'))

    def read(self, kind: str, venue: str, symbol: str, *, extra: str = '',
             build: str | None = None, as_of: datetime | None = None) -> pd.DataFrame:
        """Read a build (latest by default). ``as_of`` applies the point-in-time filter
        on ``available_ts`` — use it in every research query."""
        builds = self.builds(kind, venue, symbol, extra)
        if not builds:
            raise FileNotFoundError(f'no {kind} builds for {venue} {symbol} {extra}')
        chosen = build or builds[-1]
        df = pd.read_parquet(self._dir(kind, venue, symbol, extra) / f'{chosen}.parquet')
        if as_of is not None:
            df = df[df['available_ts'] <= pd.Timestamp(as_of)]
        return df.reset_index(drop=True)
