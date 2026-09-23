"""The launcher: one long-lived process that serves the dashboard and starts, stops and
supervises the trading platform inside itself.

    ux                      # or: python -m uxtrader.cli
    → opens http://127.0.0.1:8765 — pick a mode, pick strategies, press Start.

Modes
  demo   synthetic market, simulated fills. No keys, no network. Runs anywhere.
  paper  REAL market data from the venue, simulated fills. Needs network, no keys.
  live   real orders. Needs keys in env vars, and the operator must type LIVE.

Each run gets its own ``ScopedBus`` so stopping it leaves nothing subscribed; the API
and the journal live for the whole process, so history survives restarts of the bot.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .bus import InMemoryBus, ScopedBus
from .clock import LiveClock
from .events import Control, md_subject
from .strategies import REGISTRY
from .strategies.indicators import EMA
from .strategy import StrategyBase
from .types import Bar, _timeframe_delta

log = logging.getLogger(__name__)

DEMO_VENUE = 'demo'
PERPS = ('BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT')
START_PRICE = {'BTC/USDT:USDT': 64000.0, 'ETH/USDT:USDT': 3200.0, 'SOL/USDT:USDT': 150.0}

# What each strategy needs that the launcher can or cannot supply today. Strategies
# whose inputs nothing produces yet are shown, but cannot be started: a strategy that
# silently never trades is worse than one that is honestly unavailable.
CATALOG: dict[str, dict[str, Any]] = {
    'S4': {'launchable': True, 'budget': 0.20, 'symbols': list(PERPS), 'tag': 'Trend',
           'name': 'Donchian trend',
           'blurb': 'Buys 55-bar breakouts in the direction of the 200-day trend, with an ATR stop '
                    'that trails once in profit. Wrong two trades in three; the winners pay for it.',
           'needs': 'OHLCV (+ funding, optional)'},
    'S6': {'launchable': True, 'budget': 0.03, 'symbols': ['BTC/USDT:USDT'], 'tag': 'Range',
           'name': 'Adaptive grid',
           'blurb': 'Buys dips and sells rips around a moving centre while the market is ranging. '
                    'Short volatility: four hard kill switches end the grid the moment a trend starts.',
           'needs': 'OHLCV (+ funding, optional)'},
    'S5': {'launchable': True, 'budget': 0.05, 'symbols': ['BTC/USDT:USDT', 'ETH/USDT:USDT'],
           'tag': 'Mean reversion', 'name': 'VWAP mean reversion',
           'blurb': 'Fades stretched moves away from session VWAP, only in a mean-reverting regime '
                    'and only as a maker. Needs a live order book, so it idles in demo.',
           'needs': 'OHLCV + live order book (paper/live only)'},
    'S1': {'launchable': False, 'budget': 0.35, 'symbols': ['BTC/USDT:USDT'], 'tag': 'Carry',
           'name': 'Funding carry',
           'blurb': 'Short perp, long spot: collects funding paid by leveraged longs while staying '
                    'delta-neutral.',
           'needs': 'a spot venue for the hedge leg + a basis/open-interest feed'},
    'S2': {'launchable': False, 'budget': 0.20, 'symbols': [], 'tag': 'Momentum',
           'name': 'Cross-sectional momentum',
           'blurb': 'Weekly long/short rotation across 40 liquid perps, ranked by vol-scaled '
                    '30- and 90-day returns.',
           'needs': 'a point-in-time universe and regime job'},
    'S3': {'launchable': False, 'budget': 0.15, 'symbols': [], 'tag': 'Stat-arb',
           'name': 'Cointegrated pairs',
           'blurb': 'Trades the spread between assets that move together, with a Kalman hedge ratio '
                    'and a blacklist for relationships that break.',
           'needs': 'a qualified pair list from a cointegration screen'},
    'S7': {'launchable': False, 'budget': 0.02, 'symbols': list(PERPS), 'tag': 'Forced flow',
           'name': 'Squeeze fade',
           'blurb': 'Fades liquidation cascades once crowded funding and open interest unwind.',
           'needs': 'an open-interest and liquidation feed'},
}


class MarketClock:
    """Demo time. The synthetic market runs many times faster than the wall clock, so
    every service in a demo run shares this clock: fills, snapshots, candles and
    decisions then sit on one consistent timeline."""

    def __init__(self, start: datetime, speed: float) -> None:
        self._now = start
        self.speed = speed

    def now(self) -> datetime:
        return self._now

    def advance_to(self, ts: datetime) -> None:
        if ts > self._now:
            self._now = ts

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self.speed / 60)


class DemoCrossover(StrategyBase):
    """Demo only: an EMA crossover on 1-minute bars so the dashboard has something to
    show within seconds. It has no edge and is not one of S1–S7."""
    name = 'demo_crossover'
    timeframe = '1m'
    warmup_bars = 20
    FAST = 5
    SLOW = 20
    SIZE_PCT = 0.25

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
        return [self.target(bar.symbol, qty * want,
                            reason=f'EMA{self.FAST} {"above" if want > 0 else "below"} EMA{self.SLOW}')]


def _first_paragraph(obj) -> str:
    doc = inspect.getdoc(obj) or ''
    return doc.split('\n\n')[0].replace('\n', ' ').strip()


class SyntheticMarket:
    """Regime-switching random walk: trends, chop and the occasional shock, so every
    kind of strategy behaviour shows up in a few minutes of demo."""

    def __init__(self, symbols: tuple[str, ...], seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self.price = {s: START_PRICE.get(s, 100.0) for s in symbols}
        self.drift = {s: 0.0 for s in symbols}
        self.vol = {s: 0.0012 for s in symbols}

    def step(self, symbol: str, minutes: float = 1.0) -> tuple[float, float, float, float]:
        r = self.rng
        if r.random() < 0.004 * minutes:
            self.drift[symbol] = r.choice([-1, 0, 0, 1]) * 0.00025
            self.vol[symbol] = r.choice([0.0008, 0.0012, 0.002])
        o = self.price[symbol]
        ret = self.drift[symbol] * minutes + self.vol[symbol] * math.sqrt(minutes) * r.gauss(0, 1)
        if r.random() < 0.0008 * minutes:
            ret += r.choice([-1, 1]) * 0.02            # shock
        c = o * math.exp(ret)
        wick = abs(r.gauss(0, 1)) * self.vol[symbol] * math.sqrt(minutes) * o * 0.5
        self.price[symbol] = c
        return o, max(o, c) + wick, min(o, c) - wick, c

    def history(self, symbol: str, timeframe: str, n: int, end: datetime) -> list[Bar]:
        """n bars at `timeframe`, ending at `end`, finishing at today's price."""
        tf = _timeframe_delta(timeframe)
        minutes = tf.total_seconds() / 60
        saved = self.price[symbol]
        path, p = [], saved
        for _ in range(n):                                  # walk backwards from now
            path.append(p)
            p = p / math.exp(self.rng.gauss(0, 0.0012 * math.sqrt(minutes)))
        path.reverse()
        bars = []
        for i, c in enumerate(path):
            o = path[i - 1] if i else c
            wick = abs(self.rng.gauss(0, 1)) * 0.0006 * math.sqrt(minutes) * o
            bars.append(_bar(symbol, timeframe, end - tf * (n - i), o, max(o, c) + wick,
                             min(o, c) - wick, c))
        self.price[symbol] = saved
        return bars


def _bar(symbol, timeframe, ts, o, h, lo, c, venue=DEMO_VENUE) -> Bar:
    d = lambda x: Decimal(f'{x:.6g}')                       # noqa: E731
    return Bar(venue=venue, symbol=symbol, timeframe=timeframe, ts=ts, open=d(o),
               high=d(h), low=d(lo), close=d(c), volume=Decimal('10'))


class Supervisor:
    def __init__(self, bus: InMemoryBus, *, exchange_factory=None) -> None:
        self.bus = bus
        self.exchange_factory = exchange_factory
        self.state = 'stopped'
        self.mode: str | None = None
        self.venue: str | None = None
        self.error: str | None = None
        self.started_at: datetime | None = None
        self.strategy_names: list[str] = []
        self.on_new_run = None
        self._scope: ScopedBus | None = None
        self._tasks: list[asyncio.Task] = []
        self._exchanges: dict[str, Any] = {}
        self.services: dict[str, Any] = {}

    # -- read ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        clock = getattr(self, 'clock', None)
        market_now = clock.now().isoformat() if isinstance(clock, MarketClock) else None
        return {'state': self.state, 'mode': self.mode, 'venue': self.venue,
                'error': self.error, 'strategies': self.strategy_names,
                'started_at': self.started_at.isoformat() if self.started_at else None,
                'market_time': market_now,
                'time_scale': round(60 / clock.speed) if isinstance(clock, MarketClock) else 1}

    def catalog(self) -> dict[str, Any]:
        from .settings import credentials
        strategies = [{
            'id': 'DEMO', 'name': 'Demo crossover', 'class': 'DemoCrossover', 'tag': 'Demo',
            'description': 'An EMA crossover on 1-minute bars so the dashboard comes alive within '
                           'seconds. It has no edge and exists only for demo mode.',
            'timeframe': '1m', 'launchable': True, 'demo_only': True, 'budget': 0.20,
            'symbols': ['BTC/USDT:USDT', 'ETH/USDT:USDT'], 'needs': 'nothing', 'warmup_bars': 20,
        }]
        for sid in ('S4', 'S6', 'S5', 'S1', 'S2', 'S3', 'S7'):
            cls, meta = REGISTRY[sid], CATALOG[sid]
            strategies.append({
                'id': sid, 'name': meta['name'], 'class': cls.__name__, 'tag': meta['tag'],
                'description': meta['blurb'], 'timeframe': cls.timeframe,
                'launchable': meta['launchable'], 'demo_only': False, 'budget': meta['budget'],
                'symbols': meta['symbols'], 'needs': meta['needs'],
                'warmup_bars': cls.warmup_bars,
            })
        venues = [{'id': v, 'has_credentials': bool(credentials(v))}
                  for v in ('binanceusdm', 'bybit', 'okx', 'hyperliquid')]
        return {'strategies': strategies, 'venues': venues,
                'modes': [
                    {'id': 'demo', 'title': 'Demo', 'blurb': 'Synthetic market, simulated fills. '
                     'No keys, no network — see everything working in seconds.'},
                    {'id': 'paper', 'title': 'Paper', 'blurb': 'Real market data, simulated fills. '
                     'Needs network access to the venue; no keys.'},
                    {'id': 'live', 'title': 'Live', 'blurb': 'Real orders with real money. '
                     'Keys from environment variables, withdrawals disabled.'},
                ]}

    # -- lifecycle ---------------------------------------------------------------

    def _validate(self, req) -> list:
        chosen = [s for s in req.strategies if s.enabled]
        if not chosen:
            raise ValueError('select at least one strategy')
        ids = {s['id']: s for s in self.catalog()['strategies']}
        for s in chosen:
            meta = ids.get(s.id)
            if meta is None:
                raise ValueError(f'unknown strategy {s.id}')
            if not meta['launchable']:
                raise ValueError(f'{s.id} cannot run yet: needs {meta["needs"]}')
            if meta['demo_only'] and req.mode != 'demo':
                raise ValueError('the demo strategy only runs in demo mode')
            if not 0 < s.risk_budget <= 1:
                raise ValueError(f'{s.id}: risk budget must be in (0, 1]')
            if s.stage not in (1, 2, 3, 4):
                raise ValueError(f'{s.id}: stage must be 1–4')
        total = sum(s.risk_budget for s in chosen)
        if total > 1.0 + 1e-9:
            raise ValueError(f'risk budgets sum to {total:.0%}; the maximum is 100%')
        if req.mode == 'live':
            from .settings import credentials
            if req.confirm != 'LIVE':
                raise ValueError('live mode places real orders: type LIVE to confirm')
            if not credentials(req.venue):
                raise ValueError(f'no API keys for {req.venue}: set '
                                 f'{req.venue.upper()}_APIKEY and {req.venue.upper()}_SECRET')
        return chosen

    async def start(self, req) -> None:
        if self.state in ('starting', 'running', 'stopping'):
            raise ValueError(f'already {self.state}')
        chosen = self._validate(req)
        self.state, self.error, self.mode, self.venue = 'starting', None, req.mode, req.venue
        if self.on_new_run:
            self.on_new_run()
        try:
            await self._start(req, chosen)
        except Exception as exc:                              # noqa: BLE001
            log.exception('launch_failed')
            await self._teardown()
            self.state, self.error = 'error', str(exc) or type(exc).__name__
            return
        self.state, self.started_at = 'running', datetime.now(timezone.utc)

    async def _start(self, req, chosen) -> None:
        from .execution.paper import PaperBroker
        from .ops.metrics import MetricsService
        from .risk import RiskEngine, RiskLimits
        from .services import (
            ExecutionService, PortfolioService, RiskService, StrategyEngine, StrategySpec,
        )

        scope = self._scope = ScopedBus(self.bus)
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        clock: Any = MarketClock(start, req.speed) if req.mode == 'demo' else LiveClock()
        self.clock = clock
        venue = DEMO_VENUE if req.mode == 'demo' else req.venue
        specs = []
        for s in chosen:
            cls = DemoCrossover if s.id == 'DEMO' else REGISTRY[s.id]
            symbols = tuple(s.symbols) or tuple(CATALOG.get(s.id, {}).get('symbols', PERPS[:2]))
            specs.append(StrategySpec(cls=cls, risk_budget=s.risk_budget, symbols=symbols,
                                      stage=s.stage, name=cls.name))
        self.strategy_names = [sp.name or sp.cls.name for sp in specs]
        symbols = tuple(sorted({sym for sp in specs for sym in sp.symbols}))

        if req.mode == 'live':
            from .settings import credentials
            from .execution.live import LiveBroker
            ex = await self._exchange(req.venue, credentials(req.venue))
            broker: Any = LiveBroker({req.venue: ex}, clock=clock)
        else:
            holder: dict[str, Any] = {}
            broker = PaperBroker(clock, lambda v, s: holder['svc'].book_for(v, s))

        equity = Decimal(str(req.starting_equity))
        portfolio = PortfolioService(scope, equity, clock=clock)
        risk = RiskService(scope, RiskEngine(RiskLimits()), clock=clock)
        execution = ExecutionService(scope, broker, default_venue=venue, clock=clock)
        if req.mode != 'live':
            holder['svc'] = execution
        engine = StrategyEngine(scope, specs, clock=clock, starting_equity=equity)
        metrics = MetricsService(scope)
        self.services = {'portfolio': portfolio, 'risk': risk, 'execution': execution,
                         'engine': engine, 'metrics': metrics}

        if req.mode == 'live':
            problems = await execution.oms.reconcile_on_start([req.venue], local_positions={})
            if problems:
                raise RuntimeError('startup reconciliation failed: ' + '; '.join(problems))

        for svc in (portfolio, risk, execution, engine, metrics):
            await svc.start()

        if req.mode == 'demo':
            market = SyntheticMarket(symbols)
            now = start
            for h in engine.hosted.values():
                strat = h.strategy
                if strat.timeframe == '1m':
                    continue
                for sym in strat.symbols:
                    await engine.warmup(market.history(sym, strat.timeframe,
                                                       strat.warmup_bars + 5, now))
            for h in engine.hosted.values():
                h.bars_seen = max(h.bars_seen, 0)
                await engine.publish_status(h, force=True)
            self._tasks.append(asyncio.create_task(
                self._demo_feed(scope, market, symbols, now, req.speed, clock), name='demo-feed'))
        else:
            from .data.ingest import IngestService
            ex = self._exchanges.get(req.venue) or await self._exchange(req.venue, {})
            ingest = IngestService(ex, scope, list(symbols))
            self.services['ingest'] = ingest
            from .run import _warmup
            await _warmup(engine, {req.venue: ex})
            await ingest.start()
            if req.mode == 'live':
                self._tasks.append(asyncio.create_task(broker.run_user_stream(req.venue)))

        from .services.common import heartbeat_loop
        self._tasks.append(asyncio.create_task(heartbeat_loop(scope, f'bot-{req.mode}')))
        log.warning('launcher_started mode=%s venue=%s strategies=%s',
                    req.mode, venue, self.strategy_names)

    async def _exchange(self, venue: str, creds: dict) -> Any:
        factory = self.exchange_factory
        if factory is None:
            from uxcore import make_exchange
            factory = make_exchange
        ex = factory(venue, creds)
        await ex.setup()
        self._exchanges[venue] = ex
        return ex

    async def _demo_feed(self, bus, market: SyntheticMarket, symbols, start: datetime,
                         speed: float, clock: MarketClock) -> None:
        ts = start
        while True:
            clock.advance_to(ts + timedelta(minutes=1))        # the bar closes, time moves
            for sym in symbols:
                o, h, lo, c = market.step(sym)
                await bus.publish(md_subject(DEMO_VENUE, 'bar', sym),
                                  _bar(sym, '1m', ts, o, h, lo, c))
            ts += timedelta(minutes=1)
            await asyncio.sleep(max(speed, 0.005))

    async def stop(self, flatten: bool = True) -> None:
        if self.state not in ('running', 'error', 'starting'):
            return
        self.state = 'stopping'
        if flatten and self._scope is not None:
            await self._scope.publish('control.flatten',
                                      Control(command='flatten', reason='bot stopped'))
            portfolio = self.services.get('portfolio')
            for _ in range(100):                   # live fills arrive asynchronously
                if portfolio is None or not portfolio.portfolio.all_positions():
                    break
                await asyncio.sleep(0.1)
        await self._teardown()
        self.state = 'stopped'
        self.started_at = None

    async def _teardown(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        ingest = self.services.get('ingest')
        if ingest is not None:
            await ingest.stop()
        lock = getattr(self.services.get('execution'), 'lock', None)
        if lock is not None:
            await lock.release()
        for ex in self._exchanges.values():
            try:
                await ex.close()
            except Exception:                         # noqa: BLE001
                pass
        self._exchanges.clear()
        if self._scope is not None:
            await self._scope.close()
            self._scope = None
        self.services = {}
