"""Structured logging: JSON or console output, rotating files, secret redaction.

A per-task `correlation_id` contextvar ties every log line of a job together, so
a failed publication can be traced from the incoming URL to its final outcome:

    grep '"correlation_id":"<uuid>"' logs/app.log
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

#: Patterns whose matches are replaced wholesale in every emitted log record.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram bot tokens, including inside api.telegram.org URLs.
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "<redacted:bot_token>"),
    # Bearer / API keys in headers or query strings.
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-=]{12,}"), r"\1 <redacted>"),
    (
        re.compile(r"(?i)\b(api[-_]?key|access[-_]?token|auth[-_]?token|password|passwd|secret)"
                   r"(\"?\s*[:=]\s*\"?)([^\s\"',&}]{4,})"),
        r"\1\2<redacted>",
    ),
    # Credentials embedded in connection URLs.
    (re.compile(r"(?<=://)([^:/@\s]+):([^@\s]+)(?=@)"), r"\1:***"),
)

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


@contextmanager
def correlation_context(value: str | None) -> Iterator[None]:
    """Bind a correlation ID for the duration of a block."""
    token = _correlation_id.set(value)
    try:
        yield
    finally:
        _correlation_id.reset(token)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactionFilter(logging.Filter):
    """Scrubs secrets from the message, its args and any string `extra` values."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED and isinstance(value, str):
                record.__dict__[key] = redact(value)
        return True


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "correlation_id", None):
            record.correlation_id = _correlation_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line — ready for `docker logs`, Loki or jq."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if cid := getattr(record, "correlation_id", None):
            payload["correlation_id"] = cid
        for key, value in record.__dict__.items():
            if key in _RESERVED or key == "correlation_id":
                continue
            payload[key] = value if _json_safe(value) else repr(value)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact(self.formatStack(record.stack_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """Compact human-readable output for local development."""

    _FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

    def __init__(self) -> None:
        super().__init__(self._FMT, datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and k != "correlation_id" and v is not None
        }
        if cid := getattr(record, "correlation_id", None):
            base = f"{base} [cid={cid[:8]}]"
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in extras.items())
            base = f"{base} | {rendered}"
        return base


def _json_safe(value: object) -> bool:
    return isinstance(value, (str, int, float, bool, type(None), list, dict))


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
    log_dir: Path | None = None,
    max_bytes: int = 10_485_760,
    backup_count: int = 7,
) -> None:
    """Install handlers on the root logger. Safe to call more than once."""
    formatter: logging.Formatter = (
        JsonFormatter() if fmt == "json" else ConsoleFormatter()
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "app.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:  # read-only mount, wrong owner, ...
            root.warning("File logging disabled: %s", exc)

    for handler in root.handlers:
        handler.addFilter(RedactionFilter())
        handler.addFilter(CorrelationFilter())

    # Third-party loggers are far too chatty at INFO.
    for noisy, noisy_level in (
        ("aiogram.event", logging.WARNING),
        ("aiogram.dispatcher", logging.WARNING),
        ("aiohttp.access", logging.WARNING),
        ("sqlalchemy.engine", logging.WARNING),
        ("asyncio", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(noisy_level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
