"""Structured JSON logging. Build plan Phase 0 §2.

Secrets never reach logs (§0.8): `redact()` scrubs known-sensitive keys, and the
formatter refuses to serialise anything it cannot JSON-encode.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

from pythonjsonlogger import json as jsonlogger

SENSITIVE = (
    "token", "secret", "key", "seed", "totp", "password", "authorization",
    "vapid_private", "access_token",
)


def redact(payload: Any) -> Any:
    """Recursively mask values whose key looks sensitive."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            if any(s in str(k).lower() for s in SENSITIVE):
                out[k] = f"<redacted:{len(str(v))}>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(payload, (list, tuple)):
        return [redact(v) for v in payload]
    return payload


class _Formatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):  # noqa: ANN001
        super().add_fields(log_record, record, message_dict)
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        for noise in ("taskName", "levelname", "name"):
            log_record.pop(noise, None)
        for k, v in list(log_record.items()):
            if any(s in k.lower() for s in SENSITIVE):
                log_record[k] = f"<redacted:{len(str(v))}>"


def setup(level: str | None = None) -> None:
    lvl = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    handler = logging.StreamHandler(sys.stdout)
    # Use real LogRecord attribute names; add_fields maps them to level/logger.
    handler.setFormatter(_Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)
    # These are chatty and say nothing we need.
    for noisy in ("apscheduler.executors.default", "httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
