"""Point-in-time universe snapshots.

Survivorship bias is the largest single distortion available to a crypto backtest. A
cross-sectional momentum backtest run on *today's* top-40 overstates returns by an
estimated 15 – 40% annualized — larger than most strategies' entire edge, and it will
not show up as an obvious bug. It will show up as a beautiful equity curve.

The defence is mechanical: snapshot the eligible universe weekly, store it, and have
research query it by date. Delisted assets stay in the table with their real final
prices and a ``delisted_at`` stamp.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    snapshot_date: date
    listed_at: datetime
    delisted_at: datetime | None
    median_dollar_volume_30d: float
    venue_tag: str | None = None       # 'innovation' / 'monitoring' / 'seed' / None


SCHEMA = """
CREATE TABLE IF NOT EXISTS universe_snapshot (
    snapshot_date            Date,
    symbol                   String,
    venue                    String,
    listed_at                DateTime,
    delisted_at              Nullable(DateTime),
    median_dollar_volume_30d Float64,
    venue_tag                Nullable(String),
    available_ts             DateTime    -- when WE could first have known this row
) ENGINE = MergeTree ORDER BY (snapshot_date, venue, symbol);
"""


class UniverseStore:
    """Thin wrapper over ClickHouse. Queries filter on ``available_ts``, never
    ``snapshot_date`` alone — see docs/03 §3."""

    def __init__(self, client) -> None:
        self.client = client

    def snapshot(self, as_of: date, venue: str, *, top_n: int = 40,
                 min_volume: float = 20e6, min_age_days: int = 90) -> list[str]:
        """The universe as it was on `as_of`. This is what backtests must call."""
        rows = self.client.execute(
            """
            SELECT symbol
            FROM universe_snapshot
            WHERE venue = %(venue)s
              AND snapshot_date = (
                  SELECT max(snapshot_date) FROM universe_snapshot
                  WHERE snapshot_date <= %(as_of)s AND venue = %(venue)s)
              AND available_ts <= %(as_of_ts)s
              AND median_dollar_volume_30d >= %(min_volume)s
              AND dateDiff('day', listed_at, %(as_of_ts)s) >= %(min_age)s
              AND (delisted_at IS NULL OR delisted_at > %(as_of_ts)s)
              AND (venue_tag IS NULL OR venue_tag NOT IN ('innovation','monitoring','seed'))
            ORDER BY median_dollar_volume_30d DESC
            LIMIT %(top_n)s
            """,
            {'venue': venue, 'as_of': as_of,
             'as_of_ts': datetime.combine(as_of, datetime.min.time()),
             'min_volume': min_volume, 'min_age': min_age_days, 'top_n': top_n},
        )
        return [r[0] for r in rows]

    def write(self, entries: list[UniverseEntry], venue: str, available_ts: datetime) -> None:
        self.client.execute(
            'INSERT INTO universe_snapshot VALUES',
            [(e.snapshot_date, e.symbol, venue, e.listed_at, e.delisted_at,
              e.median_dollar_volume_30d, e.venue_tag, available_ts) for e in entries],
        )
