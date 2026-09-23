from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from uxtrader.data.resample import Resampler
from uxtrader.types import Bar

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def m1(i: int, px: float) -> Bar:
    return Bar(venue='v', symbol='S', timeframe='1m', ts=T0 + timedelta(minutes=i),
               open=Decimal(str(px)), high=Decimal(str(px + 1)), low=Decimal(str(px - 1)),
               close=Decimal(str(px + 0.5)), volume=Decimal('2'))


def test_emits_once_when_window_completes():
    r = Resampler('1h')
    out = [b for i in range(120) for b in r.push(m1(i, 100 + i))]
    assert len(out) == 2
    first = out[0]
    assert first.ts == T0 and first.timeframe == '1h'
    assert first.open == Decimal('100') and first.close == Decimal('159.5')
    assert first.high == Decimal('160') and first.low == Decimal('99')
    assert first.volume == Decimal('120')
    assert r.incomplete_bars == 0


def test_never_emits_before_the_last_minute():
    r = Resampler('1h')
    assert all(not r.push(m1(i, 100)) for i in range(59))
    assert len(r.push(m1(59, 100))) == 1


def test_missing_final_minute_is_closed_by_next_window_and_counted():
    r = Resampler('1h')
    out = [b for i in list(range(0, 59)) + [60] for b in r.push(m1(i, 100))]
    assert len(out) == 1 and out[0].ts == T0
    assert r.incomplete_bars == 1
