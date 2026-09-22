"""The laptop flatten drill — RB-05 and the DR plan's 15-minute RTO.

Deliberately dependency-light: ccxt and stdlib only. No database, no config service, no
message bus, no imports from the rest of this package. If the cluster is gone, your
config service is gone with it, and this must still run from a laptop with nothing but
API keys in environment variables.

    python -m uxtrader.ops.panic --venues binanceusdm,bybit --confirm FLATTEN

Rehearse it quarterly. A procedure you have never run is a hope, not a plan.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys


async def flatten_venue(venue: str, *, dry_run: bool) -> int:
    import ccxt.async_support as ccxt

    key = os.environ.get(f'{venue.upper()}_APIKEY')
    secret = os.environ.get(f'{venue.upper()}_SECRET')
    if not key or not secret:
        print(f'[{venue}] missing credentials', file=sys.stderr)
        return 0

    ex = getattr(ccxt, venue)({'apiKey': key, 'secret': secret,
                               'enableRateLimit': True})
    closed = 0
    try:
        await ex.load_markets()
        try:
            await ex.cancel_all_orders()
            print(f'[{venue}] cancelled all open orders')
        except Exception as exc:                              # noqa: BLE001
            print(f'[{venue}] cancel_all failed: {exc}', file=sys.stderr)

        for pos in await ex.fetch_positions():
            qty = float(pos.get('contracts') or 0.0)
            if qty == 0:
                continue
            side = 'sell' if pos.get('side') == 'long' else 'buy'
            print(f'[{venue}] {"WOULD FLATTEN" if dry_run else "FLATTENING"} '
                  f'{pos["symbol"]} {side} {abs(qty)}')
            if not dry_run:
                await ex.create_order(pos['symbol'], 'market', side, abs(qty),
                                      None, {'reduceOnly': True})
            closed += 1
    finally:
        await ex.close()
    return closed


async def main() -> int:
    parser = argparse.ArgumentParser(description='Emergency flatten across venues.')
    parser.add_argument('--venues', required=True, help='comma-separated ccxt venue ids')
    parser.add_argument('--confirm', default='', help='pass FLATTEN to actually trade')
    args = parser.parse_args()

    dry_run = args.confirm != 'FLATTEN'
    if dry_run:
        print('DRY RUN — pass --confirm FLATTEN to place reduce-only orders.\n')

    total = 0
    for venue in args.venues.split(','):
        total += await flatten_venue(venue.strip(), dry_run=dry_run)
    print(f'\n{"would close" if dry_run else "closed"} {total} position(s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
