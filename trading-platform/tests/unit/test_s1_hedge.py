"""S1 must never open an unhedged perp short."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from uxtrader.clock import SimClock
from uxtrader.strategies.funding_carry import FundingCarry
from uxtrader.strategy import StrategyContext
from uxtrader.types import Bar, Funding

PERP = 'BTC/USDT:USDT'
T = datetime(2024, 1, 1, tzinfo=timezone.utc)


async def run(spot_venue):
    cls = type('S1', (FundingCarry,), {'symbols': (PERP,), 'SPOT_VENUE': spot_venue})
    ctx = StrategyContext('s1', SimClock(T), equity=Decimal('100000'), risk_budget=0.35,
                          positions={}, marks={})
    s = cls(ctx)
    for i in range(5):
        await s.on_funding(Funding(venue='v', symbol=PERP, ts=T, rate=Decimal('0.0003')))
    s.observe_market(PERP, basis_bps=12, liq_distance=0.5, oi_notional=5e8, adv_notional=2e9)
    bar = Bar(venue='v', symbol=PERP, timeframe='1h', ts=T, open=Decimal('50000'),
              high=Decimal('50000'), low=Decimal('50000'), close=Decimal('50000'), volume=Decimal('1'))
    return await s.on_bar(bar)


async def test_refuses_to_trade_without_a_spot_venue():
    assert await run(None) == []


async def test_every_perp_leg_has_an_equal_opposite_spot_leg():
    intents = await run('binance')
    assert len(intents) == 2
    perp, spot = intents
    assert perp.symbol == PERP and perp.target.position < 0
    assert spot.symbol == 'BTC/USDT' and spot.venue == 'binance'
    assert spot.target.position == -perp.target.position
