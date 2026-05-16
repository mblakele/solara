"""Tests for NBCReader backed by EnergyCache (subtask 4).

NBCReader now reads directly from a shared EnergyCache instance instead of
wrapping a fetch callable and using NBCCache. NBC quarters are computed on
demand from raw per-second samples via util.compute_nbc_quarters().
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from metrics import EnergyCache
from load_manager import NBCReader


def _make_energy_cache(
    sample_count: int = 1200,
    value: float = -0.001,
    now: datetime | None = None,
) -> EnergyCache:
    """Create an EnergyCache pre-populated with per-second kWh samples.

    Args:
        sample_count: Number of per-second samples to generate (defaults to ~20 min).
        value: kWh/second value for each sample. Negative = generation.
        now: Current time. Defaults to datetime.now(timezone.utc).

    Returns:
        EnergyCache with sample_count samples backfilled from `now`.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cache = EnergyCache(ttl_seconds=30)
    samples = [value] * sample_count
    # Build the cache as if data was just fetched.
    with cache._lock:
        cache._samples = samples
        cache._data_start = now - timedelta(seconds=sample_count)
        cache._last_sample_at = now - timedelta(seconds=1)
        cache._sample_count = sample_count
        cache._last_fetch_at = now
    return cache


def _make_energy_cache_with_data_point(
    sample_count: int = 1200,
    value: float = -0.001,
    now: datetime | None = None,
) -> EnergyCache:
    """Same as _make_energy_cache but sets _last_fetch_at to now for is_valid()."""
    if now is None:
        now = datetime.now(timezone.utc)
    cache = _make_energy_cache(sample_count, value, now)
    return cache


# --- NBCReader.__init__ tests ---


def test_nbc_reader_default_energy_cache():
    """NBCReader creates a default EnergyCache when none is provided."""
    reader = NBCReader()
    assert isinstance(reader.energy_cache, EnergyCache)


def test_nbc_reader_accepts_external_energy_cache():
    """NBCReader uses the EnergyCache instance passed to __init__."""
    external_cache = EnergyCache(ttl_seconds=60)
    reader = NBCReader(energy_cache=external_cache)
    assert reader.energy_cache is external_cache


def test_nbc_reader_default_device_name():
    """NBCReader defaults device_name to empty string."""
    reader = NBCReader()
    assert reader.device_name == ""


def test_nbc_reader_accepts_device_name():
    """NBCReader stores the device_name passed to __init__."""
    reader = NBCReader(device_name="my_panel")
    assert reader.device_name == "my_panel"


# --- NBCReader.get_current_qh() tests ---


def test_get_current_qh_returns_none_when_cache_empty():
    """get_current_qh returns None when EnergyCache has no samples."""
    now = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)
    reader = NBCReader()
    result = reader.get_current_qh(now)
    assert result is None


def test_get_current_qh_returns_none_when_cache_invalid():
    """get_current_qh returns None when EnergyCache is_valid() is False."""
    # Cache with no _last_fetch_at → is_valid() returns False.
    now = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)
    reader = NBCReader(energy_cache=EnergyCache(ttl_seconds=30))
    result = reader.get_current_qh(now)
    assert result is None


def test_get_current_qh_returns_tuple_from_cached_samples():
    """get_current_qh extracts QH prediction from cached per-second samples."""
    now = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)
    # QH2 (15:15-15:30). data_start aligned to QH1 boundary 15:00:00.
    # 1200 samples covers 15:00:00 to 15:20:00.
    # wall_clock QH=1 (QH2), data_start QH=0 (QH1), nbc_qh_index=(1-0)%4=1 → QH2 (incomplete).
    cache = EnergyCache(ttl_seconds=30)
    samples = [-0.001] * 1200
    with cache._lock:
        cache._samples = samples
        cache._data_start = datetime(2026, 5, 7, 15, 0, 0, tzinfo=timezone.utc)
        cache._last_sample_at = now - timedelta(seconds=1)
        cache._sample_count = 1200
        cache._last_fetch_at = now
    reader = NBCReader(energy_cache=cache)

    result = reader.get_current_qh(now=now)

    assert result is not None
    qh_name, predicted_wh, seconds_remaining, data_point_at = result
    assert qh_name == "QH2"
    # -0.001 kWh/s * 900 samples * 1000 = -900 Wh raw, extrapolated for incomplete QH
    assert predicted_wh < 0  # negative = generation
    assert seconds_remaining > 0
    assert data_point_at == now


def test_get_current_qh_returns_correct_qh_for_minute_5():
    """get_current_qh identifies QH1 when minute_in_hour is 5."""
    now = datetime(2026, 5, 7, 15, 5, 30, tzinfo=timezone.utc)
    # 330 samples covers QH1 (seconds 0-899), incomplete.
    cache = _make_energy_cache(sample_count=330, value=-0.002, now=now)
    reader = NBCReader(energy_cache=cache)

    result = reader.get_current_qh(now=now)

    assert result is not None
    qh_name, _, _, _ = result
    assert qh_name == "QH1"


def test_get_current_qh_returns_correct_qh_for_minute_40():
    """get_current_qh identifies QH3 when minute_in_hour is 40."""
    now = datetime(2026, 5, 7, 15, 40, 30, tzinfo=timezone.utc)
    # 2430 samples covers QH1-QH2 complete, QH3 incomplete.
    cache = _make_energy_cache(sample_count=2430, value=-0.0015, now=now)
    reader = NBCReader(energy_cache=cache)

    result = reader.get_current_qh(now=now)

    assert result is not None
    qh_name, _, _, _ = result
    assert qh_name == "QH3"


def test_get_current_qh_returns_data_point_at_from_cache():
    """get_current_qh returns the data_point_at timestamp from EnergyCache."""
    now = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)
    # data_start aligned to QH boundary 15:15:00 (QH3 start)
    cache = EnergyCache(ttl_seconds=30)
    samples = [-0.001] * 1200
    with cache._lock:
        cache._samples = samples
        cache._data_start = datetime(2026, 5, 7, 15, 15, 0, tzinfo=timezone.utc)
        cache._last_sample_at = now - timedelta(seconds=1)
        cache._sample_count = 1200
        cache._last_fetch_at = now
    reader = NBCReader(energy_cache=cache)

    _, _, _, data_point_at = reader.get_current_qh(now=now)

    assert data_point_at == now


def test_get_current_qh_with_positive_samples():
    """get_current_qh handles positive (consumption) samples correctly."""
    now = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)
    # data_start aligned to QH boundary 15:15:00 (QH2 start)
    cache = EnergyCache(ttl_seconds=30)
    samples = [0.001] * 1200
    with cache._lock:
        cache._samples = samples
        cache._data_start = datetime(2026, 5, 7, 15, 15, 0, tzinfo=timezone.utc)
        cache._last_sample_at = now - timedelta(seconds=1)
        cache._sample_count = 1200
        cache._last_fetch_at = now
    reader = NBCReader(energy_cache=cache)

    result = reader.get_current_qh(now=now)

    assert result is not None
    qh_name, predicted_wh, _, _ = result
    assert qh_name == "QH2"
    assert predicted_wh > 0  # positive = consumption


def test_get_current_qh_force_true_triggers_refetch():
    """force=True bypasses cache and triggers a fresh fetch via metrics_fetch."""
    fixed_now = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)
    # data_start aligned to QH boundary 15:15:00 (QH3 start)
    cache = EnergyCache(ttl_seconds=30)
    samples = [-0.001] * 1200
    with cache._lock:
        cache._samples = samples
        cache._data_start = datetime(2026, 5, 7, 15, 15, 0, tzinfo=timezone.utc)
        cache._last_sample_at = fixed_now - timedelta(seconds=1)
        cache._sample_count = 1200
        cache._last_fetch_at = fixed_now
    reader = NBCReader(energy_cache=cache)

    fetch_count = 0

    def mock_fetch():
        nonlocal fetch_count
        fetch_count += 1
        # Return fresh metrics data in the format _parse_metrics expects.
        return {
            "devices": [
                {
                    "nbc": {
                        "QH2": {
                            "complete": False,
                            "predicted_wh": -1500.0,
                            "remaining_seconds": 600,
                        }
                    },
                    "name": "test-device",
                }
            ]
        }

    reader._metrics_fetch = mock_fetch

    # First call: reads from existing cache.
    result1 = reader.get_current_qh(now=fixed_now)
    assert result1 is not None

    # Second call with force=True: should trigger fetch.
    result2 = reader.get_current_qh(now=fixed_now, force=True)
    assert fetch_count == 1
    assert result2 is not None


def test_get_current_qh_force_true_without_fetch_callable():
    """force=True without metrics_fetch falls back to reading from cache."""
    fixed_now = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)
    # data_start aligned to QH boundary 15:15:00 (QH3 start)
    cache = EnergyCache(ttl_seconds=30)
    samples = [-0.001] * 1200
    with cache._lock:
        cache._samples = samples
        cache._data_start = datetime(2026, 5, 7, 15, 15, 0, tzinfo=timezone.utc)
        cache._last_sample_at = fixed_now - timedelta(seconds=1)
        cache._sample_count = 1200
        cache._last_fetch_at = fixed_now
    reader = NBCReader(energy_cache=cache)

    # No metrics_fetch set. force=True should still read from cache.
    result = reader.get_current_qh(force=True, now=fixed_now)

    assert result is not None
    qh_name, _, _, _ = result
    assert qh_name == "QH2"


# --- NBCReader.get_current_qh_direct() tests (unchanged) ---


def test_get_current_qh_direct_with_valid_data():
    """get_current_qh_direct parses metrics data directly without cache."""
    reader = NBCReader()

    metrics_data = {
        "devices": [
            {
                "name": "panel",
                "nbc": {
                    "QH1": {"wh": 200.0, "complete": True},
                    "QH2": {
                        "wh": 100.0,
                        "complete": False,
                        "predicted_wh": -300.0,
                        "remaining_seconds": 600,
                    },
                    "QH3": None,
                    "QH4": None,
                },
            }
        ]
    }

    result = reader.get_current_qh_direct(metrics_data)

    assert result is not None
    qh_name, predicted_wh, seconds_remaining = result
    assert qh_name == "QH2"
    assert predicted_wh == -300.0
    assert seconds_remaining == 600


def test_get_current_qh_direct_with_none_data():
    """get_current_qh_direct returns None when metrics data is None."""
    reader = NBCReader()
    result = reader.get_current_qh_direct(None)
    assert result is None


def test_get_current_qh_direct_with_no_incomplete_qh():
    """get_current_qh_direct returns last complete QH when all are complete."""
    reader = NBCReader()

    metrics_data = {
        "devices": [
            {
                "name": "panel",
                "nbc": {
                    "QH1": {"wh": 200.0, "complete": True},
                    "QH2": {"wh": 300.0, "complete": True},
                    "QH3": None,
                    "QH4": None,
                },
            }
        ]
    }

    result = reader.get_current_qh_direct(metrics_data)

    assert result is not None
    qh_name, predicted_wh, _ = result
    # All quarters complete or not started → return last complete QH.
    assert qh_name == "QH2"
    assert predicted_wh == 300.0
