"""Phase 0 gate (docs/06): place and cancel an order through uxcore, with a forced 429
and a forced timeout, and show correct classification and reconciliation for both.

Runs against the real ccxt ``binanceusdm`` implementation from this fork; only the
HTTP transport is stubbed.
"""
from __future__ import annotations

import pytest

import ccxt
from uxcore import ErrorClass, UXError, classify
from uxcore.ratelimit import AdaptiveRateLimiter

from conftest import order_json

CREATE = ('POST', 'fapi/v1/order')
CANCEL = ('DELETE', 'fapi/v1/order')
OPEN = ('GET', 'fapi/v1/openOrders')
ALL = ('GET', 'fapi/v1/allOrders')
TICKER = ('GET', 'fapi/v1/ticker/24hr')
SYM = 'BTC/USDT:USDT'


# --- classification matrix ---------------------------------------------------

@pytest.mark.parametrize('exc,read,write', [
    (ccxt.RequestTimeout('t'),         ErrorClass.TRANSIENT,    ErrorClass.AMBIGUOUS),
    (ccxt.NetworkError('n'),           ErrorClass.TRANSIENT,    ErrorClass.AMBIGUOUS),
    (ccxt.ExchangeNotAvailable('502'), ErrorClass.TRANSIENT,    ErrorClass.AMBIGUOUS),
    (TimeoutError(),                   ErrorClass.TRANSIENT,    ErrorClass.AMBIGUOUS),
    (ccxt.RateLimitExceeded('429'),    ErrorClass.RATE_LIMITED, ErrorClass.RATE_LIMITED),
    (ccxt.DDoSProtection('418'),       ErrorClass.RATE_LIMITED, ErrorClass.RATE_LIMITED),
    (ccxt.InvalidNonce('recvWindow'),  ErrorClass.TRANSIENT,    ErrorClass.TRANSIENT),
    (ccxt.OnMaintenance('maint'),      ErrorClass.TRANSIENT,    ErrorClass.TRANSIENT),
    (ccxt.InsufficientFunds('funds'),  ErrorClass.REJECTED,     ErrorClass.REJECTED),
    (ccxt.OrderNotFound('gone'),       ErrorClass.REJECTED,     ErrorClass.REJECTED),
    (ccxt.ExchangeError('unknown'),    ErrorClass.TRANSIENT,    ErrorClass.REJECTED),
    (ccxt.AuthenticationError('key'),  ErrorClass.FATAL,        ErrorClass.FATAL),
    (ccxt.PermissionDenied('perm'),    ErrorClass.FATAL,        ErrorClass.FATAL),
    (ccxt.BadSymbol('sym'),            ErrorClass.FATAL,        ErrorClass.FATAL),
])
def test_classification(exc, read, write):
    assert classify(exc, was_sent=False).klass is read
    assert classify(exc, was_sent=True).klass is write


# --- the exchange wiring -----------------------------------------------------

async def test_make_exchange_builds_real_ccxt_subclass(ex):
    assert isinstance(ex, ccxt.pro.binanceusdm)
    assert ex.enableRateLimit is False            # uxcore limits; upstream must not double-count
    assert ex.limiter.venue == 'binanceusdm'
    assert ex.reconciler is not None


async def test_create_and_cancel_happy_path(ex, transport):
    transport.on(*CREATE, order_json('uxhappy01'))
    transport.on(*CANCEL, order_json('uxhappy01', status='CANCELED'))

    order = await ex.ux_create_order(SYM, 'limit', 'buy', 0.01, 50000,
                                     client_order_id='uxhappy01')
    assert order['id'] == '4242' and order['clientOrderId'] == 'uxhappy01'
    assert 'newClientOrderId=uxhappy01' in transport.bodies(*CREATE)[0]

    cancelled = await ex.ux_cancel_order('4242', SYM)
    assert cancelled['status'] == 'canceled'


async def test_create_without_client_order_id_is_refused(ex, transport):
    with pytest.raises(UXError) as info:
        await ex.ux_create_order(SYM, 'limit', 'buy', 0.01, 50000, client_order_id='')
    assert info.value.klass is ErrorClass.FATAL
    assert transport.count(*CREATE) == 0


# --- forced 429 --------------------------------------------------------------

async def test_forced_429_backs_off_and_retries(ex, transport):
    transport.on(*CREATE, ccxt.RateLimitExceeded('429 Too Many Requests'),
                 order_json('ux429'))
    order = await ex.ux_create_order(SYM, 'limit', 'buy', 0.01, 50000,
                                     client_order_id='ux429')
    assert order['clientOrderId'] == 'ux429'
    assert transport.count(*CREATE) == 2
    # A 429 means the venue refused before processing: no reconciliation needed.
    assert transport.count(*OPEN) == 0


async def test_persistent_429_gives_up_with_rate_limited(ex, transport):
    transport.on(*TICKER, ccxt.RateLimitExceeded('429'))
    with pytest.raises(UXError) as info:
        await ex.ux_fetch_ticker(SYM)
    assert info.value.klass is ErrorClass.RATE_LIMITED
    assert transport.count(*TICKER) == 7          # 1 + 6 retries per policy


# --- forced timeout ----------------------------------------------------------

async def test_timeout_on_create_reconciles_then_retries_when_absent(ex, transport):
    transport.on(*CREATE, ccxt.RequestTimeout('read timeout'), order_json('uxtimeout1'))
    transport.on(*OPEN, [])
    transport.on(*ALL, [])

    order = await ex.ux_create_order(SYM, 'limit', 'buy', 0.01, 50000,
                                     client_order_id='uxtimeout1')
    assert order['clientOrderId'] == 'uxtimeout1'
    assert transport.count(*OPEN) == 1, 'must reconcile before resending'
    assert transport.count(*CREATE) == 2
    # The retry reuses the SAME client order id: if the first request did land late,
    # the venue rejects the duplicate instead of doubling the position.
    first, second = transport.bodies(*CREATE)
    assert 'newClientOrderId=uxtimeout1' in first and 'newClientOrderId=uxtimeout1' in second


async def test_timeout_on_create_does_not_resend_when_order_landed(ex, transport):
    transport.on(*CREATE, ccxt.RequestTimeout('read timeout'))
    transport.on(*OPEN, [order_json('uxlanded1')])

    with pytest.raises(UXError) as info:
        await ex.ux_create_order(SYM, 'limit', 'buy', 0.01, 50000,
                                 client_order_id='uxlanded1')
    assert info.value.klass is ErrorClass.AMBIGUOUS
    assert transport.count(*CREATE) == 1, 'resent an order that already existed'


async def test_timeout_when_reconciliation_impossible_does_not_resend(ex, transport):
    transport.on(*CREATE, ccxt.RequestTimeout('read timeout'))
    transport.on(*OPEN, ccxt.ExchangeNotAvailable('down'))
    transport.on(*ALL, ccxt.ExchangeNotAvailable('down'))

    with pytest.raises(UXError) as info:
        await ex.ux_create_order(SYM, 'limit', 'buy', 0.01, 50000,
                                 client_order_id='uxblind01')
    assert info.value.klass is ErrorClass.AMBIGUOUS
    assert transport.count(*CREATE) == 1, '"could not check" was read as "absent"'


async def test_timeout_on_read_retries_without_reconciliation(ex, transport):
    ticker = {'symbol': 'BTCUSDT', 'lastPrice': '50000', 'closeTime': 1700000000000}
    transport.on(*TICKER, ccxt.RequestTimeout('t'), ticker)
    t = await ex.ux_fetch_ticker(SYM)
    assert t['last'] == 50000.0
    assert transport.count(*OPEN) == 0


async def test_timeout_on_cancel_just_retries(ex, transport):
    transport.on(*CANCEL, ccxt.RequestTimeout('t'), order_json('uxc', status='CANCELED'))
    await ex.ux_cancel_order('4242', SYM)
    assert transport.count(*CANCEL) == 2
    assert transport.count(*OPEN) == 0


# --- rejections and the breaker ----------------------------------------------

async def test_insufficient_funds_is_not_retried_and_does_not_trip_breaker(ex, transport):
    from uxcore.resilience import breaker_for
    transport.on(*CREATE, ccxt.InsufficientFunds('Margin is insufficient'))
    for _ in range(8):
        with pytest.raises(UXError) as info:
            await ex.ux_create_order(SYM, 'limit', 'buy', 0.01, 50000,
                                     client_order_id='uxfunds')
        assert info.value.klass is ErrorClass.REJECTED
    assert transport.count(*CREATE) == 8
    assert breaker_for('binanceusdm').state == 'closed'


async def test_repeated_outage_opens_the_breaker(ex, transport):
    from uxcore.resilience import breaker_for
    transport.on(*TICKER, ccxt.ExchangeNotAvailable('503'))
    with pytest.raises(UXError):
        await ex.ux_fetch_ticker(SYM)
    assert breaker_for('binanceusdm').state == 'open'
    calls = transport.count(*TICKER)
    with pytest.raises(UXError, match='circuit open'):
        await ex.ux_fetch_ticker(SYM)
    assert transport.count(*TICKER) == calls, 'breaker let a request through'


# --- rate-limit accounting ---------------------------------------------------

async def test_venue_weight_header_is_trusted(ex, transport):
    transport.headers = {'X-MBX-USED-WEIGHT-1M': '2000'}
    transport.on(*TICKER, {'symbol': 'BTCUSDT', 'lastPrice': '1', 'closeTime': 1})
    await ex.ux_fetch_ticker(SYM)
    assert ex.limiter.consumed_reported == 2000
    assert ex.limiter.utilisation > 0.9


async def test_reserve_blocks_normal_calls_but_not_risk_reducing():
    import asyncio
    lim = AdaptiveRateLimiter('binanceusdm', budget=100, window=60.0, safety=1.0)
    async with lim.acquire(70):                   # bucket now 30 = exactly the reserve
        pass
    async with lim.acquire(5, risk_reducing=True):  # cancels still get through
        pass
    with pytest.raises(asyncio.TimeoutError):
        async with asyncio.timeout(0.2):
            async with lim.acquire(5):            # a new entry must wait
                pass
