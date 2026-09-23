"""Every limit in docs/01 §0.3 gets a test. The risk engine is the one component where
a bug costs money directly, and it is pure and synchronous precisely so this is easy."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from uxtrader.risk import KillSwitch, RiskEngine, RiskLimits, RiskState
from uxtrader.types import AlgoSpec, Intent, Position, TargetSpec


def make_intent(symbol='BTC/USDT:USDT', position='1', strategy='s4', venue='binanceusdm'):
    return Intent(strategy=strategy, venue=venue, symbol=symbol,
                  target=TargetSpec(position=Decimal(position)),
                  algo=AlgoSpec(), reason='test').with_id()


def make_state(**kw):
    defaults = dict(
        equity=Decimal('100000'), peak_equity=Decimal('100000'),
        day_start_equity=Decimal('100000'), week_start_equity=Decimal('100000'),
        marks={'BTC/USDT:USDT': Decimal('50000')},
    )
    defaults.update(kw)
    return RiskState(**defaults)


def test_approves_a_normal_intent():
    decision = RiskEngine().evaluate(make_intent(position='0.3'), make_state())
    assert decision.approved


def test_daily_loss_halts_entries_until_midnight_and_requests_flatten():
    engine = RiskEngine()
    now = datetime(2024, 3, 5, 14, 0, tzinfo=timezone.utc)
    state = make_state(equity=Decimal('96000'), now=now)          # −4% on the day
    assert not engine.evaluate(make_intent(), state).approved
    assert engine.kill.global_armed, 'a daily limit is a halt, not a permanent kill'
    assert engine.take_flatten_request() == 'daily loss limit'
    assert engine.take_flatten_request() is None                  # consumed once

    later_same_day = make_state(equity=Decimal('99000'), day_start_equity=Decimal('99000'),
                                now=now.replace(hour=23))
    assert not engine.evaluate(make_intent(position='0.1'), later_same_day).approved

    next_day = make_state(equity=Decimal('99000'), day_start_equity=Decimal('99000'),
                          now=datetime(2024, 3, 6, 0, 1, tzinfo=timezone.utc))
    assert engine.evaluate(make_intent(position='0.1'), next_day).approved


def test_weekly_loss_requires_manual_rearm():
    engine = RiskEngine()
    state = make_state(equity=Decimal('93000'), day_start_equity=Decimal('93000'))
    assert not engine.evaluate(make_intent(), state).approved
    assert not engine.kill.global_armed
    assert engine.take_flatten_request() == 'weekly loss limit'


def test_drawdown_stop_halts_everything():
    engine = RiskEngine()
    state = make_state(equity=Decimal('81000'), peak_equity=Decimal('100000'),
                       day_start_equity=Decimal('81000'),
                       week_start_equity=Decimal('81000'))
    decision = engine.evaluate(make_intent(), state)
    assert not decision.approved
    assert decision.breaches[0].limit == 'drawdown_stop'


def test_drawdown_halve_band_shrinks_rather_than_vetoes():
    engine = RiskEngine()
    state = make_state(equity=Decimal('87000'), peak_equity=Decimal('100000'),
                       day_start_equity=Decimal('87000'),
                       week_start_equity=Decimal('87000'))
    decision = engine.evaluate(make_intent(position='0.2'), state)
    assert decision.approved
    assert decision.adjusted_position == Decimal('0.1')


def test_asset_cap_shrinks_oversized_intent():
    """20% of 100k equity at 50k/BTC = 0.4 BTC maximum."""
    decision = RiskEngine().evaluate(make_intent(position='5'), make_state())
    assert decision.approved
    assert decision.adjusted_position == pytest.approx(Decimal('0.4'))


def test_risk_never_grows_an_intent():
    decision = RiskEngine().evaluate(make_intent(position='0.01'), make_state())
    assert decision.adjusted_position <= Decimal('0.01')


def test_stale_feed_blocks_entry():
    state = make_state(stale_feeds={'BTC/USDT:USDT'})
    assert not RiskEngine().evaluate(make_intent(), state).approved


def held(qty: str) -> dict:
    return {'BTC/USDT:USDT': Position(venue='binanceusdm', symbol='BTC/USDT:USDT',
                                      strategy='s4', quantity=Decimal(qty),
                                      avg_entry=Decimal('50000'))}


def test_reducing_a_position_is_allowed_past_the_cap():
    """You may always de-risk, even from a position that already breaches a limit."""
    decision = RiskEngine().evaluate(make_intent(position='5'), make_state(positions=held('10')))
    assert decision.approved
    assert decision.adjusted_position == Decimal('5')


@pytest.mark.parametrize('blocker', ['kill', 'drawdown', 'stale', 'daily'])
def test_exits_are_never_vetoed(blocker):
    """The bug this guards against: a fired kill switch vetoing a stop-loss exit,
    leaving the losing position open."""
    engine = RiskEngine()
    kw = {'positions': held('0.3')}
    if blocker == 'kill':
        engine.kill.fire('test')
    elif blocker == 'drawdown':
        kw.update(equity=Decimal('70000'), day_start_equity=Decimal('70000'),
                  week_start_equity=Decimal('70000'))
    elif blocker == 'stale':
        kw.update(stale_feeds={'BTC/USDT:USDT'})
    else:
        kw.update(equity=Decimal('95000'))
    decision = engine.evaluate(make_intent(position='0'), make_state(**kw))
    assert decision.approved and decision.adjusted_position == 0


def test_flip_is_not_treated_as_reducing():
    """Long 0.3 → short 5 opens a new (oversized) short; it must be capped like an entry."""
    decision = RiskEngine().evaluate(make_intent(position='-5'),
                                     make_state(positions=held('0.3')))
    assert decision.approved
    assert decision.adjusted_position == pytest.approx(Decimal('-0.4'))


def test_capped_add_never_forces_a_partial_sale():
    """An add that the cap shrinks below the current holding means 'hold', not 'sell'."""
    decision = RiskEngine().evaluate(make_intent(position='0.6'),
                                     make_state(positions=held('0.45')))
    assert decision.adjusted_position == Decimal('0.45')


def test_kill_switch_scopes_are_independent():
    kill = KillSwitch()
    kill.fire('bad strategy', strategy='s4')
    assert kill.blocked('s4', 'binanceusdm')
    assert kill.blocked('s1', 'binanceusdm') is None


def test_order_rate_guard():
    engine = RiskEngine(RiskLimits(max_orders_per_min=5))
    state = make_state(orders_this_min={'s4': 5})
    assert not engine.evaluate(make_intent(), state).approved
