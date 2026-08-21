"""Log formatters that render structured ``extra=`` fields.

Solara logs rich context at cycle boundaries and decision points via
``logger.info(..., extra={"event": ..., "gap_wh": ...})`` dicts. Standard
``logging`` formatters ignore caller-supplied attributes, so that context
never reached production output. These formatters append it:

- :class:`StructuredFormatter` (default) — human-readable text with a
  ``[key=value ...]`` suffix.
- :class:`JsonFormatter` — one JSON object per line, selected with
  ``LOG_FORMAT=json``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

# Standard LogRecord attributes — anything else on the record is a
# caller-supplied ``extra=`` field.
_RESERVED_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
})


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Return the caller-supplied ``extra=`` fields on *record*."""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_ATTRS
    }


def _render(value: Any) -> str:
    """Render an extra value compactly (JSON for containers)."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)
    return str(value)


class StructuredFormatter(logging.Formatter):
    """Text formatter appending extras as ``[key=value ...]``."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        extras = _extra_fields(record)
        if not extras:
            return message
        suffix = " ".join(
            f"{key}={_render(value)}" for key, value in sorted(extras.items())
        )
        return f"{message} [{suffix}]"


class JsonFormatter(logging.Formatter):
    """Formatter emitting one JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in _extra_fields(record).items():
            payload[key] = value
        return json.dumps(payload, default=str)


def create_formatter(
    json_mode: bool,
    fmt: str | None = None,
    datefmt: str | None = None,
) -> logging.Formatter:
    """Return the formatter matching the requested output mode.

    Args:
        json_mode: True selects :class:`JsonFormatter`; False (the
            default) selects :class:`StructuredFormatter`.
        fmt: Optional base text format string (text mode only). Callers
            with an established line layout (e.g. the rotating file
            handler) pass their existing format to preserve output.
        datefmt: Optional strftime format for ``%(asctime)s``.

    Returns:
        A configured formatter instance.
    """
    if json_mode:
        return JsonFormatter()
    if fmt is not None:
        return StructuredFormatter(fmt, datefmt=datefmt)
    return StructuredFormatter()
