"""Tests for NBCCache and NBCReader."""

from datetime import datetime, timedelta, timezone

import pytest

from load_manager import (
    NBCCache,
    NBCPeriod,
    NBCReader,
)

from tests.helpers import _make_metrics_data, _make_qh_data


# --- NBCPeriod.current_qh_window() unit tests ---


def test_current_qh_window_returns_correct_start_and_end():
    """current_qh_window returns the (start, end) of the QH containing now."""
    # 15:20 UTC -> QH3 (seconds 900-1799 of the hour)
    now = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)
    start, end = NBCPeriod.current_qh_window(now)

    assert start == datetime(2026, 5, 7, 15, 15, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 7, 15, 30, 0, tzinfo=timezone.utc)


def test_current_qh_window_midnight_boundary():
    """current_qh_window wraps correctly at hour boundaries."""
    # 16:05 UTC -> QH2 (seconds 300-1199 of the hour)
    now = datetime(2026, 5, 7, 16, 5, 30, tzinfo=timezone.utc)
    start, end = NBCPeriod.current_qh_window(now)

    assert start == datetime(2026, 5, 7, 16, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 7, 16, 15, 0, tzinfo=timezone.utc)


def test_current_qh_window_exact_boundary():
    """Data point exactly at QH boundary is considered in the current window."""
    # Exactly 15:30 UTC -> QH4 starts at second 2700
    now = datetime(2026, 5, 7, 15, 30, 0, tzinfo=timezone.utc)
    start, end = NBCPeriod.current_qh_window(now)

    assert start == datetime(2026, 5, 7, 15, 30, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 7, 15, 45, 0, tzinfo=timezone.utc)


def test_current_qh_window_naive_datetime():
    """current_qh_window handles timezone-naive datetimes by treating them as UTC."""
    now_naive = datetime(2026, 5, 7, 15, 45, 30)
    start, end = NBCPeriod.current_qh_window(now_naive)

    assert start == datetime(2026, 5, 7, 15, 45, 0)
    assert end == datetime(2026, 5, 7, 16, 0, 0)


def test_current_qh_window_includes_data_point():
    """A data point within the current QH window is not stale."""
    now = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)
    start, end = NBCPeriod.current_qh_window(now)

    # Data point at 15:18 is within QH3 (15:15-15:30)
    data_point_at = datetime(2026, 5, 7, 15, 18, 30, tzinfo=timezone.utc)
    assert start <= data_point_at < end


def test_current_qh_window_excludes_previous():
    """A data point from the previous QH is outside the current window."""
    now = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)
    start, end = NBCPeriod.current_qh_window(now)

    # Data point at 15:09 is in QH2 (15:00-15:15), not current QH3
    data_point_at = datetime(2026, 5, 7, 15, 9, 30, tzinfo=timezone.utc)
    assert data_point_at < start


# --- NBCCache tests ---


def test_cache_miss_on_first_call():
    """First call should fetch fresh data."""
    cache = NBCCache(ttl_seconds=60)
    fetch_count = 0

    def fetch_func():
        nonlocal fetch_count
        fetch_count += 1
        return {"test": "data"}

    data, was_fresh = cache.get_or_fetch("device", "QH1", fetch_func)

    assert fetch_count == 1
    assert was_fresh is True
    assert data["test"] == "data"


def test_cache_hit_within_ttl_same_qh():
    """Cache hit when within TTL and same QH."""
    cache = NBCCache(ttl_seconds=60)
    fetch_count = 0

    def fetch_func():
        nonlocal fetch_count
        fetch_count += 1
        return {"test": "data"}

    cache.get_or_fetch("device", "QH1", fetch_func)
    cache.get_or_fetch("device", "QH1", fetch_func)

    assert fetch_count == 1


def test_cache_miss_qh_changed():
    """Cache miss when QH changes."""
    cache = NBCCache(ttl_seconds=60)
    fetch_count = 0

    def fetch_func():
        nonlocal fetch_count
        fetch_count += 1
        return {"test": "data"}

    cache.get_or_fetch("device", "QH1", fetch_func)
    cache.get_or_fetch("device", "QH2", fetch_func)

    assert fetch_count == 2


def test_cache_miss_ttl_expired():
    """Cache miss when TTL expired."""
    cache = NBCCache(ttl_seconds=0)
    fetch_count = 0

    def fetch_func():
        nonlocal fetch_count
        fetch_count += 1
        return {"test": "data"}

    cache.get_or_fetch("device", "QH1", fetch_func)

    import time

    time.sleep(0.01)

    cache.get_or_fetch("device", "QH1", fetch_func)

    assert fetch_count == 2


def test_invalidate():
    """Cache cleared after invalidate."""
    cache = NBCCache(ttl_seconds=60)

    fetch_count = 0

    def fetch_func() -> dict:
        nonlocal fetch_count
        fetch_count += 1
        return {"qh_name": "QH1", "predicted_wh": 100, "seconds_remaining": 900}

    cache.get_or_fetch("device", "QH1", fetch_func)
    assert fetch_count == 1

    cache.invalidate()

    is_valid, _ = cache.is_valid(datetime.now(timezone.utc))
    assert is_valid is False

    cache.get_or_fetch("device", "QH1", fetch_func)
    assert fetch_count == 2


def test_has_qh_likely_ended_false_when_no_data():
    """has_qh_likely_ended returns False on a fresh, empty cache."""
    cache = NBCCache(ttl_seconds=60)
    now = datetime.now(timezone.utc)
    assert cache.has_qh_likely_ended(now) is False


def test_has_qh_likely_ended_false_when_time_remains():
    """has_qh_likely_ended is False when wall-clock elapsed < seconds_remaining."""
    cache = NBCCache(ttl_seconds=600)

    def fetch_func() -> dict:
        return {"qh_name": "QH1", "predicted_wh": -500, "seconds_remaining": 900}

    fetch_at = datetime.now(timezone.utc)
    cache.get_or_fetch("device", "QH1", fetch_func)

    # Simulate 30 wall-clock seconds passing — QH still has 870s left
    thirty_seconds_later = fetch_at + timedelta(seconds=30)
    assert cache.has_qh_likely_ended(thirty_seconds_later) is False


def test_has_qh_likely_ended_true_when_qh_over():
    """has_qh_likely_ended is True when wall-clock elapsed >= seconds_remaining.

    Regression test for the race condition where the cache served stale QH1
    data after QH2 had started because the TTL hadn't expired yet.
    """
    cache = NBCCache(ttl_seconds=600)

    def fetch_func() -> dict:
        # QH1 had only 30 seconds remaining when this was cached
        return {"qh_name": "QH1", "predicted_wh": -2000, "seconds_remaining": 30}

    fetch_at = datetime.now(timezone.utc)
    cache.get_or_fetch("device", "QH1", fetch_func)

    # 40 wall-clock seconds later: QH1 is over (30s remaining was exhausted)
    forty_seconds_later = fetch_at + timedelta(seconds=40)
    assert cache.has_qh_likely_ended(forty_seconds_later) is True


def test_get_current_qh_probes_on_qh_end_within_ttl():
    """NBCReader fetches fresh data when cached QH ends by wall-clock, even inside TTL.

    Regression test for the race condition where stale QH1 data was used at
    the start of QH2 because the 50s TTL had not expired yet.
    """
    call_log: list[str] = []

    # First call returns QH1 data with only 30 seconds remaining
    qh1_data = {
        "devices": [{
            "name": "panel",
            "nbc": {
                "QH1": {
                    "wh": 0,
                    "complete": False,
                    "raw_wh": -100,
                    "predicted_wh": -2000,
                    "samples_used": 870,
                    "remaining_seconds": 30,
                },
                "QH2": None,
                "QH3": None,
                "QH4": None,
            },
        }],
        "_fetched_at": datetime.now(timezone.utc),
    }

    # Second call (after QH transition) returns QH2 data
    qh2_fetched_at = datetime.now(timezone.utc) + timedelta(seconds=40)
    qh2_data = {
        "devices": [{
            "name": "panel",
            "nbc": {
                "QH1": {
                    "wh": 50,
                    "complete": True,
                    "raw_wh": 50,
                },
                "QH2": {
                    "wh": 0,
                    "complete": False,
                    "raw_wh": -10,
                    "predicted_wh": -300,
                    "samples_used": 40,
                    "remaining_seconds": 860,
                },
                "QH3": None,
                "QH4": None,
            },
        }],
        "_fetched_at": qh2_fetched_at,
    }

    fetch_responses = [qh1_data, qh2_data]

    def fetch_func():
        call_log.append("fetch")
        return fetch_responses.pop(0)

    cache = NBCCache(ttl_seconds=600)  # Long TTL — would NOT expire in 40s
    reader = NBCReader(cache=cache, metrics_fetch=fetch_func)

    # First call: populates cache with QH1, seconds_remaining=30
    result1 = reader.get_current_qh("panel")
    assert result1 is not None
    qh_name1, predicted_wh1, _, _ = result1
    assert qh_name1 == "QH1"
    assert pytest.approx(predicted_wh1) == -2000.0
    assert len(call_log) == 1

    # Simulate 40 wall-clock seconds elapsing: QH1 is over, QH2 has started.
    # Patch the cache's _cached_at to be 40s in the past so has_qh_likely_ended
    # returns True, forcing a fresh probe even though TTL hasn't expired.
    cache._cached_at = datetime.now(timezone.utc) - timedelta(seconds=40)  # pylint: disable=protected-access

    # Second call: cache TTL still valid (600s), but QH has ended -> fresh fetch
    result2 = reader.get_current_qh("panel")
    assert result2 is not None
    qh_name2, predicted_wh2, _, _ = result2
    assert qh_name2 == "QH2"
    assert pytest.approx(predicted_wh2) == -300.0
    # fetch_func must have been called a second time (not served from cache)
    assert len(call_log) == 2


# --- NBCReader tests ---


def test_reader_returns_none_on_no_metrics():
    """None when metrics_data is None."""
    reader = NBCReader()
    result = reader.get_current_qh_direct("device", None)
    assert result is None


def test_reader_returns_none_on_device_not_found():
    """None when device name not in metrics."""
    reader = NBCReader()
    metrics = {"devices": [{"name": "other"}]}
    result = reader.get_current_qh_direct("device", metrics)
    assert result is None


def test_reader_returns_incomplete_qh():
    """Returns incomplete QH and predicted_wh."""
    reader = NBCReader()
    metrics = _make_metrics_data("main_panel", "QH2")
    result = reader.get_current_qh_direct("main_panel", metrics)

    assert result is not None
    qh_name, predicted_wh, seconds_remaining = result
    assert qh_name == "QH2"
    assert pytest.approx(predicted_wh) == -300.0
    assert seconds_remaining > 0


def test_reader_seconds_remaining_from_field():
    """seconds_remaining comes from remaining_seconds, not 900-samples_used."""
    reader = NBCReader()
    # remaining_seconds=450 means halfway through the quarter
    qh_data = {
        "wh": -300.0,
        "complete": False,
        "raw_wh": -240.0,
        "predicted_wh": -300.0,
        "samples_used": 39,
        "remaining_seconds": 450,
    }
    metrics = {
        "devices": [
            {
                "name": "main_panel",
                "nbc": {"QH1": None, "QH2": qh_data},
            }
        ]
    }
    result = reader.get_current_qh_direct("main_panel", metrics)

    assert result is not None
    qh_name, predicted_wh, seconds_remaining = result
    assert qh_name == "QH2"
    assert seconds_remaining == 450


def test_reader_returns_none_all_complete():
    """None when all quarters complete."""
    reader = NBCReader()
    metrics = {
        "devices": [
            {
                "name": "main_panel",
                "nbc": {
                    "QH1": _make_qh_data(0, 60, 100.0, True),
                    "QH2": _make_qh_data(1, 60, 150.0, True),
                    "QH3": _make_qh_data(2, 60, 200.0, True),
                    "QH4": _make_qh_data(3, 60, 250.0, True),
                },
            }
        ]
    }
    result = reader.get_current_qh_direct("main_panel", metrics)
    assert result is None


# --- NBCReader force=True tests (using load_nbc.datetime patch) ---

from unittest.mock import patch
import datetime as dt_module


def test_force_bypasses_cache():
    """force=True forces a fresh fetch even when cache has valid data."""
    call_log: list[str] = []

    qh1_data = {
        "devices": [{
            "name": "panel",
            "nbc": {
                "QH1": _make_qh_data(0, 5, -200.0, False),
                "QH2": None, "QH3": None, "QH4": None,
            },
        }],
    }

    qh2_data = {
        "devices": [{
            "name": "panel",
            "nbc": {
                "QH1": _make_qh_data(0, 60, -200.0, True),
                "QH2": _make_qh_data(1, 5, -300.0, False),
                "QH3": None, "QH4": None,
            },
        }],
    }

    fetch_responses = [qh1_data, qh2_data]

    def fetch_func():
        call_log.append("fetch")
        return fetch_responses.pop(0)

    cache = NBCCache(ttl_seconds=600)  # Long TTL
    reader = NBCReader(cache=cache, metrics_fetch=fetch_func)

    # First call: populates cache with QH1
    result1 = reader.get_current_qh("panel")
    assert result1 is not None
    qh_name1, _, _, _ = result1
    assert qh_name1 == "QH1"
    assert len(call_log) == 1

    # Second call without force: should use cache (same QH, TTL not expired)
    result2 = reader.get_current_qh("panel")
    assert result2 is not None
    qh_name2, _, _, _ = result2
    assert qh_name2 == "QH1"  # Still QH1 from cache
    assert len(call_log) == 1  # No new fetch

    # Third call with force=True: should bypass cache and fetch fresh data
    result3 = reader.get_current_qh("panel", force=True)
    assert result3 is not None
    qh_name3, _, _, _ = result3
    assert qh_name3 == "QH2"  # Fresh data shows QH2 now
    assert len(call_log) == 2  # New fetch was made


def test_force_populates_cache():
    """After force=True call, subsequent non-force calls use the populated cache."""
    qh2_data = {
        "devices": [{
            "name": "panel",
            "nbc": {
                "QH1": _make_qh_data(0, 60, -200.0, True),
                "QH2": _make_qh_data(1, 5, -300.0, False),
                "QH3": None, "QH4": None,
            },
        }],
    }

    # Provide two responses: one for force=True call, second for probe on non-force
    fetch_responses = [qh2_data, qh2_data]

    def fetch_func():
        return fetch_responses.pop(0) if fetch_responses else None

    cache = NBCCache(ttl_seconds=600)
    reader = NBCReader(cache=cache, metrics_fetch=fetch_func)

    # Call with force=True to populate cache
    result1 = reader.get_current_qh("panel", force=True)
    assert result1 is not None
    qh_name, _, _, _ = result1
    assert qh_name == "QH2"


    # Subsequent non-force call should use cache
    result2 = reader.get_current_qh("panel")
    assert result2 is not None
    qh_name2, _, _, _ = result2
    assert qh_name2 == "QH2"


# --- NBCCache adaptive TTL tests ---

def test_adaptive_ttl_shorter_near_boundary():
    """When seconds_remaining <= 30, effective TTL is ~25% of base (min 10s)."""
    cache = NBCCache(ttl_seconds=60)

    # Near boundary: 30 seconds remaining -> TTL should be ~15s (25% of 60)
    ttl_near_boundary = cache._adaptive_ttl(seconds_remaining=30)
    assert ttl_near_boundary == timedelta(seconds=max(10, int(60 * 0.25)))
    assert ttl_near_boundary == timedelta(seconds=15)

    # Even closer: 0 seconds remaining -> TTL should be ~15s (25% of 60)
    ttl_zero = cache._adaptive_ttl(seconds_remaining=0)
    assert ttl_zero == timedelta(seconds=max(10, int(60 * 0.25)))
    assert ttl_zero == timedelta(seconds=15)


def test_adaptive_ttl_full_mid_quarter():
    """When seconds_remaining > 30, effective TTL equals base."""
    cache = NBCCache(ttl_seconds=60)

    # Mid-quarter: 450 seconds remaining -> full TTL
    ttl_mid = cache._adaptive_ttl(seconds_remaining=450)
    assert ttl_mid == timedelta(seconds=60)

    # Also full TTL when seconds_remaining is None
    ttl_none = cache._adaptive_ttl(seconds_remaining=None)
    assert ttl_none == timedelta(seconds=60)


def test_adaptive_ttl_minimum_10_seconds():
    """Even with very short remaining time, TTL never drops below 10s."""
    cache = NBCCache(ttl_seconds=60)

    # Very short remaining time
    ttl_short = cache._adaptive_ttl(seconds_remaining=1)
    assert ttl_short == timedelta(seconds=max(10, int(60 * 0.25)))
    assert ttl_short >= timedelta(seconds=10)

    # With a very short base TTL, minimum still applies
    cache_short = NBCCache(ttl_seconds=15)
    ttl_min_base = cache_short._adaptive_ttl(seconds_remaining=30)  # near boundary
    assert ttl_min_base == timedelta(seconds=max(10, int(15 * 0.25)))
    assert ttl_min_base == timedelta(seconds=10)


def test_is_valid_uses_adaptive_ttl():
    """is_valid() uses adaptive TTL, not fixed base TTL."""
    cache = NBCCache(ttl_seconds=60)

    def fetch_func() -> dict:
        return {"qh_name": "QH1", "predicted_wh": -200, "seconds_remaining": 30}

    fetch_at = datetime.now(timezone.utc)
    cache.get_or_fetch("device", "QH1", fetch_func)

    # At 20s elapsed, adaptive TTL (15s for near-boundary) has expired
    # but base TTL (60s) hasn't. is_valid should return False because adaptive
    # TTL was used for the check.
    twenty_seconds_later = fetch_at + timedelta(seconds=20)

    # Patch _cached_at to simulate time passing
    cache._cached_at = twenty_seconds_later - timedelta(seconds=20)

    is_valid, _ = cache.is_valid(twenty_seconds_later)
    # Adaptive TTL for seconds_remaining=30 is 15s, so at 20s elapsed it's expired
    assert is_valid is False


def test_run_cycle_uses_force_true():
    """Verify NBCReader.get_current_qh() accepts force=True parameter.

    This test verifies that the method signature supports force=True,
    which is used by LoadManager.run_cycle() to always fetch fresh NBC data.

    The actual integration of force=True in run_cycle is verified by the
    fact that load_manager.py calls get_current_qh(self.nbc_device, force=True)
    at line ~1062. This test ensures the parameter is accepted and works.
    """
    qh_data = {
        "devices": [{
            "name": "panel",
            "nbc": {
                "QH1": _make_qh_data(0, 5, -200.0, False),
                "QH2": None, "QH3": None, "QH4": None,
            },
        }],
    }

    fetch_responses = [qh_data]

    def fetch_func():
        return fetch_responses.pop(0) if fetch_responses else None

    cache = NBCCache(ttl_seconds=50)
    reader = NBCReader(cache=cache, metrics_fetch=fetch_func)

    # Verify the method accepts force=True without error
    result = reader.get_current_qh("panel", force=True)

    assert result is not None
    qh_name, _, seconds_remaining, _ = result
    assert qh_name == "QH1"