from __future__ import annotations

import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from uxtrader.api.main import create_app
from uxtrader.events import Control

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integration'))
from test_services_e2e import Platform, spec  # noqa: E402


async def client_for(bus, token='s3cret'):
    app = create_app(bus, token=token)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url='http://t'), ctx


async def test_views_follow_the_bus():
    pf = await Platform([spec()]).start()
    client, ctx = await client_for(pf.bus)
    await pf.minutes(5, 50000)
    snap = (await client.get('/api/snapshot')).json()
    assert snap['snapshot']['positions'][0]['strategy'] == 'toggle'
    assert len(snap['equity_curve']) >= 5
    fills = (await client.get('/api/fills')).json()
    assert fills[0]['slippage_bps'] > 0
    assert (await client.get('/')).text.startswith('<!doctype html>')
    await ctx.__aexit__(None, None, None)


async def test_control_requires_the_token():
    pf = await Platform([spec()]).start()
    client, ctx = await client_for(pf.bus)
    body = {'command': 'kill', 'reason': 'drill'}
    assert (await client.post('/api/control', json=body)).status_code == 401
    assert (await client.post('/api/control', json=body,
                              headers={'Authorization': 'Bearer wrong'})).status_code == 401
    ok = await client.post('/api/control', json=body, headers={'Authorization': 'Bearer s3cret'})
    assert ok.status_code == 200
    assert not pf.risk.engine.kill.global_armed, 'kill did not reach the risk service'
    assert (await client.get('/api/snapshot')).json()['kill_active'] is True
    await ctx.__aexit__(None, None, None)


async def test_control_disabled_without_configured_token():
    pf = await Platform([spec()]).start()
    client, ctx = await client_for(pf.bus, token='')
    r = await client.post('/api/control', json={'command': 'rearm', 'reason': 'x'},
                          headers={'Authorization': 'Bearer '})
    assert r.status_code == 403
    await ctx.__aexit__(None, None, None)


def test_websocket_pushes_events():
    from uxtrader.bus import InMemoryBus
    bus = InMemoryBus()
    app = create_app(bus, token='t')
    with TestClient(app) as client, client.websocket_connect('/ws') as ws:
        client.portal.call(bus.publish, 'control.kill', Control(command='kill', reason='drill'))
        msg = ws.receive_json()
        assert msg == {'type': 'Control', 'subject': 'control.kill'}
