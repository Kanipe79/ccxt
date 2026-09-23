from __future__ import annotations

import asyncio

from uxtrader.ops.watchdog import Watchdog, WatchdogConfig


class FakeVenue:
    def __init__(self, equity=100_000.0, positions=None):
        self.equity = equity
        self.positions = positions or []
        self.cancelled = 0
        self.orders = []

    async def fetch_balance(self):
        return {'USDT': {'total': self.equity}}

    async def cancel_all_orders(self):
        self.cancelled += 1

    async def fetch_positions(self):
        return self.positions

    async def create_order(self, symbol, type_, side, amount, price, params):
        self.orders.append((symbol, type_, side, amount, params))


def dog_with(venue, **cfg):
    config = WatchdogConfig(poll_interval=0.01, venues=('v',), **cfg)
    return Watchdog(config, {'v': venue})


async def run_briefly(dog, seconds=0.15):
    task = asyncio.create_task(dog.run())
    await asyncio.sleep(seconds)
    task.cancel()


async def test_not_armed_until_first_heartbeat():
    venue = FakeVenue()
    dog = dog_with(venue, heartbeat_timeout=0.01, dry_run=False)
    await run_briefly(dog)
    assert dog.fired_reason is None, 'fired before the trading stack ever started'


async def test_heartbeat_loss_cancels_then_flattens_reduce_only():
    venue = FakeVenue(positions=[{'symbol': 'BTC/USDT:USDT', 'contracts': 0.4, 'side': 'long'},
                                 {'symbol': 'ETH/USDT:USDT', 'contracts': 2, 'side': 'short'}])
    dog = dog_with(venue, heartbeat_timeout=0.03, dry_run=False)
    dog.heartbeat()
    await run_briefly(dog)
    assert 'no heartbeat' in (dog.fired_reason or '')
    assert venue.cancelled == 1
    assert ('BTC/USDT:USDT', 'market', 'sell', 0.4, {'reduceOnly': True}) in venue.orders
    assert ('ETH/USDT:USDT', 'market', 'buy', 2.0, {'reduceOnly': True}) in venue.orders


async def test_dry_run_trades_nothing():
    venue = FakeVenue(positions=[{'symbol': 'X', 'contracts': 1, 'side': 'long'}])
    dog = dog_with(venue, heartbeat_timeout=0.02, dry_run=True)
    dog.heartbeat()
    await run_briefly(dog)
    assert dog.fired_reason and venue.cancelled == 0 and not venue.orders


async def test_fast_equity_drop_fires():
    venue = FakeVenue()
    dog = dog_with(venue, heartbeat_timeout=60, equity_drop_pct=0.05, dry_run=True)
    task = asyncio.create_task(dog.run())
    for _ in range(5):
        dog.heartbeat()
        await asyncio.sleep(0.02)
    venue.equity = 90_000.0
    await asyncio.sleep(0.05)
    task.cancel()
    assert 'equity dropped' in (dog.fired_reason or '')


async def test_operator_global_kill_fires_l2_but_scoped_kill_does_not():
    from uxtrader.events import Control, Envelope
    venue = FakeVenue(positions=[{'symbol': 'BTC/USDT:USDT', 'contracts': 1, 'side': 'long'}])
    dog = dog_with(venue, dry_run=False)
    scoped = Envelope.wrap('control.kill', Control(command='kill', reason='x', strategy='s4'))
    assert not await dog.on_control(scoped.model_dump_json().encode())
    assert not venue.orders
    glob = Envelope.wrap('control.kill', Control(command='kill', reason='dashboard'))
    assert await dog.on_control(glob.model_dump_json().encode())
    assert 'operator kill: dashboard' == dog.fired_reason
    assert venue.orders and venue.orders[0][4] == {'reduceOnly': True}
    assert not await dog.on_control(b'not json')
