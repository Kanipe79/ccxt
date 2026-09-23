from __future__ import annotations

import asyncio

import pytest

from uxtrader.api.main import LaunchRequest, LaunchStrategy
from uxtrader.app import Supervisor
from uxtrader.bus import InMemoryBus
from uxtrader.types import Fill


def demo(*ids, speed=0.002, **kw):
    budgets = {'DEMO': 0.2, 'S4': 0.2, 'S6': 0.03, 'S5': 0.05}
    return LaunchRequest(mode='demo', speed=speed, strategies=[
        LaunchStrategy(id=i, risk_budget=budgets.get(i, 0.1), stage=4) for i in ids], **kw)


async def wait_for(cond, timeout=10.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_demo_run_trades_then_stops_flat():
    bus = InMemoryBus(roundtrip=False)
    sup = Supervisor(bus)
    await sup.start(demo('DEMO', 'S4'))
    assert sup.status()['state'] == 'running', sup.error
    assert await wait_for(lambda: len(bus.of_type(Fill)) >= 2), 'demo produced no fills'
    engine = sup.services['engine']
    s4 = engine.hosted['donchian_regime']
    assert s4.bars_seen >= s4.strategy.warmup_bars, 'S4 not warmed up from synthetic history'
    portfolio = sup.services['portfolio']
    await sup.stop(flatten=True)
    assert sup.state == 'stopped'
    assert not portfolio.portfolio.all_positions(), 'stop(flatten=True) left positions open'


async def test_restart_leaves_no_orphan_subscriptions():
    bus = InMemoryBus(roundtrip=False)
    sup = Supervisor(bus)
    await sup.start(demo('DEMO'))
    n_running = len(bus._subs)
    await sup.stop(flatten=False)
    assert len(bus._subs) == 0
    await sup.start(demo('DEMO'))
    assert len(bus._subs) == n_running
    await sup.stop(flatten=False)


@pytest.mark.parametrize('req,msg', [
    (demo('S1'), 'cannot run yet'),
    (demo(), 'at least one'),
    (LaunchRequest(mode='paper', strategies=[LaunchStrategy(id='DEMO', risk_budget=0.1)]),
     'only runs in demo'),
    (LaunchRequest(mode='demo', strategies=[LaunchStrategy(id='S4', risk_budget=0.7),
                                            LaunchStrategy(id='DEMO', risk_budget=0.6)]), '100%'),
    (LaunchRequest(mode='live', strategies=[LaunchStrategy(id='S4', risk_budget=0.1)]), 'LIVE'),
    (LaunchRequest(mode='live', confirm='LIVE',
                   strategies=[LaunchStrategy(id='S4', risk_budget=0.1)]), 'no API keys'),
])
async def test_validation(req, msg, monkeypatch):
    for k in list(__import__('os').environ):
        if k.startswith('BINANCEUSDM_'):
            monkeypatch.delenv(k)
    sup = Supervisor(InMemoryBus())
    with pytest.raises(ValueError, match=msg):
        await sup.start(req)
    assert sup.state == 'stopped'


async def test_catalog_is_honest_about_what_can_run():
    cat = Supervisor(InMemoryBus()).catalog()
    by_id = {s['id']: s for s in cat['strategies']}
    assert by_id['S4']['launchable'] and by_id['DEMO']['demo_only']
    assert not by_id['S1']['launchable'] and 'spot venue' in by_id['S1']['needs']
    assert {m['id'] for m in cat['modes']} == {'demo', 'paper', 'live'}


async def test_paper_mode_uses_the_venue_feed_and_a_failed_start_reports_error():
    class NoNetwork:
        id = 'binanceusdm'

        async def setup(self):
            raise ConnectionError('venue unreachable')

        async def close(self):
            pass
    sup = Supervisor(InMemoryBus(), exchange_factory=lambda v, c: NoNetwork())
    await sup.start(LaunchRequest(mode='paper',
                                  strategies=[LaunchStrategy(id='S4', risk_budget=0.2)]))
    assert sup.state == 'error' and 'unreachable' in sup.error
    assert sup.services == {}
