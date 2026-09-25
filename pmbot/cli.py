"""Command line: `pmbot monitor | report | analyze-tape | analyze-wallet | kill`.

Phase 1 contains no command that can place, modify or cancel orders.
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


async def _run_monitor(cfg_path: str, verbose: bool) -> int:
    from pmbot.feeds.chainlink import ChainlinkFeed
    from pmbot.marketdata import MarketDataStream, PublicAPI
    from pmbot.monitor import Monitor

    cfg = load_config(cfg_path)
    secrets = load_secrets()
    setup_logging(cfg.paths.log_dir, "monitor", secrets.values(), cfg.paths.log_max_bytes,
                  cfg.paths.log_backups, verbose)
    if Path(cfg.paths.kill_file).exists():
        log.error("kill file %s exists; remove it to start", cfg.paths.kill_file)
        return 2
    store = Store(cfg.paths.db_path, mode="monitor")
    public = PublicAPI(cfg.api)
    feed = ChainlinkFeed(cfg.chainlink, cfg.api)
    monitor = Monitor(cfg, secrets, store, public, feed=feed)
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
        except NotImplementedError:  # Windows
            pass

    log.info("monitor starting (Phase 1: no trading). api_key=%s chainlink=%s",
             "yes" if secrets.has_api_key else "no", cfg.chainlink.source)
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
    return 0


def _parse_since(s: str | None) -> float | None:
    if not s:
        return None
    if s.endswith("h"):
        return time.time() - float(s[:-1]) * 3600
    if s.endswith("d"):
        return time.time() - float(s[:-1]) * 86400
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def main(argv: list[str] | None = None) -> int:
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

    t = sub.add_parser("analyze-tape", help="summarize the anonymous US trade tape")
    t.add_argument("--out", default="reports/tape.csv")

    w = sub.add_parser("analyze-wallet",
                       help="summarize a PUBLIC polymarket.com wallet (read-only)")
    w.add_argument("address")
    w.add_argument("--out", default="reports/wallet.csv")

    sub.add_parser("kill", help="create the kill file (running processes stop)")

    args = p.parse_args(argv)
    cfg = load_config(args.config)

    if args.cmd == "monitor":
        return asyncio.run(_run_monitor(args.config, args.verbose))
    if args.cmd == "kill":
        Path(cfg.paths.kill_file).touch()
        print(f"kill file created: {cfg.paths.kill_file} (delete it to allow restarts)")
        return 0
    if args.cmd == "analyze-wallet":
        from pmbot.wallet import analyze_wallet

        summary = analyze_wallet(args.address, args.out)
        print(json.dumps(summary, indent=2))
        print(f"per-market CSV: {args.out}")
        return 0

    store = Store(cfg.paths.db_path, mode="monitor")
    try:
        if args.cmd == "report":
            from pmbot.reports import build_report, export_csv, render_text

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
