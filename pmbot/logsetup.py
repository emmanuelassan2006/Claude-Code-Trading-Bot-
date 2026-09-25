"""Logging: console + size-rotated file, with secret redaction on every record."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


class RedactSecrets(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            for s in self.secrets:
                msg = msg.replace(s, "****")
            record.msg, record.args = msg, None
        return True


def setup_logging(log_dir: str, name: str, secrets: list[str], max_bytes: int,
                  backups: int, verbose: bool = False) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact = RedactSecrets(secrets)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            Path(log_dir) / f"{name}.log", maxBytes=max_bytes, backupCount=backups),
    ]
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(redact)
        root.addHandler(h)
    # third-party libraries are chatty at DEBUG and may log request headers
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
