"""``ux`` — the one command.

    ux                 start the dashboard; launch the bot from the browser
    ux demo            same, and start a demo run immediately
    ux doctor          check this machine is ready (python, ccxt, network, keys)
    ux backfill ...    download history to Parquet     (uxtrader.data.history)
    ux run ...         headless services, e.g. one per container (uxtrader.run)
    ux panic ...       emergency flatten from any laptop (uxtrader.ops.panic)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import webbrowser
from pathlib import Path

BOLD, DIM, GREEN, RED, YELLOW, RESET = ('\033[1m', '\033[2m', '\033[32m', '\033[31m',
                                        '\033[33m', '\033[0m')
if not sys.stdout.isatty() or os.environ.get('NO_COLOR'):
    BOLD = DIM = GREEN = RED = YELLOW = RESET = ''


def _banner(url: str, token: str, data_dir: Path) -> None:
    print(f'''
  {BOLD}ux-trader{RESET}  {DIM}— dashboard is up{RESET}

  {BOLD}Open{RESET}      {url}/#token={token}
  {BOLD}Token{RESET}     {token}   {DIM}(the link above signs you in){RESET}
  {BOLD}Journal{RESET}   {data_dir / "journal.db"}

  {DIM}Pick Demo to see it run without keys or network. Ctrl-C stops everything.{RESET}
''')


async def _serve(args, autostart=None) -> int:
    import uvicorn

    from .api.main import LaunchRequest, LaunchStrategy, create_app
    from .app import Supervisor
    from .bus import InMemoryBus
    from .services.journal import Journal

    data_dir = Path(args.data_dir).expanduser()
    token = args.token or os.environ.get('UX_API_TOKEN') or secrets.token_urlsafe(12)
    bus = InMemoryBus()
    journal = Journal(data_dir / 'journal.db')
    await journal.start(bus)
    supervisor = Supervisor(bus)
    app = create_app(bus, token=token, journal=journal, launcher=supervisor)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port,
                                           log_level='warning'))
    url = f'http://{args.host}:{args.port}'

    async def after_start() -> None:
        while not server.started:
            await asyncio.sleep(0.05)
        _banner(url, token, data_dir)
        if not args.no_browser:
            webbrowser.open(f'{url}/#token={token}')
        if autostart:
            req = LaunchRequest(mode='demo', strategies=[
                LaunchStrategy(id='DEMO', risk_budget=0.2, stage=4),
                LaunchStrategy(id='S4', risk_budget=0.2, stage=4)], speed=args.speed)
            await supervisor.start(req)
            print(f'  {GREEN}●{RESET} demo running — {supervisor.status()["strategies"]}')

    try:
        await asyncio.gather(server.serve(), after_start())
    finally:
        journal.close()
    return 0


def _doctor(args) -> int:
    ok = True

    def check(label: str, passed: bool, detail: str = '', warn: bool = False) -> None:
        nonlocal ok
        mark = f'{GREEN}✓{RESET}' if passed else (f'{YELLOW}!{RESET}' if warn else f'{RED}✗{RESET}')
        ok = ok and (passed or warn)
        print(f'  {mark} {label}{f"  {DIM}{detail}{RESET}" if detail else ""}')

    print(f'\n  {BOLD}ux doctor{RESET}\n')
    check('Python ≥ 3.11', sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        import ccxt
        src = Path(ccxt.__file__).resolve()
        fork = (Path(__file__).resolve().parents[3] / 'python' / 'ccxt').resolve()
        check('ccxt importable', True, f'{ccxt.__version__} from {src.parent}')
        is_fork = str(src).startswith(str(fork))
        check('ccxt is this fork', is_fork, 'installed from this repository' if is_fork
              else 'using the PyPI build instead — run ./start.sh or pip install -e ..', warn=True)
    except Exception as exc:                                  # noqa: BLE001
        check('ccxt importable', False, str(exc))
    for mod in ('fastapi', 'uvicorn', 'pydantic', 'numpy', 'pandas', 'pyarrow', 'websockets'):
        try:
            __import__(mod)
            check(f'{mod}', True)
        except ImportError:
            check(f'{mod}', False, 'pip install -e .')
    from .settings import credentials
    for venue in ('binanceusdm', 'bybit', 'okx'):
        has = bool(credentials(venue))
        check(f'{venue} keys', has, 'present (only needed for live)' if has
              else f'not set — {venue.upper()}_APIKEY / _SECRET (only needed for live)', warn=True)
    if not args.offline:
        import urllib.request
        for venue, url in (('binanceusdm', 'https://fapi.binance.com/fapi/v1/time'),
                           ('bybit', 'https://api.bybit.com/v5/market/time')):
            try:
                urllib.request.urlopen(url, timeout=5)
                check(f'{venue} reachable', True, 'paper and live can run')
            except Exception as exc:                          # noqa: BLE001
                check(f'{venue} reachable', False, f'{exc} — demo mode still works', warn=True)
    print(f'\n  {GREEN + "Ready." if ok else RED + "Fix the ✗ items above."}{RESET}\n')
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough = {'backfill': 'uxtrader.data.history', 'run': 'uxtrader.run',
                   'panic': 'uxtrader.ops.panic'}
    if argv and argv[0] in passthrough:
        import runpy
        sys.argv = [f'ux {argv[0]}'] + argv[1:]
        runpy.run_module(passthrough[argv[0]], run_name='__main__')
        return 0

    ap = argparse.ArgumentParser(prog='ux', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', nargs='?', default='up', choices=['up', 'demo', 'doctor'])
    ap.add_argument('--host', default='127.0.0.1',
                    help='bind address (default: localhost only — keep it that way)')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--token', default=None, help='API token (default: random per start)')
    ap.add_argument('--data-dir', default=os.environ.get('UX_DATA_DIR', '~/.uxtrader'))
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--speed', type=float, default=0.1,
                    help='demo: real seconds per simulated minute')
    ap.add_argument('--offline', action='store_true', help='doctor: skip network checks')
    args = ap.parse_args(argv)

    if args.command == 'doctor':
        return _doctor(args)
    try:
        return asyncio.run(_serve(args, autostart=args.command == 'demo'))
    except KeyboardInterrupt:
        print('\n  stopped.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
