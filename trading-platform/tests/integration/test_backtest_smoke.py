"""End-to-end smoke: synthetic bars → S4 → risk gate → OMS → paper fills → equity curve.

This is the regression test referenced in docs/04 §8: a fixed dataset and a fixed seed
must produce the same equity curve every time. It catches accidental behaviour changes
in shared code far better than any unit test, because it exercises the actual wiring.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from uxtrader.lab.backtest import EventDrivenBacktester
from uxtrader.strategies.donchian_regime import DonchianRegime
from uxtrader.types import Bar

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def synthetic_bars(n: int = 1600, symbol: str = 'BTC/USDT:USDT') -> list[Bar]:
    """A deterministic trending-then-ranging series. Not a market — a wiring test."""
    bars, price = [], 40000.0
    for i in range(n):
        drift = 18.0 if i < 1300 else -25.0
        wobble = math.sin(i / 11.0) * 140 + math.cos(i / 4.3) * 70
        close = price + drift + wobble
        high = max(price, close) + 90
        low = min(price, close) - 90
        bars.append(Bar(
            venue='sim', symbol=symbol, timeframe='4h',
            ts=START + timedelta(hours=4 * i),
            open=Decimal(str(round(price, 2))), high=Decimal(str(round(high, 2))),
            low=Decimal(str(round(low, 2))), close=Decimal(str(round(close, 2))),
            volume=Decimal('120'), closed=True))
        price = close
    return bars


class S4(DonchianRegime):
    symbols = ('BTC/USDT:USDT',)


@pytest.mark.asyncio
async def test_backtest_runs_end_to_end():
    bars = synthetic_bars()
    bt = EventDrivenBacktester(starting_equity=Decimal('100000'))
    result = await bt.run(S4, bars, start=START)

    assert len(result.equity_curve) == len(bars)
    assert result.final_equity > 0
    # The strategy must have produced *something*; a silent no-op means broken wiring.
    assert result.intents, 'strategy produced no intents — check indicator warmup'


@pytest.mark.asyncio
async def test_backtest_is_deterministic():
    """Same data, same seed, same curve. This is the regression guarantee."""
    bars = synthetic_bars()
    curves = []
    for _ in range(2):
        bt = EventDrivenBacktester(starting_equity=Decimal('100000'))
        result = await bt.run(S4, bars, start=START)
        curves.append([round(e, 6) for _, e in result.equity_curve])
    assert curves[0] == curves[1]


@pytest.mark.asyncio
async def test_no_trading_before_warmup():
    """A 55-bar Donchian plus a 1200-bar EMA cannot signal on bar 10."""
    bars = synthetic_bars(60)
    bt = EventDrivenBacktester(starting_equity=Decimal('100000'))
    result = await bt.run(S4, bars, start=START)
    assert not result.fills, 'traded before indicators were ready'


@pytest.mark.asyncio
async def test_risk_engine_shrinks_oversized_strategy_request():
    """S4 sizes from stop distance, which on a low-ATR instrument asks for >2x equity
    in notional. The risk engine's 20%-per-asset cap must shrink it rather than let it
    through — and shrink, not veto, so the strategy still gets a position."""
    bars = synthetic_bars()
    bt = EventDrivenBacktester(starting_equity=Decimal('100000'))
    result = await bt.run(S4, bars, start=START)

    entry = next(i for i in result.intents if 'break' in i.reason)
    first_fill = result.fills[0]
    assert entry.target.position > first_fill.amount, 'risk engine did not shrink'

    notional = first_fill.amount * first_fill.price
    assert notional <= Decimal('100000') * Decimal('0.20') * Decimal('1.01')


@pytest.mark.asyncio
async def test_strategy_sees_fills_before_the_next_bar():
    """Regression: fills execute at bar t+1's open, before the strategy sees bar t+1.
    The strategy's view must already include them — it once lagged a full bar."""
    from uxtrader.strategy import StrategyBase

    seen: list[Decimal] = []

    class Probe(StrategyBase):
        name = 'probe'
        symbols = ('BTC/USDT:USDT',)

        async def on_bar(self, bar):
            seen.append(self.ctx.position(bar.symbol))
            if len(seen) == 3:
                return [self.target(bar.symbol, Decimal('0.1'), reason='probe entry')]
            return []

    bars = synthetic_bars(6)
    await EventDrivenBacktester(starting_equity=Decimal('100000')).run(Probe, bars, start=START)
    assert seen[2] == 0            # decided on bar 3's close
    assert seen[3] == Decimal('0.1'), 'strategy saw a stale position after the fill'
