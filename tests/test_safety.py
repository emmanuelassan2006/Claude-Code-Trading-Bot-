"""Phase 1 guarantees: no trading code paths, secrets never printed."""

import logging
import re
from pathlib import Path

import pytest

from pmbot.config import Secret, load_config
from pmbot.logsetup import RedactSecrets

PKG = Path(__file__).resolve().parents[1] / "pmbot"
FORBIDDEN = [
    r"/v1/orders?\b", r"/v1/order/", r"\.orders\.", r"cancel_all", r"close_position",
    r"ws\.private", r"PrivateWebSocket", r"/v1/portfolio", r"/v1/account",
]


@pytest.mark.parametrize("pattern", FORBIDDEN)
def test_no_trading_endpoints_in_phase1(pattern):
    hits = [p.name for p in PKG.rglob("*.py") if re.search(pattern, p.read_text())]
    assert hits == [], f"trading/private API reference {pattern!r} in {hits}"


def test_secret_never_reprs():
    s = Secret("super-secret-value")
    assert "super" not in repr(s) and "super" not in str(s) and "super" not in f"{s}"
    assert s.get() == "super-secret-value"


def test_log_redaction():
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "key=%s", ("abcdef123456",), None)
    RedactSecrets(["abcdef123456"]).filter(rec)
    assert rec.getMessage() == "key=****"


def test_dry_run_default_and_unknown_keys(tmp_path):
    assert load_config(None).dry_run is True
    bad = tmp_path / "c.toml"
    bad.write_text("[fees]\ntaker_rat = 0.05\n")
    with pytest.raises(ValueError):
        load_config(bad)


def test_config_decimal_coercion(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[fees]\ntaker_rate = 0.07\n[monitor]\nmin_gap = 0.005\n")
    cfg = load_config(p)
    assert str(cfg.fees.taker_rate) == "0.07" and str(cfg.monitor.min_gap) == "0.005"


def test_env_is_gitignored():
    gi = (PKG.parent / ".gitignore").read_text().splitlines()
    assert ".env" in gi
