"""Command line: ``python -m gse <command>``.

  terminal            start the web terminal (and the bot, if autostart is on)
  backtest            run a backtest from the command line and print the gates
  data ...            list, import, download or generate market data
  auth ...            cTrader OAuth helpers for headless servers
  accounts            list the cTrader accounts behind the stored token
  mock-server         run a local imitation of the cTrader Open API (testing)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    from . import paths

    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        from logging.handlers import RotatingFileHandler

        handlers.append(RotatingFileHandler(paths.logs_dir() / "gse.log", maxBytes=5_000_000, backupCount=5))
    except OSError:
        pass
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def cmd_terminal(a) -> None:
    from .config import load_settings
    from .terminal.server import run_terminal

    s = load_settings()
    run_terminal(a.host or s.terminal.host, a.port or s.terminal.port, autostart=not a.no_autostart)


def _parse_set(items: list[str]) -> dict:
    out = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def cmd_backtest(a) -> None:
    from .backtest.report import PRESETS, run_full
    from .config import load_settings

    params = {
        "dataset": a.dataset, "start": a.start, "end": a.end, "settings": load_settings().model_dump(),
        "preset": a.preset, "overrides": _parse_set(a.set), "random_iterations": a.iterations,
        "use_news": not a.no_news, "fill_model": "ticks" if a.ticks else "bars",
    }
    if a.preset and a.preset not in PRESETS:
        sys.exit(f"unknown preset; choose from {', '.join(PRESETS)}")
    last = [-1]

    def progress(frac, msg):
        pct = int(frac * 100)
        if pct != last[0]:
            last[0] = pct
            print(f"\r{pct:3d}% {msg[:60]:<60}", end="", flush=True)

    rep = run_full(params, progress)
    print()
    s = rep["summary"]
    print(f"\n{rep['dataset'].get('id')} {rep['start']} -> {rep['end']}  ({rep['bars']} bars, {rep['elapsed_s']:.1f}s)")
    for w in rep["warnings"]:
        print(f"  ! {w}")
    if s.get("trades"):
        print(f"\nTrades {s['trades']}  win {s['win_rate']:.1%}  mean {s['mean_r']:+.3f}R  t {s['t_stat']:.2f}  "
              f"PF {s['profit_factor'] or 0:.2f}  return {s['return_pct']:+.2f}%  maxDD {s['max_drawdown_pct']:.2f}%  "
              f"worst day {s['worst_day_pct']:.2f}%")
        for m, st in rep["modules"].items():
            print(f"  {m}: {st['trades']:5d} trades  mean {st['mean_r']:+.3f}R  t {st['t_stat']:+.2f}  PF {st['profit_factor'] or 0:.2f}")
    print("\nValidation gates:")
    for g in rep["gates"]:
        res = "PASS" if g["pass"] is True else "FAIL" if g["pass"] is False else "----"
        print(f"  [{res}] {g['gate']:<24} {g['module']:<4} {g['threshold']}")
    if a.out:
        Path(a.out).write_text(json.dumps(rep, indent=1))
        print(f"\nFull report written to {a.out}")


def cmd_data(a) -> None:
    from .backtest import data as store

    if a.data_cmd == "list":
        for d in store.list_datasets():
            print(f"{d['id']:<28} {d['source']:<20} {d['start']} -> {d['end']}  {d['bars']:>9} bars  spread {d['spread_coverage']:.0%}")
    elif a.data_cmd == "import":
        bars = store.parse_csv_bars(Path(a.file).read_text(errors="replace"), tz_offset_hours=a.tz)
        if not len(bars):
            sys.exit("no bars recognised in the file")
        meta = store.save_dataset(a.dataset, bars, source="csv", notes=f"imported {Path(a.file).name}")
        print(f"Imported {meta['bars']} bars into {a.dataset} ({meta['start']} -> {meta['end']})")
    elif a.data_cmd == "synthetic":
        bars = store.generate_synthetic(date.fromisoformat(a.start), date.fromisoformat(a.end), seed=a.seed)
        store.delete_dataset(a.dataset)
        meta = store.save_dataset(a.dataset, bars, source="synthetic", notes="Random data for testing the software only")
        print(f"Generated {meta['bars']} synthetic bars into {a.dataset}")
    elif a.data_cmd == "dukascopy":
        from .backtest.dukascopy import download

        def progress(frac, msg):
            print(f"\r{frac:6.1%} {msg}", end="", flush=True)

        meta = asyncio.run(download(date.fromisoformat(a.start), date.fromisoformat(a.end), a.dataset, progress=progress))
        print(f"\nSaved {meta.get('bars', 0)} bars into {a.dataset}")


def cmd_auth(a) -> None:
    from .broker.ctrader.oauth import authorize_url, exchange_code
    from .config import load_secrets, load_settings, save_secrets

    s = load_secrets()
    if a.auth_cmd == "app":
        s.update(client_id=a.client_id, client_secret=a.client_secret)
        save_secrets(s)
        print("App credentials saved.")
    elif a.auth_cmd == "url":
        print(authorize_url(s["client_id"], load_settings().ctrader.redirect_uri))
        print("\nOpen it, log in, then copy the 'code' parameter from the URL you are redirected to and run:\n"
              "  python -m gse auth code <CODE>")
    elif a.auth_cmd == "code":
        tokens = asyncio.run(exchange_code(s["client_id"], s["client_secret"], a.code, load_settings().ctrader.redirect_uri))
        s.update(tokens)
        save_secrets(s)
        print("Access token saved.")
    elif a.auth_cmd == "token":
        import time

        s.update(access_token=a.access_token, token_expires_at=int(time.time() + 30 * 86400))
        if a.refresh_token:
            s["refresh_token"] = a.refresh_token
        save_secrets(s)
        print("Token saved.")


def cmd_accounts(a) -> None:
    from .live.manager import BotManager

    async def go():
        m = BotManager()
        for acc in await m.list_accounts():
            print(f"{acc['account_id']:>10}  {'LIVE' if acc['is_live'] else 'demo'}  login {acc.get('login')}  "
                  f"{acc.get('broker', '')}  {acc.get('balance')} {acc.get('currency', '')}")

    asyncio.run(go())


def cmd_mock(a) -> None:
    from .broker.ctrader.mock_server import serve_forever

    asyncio.run(serve_forever(a.port))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="gse", description="Gold Session Engine: XAU/USD bot for the cTrader Open API")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    t = sub.add_parser("terminal", help="start the web terminal")
    t.add_argument("--host")
    t.add_argument("--port", type=int)
    t.add_argument("--no-autostart", action="store_true", help="do not start the bot even if autostart is on")
    t.set_defaults(fn=cmd_terminal)

    b = sub.add_parser("backtest", help="run a backtest")
    b.add_argument("--dataset", required=True)
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--preset")
    b.add_argument("--iterations", type=int, default=200, help="random-entry baseline iterations")
    b.add_argument("--set", action="append", metavar="KEY=VALUE", help="override a setting, e.g. module_c.target_r=1.5")
    b.add_argument("--no-news", action="store_true")
    b.add_argument("--ticks", action="store_true", help="tick replay (needs raw cTrader ticks for the dataset)")
    b.add_argument("--out", help="write the full JSON report here")
    b.set_defaults(fn=cmd_backtest)

    d = sub.add_parser("data", help="market data")
    dsub = d.add_subparsers(dest="data_cmd", required=True)
    dsub.add_parser("list")
    di = dsub.add_parser("import")
    di.add_argument("file")
    di.add_argument("--dataset", required=True)
    di.add_argument("--tz", type=float, default=0.0, help="UTC offset of the file's timestamps, hours")
    dsy = dsub.add_parser("synthetic")
    dsy.add_argument("--start", default="2018-01-01")
    dsy.add_argument("--end", default="2026-09-30")
    dsy.add_argument("--seed", type=int, default=7)
    dsy.add_argument("--dataset", default="synthetic-XAUUSD")
    dd = dsub.add_parser("dukascopy")
    dd.add_argument("--start", default="2018-01-01")
    dd.add_argument("--end", default=date.today().isoformat())
    dd.add_argument("--dataset", default="dukascopy-XAUUSD")
    d.set_defaults(fn=cmd_data)

    a = sub.add_parser("auth", help="cTrader OAuth helpers")
    asub = a.add_subparsers(dest="auth_cmd", required=True)
    aa = asub.add_parser("app")
    aa.add_argument("client_id")
    aa.add_argument("client_secret")
    asub.add_parser("url")
    ac = asub.add_parser("code")
    ac.add_argument("code")
    at = asub.add_parser("token")
    at.add_argument("access_token")
    at.add_argument("--refresh-token")
    a.set_defaults(fn=cmd_auth)

    sub.add_parser("accounts", help="list cTrader accounts").set_defaults(fn=cmd_accounts)
    m = sub.add_parser("mock-server", help="local imitation of the cTrader Open API")
    m.add_argument("--port", type=int, default=5035)
    m.set_defaults(fn=cmd_mock)

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return
    _setup_logging(args.verbose)
    args.fn(args)


if __name__ == "__main__":
    main()
