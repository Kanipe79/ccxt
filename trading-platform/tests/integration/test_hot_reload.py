"""Hot reload: new code, same book, internal state carried across."""
from __future__ import annotations

import importlib
import os
import sys
import time
from decimal import Decimal

from uxtrader.bus import InMemoryBus
from uxtrader.services import StrategyEngine, StrategySpec

V1 = """
from decimal import Decimal
from uxtrader.strategy import StrategyBase

class Hot(StrategyBase):
    name = 'hot'
    timeframe = '1m'
    VERSION = 1

    def __init__(self, ctx):
        super().__init__(ctx)
        self._bars_seen = 0

    async def on_bar(self, bar):
        self._bars_seen += 1
        return []
"""


async def test_reload_swaps_code_and_keeps_state(tmp_path, monkeypatch):
    mod = tmp_path / 'hot_strategy_mod.py'
    mod.write_text(V1)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop('hot_strategy_mod', None)
    importlib.invalidate_caches()

    engine = StrategyEngine(InMemoryBus(), [StrategySpec(
        cls='hot_strategy_mod.Hot', risk_budget=0.1, symbols=('S',), stage=4)],
        starting_equity=Decimal('1000'))
    await engine.start()
    h = engine.hosted['hot']
    h.strategy._bars_seen = 41
    ctx_before = h.ctx

    mod.write_text(V1.replace('VERSION = 1', 'VERSION = 2'))
    future = time.time() + 5
    os.utime(mod, (future, future))
    assert await engine.check_reload() == ['hot']

    h2 = engine.hosted['hot']
    assert type(h2.strategy).VERSION == 2, 'new code not loaded'
    assert h2.strategy._bars_seen == 41, 'internal state lost across reload'
    assert h2.ctx is ctx_before, 'book/context must carry over untouched'
    assert await engine.check_reload() == []
