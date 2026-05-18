"""
Utility functions and custom JSON provider for the application.
"""

from datetime import datetime, time as TimeType, timedelta
from typing import Any

import isodate
from flask.json.provider import DefaultJSONProvider

from config import cfg


def get_timezone() -> str:
    """Return the configured timezone, evaluated lazily for testability."""
    return cfg.timezone


def is_debug() -> bool:
    """Return whether debug mode is enabled, evaluated lazily for testability."""
    return cfg.debug


def custom_json_default(o: object) -> object:
    """Convert datetime, time and timedelta objects to ISO format strings."""
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, TimeType):
        return o.isoformat()
    if isinstance(o, timedelta):
        return isodate.duration_isoformat(o)
    if hasattr(o, "__iter__"):
        try:
            return list(iter(o))  # type: ignore[arg-type]
        except TypeError:
            pass
    return o


class CustomJSONProvider(DefaultJSONProvider):
    """Custom JSON provider handling datetime and timedelta serialization."""

    def default(self, o: object) -> object:
        """Serialize datetime and timedelta objects to ISO format strings."""
        return custom_json_default(o)


QH_PERIOD_SECONDS = 900

QH_NAMES: list[str] = ["QH1", "QH2", "QH3", "QH4"]


def ceil_to_qh(dt: datetime) -> datetime:
    """Return dt advanced to the next quarter-hour boundary, or unchanged if already on one."""
    truncated = dt.replace(second=0, microsecond=0)
    remainder = truncated.minute % 15
    if remainder == 0 and dt == truncated:
        return truncated
    minutes_to_next = (15 - remainder) % 15 or 15
    return truncated + timedelta(minutes=minutes_to_next)

def qh_seconds_remaining(dt: datetime) -> int:
    """Calculate seconds remaining in the QH period,
    using input datetime.

    Args:
        dt: datetime representing current wall-clock time.

    Returns:
        Remaining seconds in QH period.
    """
    return QH_PERIOD_SECONDS - (dt.second + (dt.minute % 15) * 60)

def compute_nbc_quarter(
    values: list[float]
) -> dict[str, Any] | None:
    """Compute NBC metrics for a single quarter-hour period from per-second kWh data.

    Args:
        values: Up to 900 per-second kWh values for a single QH period.
    """
    if values is None:
        return None

    values_len = len(values)
    if values_len < 1:
        return None

    assert values_len <= QH_PERIOD_SECONDS

    is_complete = values_len == QH_PERIOD_SECONDS
    raw_wh = sum(values) * 1000
    result = {
        "complete": is_complete,
        "raw_wh": raw_wh,
        "wh": max(0, raw_wh)
    }

    if not is_complete:
        remaining_seconds = QH_PERIOD_SECONDS - values_len
        predicted_wh = QH_PERIOD_SECONDS * raw_wh / values_len
        result["predicted_wh"] = predicted_wh
        result["remaining_seconds"] = remaining_seconds
        result["samples_used"] = values_len

    return result


def compute_nbc_quarters(values: list[float]) -> dict[str, Any]:
    """Compute NBC metrics for each quarter-hour from per-second kWh data.

    PG&E bills Non-Bypassable Charges based on net consumption over each
    15-minute interval. For complete quarters, sums all per-second kWh values
    and converts to Wh (clamped at zero). For incomplete quarters, computes a
    rate from the last 60 seconds of data within the current quarter (never
    crossing the quarter boundary) and extrapolates.

    Args:
        values: Per-second kWh values for up to 3600 seconds, aligned to QH boundary.

    Returns:
        Dict with keys QH1-QH4, each containing NBC metrics or None if the
        quarter has not yet started.
    """
    result: dict[str, Any] = {}
    values_len = len(values)
    assert values_len <= 3600

    incomplete_len = values_len % QH_PERIOD_SECONDS
    names_remaining = QH_NAMES

    # By definition, incomplete data is always in the most recent QH period.
    if incomplete_len > 0:
        values_incomplete = values[-incomplete_len:]
        result[QH_NAMES[0]] = compute_nbc_quarter(values_incomplete)
        names_remaining = names_remaining[1:]

    remaining_len = values_len - incomplete_len
    values_remaining = values[:remaining_len]
    for qh_name in names_remaining:
        values_qh = values_remaining[-QH_PERIOD_SECONDS:]
        values_remaining = values_remaining[:-QH_PERIOD_SECONDS]
        result[qh_name] = compute_nbc_quarter(values_qh)

    return result


def _clock_boundary_windows(now: datetime) -> list[tuple[datetime, datetime]]:
    """Return the 4 most recent 15-minute windows ending at or before ``now``.

    For example, if now is 2026-01-01 12:20:34, returns::

        [(12:15, 12:30), (12:00, 12:15), (11:45, 12:00), (11:30, 11:45)]

    Each window is a ``(start, end)`` tuple where start is inclusive and
    end is exclusive.

    Args:
        now: Current datetime (any timezone).

    Returns:
        List of 4 ``(start, end)`` tuples, most recent first.
    """
    windows: list[tuple[datetime, datetime]] = []
    # Find the start of the current 15-minute window.
    current_window_start = now.replace(
        minute=(now.minute // 15) * 15, second=0, microsecond=0
    )
    for i in range(4):
        window_start = current_window_start - timedelta(minutes=15 * i)
        window_end = window_start + timedelta(minutes=15)
        windows.append((window_start, window_end))

    return windows
