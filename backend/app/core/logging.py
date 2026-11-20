"""Structured JSON logging with secret redaction. The redaction processor must stay in the chain."""

from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

REDACTED = "[redacted]"
_SECRET_KEY = re.compile(r"(pass|secret|token|api_?key|authorization|cookie|ciphertext|dek|kek|signature)", re.I)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_scrub(v) for v in value)
    return value


def redact_secrets(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key == "event":
            continue
        if _SECRET_KEY.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _scrub(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        cache_logger_on_first_use=True,
    )
