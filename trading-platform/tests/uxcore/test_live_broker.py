"""LiveBroker over real ccxt binanceusdm (HTTP stubbed)."""
from __future__ import annotations

from decimal import Decimal


import ccxt
from conftest import order_json
from uxtrader.execution.live import LiveBroker
from uxtrader.types import Order, OrderState

CREATE = ('POST', 'fapi/v1/order')
CANCEL = ('DELETE', 'fapi/v1/order')
SYM = 'BTC/USDT:USDT'


def make_order(**kw) -> Order:
    base = dict(client_order_id='uxlive0001', intent_id='i1', strategy='s4', venue='binanceusdm',
                symbol=SYM, side='buy', type='limit', amount=Decimal('0.0123456'),
                price=Decimal('50000.07'))
    base.update(kw)
    return Order(**base)


async def test_submit_rounds_to_venue_precision_and_passes_flags(ex, transport):
    transport.on(*CREATE, order_json('uxlive0001'))
    broker = LiveBroker({'binanceusdm': ex})
    out = await broker.submit(make_order(reduce_only=True))
    body = transport.bodies(*CREATE)[0]
    assert 'quantity=0.012' in body and 'price=50000' in body   # fixture precision 0.001 / 0.1
    assert 'reduceOnly=true' in body
    assert out.state is OrderState.NEW and out.venue_order_id == '4242'


async def test_post_only_maps_to_gtx(ex, transport):
    transport.on(*CREATE, order_json('uxlive0001'))
    await LiveBroker({'binanceusdm': ex}).submit(make_order(post_only=True))
    assert 'timeInForce=GTX' in transport.bodies(*CREATE)[0]


async def test_below_minimum_is_rejected_locally(ex, transport):
    out = await LiveBroker({'binanceusdm': ex}).submit(make_order(amount=Decimal('0.0001')))
    assert out.state is OrderState.REJECTED
    assert transport.count(*CREATE) == 0


async def test_venue_rejection_becomes_rejected_state(ex, transport):
    transport.on(*CREATE, ccxt.InsufficientFunds('margin'))
    out = await LiveBroker({'binanceusdm': ex}).submit(make_order())
    assert out.state is OrderState.REJECTED


async def test_cancel_of_vanished_order_is_terminal(ex, transport):
    transport.on(*CANCEL, ccxt.OrderNotFound('Unknown order sent.'))
    out = await LiveBroker({'binanceusdm': ex}).cancel(make_order(venue_order_id='4242'))
    assert out.state is OrderState.CANCELED


async def test_user_stream_fills_are_attributed_and_deduplicated(ex, transport):
    transport.on(*CREATE, order_json('uxlive0001'))
    broker = LiveBroker({'binanceusdm': ex})
    got = []

    async def on_fill(f):
        got.append(f)
    broker.set_fill_handler(on_fill)
    await broker.submit(make_order())
    trade = {'id': 't1', 'order': '4242', 'symbol': SYM, 'side': 'buy', 'price': 50000,
             'amount': 0.012, 'fee': {'cost': 0.33, 'currency': 'USDT'},
             'timestamp': 1_700_000_000_000, 'takerOrMaker': 'maker', 'info': {}}
    await broker.handle_trade('binanceusdm', trade)
    await broker.handle_trade('binanceusdm', trade)          # redelivery
    assert len(got) == 1
    assert got[0].strategy == 's4' and got[0].client_order_id == 'uxlive0001'
    assert got[0].is_maker


async def test_external_fill_is_recorded_not_dropped(ex):
    broker = LiveBroker({'binanceusdm': ex})
    got = []

    async def on_fill(f):
        got.append(f)
    broker.set_fill_handler(on_fill)
    await broker.handle_trade('binanceusdm', {
        'id': 'x9', 'order': '999', 'symbol': SYM, 'side': 'sell', 'price': 1, 'amount': 1,
        'timestamp': 0, 'info': {}})
    assert got and got[0].strategy == 'external'
