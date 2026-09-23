"""Run the platform.

    # everything in one process, paper trading, in-memory bus
    python -m uxtrader.run --config config/config.yaml --strategies config/strategies.yaml

    # one service per container (docker-compose / k8s)
    python -m uxtrader.run --role risk --config ... --strategies ...

Roles: all | ingest | portfolio | risk | execution | engine.

Live trading (mode prod / prod-small) additionally requires the flag
``--i-understand-this-trades-real-money``, and the execution role refuses to start
if startup reconciliation finds local and venue state in disagreement.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from . import config as config_mod
from .bus import Bus, connect
from .clock import LiveClock
from .risk import RiskEngine
from .services.common import heartbeat_loop, lock_from_env
from .services.execution import ExecutionService
from .services.portfolio import PortfolioService
from .services.risk import RiskService
from .services.strategy_engine import StrategyEngine

log = logging.getLogger('uxtrader.run')
LIVE_MODES = {'prod', 'prod-small'}
ROLES = ('all', 'ingest', 'portfolio', 'risk', 'execution', 'engine')


async def build(cfg: config_mod.PlatformConfig, role: str, bus: Bus,
                exchanges: dict[str, Any]) -> list[Any]:
    """Construct and start the components for `role`. Returns objects to keep alive."""
    clock = LiveClock()
    started: list[Any] = []
    want = (lambda r: role in ('all', r))

    if want('portfolio'):
        svc = PortfolioService(bus, cfg.starting_equity)
        await svc.start()
        started.append(svc)
    if want('risk'):
        svc = RiskService(bus, RiskEngine(cfg.risk), clock=clock, clusters=cfg.clusters)
        await svc.start()
        started.append(svc)
    if want('execution'):
        started.append(await _execution(cfg, bus, exchanges, clock))
    if want('engine'):
        engine = StrategyEngine(bus, cfg.strategies, clock=clock,
                                starting_equity=cfg.starting_equity)
        await engine.start()
        await _warmup(engine, exchanges)
        started.append(engine)
    if want('ingest'):
        from .data.ingest import IngestService
        symbols = sorted({s for spec in cfg.strategies for s in spec.symbols})
        for venue, ex in exchanges.items():
            ingest = IngestService(ex, bus, symbols)
            await ingest.start()
            started.append(ingest)
    return started


async def _execution(cfg, bus, exchanges, clock) -> ExecutionService:
    if cfg.mode in LIVE_MODES:
        from .execution.live import LiveBroker
        broker: Any = LiveBroker(exchanges, clock=clock)
    else:
        from .execution.paper import PaperBroker
        holder: dict[str, ExecutionService] = {}
        broker = PaperBroker(clock, lambda v, s: holder['svc'].book_for(v, s))
    svc = ExecutionService(bus, broker, default_venue=cfg.default_venue, clock=clock,
                           lock=lock_from_env())
    if cfg.mode not in LIVE_MODES:
        holder['svc'] = svc
    else:
        problems = await svc.oms.reconcile_on_start(list(exchanges), local_positions={})
        if problems:
            # Blocking by design (RB-02): never start trading unsure of what we own.
            raise SystemExit('startup reconciliation failed:\n  ' + '\n  '.join(problems))
        for venue in exchanges:
            asyncio.create_task(broker.run_user_stream(venue), name=f'user:{venue}')
    await svc.start()
    return svc


async def _warmup(engine: StrategyEngine, exchanges: dict[str, Any]) -> None:
    """Backfill each strategy's warm-up window over REST, intents discarded."""
    if not exchanges:
        return
    from .data.history import TF_MS, HistoryLoader, bars_from_frame
    venue, ex = next(iter(exchanges.items()))
    loader = HistoryLoader(ex)
    for h in engine.hosted.values():
        s = h.strategy
        tf = s.timeframe
        if tf not in TF_MS:
            continue
        since = loader.now_ms() - (s.warmup_bars + 5) * TF_MS[tf]
        for symbol in s.symbols:
            df = await loader.ohlcv(symbol, tf, since)
            n = await engine.warmup(bars_from_frame(df, venue, symbol, tf))
            log.info('warmup strategy=%s symbol=%s bars=%d', h.ctx.strategy, symbol, n)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', default='config/config.yaml')
    ap.add_argument('--strategies', default='config/strategies.yaml')
    ap.add_argument('--role', default='all', choices=ROLES)
    ap.add_argument('--bus', default=None, help='memory:// or nats://host:4222')
    ap.add_argument('--i-understand-this-trades-real-money', dest='live_ack', action='store_true')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')

    cfg = config_mod.load(args.config, args.strategies)
    if cfg.mode in LIVE_MODES and not args.live_ack:
        print(f'mode={cfg.mode} places real orders. Re-run with '
              '--i-understand-this-trades-real-money if that is what you want.', file=sys.stderr)
        return 2
    # One process needs no broker: default to the in-memory bus for --role all.
    bus_url = args.bus or ('memory://' if args.role == 'all' else cfg.bus_url)
    if args.role != 'all' and bus_url.startswith('memory'):
        print('a single role on an in-memory bus talks to nobody; pass --bus nats://…',
              file=sys.stderr)
        return 2

    from uxcore import make_exchange
    exchanges: dict[str, Any] = {}
    needs_venue = args.role in ('all', 'ingest', 'execution', 'engine')
    if needs_venue:
        for venue, vcfg in cfg.venues.items():
            if not vcfg.get('enabled', True):
                continue
            creds = config_mod.credentials(venue) if cfg.mode in LIVE_MODES else {}
            ex = make_exchange(venue, creds)
            await ex.setup()
            exchanges[venue] = ex

    bus = await connect(bus_url, service=args.role)
    keep = await build(cfg, args.role, bus, exchanges)
    hb = asyncio.create_task(heartbeat_loop(bus, f'{args.role}'))
    log.warning('running role=%s mode=%s bus=%s components=%d',
                args.role, cfg.mode, bus_url, len(keep))
    try:
        await asyncio.Event().wait()
    finally:
        hb.cancel()
        for ex in exchanges.values():
            await ex.close()
        await bus.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
