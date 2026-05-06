"""Tests for NBCCache and NBCReader."""

from datetime import datetime, timedelta, timezone

import pytest

from load_manager import (
    NBCCache,
    NBCReader,
)

from tests.helpers import _make_metrics_data


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
    qh_name1, predicted_wh1, _, _, _ = result1
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
    qh_name2, predicted_wh2, _, _, _ = result2
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
    from tests.helpers import _make_qh_data

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
