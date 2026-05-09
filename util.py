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


_QH_QUARTERS: list[tuple[str, int, int]] = [
    ("QH1", 0, 899),       # seconds 0-899   (minutes 0-14)
    ("QH2", 900, 1799),    # seconds 900-1799 (minutes 15-29)
    ("QH3", 1800, 2699),   # seconds 1800-2699 (minutes 30-44)
    ("QH4", 2700, 3599),   # seconds 2700-3599 (minutes 45-59)
]


def compute_nbc_quarters(
    per_second_data: list[float], n: int
) -> dict[str, Any]:
    """Compute NBC metrics for each quarter-hour from per-second kWh data.

    PG&E bills Non-Bypassable Charges based on net consumption over each
    15-minute interval. For complete quarters, sums all per-second kWh values
    and converts to Wh (clamped at zero). For incomplete quarters, computes a
    rate from the last 60 seconds of data within the current quarter (never
    crossing the quarter boundary) and extrapolates.

    Args:
        per_second_data: Per-second kWh values for the current hour.
        n: Number of seconds observed so far (may be less than
            ``len(per_second_data)`` due to API lag).

    Returns:
        Dict with keys QH1-QH4, each containing NBC metrics or None if the
        quarter has not yet started.
    """
    result: dict[str, Any] = {}
    for qh_name, start_idx, end_idx in _QH_QUARTERS:
        if n <= start_idx:
            result[qh_name] = None
            continue

        if n > end_idx:
            values = per_second_data[start_idx:end_idx + 1]
            raw_wh = sum(values) * 1000
            result[qh_name] = {
                "wh": max(0, raw_wh),
                "complete": True,
                "raw_wh": raw_wh,
            }
        else:
            remaining_seconds = end_idx + 1 - n
            obs_start = start_idx
            obs_end = min(n, end_idx + 1)
            raw_values = per_second_data[obs_start:obs_end]
            lookback_start = max(n - 60, start_idx)
            lookback_values = per_second_data[lookback_start:n]
            rate = (
                sum(lookback_values) / len(lookback_values)
                if lookback_values
                else 0.0
            )

            if not raw_values:
                result[qh_name] = {
                    "wh": 0,
                    "complete": False,
                    "raw_wh": 0,
                    "predicted_wh": 0,
                    "samples_used": 0,
                    "remaining_seconds": remaining_seconds,
                }
                continue

            raw_wh = sum(raw_values) * 1000
            predicted_wh = raw_wh + rate * remaining_seconds * 1000
            result[qh_name] = {
                "wh": max(0, predicted_wh),
                "complete": False,
                "raw_wh": raw_wh,
                "predicted_wh": predicted_wh,
                "samples_used": len(lookback_values),
                "remaining_seconds": remaining_seconds,
            }

    return result


def compute_nbc_quarters_for_window(
    prev_hour_data: list[float],
    current_hour_data: list[float],
    n_prev: int,
    n_current: int,
) -> dict[str, Any]:
    """Compute NBC metrics for the most recent 4 quarter-hour periods across two hours.

    Takes per-second data and observation counts for both the previous and current
    hours, computes NBC quarters for each hour independently, then selects the 4 most
    recent non-None quarters and relabels them QH1–QH4 in chronological order.

    Args:
        prev_hour_data: Per-second kWh values for the previous hour (up to 3600).
        current_hour_data: Per-second kWh values for the current hour (up to 3600).
        n_prev: Number of seconds observed in the previous hour (typically 3600).
        n_current: Number of seconds observed so far in the current hour.

    Returns:
        Dict with keys QH1-QH4 (most recent 4 non-None quarters, oldest first).
    """
    prev_result: dict[str, Any] = {}
    if prev_hour_data and n_prev > 0:
        prev_result = compute_nbc_quarters(prev_hour_data, n_prev)

    curr_result: dict[str, Any] = {}
    if current_hour_data and n_current > 0:
        curr_result = compute_nbc_quarters(current_hour_data, n_current)

    # Collect all non-None quarters in chronological order:
    # prev QH1, prev QH2, prev QH3, prev QH4, curr QH1, curr QH2, curr QH3, curr QH4
    all_quarters: list[Any] = []
    for qh in ("QH1", "QH2", "QH3", "QH4"):
        if prev_result.get(qh) is not None:
            all_quarters.append(prev_result[qh])
    for qh in ("QH1", "QH2", "QH3", "QH4"):
        if curr_result.get(qh) is not None:
            all_quarters.append(curr_result[qh])

    # Take the 4 most recent (from end of list)
    selected = all_quarters[-4:] if len(all_quarters) > 4 else list(all_quarters)

    # Build result dict with QH1–QH4 labels
    result: dict[str, Any] = {}
    for i, qh_name in enumerate(("QH1", "QH2", "QH3", "QH4")):
        if i < len(selected):
            result[qh_name] = selected[i]
        else:
            result[qh_name] = None

    return result
