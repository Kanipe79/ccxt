"""Demo mode: the full platform on a SYNTHETIC feed, with the dashboard.

    python -m uxtrader.demo --port 8765 --token demo
    open http://localhost:8765

Everything real except the market: portfolio, risk, paper execution, strategy engine,
alerts, metrics and the API all run exactly as in paper mode. The price is a random
walk and the strategy is a toy EMA crossover.

**The demo strategy has no edge and is not one of S1–S7.** It exists to make fills,
books and risk events happen quickly enough to see the dashboard working.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .bus import InMemoryBus
from .clock import LiveClock
from .events import md_subject
from .execution.paper import PaperBroker
from .ops.alerts import AlertService, log_sink
from .ops.metrics import MetricsService
from .services import (
    ExecutionService, PortfolioService, RiskService, StrategyEngine, StrategySpec,
)
from .strategies.indicators import EMA
from .strategy import StrategyBase
from .types import Bar

VENUE = 'demo'
SYMBOLS = ('BTC/USDT:USDT', 'ETH/USDT:USDT')


class DemoCrossover(StrategyBase):
    """UI demo only — an EMA crossover on synthetic data. No edge, not for trading."""
    name = 'demo_crossover'
    timeframe = '1m'
    FAST = 5
    SLOW = 20
    SIZE_PCT = 0.05

    def __init__(self, ctx):
        super().__init__(ctx)
        self._fast: dict[str, EMA] = {}
        self._slow: dict[str, EMA] = {}

    async def on_bar(self, bar):
        f = self._fast.setdefault(bar.symbol, EMA(int(self.FAST))).push(float(bar.close))
        s = self._slow.setdefault(bar.symbol, EMA(int(self.SLOW))).push(float(bar.close))
        if f is None or s is None:
            return []
        want = 1 if f > s else -1
        pos = self.ctx.position(bar.symbol)
        if pos != 0 and (pos > 0) == (want > 0):
            return []
        qty = self.ctx.sleeve_equity * Decimal(str(self.SIZE_PCT)) / bar.close
        return [self.target(bar.symbol, qty * want, reason=f'demo cross fast={f:.2f} slow={s:.2f}')]


async def feed(bus, interval: float, seed: int) -> None:
    rng = random.Random(seed)
    price = {'BTC/USDT:USDT': 60000.0, 'ETH/USDT:USDT': 3000.0}
    ts = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=1)
    while True:
        for sym in SYMBOLS:
            o = price[sym]
            c = o * (1 + rng.gauss(0, 0.0025))
            price[sym] = c
            hi, lo = max(o, c) * (1 + abs(rng.gauss(0, 0.0008))), min(o, c) * (1 - abs(rng.gauss(0, 0.0008)))
            d = lambda x: Decimal(f'{x:.2f}')                          # noqa: E731
            await bus.publish(md_subject(VENUE, 'bar', sym), Bar(
                venue=VENUE, symbol=sym, timeframe='1m', ts=ts, open=d(o), high=d(hi),
                low=d(lo), close=d(c), volume=Decimal('10')))
        ts += timedelta(minutes=1)
        await asyncio.sleep(interval)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--token', default='demo', help='API token for the demo controls')
    ap.add_argument('--interval', type=float, default=0.5, help='seconds per synthetic minute')
    ap.add_argument('--seed', type=int, default=7)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    import uvicorn

    from .api.main import create_app

    bus = InMemoryBus()
    clock = LiveClock()
    holder = {}
    broker = PaperBroker(clock, lambda v, s: holder['svc'].book_for(v, s))
    execution = ExecutionService(bus, broker, default_venue=VENUE, clock=clock)
    holder['svc'] = execution
    specs = [
        StrategySpec(cls=DemoCrossover, risk_budget=0.5, symbols=SYMBOLS, stage=4, name='demo_fast'),
        StrategySpec(cls=DemoCrossover, risk_budget=0.5, symbols=('BTC/USDT:USDT',), stage=4,
                     name='demo_slow', params={'fast': 12, 'slow': 48}),
    ]
    services = [PortfolioService(bus, Decimal('100000')), RiskService(bus, clock=clock),
                execution, StrategyEngine(bus, specs, clock=clock, starting_equity=Decimal('100000')),
                AlertService(bus, {'INFO': [], 'WARN': [log_sink], 'CRIT': [log_sink]}),
                MetricsService(bus)]
    app = create_app(bus, token=args.token)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning'))

    async def boot():
        while not server.started:
            await asyncio.sleep(0.05)
        for svc in services:
            await svc.start()
        print(f'demo running → http://127.0.0.1:{args.port}   (API token: {args.token})')
        await feed(bus, args.interval, args.seed)

    await asyncio.gather(server.serve(), boot())
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
