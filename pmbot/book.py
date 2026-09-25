"""Local order books and the Up/Down views derived from them.

Polymarket US has one instrument per market (YES / "long"). A crypto window may
be listed as:
  * "single": one market; Up = long YES, Down = short YES (or inverted if the
    market's outcome is "Down"). Down's asks are derived from YES bids at 1-p.
  * "pair":   two markets, one for Up and one for Down, each with its own book.
Which one Polymarket US uses is UNVERIFIED; discovery detects it per window and
everything downstream works on the Up/Down views below.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ONE = Decimal("1")


@dataclass(frozen=True)
class Level:
    price: Decimal
    qty: Decimal


def _amount(x: Any) -> Decimal | None:
    if x is None:
        return None
    if isinstance(x, dict):
        x = x.get("value")
        if x is None:
            return None
    return Decimal(str(x))


def parse_levels(raw: list[dict[str, Any]] | None) -> list[Level]:
    out: list[Level] = []
    for lvl in raw or []:
        px = _amount(lvl.get("px", lvl.get("price")))
        qty = _amount(lvl.get("qty", lvl.get("quantity", lvl.get("size"))))
        if px is None or qty is None or qty <= 0:
            continue
        out.append(Level(px, qty))
    return out


@dataclass
class OrderBook:
    slug: str
    bids: list[Level] = field(default_factory=list)  # best (highest) first
    asks: list[Level] = field(default_factory=list)  # best (lowest) first
    state: str | None = None
    exchange_ts: str | None = None
    recv_ts: float = 0.0
    updates: int = 0

    def apply_snapshot(self, md: dict[str, Any], recv_ts: float | None = None) -> None:
        """Replace the book with a full `marketData` payload (WS or REST)."""
        self.bids = sorted(parse_levels(md.get("bids")), key=lambda l: l.price, reverse=True)
        self.asks = sorted(parse_levels(md.get("offers", md.get("asks"))), key=lambda l: l.price)
        self.state = md.get("state", self.state)
        self.exchange_ts = md.get("transactTime", self.exchange_ts)
        self.recv_ts = recv_ts if recv_ts is not None else time.time()
        self.updates += 1

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None


def _complement(levels: list[Level]) -> list[Level]:
    return [Level(ONE - l.price, l.qty) for l in levels]


@dataclass
class WindowBooks:
    """Up/Down view over one ("single") or two ("pair") books."""

    structure: str  # "single" | "pair"
    up: OrderBook
    down: OrderBook | None = None
    long_is_up: bool = True  # single only: does buying YES mean "Up"?

    def __post_init__(self) -> None:
        if self.structure not in ("single", "pair"):
            raise ValueError(self.structure)
        if self.structure == "pair" and self.down is None:
            raise ValueError("pair structure needs a down book")

    def up_asks(self) -> list[Level]:
        if self.structure == "pair" or self.long_is_up:
            return self.up.asks
        return _complement(self.up.bids)

    def up_bids(self) -> list[Level]:
        if self.structure == "pair" or self.long_is_up:
            return self.up.bids
        return _complement(self.up.asks)

    def down_asks(self) -> list[Level]:
        if self.structure == "pair":
            return self.down.asks  # type: ignore[union-attr]
        return _complement(self.up.bids) if self.long_is_up else self.up.asks

    def down_bids(self) -> list[Level]:
        if self.structure == "pair":
            return self.down.bids  # type: ignore[union-attr]
        return _complement(self.up.asks) if self.long_is_up else self.up.bids

    def books(self) -> list[OrderBook]:
        return [self.up] if self.down is None else [self.up, self.down]

    def ready(self) -> bool:
        return all(b.updates > 0 for b in self.books())
