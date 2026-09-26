"""Command line: `pmbot monitor | run | backtest | report | calibrate | leadlag |
longshot | ladder | analyze-tape | analyze-wallet | kill`.

No command can place, modify or cancel a real order: `run` paper-trades
against a simulated exchange, and `run --live` is refused (Phase 3).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pmbot.config import load_config, load_secrets
from pmbot.logsetup import setup_logging
from pmbot.store import Store

log = logging.getLogger("pmbot")


async def _run_monitor(cfg_path: str, verbose: bool, paper: bool = False) -> int:
    from pmbot.feeds.chainlink import ChainlinkFeed
    from pmbot.feeds.exchanges import ExchangeFeed
    from pmbot.marketdata import MarketDataStream, PublicAPI
    from pmbot.monitor import Monitor

    cfg = load_config(cfg_path)
    secrets = load_secrets()
    setup_logging(cfg.paths.log_dir, "paper" if paper else "monitor", secrets.values(),
                  cfg.paths.log_max_bytes,
                  cfg.paths.log_backups, verbose)
    if Path(cfg.paths.kill_file).exists():
        log.error("kill file %s exists; remove it to start", cfg.paths.kill_file)
        return 2
    store = Store(cfg.paths.db_path, mode="monitor")
    public = PublicAPI(cfg.api)
    feed = ChainlinkFeed(cfg.price_feed, cfg.api) if cfg.price_feed.source == "rtds" \
        else ExchangeFeed(cfg.price_feed, cfg.api)
    monitor = Monitor(cfg, secrets, store, public, feed=feed)
    engine_store = None
    if paper:
        from pmbot.engine import TradingEngine

        engine_store = Store(cfg.paths.db_path, mode="dry_run")
        engine = TradingEngine(cfg, engine_store, feed.history, mode="dry_run")
        monitor.engine = engine
        feed.on_tick = lambda asset, tick: engine.on_price(asset, time.time())
    stream = MarketDataStream(
        cfg.api, cfg.monitor, secrets, public, monitor.on_book, monitor.on_trade,
        on_status=lambda kind, detail: store.log_event("info", kind, detail),
    )
    monitor.stream = stream

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # websockets can raise inside transport callbacks when a proxy/server drops
    # the handshake; log one line instead of a full traceback.
    loop.set_exception_handler(
        lambda _l, ctx: log.warning("asyncio: %s %s", ctx.get("message"), ctx.get("exception")))
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: no loop signal handlers; a plain handler still lets Ctrl+C
            # trigger the same clean shutdown instead of a KeyboardInterrupt.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    log.info("%s starting. api_key=%s price_feed=%s",
             "PAPER TRADING (dry run, simulated orders only)" if paper else "monitor (no trading)",
             "yes" if secrets.has_api_key else "no", cfg.price_feed.source)
    store.log_event("info", "start", "monitor")
    tasks = [
        asyncio.create_task(monitor.discovery_loop(stop)),
        asyncio.create_task(monitor.lifecycle_loop(stop)),
        asyncio.create_task(stream.run_ws(stop)),
        asyncio.create_task(stream.run_poll(stop)),
        asyncio.create_task(feed.run(stop)),
    ]
    await stop.wait()
    log.info("stopping")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    monitor.shutdown()
    store.log_event("info", "stop", "monitor")
    await public.close()
    store.close()
    if engine_store is not None:
        engine_store.close()
    return 0


def _parse_since(s: str | None) -> float | None:
    if not s:
        return None
    if s.endswith("h"):
        return time.time() - float(s[:-1]) * 3600
    if s.endswith("d"):
        return time.time() - float(s[:-1]) * 86400
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def _use_certifi_bundle() -> None:
    """python.org macOS builds ship without a CA bundle; point OpenSSL at certifi's."""
    import os

    try:
        import certifi
    except ImportError:
        return
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())


def main(argv: list[str] | None = None) -> int:
    _use_certifi_bundle()
    p = argparse.ArgumentParser(prog="pmbot", description=__doc__)
    p.add_argument("--config", default="config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("monitor", help="discover windows, record books/gaps/trades (no trading)")
    m.add_argument("-v", "--verbose", action="store_true")

    r = sub.add_parser("report", help="print monitor statistics")
    r.add_argument("--since", help="e.g. 24h, 7d, or ISO date")
    r.add_argument("--min-edge", type=float, default=0.02, help="maker edge threshold")
    r.add_argument("--horizon", type=float, default=60.0, help="maker fill proxy horizon (s)")
    r.add_argument("--csv", metavar="DIR", help="also write CSVs to DIR")
    r.add_argument("--json", action="store_true")
    r.add_argument("--strategy", action="store_true", help="paper-trading P&L report")

    t = sub.add_parser("analyze-tape", help="summarize the anonymous US trade tape")
    t.add_argument("--out", default="reports/tape.csv")

    w = sub.add_parser("analyze-wallet",
                       help="summarize a PUBLIC polymarket.com wallet (read-only)")
    w.add_argument("address")
    w.add_argument("--out", default="reports/wallet.csv")

    run = sub.add_parser("run", help="paper-trade the strategy on live data (dry run)")
    run.add_argument("--live", action="store_true",
                     help="live trading (needs dry_run=false in config; not built yet)")
    run.add_argument("-v", "--verbose", action="store_true")

    b = sub.add_parser("backtest", help="replay recorded monitor data through the strategy")
    b.add_argument("--since", help="e.g. 24h, 7d, or ISO date")
    b.add_argument("--out", default=":memory:", help="SQLite file for results (default: memory)")

    cal = sub.add_parser("calibrate", help="is the model a better predictor than the market?")
    cal.add_argument("--since", help="e.g. 24h, 7d, or ISO date")
    cal.add_argument("--every", type=float, default=10.0, help="seconds between samples")

    ll = sub.add_parser("leadlag", help="does the book lag BTC moves enough to trade?")
    ll.add_argument("--since", help="e.g. 24h, 7d, or ISO date")
    ll.add_argument("--lookback", type=int, default=5, help="seconds of move to compare")
    ll.add_argument("--horizon", type=int, default=10, help="seconds to measure follow-through")

    ls = sub.add_parser("longshot", help="do cheap/late contracts win more than their price?")
    ls.add_argument("--since", help="e.g. 24h, 7d, or ISO date")

    lad = sub.add_parser("ladder", help="replay a two-sided resting-bid ladder on the US tape")
    lad.add_argument("--since", help="e.g. 24h, 7d, or ISO date")
    lad.add_argument("--levels", help="comma-separated bid prices (default 0.05,0.15,...,0.95)")
    lad.add_argument("--shares", type=float, default=1.0, help="shares per level")
    lad.add_argument("--place-until", type=float, default=None,
                     help="stop placing new levels this many seconds into the window")

    sub.add_parser("kill", help="create the kill file (running processes stop)")

    args = p.parse_args(argv)
    cfg = load_config(args.config)

    if args.cmd == "monitor":
        return asyncio.run(_run_monitor(args.config, args.verbose))
    if args.cmd == "run":
        if args.live:
            if cfg.dry_run:
                print("refusing --live: config has dry_run = true (both are required)")
                return 2
            print("live order submission is not built yet (Phase 3). Nothing was sent.")
            return 2
        return asyncio.run(_run_monitor(args.config, args.verbose, paper=True))
    if args.cmd == "backtest":
        from pmbot.backtest import run_backtest
        from pmbot.reports import build_strategy_report, render_strategy_text

        logging.basicConfig(level=logging.WARNING)
        src = Store(cfg.paths.db_path, mode="monitor")
        out = Store(args.out, mode="backtest")
        info = run_backtest(cfg, src, out, _parse_since(args.since))
        print(f"replayed: {info}")
        print(render_strategy_text(build_strategy_report(out, "backtest")))
        src.close()
        out.close()
        return 0
    if args.cmd == "kill":
        Path(cfg.paths.kill_file).touch()
        print(f"kill file created: {cfg.paths.kill_file} (delete it to allow restarts)")
        return 0
    if args.cmd == "analyze-wallet":
        from pmbot.wallet import analyze_wallet

        print("fetching trades and market resolutions (read-only; ~1 min per 300 markets)...")
        summary = analyze_wallet(args.address, args.out, progress=lambda i, n: print(
            f"  resolutions {i}/{n}", flush=True))
        print(json.dumps(summary, indent=2))
        print(f"per-market CSV: {args.out}")
        return 0

    store = Store(cfg.paths.db_path, mode="monitor")
    try:
        if args.cmd == "longshot":
            from pmbot.patterns import longshot, render_longshot

            print(render_longshot(longshot(cfg, store, _parse_since(args.since))))
            return 0
        if args.cmd == "ladder":
            from pmbot.patterns import ladder, render_ladder

            levels = [float(x) for x in args.levels.split(",")] if args.levels else None
            print(render_ladder(ladder(store, _parse_since(args.since), levels, args.shares,
                                       place_until_s=args.place_until)))
            return 0
        if args.cmd == "leadlag":
            from pmbot.leadlag import analyze
            from pmbot.leadlag import render as render_ll

            print(render_ll(analyze(cfg, store, _parse_since(args.since), args.lookback,
                                    args.horizon)))
            return 0
        if args.cmd == "calibrate":
            from pmbot.calibrate import collect, render, summarize

            print(render(summarize(collect(cfg, store, _parse_since(args.since), args.every))))
            return 0
        if args.cmd == "report":
            from pmbot.reports import build_report, export_csv, render_text

            if args.strategy:
                from pmbot.reports import build_strategy_report, render_strategy_text

                srep = build_strategy_report(store, "dry_run", _parse_since(args.since))
                print(json.dumps(srep, indent=2, default=str) if args.json
                      else render_strategy_text(srep))
                return 0
            rep = build_report(store, _parse_since(args.since), args.min_edge, args.horizon)
            print(json.dumps(rep, indent=2) if args.json
                  else render_text(rep, args.min_edge, args.horizon))
            if args.csv:
                for path in export_csv(store, rep, args.csv):
                    print(f"wrote {path}")
        elif args.cmd == "analyze-tape":
            from pmbot.wallet import analyze_tape

            print(json.dumps(analyze_tape(store, args.out), indent=2))
            print(f"per-window CSV: {args.out}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
