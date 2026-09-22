"""Binance Portfolio Margin — the endpoints S1 needs before upstream has them.

Portfolio margin is what makes delta-neutral carry viable above 1.0x: the venue nets the
spot and perp legs for margin purposes instead of margining them separately. Before you
size up on it, verify how *this venue* computes maintenance margin on a hedged book
during a stress move — on testnet, with a simulated gap, not from the documentation.
"""
from __future__ import annotations

from typing import Any

from ..plugin_registry import Plugin, register


@register('binanceusdm', 'binance')
class PortfolioMargin(Plugin):
    name = 'portfolio_margin'

    def __init__(self) -> None:
        self.available = False

    async def on_attach(self, exchange: Any) -> None:
        # Probe once; the account either has PM enabled or it does not.
        try:
            await exchange.papi_get_balance()
            self.available = True
        except Exception:       # noqa: BLE001 - absence is the expected case
            self.available = False

    async def account(self, exchange: Any) -> dict[str, Any]:
        return await exchange.papi_get_account()

    async def uniMMR(self, exchange: Any) -> float:  # noqa: N802 - venue's own name
        """Unified maintenance-margin ratio. Below ~1.3 you are close to liquidation.

        S1 treats this as a hard risk input: alert at 2.0, de-risk at 1.5, halve the
        sleeve at 1.3. Do not wait for the venue's own margin call.
        """
        acct = await self.account(exchange)
        return float(acct.get('uniMMR', 0.0))
