"""Shared helper functions for load management tests."""

from datetime import datetime, timezone


def _make_qh_data(
    qh_index: int, minute_in_hour: int, predicted_wh: float, complete: bool = False
) -> dict:
    """Create NBC quarter-hour data.

    Args:
        qh_index: 0-3 for QH1-QH4 (each starts at minute 0, 15, 30, 45).
        minute_in_hour: Absolute minute in the hour (0-59).
        predicted_wh: Predicted Wh for this quarter.
        complete: Whether the quarter is complete.
    """
    qh_start_minutes = [0, 15, 30, 45]
    qh_start_seconds = [m * 60 for m in qh_start_minutes]
    if complete:
        samples_used = 900
        remaining_seconds = 0
    else:
        elapsed_minutes = minute_in_hour - qh_start_minutes[qh_index]
        samples_used = max(0, min(elapsed_minutes * 60, 899))
        elapsed_seconds = minute_in_hour * 60 - qh_start_seconds[qh_index]
        remaining_seconds = max(0, 900 - elapsed_seconds)
    return {
        "wh": predicted_wh,
        "complete": complete,
        "raw_wh": predicted_wh * 0.8,
        "predicted_wh": predicted_wh,
        "samples_used": samples_used,
        "remaining_seconds": remaining_seconds,
    }


def _make_metrics_data(
    device_name: str, incomplete_qh: str | None, minute_in_hour: int = 20
) -> dict:
    """Create mock metrics data for NBCReader tests.

    Args:
        device_name: Name of the device in the metrics response.
        incomplete_qh: Which quarter-hour is currently incomplete (e.g., "QH2").
        minute_in_hour: Current minute within the hour (0-59).
    """
    qh_data = {}
    qh_order = ["QH1", "QH2", "QH3", "QH4"]

    for i, qh in enumerate(qh_order):
        if qh == incomplete_qh:
            qh_data[qh] = _make_qh_data(i, minute_in_hour, -300.0, False)
        elif incomplete_qh and qh_order.index(qh) < qh_order.index(incomplete_qh):
            qh_data[qh] = _make_qh_data(i, 60, 100.0, True)
        else:
            qh_data[qh] = None

    return {
        "devices": [
            {
                "name": device_name,
                "nbc": qh_data,
            }
        ]
    }


def _make_metrics_with_wh(
    device_name: str, predicted_wh: float
) -> dict:
    """Create mock metrics data with a specific predicted Wh value.

    Args:
        device_name: Name of the device in the metrics response.
        predicted_wh: The predicted Wh value for the incomplete quarter.
    """
    qh_order = ["QH1", "QH2", "QH3", "QH4"]

    qh_data = {}
    for i, qh in enumerate(qh_order):
        if i == 0:
            samples_used = 5 * 60
            remaining_seconds = 900 - samples_used
            qh_data[qh] = {
                "wh": predicted_wh,
                "complete": False,
                "raw_wh": predicted_wh * 0.8,
                "predicted_wh": predicted_wh,
                "samples_used": samples_used,
                "remaining_seconds": remaining_seconds,
            }
        else:
            qh_data[qh] = {
                "wh": 100.0,
                "complete": True,
                "raw_wh": 80.0,
                "predicted_wh": 100.0,
                "samples_used": 900,
            }

    return {"devices": [{"name": device_name, "nbc": qh_data}]}


def _now_utc() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)
