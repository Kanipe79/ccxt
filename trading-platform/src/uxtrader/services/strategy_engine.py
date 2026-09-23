"""Strategy engine — hosts strategies, feeds them data, publishes their intents.

* Resamples 1m bars into each strategy's timeframe (``data/resample.py``).
* Keeps each ``StrategyContext`` in sync with portfolio snapshots.
* Warms strategies up on history with intents discarded, so a restart does not
  wait 200 days for a 200-day EMA.
* Stages capital: stage 1/2/3/4 → 10/25/50/100% of the configured risk budget
  (docs/03 §4). Demotion is a config change, not a code change.
* Hot-reloads strategy modules without touching positions.
"""
from __future__ import annotations

import copy
import importlib
import inspect
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..bus import Bus
from ..clock import Clock, LiveClock
from ..data.resample import Resampler
from ..events import PortfolioSnapshot, StaleFeed, intent_subject
from ..strategy import StrategyBase, StrategyContext
from ..types import Bar, BookSnapshot, Fill, Funding, Intent, TradeTick

log = logging.getLogger(__name__)

STAGE_FRACTION = {1: 0.10, 2: 0.25, 3: 0.50, 4: 1.00}


@dataclass
class StrategySpec:
    cls: type[StrategyBase] | str
    risk_budget: float
    symbols: tuple[str, ...] = ()
    stage: int = 1
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    name: str | None = None                 # override cls.name (two instances of one class)

    def resolve(self) -> type[StrategyBase]:
        if isinstance(self.cls, str):
            module, _, attr = self.cls.rpartition('.')
            self.cls = getattr(importlib.import_module(module), attr)
        return self.cls


@dataclass
class _Hosted:
    spec: StrategySpec
    strategy: StrategyBase
    ctx: StrategyContext
    resampler: Resampler | None
    module_file: str | None
    module_mtime: float


class StrategyEngine:
    def __init__(self, bus: Bus, specs: Iterable[StrategySpec], *,
                 clock: Clock | None = None, starting_equity: Decimal = Decimal('0')) -> None:
        self.bus = bus
        self.clock = clock or LiveClock()
        self.starting_equity = starting_equity
        self._specs = [s for s in specs if s.enabled]
        self.hosted: dict[str, _Hosted] = {}
        self.warming = False
        self.intents_published = 0

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        for spec in self._specs:
            await self._host(spec)
        await self.bus.subscribe('md.>', self._on_md)
        await self.bus.subscribe('portfolio.snapshot', self._on_snapshot)
        await self.bus.subscribe('fill.>', self._on_fill)
        await self.bus.subscribe('feed.stale', self._on_stale)

    async def _host(self, spec: StrategySpec, state: dict | None = None) -> None:
        cls = spec.resolve()
        name = spec.name or cls.name
        budget = spec.risk_budget * STAGE_FRACTION.get(spec.stage, 0.10)
        prev = self.hosted.get(name)
        ctx = prev.ctx if prev else StrategyContext(
            strategy=name, clock=self.clock, equity=self.starting_equity,
            risk_budget=budget, positions={}, marks={}, params=spec.params)
        ctx.risk_budget = budget
        attrs = {'name': name}
        if spec.symbols:
            attrs['symbols'] = tuple(spec.symbols)
        # Per-instance subclass: sets name/symbols/params without mutating the class.
        bound = type(cls.__name__, (cls,), {**attrs, **{k.upper(): v for k, v in spec.params.items()
                                                     if hasattr(cls, k.upper())}})
        strategy = bound(ctx)
        await strategy.on_start()
        if state:
            restore_state(strategy, state)
        module_file = inspect.getsourcefile(cls)
        self.hosted[name] = _Hosted(
            spec=spec, strategy=strategy, ctx=ctx,
            resampler=Resampler(cls.timeframe) if cls.timeframe != '1m' else None,
            module_file=module_file,
            module_mtime=os.path.getmtime(module_file) if module_file else 0.0)
        log.info('strategy_hosted name=%s stage=%s budget=%.4f symbols=%s',
                 name, spec.stage, budget, list(strategy.symbols))

    async def warmup(self, bars: Iterable[Bar]) -> int:
        """Replay history through every strategy with intents discarded."""
        self.warming = True
        n = 0
        try:
            for bar in bars:
                await self._dispatch_bar(bar)
                n += 1
        finally:
            self.warming = False
        return n

    async def check_reload(self) -> list[str]:
        """Re-import any strategy whose source changed. Positions are untouched: the
        context (and therefore the book) carries over, and internal state is restored
        where the new code still recognises it."""
        reloaded = []
        for name, h in list(self.hosted.items()):
            if not h.module_file or not os.path.exists(h.module_file):
                continue
            mtime = os.path.getmtime(h.module_file)
            if mtime <= h.module_mtime:
                continue
            state = snapshot_state(h.strategy)
            await h.strategy.on_stop('reload')
            module = importlib.reload(importlib.import_module(h.spec.resolve().__module__))
            h.spec.cls = getattr(module, h.spec.resolve().__name__)
            await self._host(h.spec, state)
            reloaded.append(name)
            log.warning('strategy_reloaded name=%s', name)
        return reloaded

    # -- data in ---------------------------------------------------------------

    async def _on_md(self, _subject: str, msg) -> None:
        if isinstance(msg, Bar):
            await self._dispatch_bar(msg)
            return
        for h in self.hosted.values():
            if msg.symbol not in h.strategy.symbols:
                continue
            if isinstance(msg, TradeTick):
                await self._emit(h, await h.strategy.on_trade(msg))
            elif isinstance(msg, BookSnapshot):
                await self._emit(h, await h.strategy.on_book(msg))
            elif isinstance(msg, Funding):
                await self._emit(h, await h.strategy.on_funding(msg))

    async def _dispatch_bar(self, bar: Bar) -> None:
        for h in self.hosted.values():
            if bar.symbol not in h.strategy.symbols:
                continue
            h.ctx._marks[bar.symbol] = bar.close                 # noqa: SLF001
            if bar.timeframe == h.strategy.timeframe:
                await self._emit(h, await h.strategy.on_bar(bar))
            elif h.resampler is not None and bar.timeframe == '1m':
                for agg in h.resampler.push(bar):
                    await self._emit(h, await h.strategy.on_bar(agg))

    async def _emit(self, h: _Hosted, intents: list[Intent]) -> None:
        if self.warming or not intents:
            return
        for intent in intents:
            if intent.strategy != h.ctx.strategy:
                intent = intent.model_copy(update={'strategy': h.ctx.strategy})
            await self.bus.publish(intent_subject(h.ctx.strategy), intent)
            self.intents_published += 1

    async def _on_snapshot(self, _subject: str, snap) -> None:
        if not isinstance(snap, PortfolioSnapshot):
            return
        for h in self.hosted.values():
            h.ctx.equity = snap.equity
            h.ctx._positions = {p.symbol: p for p in snap.positions   # noqa: SLF001
                                if p.strategy == h.ctx.strategy}
            h.ctx._marks.update(snap.marks)                          # noqa: SLF001

    async def _on_fill(self, _subject: str, fill) -> None:
        if isinstance(fill, Fill) and fill.strategy in self.hosted:
            await self.hosted[fill.strategy].strategy.on_fill(fill)

    async def _on_stale(self, _subject: str, msg) -> None:
        if not isinstance(msg, StaleFeed):
            return
        for h in self.hosted.values():
            if msg.symbol in h.strategy.symbols:
                await self._emit(h, await h.strategy.on_stale(msg.key, msg.age_s))


_SKIP = {'ctx', '_ready'}


def snapshot_state(strategy: StrategyBase) -> dict[str, Any]:
    """Deep copy of the strategy's private state. Unpicklable members are skipped."""
    out: dict[str, Any] = {}
    for k, v in vars(strategy).items():
        if k in _SKIP:
            continue
        try:
            out[k] = copy.deepcopy(v)
        except Exception:                                # noqa: BLE001
            log.warning('reload_state_skipped attr=%s', k)
    return out


def restore_state(strategy: StrategyBase, state: dict[str, Any]) -> None:
    for k, v in state.items():
        if hasattr(strategy, k):          # only attributes the new code still defines
            setattr(strategy, k, v)
