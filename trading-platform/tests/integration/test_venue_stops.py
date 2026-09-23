"""Kill-switch layer 3 through the full service stack (paper venue)."""
from __future__ import annotations

from decimal import Decimal

from uxtrader.strategy import StrategyBase
from uxtrader.types import Fill, StopSpec

from test_services_e2e import SYM, Platform, spec


class Protected(StrategyBase):
    """Long 0.1 with a stop at 49,000 on 5m bar #1; trails the stop to 49,500 on #2."""
    name = 'protected'
    timeframe = '5m'
    SIDE = 1

    def __init__(self, ctx):
        super().__init__(ctx)
        self.n = 0

    async def on_bar(self, bar):
        self.n += 1
        if self.n == 1:
            px = Decimal('49000') if self.SIDE > 0 else Decimal('51000')
            return [self.target(bar.symbol, Decimal('0.1') * self.SIDE, reason='enter',
                                stop=StopSpec(price=px))]
        if self.n == 2 and self.SIDE > 0:
            return [self.target(bar.symbol, self.ctx.position(bar.symbol), reason='trail',
                                stop=StopSpec(price=Decimal('49500')))]
        return []


def stops(pf):
    return pf.execution.stops.live


async def test_stop_is_placed_wider_than_the_software_stop():
    pf = await Platform([spec(cls=Protected)]).start()
    await pf.minutes(5, 50000)
    live = stops(pf)[('paperx', SYM, 'protected')]
    assert live.side == 'sell' and live.amount == Decimal('0.1')
    assert live.price == Decimal('49000') * Decimal('0.995')     # 0.5% buffer
    assert live.order.reduce_only


async def test_trail_intent_moves_the_venue_stop_without_trading():
    pf = await Platform([spec(cls=Protected)]).start()
    await pf.minutes(5, 50000)
    fills_before = len(pf.bus.of_type(Fill))
    await pf.minutes(5, 50200)
    assert len(pf.bus.of_type(Fill)) == fills_before, 'a stop update must not trade'
    assert stops(pf)[('paperx', SYM, 'protected')].price == Decimal('49500') * Decimal('0.995')
    assert pf.execution.stops.cancelled == 1


async def test_crash_triggers_the_venue_stop_and_flattens_the_book():
    pf = await Platform([spec(cls=Protected)]).start()
    await pf.minutes(5, 50000)
    await pf.minute_bar(48000)                    # through 48,755 trigger
    assert pf.book('protected') == 0
    stop_fill = pf.bus.of_type(Fill)[-1]
    assert stop_fill.strategy == 'protected' and stop_fill.side == 'sell'
    assert stops(pf) == {}


async def test_exit_cancels_the_stop():
    pf = await Platform([spec(cls=Protected)]).start()
    await pf.minutes(5, 50000)
    await pf.bus.publish('control.flatten', __import__('uxtrader.events', fromlist=['Control'])
                         .Control(command='flatten', reason='t'))
    assert pf.book('protected') == 0
    assert stops(pf) == {}


async def test_offsetting_books_get_plain_not_reduce_only_stops():
    """Net short (the other book) means a reduce-only SELL for the long book would be
    rejected by the venue — so it must be placed as a plain order."""
    from test_services_e2e import FiveMinuteToggle
    pf = await Platform([spec(cls=Protected, name='longbook'),
                         spec(cls=FiveMinuteToggle, name='shortbook', SIDE=-1, SIZE=0.3)]).start()
    await pf.minutes(5, 50000)
    live = stops(pf)[('paperx', SYM, 'longbook')]
    assert not live.order.reduce_only
