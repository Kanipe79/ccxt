"""Configuration loading for ``config/config.yaml`` and ``config/strategies.yaml``.

Secrets never come from these files. Venue credentials are read from environment
variables using ccxt's own naming (``BINANCEUSDM_APIKEY``, ``BINANCEUSDM_SECRET``, …),
which is also what the repo's test runner and the panic script use.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .risk import RiskLimits
from .services.strategy_engine import StrategySpec

CREDENTIAL_FIELDS = ('apiKey', 'secret', 'password', 'uid', 'walletAddress', 'privateKey')


@dataclass
class PlatformConfig:
    mode: str
    starting_equity: Decimal
    venues: dict[str, dict[str, Any]]
    risk: RiskLimits
    bus_url: str
    clusters: dict[str, str] = field(default_factory=dict)
    strategies: list[StrategySpec] = field(default_factory=list)

    @property
    def default_venue(self) -> str:
        enabled = [v for v, c in self.venues.items() if c.get('enabled', True)]
        if not enabled:
            raise ValueError('no enabled venues in config')
        return enabled[0]


def load(config_path: str | Path, strategies_path: str | Path | None = None) -> PlatformConfig:
    raw = yaml.safe_load(Path(config_path).read_text())
    limits = RiskLimits(**{k: v for k, v in (raw.get('risk') or {}).items()
                           if k in RiskLimits.__dataclass_fields__})
    cfg = PlatformConfig(
        mode=raw.get('mode', 'paper'),
        starting_equity=Decimal(str((raw.get('account') or {}).get('starting_equity', 0))),
        venues=raw.get('venues') or {},
        risk=limits,
        bus_url=os.environ.get('UX_BUS', (raw.get('storage') or {}).get('nats', {}).get('url', 'memory://')),
    )
    if strategies_path:
        sraw = yaml.safe_load(Path(strategies_path).read_text())
        for item in sraw.get('strategies') or []:
            cfg.strategies.append(StrategySpec(
                cls=item['class'], risk_budget=float(item['risk_budget']),
                symbols=tuple(item.get('symbols') or ()), stage=int(item.get('stage', 1)),
                params=item.get('params') or {}, enabled=bool(item.get('enabled', True))))
        for cluster, symbols in (sraw.get('correlation_clusters') or {}).items():
            for s in symbols:
                cfg.clusters[s] = cluster
        total = sum(s.risk_budget for s in cfg.strategies)
        if total > 1.0 + 1e-9:
            raise ValueError(f'risk budgets sum to {total:.2f} > 1.00')
    if cfg.mode not in {'dev', 'paper', 'prod-small', 'prod'}:
        raise ValueError(f'unknown mode {cfg.mode!r}')
    return cfg


def credentials(venue: str) -> dict[str, str]:
    out = {}
    for name in CREDENTIAL_FIELDS:
        value = os.environ.get(f'{venue.upper()}_{name.upper()}')
        if value:
            out[name] = value
    return out
