"""NatsBus against a real nats-server (JetStream on). Skipped when the binary is absent:
set UX_NATS_SERVER=/path/to/nats-server or put it on PATH."""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
from decimal import Decimal

import pytest

from uxtrader.bus import NatsBus
from uxtrader.events import EXEC_ORDER, ApprovedIntent, PortfolioSnapshot
from uxtrader.types import Fill, Intent, RiskDecision, TargetSpec, utcnow

BIN = os.environ.get('UX_NATS_SERVER') or shutil.which('nats-server')
pytestmark = pytest.mark.skipif(not BIN, reason='nats-server binary not available')


@pytest.fixture
def nats_url(tmp_path):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([BIN, '-js', '-sd', str(tmp_path), '-a', '127.0.0.1', '-p', str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(('127.0.0.1', port), timeout=0.1).close()
            break
        except OSError:
            import time
            time.sleep(0.1)
    yield f'nats://127.0.0.1:{port}'
    proc.terminate()
    proc.wait(5)


async def settle(cond, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


def a_fill(i=0):
    return Fill(client_order_id=f'c{i}', venue_order_id=None, strategy='s', venue='v',
                symbol='BTC/USDT:USDT', side='buy', price=Decimal('1'), amount=Decimal('1'),
                fee=Decimal('0'), fee_currency='USDT', ts=utcnow(), is_maker=False)


async def test_two_services_on_the_same_durable_subject_both_receive(nats_url):
    """Regression: durables named by pattern alone split messages between services."""
    a, b, pub = (await NatsBus(nats_url, service='portfolio').connect(),
                 await NatsBus(nats_url, service='engine').connect(),
                 await NatsBus(nats_url, service='exec').connect())
    got_a, got_b = [], []
    await a.subscribe('fill.>', lambda s, m: _append(got_a, m))
    await b.subscribe('fill.>', lambda s, m: _append(got_b, m))
    for i in range(5):
        await pub.publish('fill.v', a_fill(i))
    assert await settle(lambda: len(got_a) == 5 and len(got_b) == 5), (len(got_a), len(got_b))
    for bus in (a, b, pub):
        await bus.close()


async def test_new_consumer_does_not_replay_old_orders(nats_url):
    """Regression: a fresh durable replayed the stream — i.e. re-executed old trades."""
    pub = await NatsBus(nats_url, service='risk').connect()
    old = ApprovedIntent(
        intent=Intent(strategy='s', symbol='X', target=TargetSpec(position=Decimal('1')),
                      reason='old').with_id(),
        decision=RiskDecision(intent_id='x', approved=True, adjusted_position=Decimal('1')))
    await pub.publish(EXEC_ORDER, old)
    await asyncio.sleep(0.2)
    execu = await NatsBus(nats_url, service='execution').connect()
    got = []
    await execu.subscribe('exec.order', lambda s, m: _append(got, m))
    await asyncio.sleep(0.5)
    assert got == [], 'an old exec.order was replayed to a new execution service'
    await pub.publish(EXEC_ORDER, old)
    assert await settle(lambda: len(got) == 1)
    for bus in (pub, execu):
        await bus.close()


async def test_every_published_subject_is_routable(nats_url):
    """Regression: portfolio.* and control.* were in no stream, so JetStream publishes
    to them failed. Publish one message on every subject the services use."""
    from uxtrader.events import Control, Heartbeat, RiskStatus, StrategyStatus
    bus = await NatsBus(nats_url, service='t').connect()
    snap = PortfolioSnapshot(seq=1, equity=Decimal('1'), peak_equity=Decimal('1'),
                             day_start_equity=Decimal('1'), week_start_equity=Decimal('1'),
                             positions=(), marks={})
    msgs = {
        'portfolio.snapshot': snap, 'control.kill': Control(command='kill', reason='t'),
        'risk.status': RiskStatus(kill_global=False),
        'strategy.status': StrategyStatus(name='s', cls='C', timeframe='1m', symbols=(),
                                          stage=1, risk_budget=0.1, bars_seen=0, warmup_bars=0),
        'heartbeat.x': Heartbeat(service='x'), 'fill.v': a_fill(),
        'risk.veto': RiskDecision(intent_id='i', approved=False),
    }
    for subject, msg in msgs.items():
        await bus.publish(subject, msg)
    await bus.close()


async def test_platform_round_trip_with_each_service_on_its_own_connection(nats_url):
    """The container layout, in one test: four services, four NATS connections."""
    from test_services_e2e import FiveMinuteToggle, spec
    from uxtrader.clock import LiveClock
    from uxtrader.events import md_subject
    from uxtrader.execution.paper import PaperBroker
    from uxtrader.services import ExecutionService, PortfolioService, RiskService, StrategyEngine
    from uxtrader.types import Bar

    buses = {r: await NatsBus(nats_url, service=r).connect()
             for r in ('portfolio', 'risk', 'execution', 'engine', 'feed')}
    portfolio = PortfolioService(buses['portfolio'], Decimal('100000'))
    risk = RiskService(buses['risk'])
    holder = {}
    broker = PaperBroker(LiveClock(), lambda v, s: holder['svc'].book_for(v, s))
    execution = ExecutionService(buses['execution'], broker, default_venue='paperx')
    holder['svc'] = execution
    engine = StrategyEngine(buses['engine'], [spec(cls=FiveMinuteToggle)],
                            starting_equity=Decimal('100000'))
    for svc in (portfolio, risk, execution, engine):
        await svc.start()
    await asyncio.sleep(0.2)

    from datetime import datetime, timedelta, timezone
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        p = Decimal('50000')
        await buses['feed'].publish(md_subject('paperx', 'bar', 'BTC/USDT:USDT'), Bar(
            venue='paperx', symbol='BTC/USDT:USDT', timeframe='1m',
            ts=t0 + timedelta(minutes=i), open=p, high=p, low=p, close=p, volume=Decimal('1')))
        await asyncio.sleep(0.1)

    def held():
        return sum((x.quantity for x in portfolio.portfolio.all_positions()), Decimal('0'))
    assert await settle(lambda: held() == Decimal('0.1')), held()
    assert await settle(lambda: engine.hosted['toggle'].ctx.position('BTC/USDT:USDT') == Decimal('0.1'))
    for bus in buses.values():
        await bus.close()


async def _append(lst, m):
    lst.append(m)
