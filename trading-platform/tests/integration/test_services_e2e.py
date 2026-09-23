"""The whole platform over the bus: market data → strategy engine → risk → execution
(paper) → fills → portfolio → snapshots back to risk and strategies.

Same service classes that run as separate containers on NATS, here on one
InMemoryBus so the test is deterministic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal


from uxtrader.bus import InMemoryBus
from uxtrader.events import (
    CONTROL_KILL, CONTROL_REARM, FEED_RECOVERED, FEED_STALE, Control, FeedRecovered,
    PortfolioSnapshot, StaleFeed, md_subject,
)
from uxtrader.execution.paper import PaperBroker
from uxtrader.clock import LiveClock
from uxtrader.services import (
    ExecutionService, PortfolioService, RiskService, StrategyEngine, StrategySpec,
)
from uxtrader.strategy import StrategyBase
from uxtrader.types import Bar, Fill, RiskDecision

SYM = 'BTC/USDT:USDT'
VENUE = 'paperx'
T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


class FiveMinuteToggle(StrategyBase):
    """Trades on 5m bars built by the engine's resampler from 1m input.
    Wants long on 5m bar #1, flat on #3. The target is set via params."""
    name = 'toggle'
    timeframe = '5m'
    SIZE = 0.1
    SIDE = 1

    def __init__(self, ctx):
        super().__init__(ctx)
        self.n = 0
        self.seen_positions: list[Decimal] = []

    async def on_bar(self, bar):
        self.n += 1
        self.seen_positions.append(self.ctx.position(bar.symbol))
        if self.n == 1:
            return [self.target(bar.symbol, Decimal(str(self.SIZE * self.SIDE)), reason='on')]
        if self.n == 3:
            return [self.target(bar.symbol, Decimal('0'), reason='off')]
        return []


class Platform:
    def __init__(self, specs):
        self.bus = InMemoryBus()
        self.portfolio = PortfolioService(self.bus, Decimal('100000'))
        self.risk = RiskService(self.bus)
        holder = {}
        broker = PaperBroker(LiveClock(), lambda v, s: holder['svc'].book_for(v, s))
        self.execution = ExecutionService(self.bus, broker, default_venue=VENUE)
        holder['svc'] = self.execution
        self.engine = StrategyEngine(self.bus, specs, starting_equity=Decimal('100000'))
        self.minute = 0

    async def start(self):
        for svc in (self.portfolio, self.risk, self.execution, self.engine):
            await svc.start()
        return self

    async def minute_bar(self, price: float, symbol: str = SYM):
        ts = T0 + timedelta(minutes=self.minute)
        self.minute += 1
        p = Decimal(str(price))
        await self.bus.publish(md_subject(VENUE, 'bar', symbol), Bar(
            venue=VENUE, symbol=symbol, timeframe='1m', ts=ts, open=p, high=p, low=p,
            close=p, volume=Decimal('1')))

    async def minutes(self, n: int, price: float):
        for _ in range(n):
            await self.minute_bar(price)

    def book(self, strategy: str) -> Decimal:
        snap = self.risk.snapshot
        return sum((p.quantity for p in snap.positions if p.strategy == strategy), Decimal('0'))


def spec(cls=FiveMinuteToggle, name=None, **params):
    return StrategySpec(cls=cls, risk_budget=0.2, symbols=(SYM,), stage=4,
                        name=name, params=params)


async def test_round_trip_through_every_service():
    pf = await Platform([spec()]).start()
    await pf.minutes(5, 50000)                       # 5m bar #1 closes → entry
    assert pf.book('toggle') == Decimal('0.1')
    fills = pf.bus.of_type(Fill)
    assert len(fills) == 1 and fills[0].strategy == 'toggle'
    assert fills[0].price > Decimal('50000')         # paid the synthetic spread

    await pf.minutes(5, 50500)                       # bar #2: holds
    toggle = pf.engine.hosted['toggle'].strategy
    assert toggle.seen_positions[-1] == Decimal('0.1'), 'strategy view not synced'

    await pf.minutes(5, 51000)                       # bar #3: exit
    assert pf.book('toggle') == 0
    attr = pf.portfolio.portfolio.attribution()['toggle']
    assert attr['net_pnl'] > 0 and attr['fees'] > 0


async def test_two_strategies_same_symbol_keep_separate_books():
    pf = await Platform([spec(name='longer', SIZE=0.1, SIDE=1),
                         spec(name='shorter', SIZE=0.05, SIDE=-1)]).start()
    await pf.minutes(5, 50000)
    assert pf.book('longer') == Decimal('0.1')
    assert pf.book('shorter') == Decimal('-0.05')
    # The venue sees only the net, and reconciliation agrees with it.
    assert await pf.portfolio.reconcile(pf.execution.broker, [VENUE]) == []


async def test_operator_kill_flattens_and_blocks_entries():
    pf = await Platform([spec()]).start()
    await pf.minutes(5, 50000)
    assert pf.book('toggle') == Decimal('0.1')
    await pf.bus.publish(CONTROL_KILL, Control(command='kill', reason='test'))
    assert pf.book('toggle') == 0

    pf.engine.hosted['toggle'].strategy.n = 0        # make it try to enter again
    await pf.minutes(5, 50000)
    assert pf.book('toggle') == 0
    assert any(not d.approved for d in pf.bus.of_type(RiskDecision))

    await pf.bus.publish(CONTROL_REARM, Control(command='rearm', reason='reviewed'))
    pf.engine.hosted['toggle'].strategy.n = 0
    await pf.minutes(5, 50000)
    assert pf.book('toggle') == Decimal('0.1')


async def test_stale_feed_blocks_entries_until_recovered():
    pf = await Platform([spec()]).start()
    await pf.bus.publish(FEED_STALE, StaleFeed(key=f'{VENUE}|{SYM}|book', venue=VENUE,
                                               symbol=SYM, age_s=9))
    await pf.minutes(5, 50000)
    assert pf.book('toggle') == 0
    await pf.bus.publish(FEED_RECOVERED, FeedRecovered(key=f'{VENUE}|{SYM}|book',
                                                       venue=VENUE, symbol=SYM))
    pf.engine.hosted['toggle'].strategy.n = 0
    await pf.minutes(5, 50000)
    assert pf.book('toggle') == Decimal('0.1')


async def test_daily_loss_flattens_the_book_without_any_strategy_asking():
    pf = await Platform([spec(SIZE=1.0)]).start()     # 1 BTC ≈ 50% of equity... capped
    await pf.minutes(5, 50000)
    held = pf.book('toggle')
    assert held > 0
    # Crash: a −20% move on the (capped, 20%-of-equity) position ≈ −4% of equity.
    await pf.minute_bar(40000)
    assert pf.book('toggle') == 0, 'risk did not flatten on the daily-loss breach'
    assert pf.risk.engine.entries_halted_until is not None


async def test_no_snapshot_means_no_trading():
    """Fail closed: before the portfolio has published anything, risk vetoes."""
    bus = InMemoryBus()
    risk = RiskService(bus)
    await risk.start()
    from uxtrader.types import Intent, TargetSpec
    await bus.publish('intent.x', Intent(strategy='x', symbol=SYM,
                                         target=TargetSpec(position=Decimal('1')),
                                         reason='t').with_id())
    decisions = bus.of_type(RiskDecision)
    assert decisions and not decisions[0].approved


async def test_snapshots_are_monotonic():
    pf = await Platform([spec()]).start()
    await pf.minutes(12, 50000)
    seqs = [s.seq for s in pf.bus.of_type(PortfolioSnapshot)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
