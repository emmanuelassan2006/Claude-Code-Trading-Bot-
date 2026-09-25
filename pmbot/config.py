"""Configuration: config.toml (non-secret settings) + .env (secrets only).

Every tunable number lives here with a documented default. Secrets are read
from the environment / .env and wrapped so they never appear in repr or logs.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass
class FeeConfig:
    # Taker fee = taker_rate * shares * p * (1 - p)   (docs.polymarket.us/fees)
    taker_rate: Decimal = Decimal("0.06")
    # Makers pay no fee.
    maker_fee_rate: Decimal = Decimal("0")
    # Maker rebate = maker_rebate_rate * shares * p * (1 - p). Sources disagree:
    # "0.0125" vs "25% of taker fee" (=0.015). Conservative default; verify.
    maker_rebate_rate: Decimal = Decimal("0.0125")
    # Fees/rebates are rounded per fill to this increment with banker's rounding.
    rounding_increment: Decimal = Decimal("0.01")
    rounding_mode: str = "half_even"  # "half_even" | "half_up" | "none"
    # Aggressive orders with several fills: total charged never exceeds the
    # rounded cumulative exact fee (adjustment only reduces a fill's charge).
    cap_cumulative: bool = True


@dataclass
class MarketsConfig:
    assets: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL", "XRP"])
    durations: list[str] = field(default_factory=lambda: ["5m", "15m", "1h"])
    # Text queries used to discover windows via /v1/search and to filter /v1/events.
    search_queries: list[str] = field(default_factory=lambda: ["up or down"])
    title_keywords: list[str] = field(default_factory=lambda: ["up or down", "updown"])
    # Default tick if a market does not tell us otherwise (contract spec: 0.001-0.01).
    tick_size: Decimal = Decimal("0.01")
    min_order_size: int = 1  # UNVERIFIED for Polymarket US
    discovery_interval_s: float = 20.0
    # Subscribe to windows that start within this many seconds.
    lookahead_s: float = 120.0
    # Keep a window subscribed this long after its end (to catch final trades).
    post_close_grace_s: float = 5.0


@dataclass
class MonitorConfig:
    # Log a taker-gap episode whenever ask_up + ask_down + fees < 1 - min_gap.
    min_gap: Decimal = Decimal("0")
    # Only walk this many levels when sizing a gap.
    max_depth_levels: int = 10
    # Book spread samples: at most one per window per interval (0 = every update).
    spread_sample_interval_s: float = 1.0
    record_trades: bool = True
    # REST polling fallback when the WebSocket is unavailable.
    book_poll_interval_s: float = 2.0
    ws_stale_after_s: float = 30.0
    # After a window ends, poll its settlement for up to this long.
    settlement_poll_s: float = 900.0


@dataclass
class ChainlinkConfig:
    # "rtds": polymarket.com Real-Time Data Service relay of Chainlink streams
    #         (public, read-only; international infrastructure).
    # "none": disabled.
    source: str = "rtds"
    rtds_url: str = "wss://ws-live-data.polymarket.com"
    rtds_topic: str = "crypto_prices_chainlink"
    symbols: dict[str, str] = field(
        default_factory=lambda: {
            "BTC": "btc/usd", "ETH": "eth/usd", "SOL": "sol/usd", "XRP": "xrp/usd",
        }
    )
    # TWAP window (seconds) per duration; polymarket.com switched to TWAP
    # settlement on 2026-08-07. Whether Polymarket US does is UNVERIFIED, so the
    # monitor records several candidate rules and scores them against settlement.
    twap_window_s: dict[str, float] = field(
        default_factory=lambda: {"5m": 30, "15m": 60, "1h": 60, "4h": 60}
    )
    history_s: float = 7200.0
    # Which candidate rule to show as "the" price to beat in live samples.
    # One of: twap_ending, twap_starting, first_tick_after, last_tick_before.
    ptb_rule: str = "twap_ending"


@dataclass
class ApiConfig:
    gateway_url: str = "https://gateway.polymarket.us"
    api_url: str = "https://api.polymarket.us"
    # Documented limit is 20 req/s per key across all endpoints; stay under it.
    rest_rate_per_s: float = 10.0
    rest_burst: int = 10
    timeout_s: float = 10.0
    reconnect_initial_s: float = 1.0
    reconnect_max_s: float = 60.0


@dataclass
class PathsConfig:
    db_path: str = "data/pmbot.db"
    log_dir: str = "logs"
    log_max_bytes: int = 20_000_000
    log_backups: int = 10
    kill_file: str = "KILL"


@dataclass
class Config:
    # Phase 1 never trades; this flag is carried for Phase 2/3.
    dry_run: bool = True
    fees: FeeConfig = field(default_factory=FeeConfig)
    markets: MarketsConfig = field(default_factory=MarketsConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    chainlink: ChainlinkConfig = field(default_factory=ChainlinkConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)


def _coerce(value: Any, default: Any, name: str) -> Any:
    if isinstance(default, Decimal):
        return Decimal(str(value))
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"config {name}: expected bool, got {value!r}")
        return value
    if isinstance(default, float) and isinstance(value, int):
        return float(value)
    return value


def _merge(obj: Any, data: dict[str, Any], prefix: str) -> None:
    fields = {f.name: f for f in dataclasses.fields(obj)}
    for key, value in data.items():
        if key not in fields:
            raise ValueError(f"unknown config key: {prefix}{key}")
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"config {prefix}{key} must be a table")
            _merge(current, value, f"{prefix}{key}.")
        else:
            setattr(obj, key, _coerce(value, current, prefix + key))


def load_config(path: str | os.PathLike[str] | None = "config.toml") -> Config:
    """Load config.toml over the defaults. A missing file means all defaults."""
    cfg = Config()
    if path and Path(path).exists():
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f), "")
    return cfg


class Secret:
    """A string that refuses to print itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None) -> None:
        self._value = value or ""

    def get(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret(****)" if self._value else "Secret(<empty>)"

    __str__ = __repr__


@dataclass(frozen=True)
class Secrets:
    key_id: Secret
    secret_key: Secret
    telegram_token: Secret
    telegram_chat_id: Secret

    @property
    def has_api_key(self) -> bool:
        return bool(self.key_id) and bool(self.secret_key)

    def values(self) -> list[str]:
        return [s.get() for s in (self.key_id, self.secret_key, self.telegram_token) if s]


def load_secrets(env_file: str | None = ".env") -> Secrets:
    if env_file and Path(env_file).exists():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    return Secrets(
        key_id=Secret(os.environ.get("POLYMARKET_KEY_ID")),
        secret_key=Secret(os.environ.get("POLYMARKET_SECRET_KEY")),
        telegram_token=Secret(os.environ.get("TELEGRAM_BOT_TOKEN")),
        telegram_chat_id=Secret(os.environ.get("TELEGRAM_CHAT_ID")),
    )
