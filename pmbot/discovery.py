"""Discover crypto Up/Down windows from the public Polymarket US gateway.

Parsing is deliberately tolerant: the exact US event/market shapes for crypto
windows could not be verified from the build environment. Anything that can't
be classified is logged (once) and skipped, never guessed into a trade.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from pmbot.config import MarketsConfig

log = logging.getLogger(__name__)

ASSET_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "btc"),
    "ETH": ("ethereum", "eth"),
    "SOL": ("solana", "sol"),
    "XRP": ("xrp", "ripple"),
    "DOGE": ("dogecoin", "doge"),
    "BNB": ("bnb",),
    "HYPE": ("hyperliquid", "hype"),
}
DURATION_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_SLUG_DURATION = re.compile(r"(?:^|-)(5m|15m|1h|4h|1d|hourly|daily)(?:-|$)")
_SLUG_TS = re.compile(r"-(\d{10})(?:$|-)")
# Polymarket US: btc-updown-15m-2026-09-26-0245z (window start, UTC)
_SLUG_ISO = re.compile(r"-(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})z(?:$|-)")
_TITLE_MIN = re.compile(r"(\d+)\s*(?:min|minute)")
_MINUTES = {5: "5m", 15: "15m", 60: "1h", 240: "4h"}


def slug_start_ts(slug: str) -> float | None:
    """Window start encoded in the slug (US ISO-ish form or a unix timestamp)."""
    m = _SLUG_ISO.search(slug.lower())
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()
    m = _SLUG_TS.search(slug)
    return float(m.group(1)) if m else None


@dataclass
class Window:
    key: str                 # event slug (unique per window)
    asset: str
    duration: str
    start_ts: float
    end_ts: float
    structure: str           # "single" | "pair"
    up_slug: str
    down_slug: str | None
    long_is_up: bool
    title: str = ""
    raw_markets: list[dict[str, Any]] = field(default_factory=list)

    @property
    def slugs(self) -> list[str]:
        return [s for s in (self.up_slug, self.down_slug) if s]

    def seconds_to_close(self, now: float | None = None) -> float:
        return self.end_ts - (now if now is not None else time.time())


def parse_ts(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value / 1000 if value > 1e12 else value)
    s = str(value)
    if s.isdigit():
        return parse_ts(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def detect_asset(text: str, allowed: list[str]) -> str | None:
    t = text.lower()
    for asset in allowed:
        for alias in ASSET_ALIASES.get(asset.upper(), (asset.lower(),)):
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", t):
                return asset.upper()
    return None


def detect_duration(start: float | None, end: float | None, slug: str,
                    title: str = "") -> str | None:
    if start is not None and end is not None:
        span = round(end - start)
        for name, secs in DURATION_SECONDS.items():
            if abs(span - secs) <= 5:
                return name
    m = _SLUG_DURATION.search(slug.lower())
    if m:
        return {"hourly": "1h", "daily": "1d"}.get(m.group(1), m.group(1))
    m = _TITLE_MIN.search(title.lower())
    if m:
        return _MINUTES.get(int(m.group(1)))
    return None


def _side_of(market: dict[str, Any]) -> str | None:
    text = " ".join(
        str(market.get(k, "")) for k in ("outcome", "title", "slug")
    ).lower()
    has_up = re.search(r"(?<![a-z])up(?![a-z])", text) is not None
    has_down = re.search(r"(?<![a-z])down(?![a-z])", text) is not None
    if has_up and not has_down:
        return "up"
    if has_down and not has_up:
        return "down"
    # "Up or Down" titles: fall back to the outcome field alone
    out = str(market.get("outcome", "")).strip().lower()
    if out in ("up", "yes", "higher", "above"):
        return "up"
    if out in ("down", "no", "lower", "below"):
        return "down"
    return None


def classify_event(event: dict[str, Any], cfg: MarketsConfig) -> Window | None:
    """Turn a gateway event into a Window, or None if it isn't a tracked Up/Down window."""
    title = str(event.get("title", ""))
    slug = str(event.get("slug", ""))
    text = f"{title} {slug}".lower()
    if not any(k in text for k in cfg.title_keywords):
        return None
    asset = detect_asset(f"{title} {slug}", cfg.assets)
    if asset is None:
        return None

    markets = [m for m in event.get("markets") or [] if m.get("slug")]
    start = parse_ts(event.get("startTime") or event.get("startDate"))
    end = parse_ts(event.get("endTime") or event.get("endDate"))
    duration = detect_duration(start, end, slug, title)
    if start is None:
        start = slug_start_ts(slug)
    if duration:
        if end is None and start is not None:
            end = start + DURATION_SECONDS[duration]
        elif start is None and end is not None:
            start = end - DURATION_SECONDS[duration]
    if duration is None or duration not in cfg.durations or start is None or end is None:
        return None

    if len(markets) == 1:
        m = markets[0]
        side = _side_of(m)
        if side is None:
            # e.g. market titled "BTC Up or Down: 15 min" with no outcome field:
            # assume long/YES = Up (UNVERIFIED; check the market description).
            log.info("assuming long=Up for single market %s", m["slug"])
        return Window(slug, asset, duration, start, end, "single", m["slug"], None,
                      long_is_up=(side != "down"), title=title, raw_markets=markets)
    if len(markets) == 2:
        sides = {_side_of(m): m for m in markets}
        if "up" in sides and "down" in sides:
            return Window(slug, asset, duration, start, end, "pair", sides["up"]["slug"],
                          sides["down"]["slug"], True, title=title, raw_markets=markets)
    log.warning("unrecognised market layout for %s (%d markets); skipping", slug, len(markets))
    return None


Fetch = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class Discoverer:
    """Polls the gateway and returns windows that are live or about to open."""

    def __init__(self, cfg: MarketsConfig, fetch: Fetch) -> None:
        self.cfg = cfg
        self.fetch = fetch  # async (path, params) -> json; rate-limited by the caller
        self.known: dict[str, Window] = {}

    async def _candidate_events(self) -> list[dict[str, Any]]:
        events: dict[str, dict[str, Any]] = {}
        calls: list[tuple[str, dict[str, Any]]] = [
            ("/v1/events", {"active": True, "closed": False, "limit": 200}),
        ]
        calls += [("/v1/search", {"query": q, "limit": 100})
                  for q in self.cfg.search_queries]
        for path, params in calls:
            try:
                data = await self.fetch(path, params)
            except Exception as e:  # network errors are retried next cycle
                log.warning("discovery %s failed: %s", path, e)
                continue
            for ev in data.get("events", []) or []:
                if ev.get("slug"):
                    events[ev["slug"]] = ev
        return list(events.values())

    async def poll(self, now: float | None = None) -> list[Window]:
        """Return newly discovered windows that are open or open within lookahead."""
        now = now if now is not None else time.time()
        new: list[Window] = []
        for ev in await self._candidate_events():
            if ev["slug"] in self.known:
                continue
            w = classify_event(ev, self.cfg)
            if w is None:
                continue
            if w.end_ts <= now or w.start_ts > now + self.cfg.lookahead_s:
                continue
            self.known[w.key] = w
            new.append(w)
        # forget long-finished windows
        for k in [k for k, w in self.known.items() if w.end_ts < now - 3600]:
            del self.known[k]
        return new
