"""
Utility functions and custom JSON provider for the application.
"""

from dataclasses import dataclass
from datetime import datetime, time as TimeType, timedelta
from typing import Any

import isodate
from flask.json.provider import DefaultJSONProvider

from config import Config, _config


def get_timezone(config: Config | None = None) -> str:
    """Return the configured timezone, evaluated lazily for testability.

    Args:
        config: Optional Config instance. Falls back to module-level
            singleton when None.
    """
    resolved = config if config is not None else _config
    return resolved.timezone


def is_debug(config: Config | None = None) -> bool:
    """Return whether debug mode is enabled, evaluated lazily for testability.

    Args:
        config: Optional Config instance. Falls back to module-level
            singleton when None.
    """
    resolved = config if config is not None else _config
    return resolved.debug


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

@dataclass(frozen=True)
class NBCQuarter:
    """NBC (Non-Bypassable Charge) metrics for a single 15-minute quarter-hour.

    For complete quarters, ``prediction_w``, ``predicted_wh``,
    ``remaining_seconds``, and ``samples_used`` are all ``None``.

    Attributes:
        complete: Whether all 900 per-second samples are present.
        raw_wh: Sum of per-second kWh values converted to watt-hours.
        wh: ``raw_wh`` clamped to zero (negative values become 0).
        prediction_w: Estimated power rate in watts for incomplete quarters.
        predicted_wh: Extrapolated watt-hours for incomplete quarters.
        remaining_seconds: Seconds remaining in the quarter for incomplete quarters.
        samples_used: Number of per-second samples used for incomplete quarters.
    """

    complete: bool
    raw_wh: float
    wh: float
    prediction_values: int | None = None
    prediction_w: float | None = None
    predicted_wh: float | None = None
    remaining_seconds: int | None = None
    samples_used: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict with the same shape as the original."""
        return {
            "complete": self.complete,
            "raw_wh": self.raw_wh,
            "wh": self.wh,
            "prediction_values": self.prediction_values,
            "prediction_w": self.prediction_w,
            "predicted_wh": self.predicted_wh,
            "remaining_seconds": self.remaining_seconds,
            "samples_used": self.samples_used,
        }


@dataclass(frozen=True)
class NBCQuarterSet:
    """All four quarter-hour NBC results for the current hour.

    Attributes:
        qh1: Most recent quarter-hour (incomplete if data is still arriving).
        qh2: Second most recent quarter-hour (always complete).
        qh3: Third most recent quarter-hour (always complete).
        qh4: Fourth most recent quarter-hour (always complete).
    """

    qh1: NBCQuarter | None
    qh2: NBCQuarter | None
    qh3: NBCQuarter | None
    qh4: NBCQuarter | None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to ``{"QH1": ..., "QH2": ..., "QH3": ..., "QH4": ...}``.

        Returns:
            Dict with quarter names as keys and ``None`` for absent quarters.
        """
        return {
            "QH1": self.qh1.to_dict() if self.qh1 else None,
            "QH2": self.qh2.to_dict() if self.qh2 else None,
            "QH3": self.qh3.to_dict() if self.qh3 else None,
            "QH4": self.qh4.to_dict() if self.qh4 else None,
        }


def compute_nbc_quarter(
    values: list[float],
    prediction_window_seconds: int | None = None,
) -> NBCQuarter | None:
    """Compute NBC metrics for a single quarter-hour period from per-second kWh data.

    Only QH1 can ever be incomplete (0–899 samples). QH2, QH3, QH4 always
    receive exactly 900 samples and are always complete.

    Args:
        values: Up to 900 per-second kWh values for a single QH period.
        prediction_window_seconds: Number of trailing seconds to use for
            rate extrapolation when the quarter is incomplete. Defaults to
            60 when ``None``. Caps at ``len(values)``. A value of 0 also
            falls back to 60.
    """
    if values is None:
        return None

    values_len = len(values)
    if values_len < 1:
        return None

    assert values_len <= QH_PERIOD_SECONDS

    is_complete = values_len == QH_PERIOD_SECONDS
    raw_wh = 1000 * sum(values)
    wh = max(0, raw_wh)

    if not is_complete:
        window = min(prediction_window_seconds or 60, values_len)
        prediction_values = values[-window:]
        prediction_values_len = len(prediction_values)
        prediction_w = 1000 * sum(prediction_values) / prediction_values_len
        remaining_seconds = QH_PERIOD_SECONDS - values_len
        return NBCQuarter(
            complete=False,
            raw_wh=raw_wh,
            wh=wh,
            prediction_values=prediction_values_len,
            prediction_w=prediction_w,
            predicted_wh=raw_wh + remaining_seconds * prediction_w,
            remaining_seconds=remaining_seconds,
            samples_used=values_len,
        )

    return NBCQuarter(complete=True, raw_wh=raw_wh, wh=wh)


def compute_nbc_quarters(
    values: list[float],
    prediction_window_seconds: int | None = None,
) -> NBCQuarterSet:
    """Compute NBC metrics for each quarter-hour from per-second kWh data.

    PG&E bills Non-Bypassable Charges based on net consumption over each
    15-minute interval. For complete quarters, sums all per-second kWh values
    and converts to Wh (clamped at zero). For incomplete quarters, computes a
    rate from the trailing samples within the current quarter (never crossing
    the quarter boundary) and extrapolates.

    Only QH1 can be incomplete — QH2, QH3, QH4 always receive exactly 900
    samples and are always complete.

    Args:
        values: Per-second kWh values for up to 3600 seconds, aligned to QH boundary.
        prediction_window_seconds: Number of trailing seconds to use for
            rate extrapolation of the incomplete quarter. Passed through to
            ``compute_nbc_quarter``. Defaults to 60 when ``None``.

    Returns:
        An ``NBCQuarterSet`` with keys QH1-QH4, each containing NBC metrics or
        ``None`` if the quarter has not yet started.
    """
    values_len = len(values)
    assert values_len <= 3600

    incomplete_len = values_len % QH_PERIOD_SECONDS

    # By definition, incomplete data is always in the most recent QH period.
    if incomplete_len > 0:
        qh1 = compute_nbc_quarter(
            values[-incomplete_len:],
            prediction_window_seconds=prediction_window_seconds,
        )
        names_remaining = QH_NAMES[1:]
    else:
        qh1 = None
        names_remaining = QH_NAMES

    remaining_len = values_len - incomplete_len
    values_remaining = values[:remaining_len]
    quarters: list[NBCQuarter | None] = []
    if incomplete_len > 0:
        quarters.append(qh1)
    for _qh_name in names_remaining:
        values_qh = values_remaining[-QH_PERIOD_SECONDS:]
        values_remaining = values_remaining[:-QH_PERIOD_SECONDS]
        quarters.append(compute_nbc_quarter(values_qh))

    # Pad with None if fewer than 4 quarters processed.
    while len(quarters) < 4:
        quarters.append(None)

    return NBCQuarterSet(qh1=quarters[0], qh2=quarters[1], qh3=quarters[2], qh4=quarters[3])


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
