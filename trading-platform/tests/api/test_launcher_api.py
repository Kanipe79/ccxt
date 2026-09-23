"""The launcher over HTTP: what the dashboard's Start / Stop buttons actually call."""
from __future__ import annotations

import asyncio

import httpx

from uxtrader.api.main import create_app
from uxtrader.app import Supervisor
from uxtrader.bus import InMemoryBus

AUTH = {'Authorization': 'Bearer t0k'}
DEMO = {'mode': 'demo', 'speed': 0.002,
        'strategies': [{'id': 'DEMO', 'risk_budget': 0.2, 'stage': 4}]}


async def client():
    bus = InMemoryBus(roundtrip=False)
    sup = Supervisor(bus)
    app = create_app(bus, token='t0k', launcher=sup)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t'), ctx, sup


async def test_start_requires_the_token():
    c, ctx, sup = await client()
    assert (await c.post('/api/launcher/start', json=DEMO)).status_code == 401
    assert sup.state == 'stopped'
    await ctx.__aexit__(None, None, None)


async def test_catalog_state_start_and_stop_round_trip():
    c, ctx, sup = await client()
    cat = (await c.get('/api/launcher')).json()['catalog']
    assert {s['id'] for s in cat['strategies']} >= {'DEMO', 'S4', 'S1'}
    r = await c.post('/api/launcher/start', json=DEMO, headers=AUTH)
    assert r.status_code == 200 and r.json()['state'] == 'running'
    for _ in range(300):
        state = (await c.get('/api/state')).json()
        if state['snapshot'] and state['strategies'] and state['symbols']:
            break
        await asyncio.sleep(0.02)
    assert state['launcher']['mode'] == 'demo' and state['launcher']['time_scale'] > 1
    assert state['strategies'][0]['cls'] == 'DemoCrossover'
    assert state['risk']['limits']['daily_loss'] == 0.03
    candles = (await c.get('/api/candles', params={'symbol': state['symbols'][0]})).json()
    assert candles and len(candles[0]) == 6
    r = await c.post('/api/launcher/stop', json={'flatten': True}, headers=AUTH)
    assert r.json()['state'] == 'stopped'
    await ctx.__aexit__(None, None, None)


async def test_invalid_launch_is_a_400_with_the_reason():
    c, ctx, _ = await client()
    bad = {'mode': 'live', 'strategies': [{'id': 'S4', 'risk_budget': 0.1}]}
    r = await c.post('/api/launcher/start', json=bad, headers=AUTH)
    assert r.status_code == 400 and 'LIVE' in r.json()['detail']
    await ctx.__aexit__(None, None, None)


async def test_dashboard_assets_are_served():
    c, ctx, _ = await client()
    assert 'app.js' in (await c.get('/')).text
    for asset in ('/static/app.js', '/static/charts.js', '/static/app.css'):
        assert (await c.get(asset)).status_code == 200
    await ctx.__aexit__(None, None, None)
