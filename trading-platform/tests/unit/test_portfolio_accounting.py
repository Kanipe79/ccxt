"""Position accounting, including the sign flip that everyone gets wrong once."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from uxtrader.portfolio import Portfolio
from uxtrader.types import Fill

TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


def fill(side, price, amount, fee='0'):
    return Fill(client_order_id='c1', venue_order_id=None, strategy='s4',
                venue='v', symbol='BTC/USDT:USDT', side=side, price=Decimal(price),
                amount=Decimal(amount), fee=Decimal(fee), fee_currency='USDT',
                ts=TS, is_maker=False)


def test_average_entry_on_add():
    p = Portfolio(Decimal('100000'))
    p.apply_fill(fill('buy', '50000', '1'))
    p.apply_fill(fill('buy', '60000', '1'))
    pos = p.position('v', 'BTC/USDT:USDT', 's4')
    assert pos.quantity == Decimal('2')
    assert pos.avg_entry == Decimal('55000')


def test_realized_pnl_on_partial_close():
    p = Portfolio(Decimal('100000'))
    p.apply_fill(fill('buy', '50000', '2'))
    p.apply_fill(fill('sell', '55000', '1'))
    pos = p.position('v', 'BTC/USDT:USDT', 's4')
    assert pos.quantity == Decimal('1')
    assert pos.realized_pnl == Decimal('5000')
    assert pos.avg_entry == Decimal('50000')      # unchanged by a reduction


def test_flip_through_flat_resets_entry():
    p = Portfolio(Decimal('100000'))
    p.apply_fill(fill('buy', '50000', '1'))
    p.apply_fill(fill('sell', '55000', '3'))       # close 1, open 2 short
    pos = p.position('v', 'BTC/USDT:USDT', 's4')
    assert pos.quantity == Decimal('-2')
    assert pos.realized_pnl == Decimal('5000')
    assert pos.avg_entry == Decimal('55000')


def test_short_pnl_sign():
    p = Portfolio(Decimal('100000'))
    p.apply_fill(fill('sell', '50000', '1'))
    p.apply_fill(fill('buy', '45000', '1'))
    assert p.position('v', 'BTC/USDT:USDT', 's4').realized_pnl == Decimal('5000')


def test_funding_is_paid_by_longs():
    p = Portfolio(Decimal('100000'))
    p.apply_fill(fill('buy', '50000', '1'))
    paid = p.apply_funding('v', 'BTC/USDT:USDT', 's4', Decimal('0.0001'), Decimal('50000'))
    assert paid == Decimal('-5')                   # long pays when the rate is positive


def test_reconcile_detects_drift():
    p = Portfolio(Decimal('100000'))
    p.apply_fill(fill('buy', '50000', '1'))
    assert p.reconcile('v', {'BTC/USDT:USDT': Decimal('1')}) == []
    assert p.reconcile('v', {'BTC/USDT:USDT': Decimal('2')}) == ['BTC/USDT:USDT']


def test_two_strategies_on_one_symbol_keep_separate_books():
    """S1 short and S4 long on the same perp must not overwrite each other."""
    p = Portfolio(Decimal('100000'))
    p.apply_fill(fill('buy', '50000', '0.3'))                       # s4
    short = fill('sell', '50000', '0.5').model_copy(update={'strategy': 's1'})
    p.apply_fill(short)
    assert p.position('v', 'BTC/USDT:USDT', 's4').quantity == Decimal('0.3')
    assert p.position('v', 'BTC/USDT:USDT', 's1').quantity == Decimal('-0.5')
    assert p.net_quantity('v', 'BTC/USDT:USDT') == Decimal('-0.2')
    assert set(p.strategy_positions('s4')) == {'BTC/USDT:USDT'}
    # Reconciliation compares the NET against the venue.
    assert p.reconcile('v', {'BTC/USDT:USDT': Decimal('-0.2')}) == []
