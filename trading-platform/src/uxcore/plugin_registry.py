"""Plugin registry for venue-specific endpoints that cannot go upstream.

You will need a venue's portfolio-margin endpoint, or its undocumented liquidation feed,
months before CCXT supports it. Patching the vendored exchange file is how a fork becomes
unmergeable. Instead: register a plugin, and it is applied to the exchange instance at
construction.

    @register('binanceusdm')
    class PortfolioMargin(Plugin):
        async def fetch_pm_account(self, ex):
            return await ex.papi_get_account()

Each plugin's methods are bound onto the instance under `ex.plugins['portfolio_margin']`,
never onto the class, so two instances of the same venue can carry different plugins and
an upstream merge never conflicts.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)


class Plugin:
    """Base class. Subclasses add venue-specific coroutines taking the exchange first."""

    name: str = 'unnamed'
    venues: tuple[str, ...] = ()

    async def on_attach(self, exchange: Any) -> None:
        """Hook for anything that must run once at construction (capability probe, etc.)."""
        return None


_REGISTRY: dict[str, list[type[Plugin]]] = defaultdict(list)


def register(*venues: str):
    def decorator(cls: type[Plugin]) -> type[Plugin]:
        cls.venues = venues
        for venue in venues:
            _REGISTRY[venue].append(cls)
        log.debug('plugin_registered name=%s venues=%s', cls.name, venues)
        return cls
    return decorator


def plugins_for(venue: str) -> list[type[Plugin]]:
    return list(_REGISTRY.get(venue, []))


async def attach_all(exchange: Any) -> dict[str, Plugin]:
    attached: dict[str, Plugin] = {}
    for cls in plugins_for(getattr(exchange, 'id', '')):
        plugin = cls()
        await plugin.on_attach(exchange)
        attached[plugin.name] = plugin
    return attached
