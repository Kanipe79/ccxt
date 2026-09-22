"""``UXExchange`` — the one place the fork touches CCXT's own surface.

Design rule for the fork: **do not change unified method semantics.** ``fetch_ohlcv``
returns exactly what upstream returns. Everything added here is either a new method or a
wrapper that adds reliability around an unchanged call. That rule is what keeps the
monthly upstream merge a five-minute job instead of a weekend.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from .errors import ErrorClass, UXError
from .plugin_registry import attach_all
from .ratelimit import AdaptiveRateLimiter
from .resilience import resilient
from .ws import FeedMonitor

log = logging.getLogger(__name__)

# Per-call weights. Verify against venue docs on each upstream merge.
WEIGHTS = {
    'fetch_ticker': 2, 'fetch_ohlcv': 5, 'fetch_order_book': 10,
    'fetch_balance': 10, 'fetch_positions': 5, 'fetch_funding_rate_history': 2,
    'create_order': 1, 'cancel_order': 1, 'cancel_all_orders': 1,
    'fetch_open_orders': 3, 'fetch_order': 2,
}


class UXExchangeMixin:
    """Mixed into any ccxt.pro exchange class.

    ``UXBinance = type('UXBinance', (UXExchangeMixin, ccxtpro.binanceusdm), {})``

    Adds:
      * an adaptive, weight-aware rate limiter with a reserve for risk-reducing calls
      * typed errors with retry policy, and ambiguous-op reconciliation
      * a feed monitor for staleness and sequence gaps
      * plugin attachment
      * helpers the platform needs that upstream reasonably does not provide
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})          # type: ignore[call-arg]
        self.limiter = AdaptiveRateLimiter(getattr(self, 'id', 'unknown'))
        self.monitor = FeedMonitor(on_resync=self._resync_feed)
        self.reconciler: Any = None             # injected by the OMS
        self.plugins: dict[str, Any] = {}
        # We do our own limiting; upstream's fixed sleep would double-count.
        self.enableRateLimit = False            # noqa: N815 - ccxt's own casing

    async def setup(self) -> None:
        await self.load_markets()               # type: ignore[attr-defined]
        self.plugins = await attach_all(self)
        await self.monitor.start()

    # -- resilient wrappers over unchanged unified calls -----------------------

    @resilient(mutating=False, weight=WEIGHTS['fetch_ticker'])
    async def ux_fetch_ticker(self, symbol: str, params: dict | None = None):
        return await self.fetch_ticker(symbol, params or {})       # type: ignore[attr-defined]

    @resilient(mutating=False, weight=WEIGHTS['fetch_ohlcv'])
    async def ux_fetch_ohlcv(self, symbol: str, timeframe: str = '1h',
                             since: int | None = None, limit: int | None = None,
                             params: dict | None = None):
        return await self.fetch_ohlcv(symbol, timeframe, since, limit, params or {})  # type: ignore[attr-defined]

    @resilient(mutating=False, weight=WEIGHTS['fetch_positions'])
    async def ux_fetch_positions(self, symbols: list[str] | None = None,
                                 params: dict | None = None):
        return await self.fetch_positions(symbols, params or {})   # type: ignore[attr-defined]

    @resilient(mutating=True, weight=WEIGHTS['create_order'])
    async def ux_create_order(self, symbol: str, type_: str, side: str,
                              amount: float, price: float | None = None, *,
                              client_order_id: str, params: dict | None = None):
        """Idempotent order creation.

        ``client_order_id`` is mandatory and deterministic — it is the idempotency key
        that makes an AMBIGUOUS retry safe to reason about. A create without one is a
        bug, not a convenience, so it raises.
        """
        if not client_order_id:
            raise UXError('client_order_id is mandatory', ErrorClass.FATAL,
                          venue=getattr(self, 'id', ''))
        p = dict(params or {})
        p.setdefault('clientOrderId', client_order_id)
        return await self.create_order(symbol, type_, side, amount, price, p)  # type: ignore[attr-defined]

    # Cancels are risk-reducing: they bypass the rate-limiter reserve.
    @resilient(mutating=True, weight=WEIGHTS['cancel_order'], risk_reducing=True)
    async def ux_cancel_order(self, order_id: str, symbol: str, *,
                              client_order_id: str | None = None,
                              params: dict | None = None):
        return await self.cancel_order(order_id, symbol, params or {})  # type: ignore[attr-defined]

    @resilient(mutating=True, weight=WEIGHTS['cancel_all_orders'], risk_reducing=True)
    async def ux_cancel_all(self, symbol: str | None = None, params: dict | None = None):
        return await self.cancel_all_orders(symbol, params or {})       # type: ignore[attr-defined]

    # -- additions the platform needs -----------------------------------------

    async def funding_apr(self, symbol: str, *, lookback: int = 21,
                          halflife: int = 7) -> float:
        """EWMA-smoothed annualized funding. The S1 signal, computed at the source."""
        import math
        history = await self.fetch_funding_rate_history(symbol, limit=lookback)  # type: ignore[attr-defined]
        rates = [float(h['fundingRate']) for h in history if h.get('fundingRate') is not None]
        if len(rates) < 8:
            return 0.0
        alpha = 1.0 - math.exp(-math.log(2.0) / halflife)
        ewma = rates[0]
        for r in rates[1:]:
            ewma = alpha * r + (1.0 - alpha) * ewma
        interval_h = await self._funding_interval_hours(symbol)
        return ewma * (24.0 / interval_h) * 365.0

    async def _funding_interval_hours(self, symbol: str) -> float:
        market = self.market(symbol)            # type: ignore[attr-defined]
        info = market.get('info', {}) or {}
        for key in ('fundingIntervalHours', 'fundingInterval', 'funding_interval_hours'):
            if key in info:
                try:
                    return float(info[key])
                except (TypeError, ValueError):
                    pass
        return 8.0

    async def spot_perp_basis_bps(self, perp_symbol: str, spot_symbol: str) -> float:
        perp = await self.ux_fetch_ticker(perp_symbol)
        spot = await self.ux_fetch_ticker(spot_symbol)
        if not spot['last']:
            return 0.0
        return (float(perp['last']) - float(spot['last'])) / float(spot['last']) * 1e4

    async def liquidation_distance(self, symbol: str) -> float | None:
        """Fractional adverse move to liquidation. S1's core margin-safety metric."""
        positions = await self.ux_fetch_positions([symbol])
        for pos in positions:
            liq, mark = pos.get('liquidationPrice'), pos.get('markPrice')
            if liq and mark:
                return abs(float(mark) - float(liq)) / float(mark)
        return None

    def round_to_market(self, symbol: str, amount: Decimal,
                        price: Decimal | None = None) -> tuple[Decimal, Decimal | None]:
        """Apply the venue's precision and minimums. Doing this late causes rejects."""
        amt = Decimal(str(self.amount_to_precision(symbol, float(amount))))  # type: ignore[attr-defined]
        px = (Decimal(str(self.price_to_precision(symbol, float(price))))    # type: ignore[attr-defined]
              if price is not None else None)
        market = self.market(symbol)                                          # type: ignore[attr-defined]
        min_amt = (market.get('limits', {}).get('amount', {}) or {}).get('min')
        if min_amt is not None and amt < Decimal(str(min_amt)):
            amt = Decimal('0')
        return amt, px

    # -- feed plumbing ---------------------------------------------------------

    async def _resync_feed(self, key: str) -> None:
        """Sequence gap ⇒ pull a REST snapshot and rebase the local book."""
        try:
            _, symbol, channel = key.split('|', 2)
        except ValueError:
            log.error('resync_bad_key key=%s', key)
            return
        if channel != 'book':
            self.monitor.mark_resynced(key, None)
            return
        snapshot = await self.fetch_order_book(symbol, limit=1000)   # type: ignore[attr-defined]
        self.monitor.mark_resynced(key, snapshot.get('nonce'))
        log.warning('book_resynced symbol=%s nonce=%s', symbol, snapshot.get('nonce'))

    def feed_key(self, symbol: str, channel: str) -> str:
        return f'{getattr(self, "id", "?")}|{symbol}|{channel}'


def make_exchange(venue: str, config: dict[str, Any] | None = None) -> Any:
    """Build a rate-limited, resilient, plugin-equipped exchange for `venue`.

    Keeps the fork's surface to a single factory the rest of the platform calls.
    """
    import ccxt.pro as ccxtpro
    base = getattr(ccxtpro, venue, None)
    if base is None:
        raise UXError(f'unknown venue {venue}', ErrorClass.FATAL, venue=venue)
    cls = type(f'UX{venue.title()}', (UXExchangeMixin, base), {})
    return cls(config or {})
