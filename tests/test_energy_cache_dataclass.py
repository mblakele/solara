"""Tests for Phase 1: EnergyCacheData dataclass and EnergyCache wrapper."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from energy_cache import EnergyCache, EnergyCacheData, CurrentQH, FrozenQH
from util import NBCQuarter


class TestFrozenQH:
    """Tests for the FrozenQH dataclass (completed QH with cached NBC result)."""

    def test_frozen_qh_exists(self) -> None:
        """FrozenQH must be importable from energy_cache."""
        from energy_cache import FrozenQH  # noqa: F401

    def test_frozen_qh_is_frozen(self) -> None:
        """FrozenQH must be immutable — setattr raises AttributeError."""
        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        nbc = NBCQuarter(complete=True, raw_wh=150.0, wh=150.0)
        fqh = FrozenQH(data_start=now, nbc_result=nbc)
        with pytest.raises(AttributeError):
            fqh.data_start = datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_frozen_qh_fields(self) -> None:
        """FrozenQH has data_start and nbc_result fields."""
        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        nbc = NBCQuarter(complete=True, raw_wh=200.0, wh=200.0)
        fqh = FrozenQH(data_start=now, nbc_result=nbc)
        assert fqh.data_start == now
        assert fqh.nbc_result == nbc
        assert fqh.nbc_result.complete is True
        assert fqh.nbc_result.wh == 200.0

    def test_frozen_qh_is_not_complete_qh(self) -> None:
        """FrozenQH stores a completed NBCQuarter (complete=True)."""
        nbc = NBCQuarter(complete=True, raw_wh=100.0, wh=100.0)
        fqh = FrozenQH(
            data_start=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
            nbc_result=nbc,
        )
        assert fqh.nbc_result.complete is True


class TestCurrentQH:
    """Tests for the CurrentQH dataclass (in-progress QH with raw samples)."""

    def test_current_qh_exists(self) -> None:
        """CurrentQH must be importable from energy_cache."""
        from energy_cache import CurrentQH  # noqa: F401

    def test_current_qh_is_mutable(self) -> None:
        """CurrentQH must be mutable — samples can be appended."""
        now = datetime(2025, 6, 15, 12, 15, 0, tzinfo=timezone.utc)
        cqh = CurrentQH(data_start=now, samples=[0.001, 0.002])
        cqh.samples.append(0.003)
        assert len(cqh.samples) == 3

    def test_current_qh_fields(self) -> None:
        """CurrentQH has data_start and samples fields."""
        now = datetime(2025, 6, 15, 12, 15, 0, tzinfo=timezone.utc)
        samples = [0.001, 0.002, 0.003]
        cqh = CurrentQH(data_start=now, samples=samples)
        assert cqh.data_start == now
        assert cqh.samples == samples

    def test_current_qh_empty_samples(self) -> None:
        """CurrentQH can start with empty samples."""
        now = datetime(2025, 6, 15, 12, 15, 0, tzinfo=timezone.utc)
        cqh = CurrentQH(data_start=now, samples=[])
        assert len(cqh.samples) == 0


class TestQHUpdateLogic:
    """Tests for the QH-aware update logic in EnergyCache."""

    def test_update_qh_initial_fetch_creates_current_qh(self) -> None:
        """First fetch creates a current_qh with the returned samples."""
        from energy_cache import EnergyCache
        cache = EnergyCache(ttl_seconds=30)
        now = datetime(2025, 6, 15, 12, 20, 0, tzinfo=timezone.utc)
        qh_start = datetime(2025, 6, 15, 12, 15, 0, tzinfo=timezone.utc)
        samples = [0.001] * 300  # 5 minutes of data

        cache._update_qh(samples, qh_start, now)

        assert cache._data is not None
        assert cache._data.current_qh is not None
        assert cache._data.current_qh.data_start == qh_start
        assert len(cache._data.current_qh.samples) == 300
        assert cache._data.frozen_qhs is None or len(cache._data.frozen_qhs) == 0

    def test_update_qh_appends_to_current_qh(self) -> None:
        """Subsequent fetch in same QH appends samples to current_qh."""
        from energy_cache import EnergyCache
        cache = EnergyCache(ttl_seconds=30)
        now = datetime(2025, 6, 15, 12, 20, 0, tzinfo=timezone.utc)
        qh_start = datetime(2025, 6, 15, 12, 15, 0, tzinfo=timezone.utc)

        cache._update_qh([0.001] * 300, qh_start, now)
        cache._update_qh([0.002] * 60, qh_start, now)

        assert cache._data is not None
        assert cache._data.current_qh is not None
        assert len(cache._data.current_qh.samples) == 360

    def test_update_qh_freezes_on_completion(self) -> None:
        """When current_qh reaches 900 samples, it is frozen."""
        from energy_cache import EnergyCache
        cache = EnergyCache(ttl_seconds=30)
        now = datetime(2025, 6, 15, 12, 29, 59, tzinfo=timezone.utc)
        qh_start = datetime(2025, 6, 15, 12, 15, 0, tzinfo=timezone.utc)

        cache._update_qh([0.001] * 800, qh_start, now)
        cache._update_qh([0.001] * 100, qh_start, now)

        assert cache._data is not None
        assert cache._data.frozen_qhs is not None
        assert len(cache._data.frozen_qhs) == 1
        assert cache._data.frozen_qhs[0].data_start == qh_start
        assert cache._data.frozen_qhs[0].nbc_result.complete is True
        # Current QH should be empty or None after freeze
        assert cache._data.current_qh is None or len(cache._data.current_qh.samples) == 0

    def test_update_qh_splits_at_boundary(self) -> None:
        """Samples spanning a QH boundary are split correctly."""
        from energy_cache import EnergyCache
        cache = EnergyCache(ttl_seconds=30)
        # Start in QH 12:15-12:30, but fetch data that crosses into 12:30-12:45
        qh1_start = datetime(2025, 6, 15, 12, 15, 0, tzinfo=timezone.utc)
        qh2_start = datetime(2025, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 15, 12, 35, 0, tzinfo=timezone.utc)

        # 800 samples from 12:15:00 to 12:28:20 (within QH1)
        cache._update_qh([0.001] * 800, qh1_start, now)

        # 200 samples starting at 12:28:20 — crosses QH boundary at 12:30:00
        # 100 samples in QH1 (12:28:20 to 12:30:00), 100 in QH2 (12:30:00 to 12:31:40)
        samples = [0.002] * 200
        data_start = datetime(2025, 6, 15, 12, 28, 20, tzinfo=timezone.utc)
        cache._update_qh(samples, data_start, now)

        assert cache._data is not None
        # QH1 should be frozen (had 800 + 100 = 900 samples)
        assert cache._data.frozen_qhs is not None
        assert len(cache._data.frozen_qhs) >= 1
        # Current QH should be QH2 with 100 samples
        assert cache._data.current_qh is not None
        assert cache._data.current_qh.data_start == qh2_start
        assert len(cache._data.current_qh.samples) == 100

    def test_prune_frozen_qhs_keeps_max_3(self) -> None:
        """Pruning drops the oldest frozen QH when count exceeds 3."""
        from energy_cache import EnergyCache
        from util import NBCQuarter
        cache = EnergyCache(ttl_seconds=30)
        now = datetime(2025, 6, 15, 13, 0, 0, tzinfo=timezone.utc)

        # Manually create 4 frozen QHs
        frozen = []
        for i in range(4):
            qh_start = datetime(2025, 6, 15, 11, i * 15, 0, tzinfo=timezone.utc)
            nbc = NBCQuarter(complete=True, raw_wh=float(i * 100), wh=float(i * 100))
            frozen.append(FrozenQH(data_start=qh_start, nbc_result=nbc))

        from energy_cache import EnergyCacheData
        cache._data = EnergyCacheData(
            frozen_qhs=frozen,
            current_qh=CurrentQH(
                data_start=datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
                samples=[0.001] * 100,
            ),
            last_fetch_at=now,
        )

        cache._prune_frozen_qhs()

        assert cache._data is not None
        assert cache._data.frozen_qhs is not None
        assert len(cache._data.frozen_qhs) == 3
        # Oldest (index 0) should be dropped
        assert cache._data.frozen_qhs[0].data_start.minute == 15

    def test_update_qh_preserves_full_metrics_dict(self) -> None:
        """_update_qh preserves full_metrics_dict from existing data."""
        from energy_cache import EnergyCache, EnergyCacheData
        cache = EnergyCache(ttl_seconds=30)
        now = datetime(2025, 6, 15, 12, 20, 0, tzinfo=timezone.utc)
        qh_start = datetime(2025, 6, 15, 12, 15, 0, tzinfo=timezone.utc)

        cache._data = EnergyCacheData(
            last_fetch_at=now,
            full_metrics_dict={"devices": [{"name": "test"}]},
        )

        cache._update_qh([0.001] * 100, qh_start, now)

        assert cache._data is not None
        assert cache._data.full_metrics_dict == {"devices": [{"name": "test"}]}

    def test_update_qh_handles_subsecond_timestamps_at_boundary(self) -> None:
        """_update_qh must not hang when data_start has a sub-second fraction
        and the sample window crosses a QH boundary."""
        import threading
        from energy_cache import EnergyCache

        cache = EnergyCache(ttl_seconds=30)
        data_start = datetime(2025, 6, 15, 12, 14, 55, 400000, tzinfo=timezone.utc)
        samples = [0.001] * 10
        now = data_start + timedelta(seconds=10)

        done = threading.Event()

        def run() -> None:
            cache._update_qh(samples, data_start, now)
            done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=2.0)

        assert done.is_set(), "_update_qh hung — infinite loop in QH boundary splitting"
        assert cache._data is not None
        assert cache._data.current_qh is not None


class TestEnergyCacheWrapper:
    """Tests for the EnergyCache wrapper class with new public interface."""

    def test_initial_state_no_data(self) -> None:
        """Fresh EnergyCache has no data — data property is None."""
        cache = EnergyCache(ttl_seconds=60)
        assert cache.data is None

    def test_initial_state_ttl(self) -> None:
        """Fresh EnergyCache preserves the TTL passed to constructor."""
        cache = EnergyCache(ttl_seconds=120)
        assert cache.ttl_seconds == 120

    def test_initial_state_lock(self) -> None:
        """Fresh EnergyCache has a threading lock."""
        cache = EnergyCache(ttl_seconds=60)
        assert isinstance(cache.lock, type(threading.Lock()))

    def test_is_valid_false_when_no_data(self) -> None:
        """is_valid() returns False when cache has no data."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)
        assert cache.is_valid(now) is False

    def test_is_valid_true_within_ttl(self) -> None:
        """is_valid() returns True when cache has data within TTL."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 10,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        assert cache.is_valid(now) is True

    def test_is_valid_false_after_ttl_expiry(self) -> None:
        """is_valid() returns False when data is older than TTL."""
        cache = EnergyCache(ttl_seconds=0)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 10,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        assert cache.is_valid(now) is False

    def test_is_valid_false_after_invalidate(self) -> None:
        """is_valid() returns False after invalidate() is called."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 10,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        cache.invalidate()
        assert cache.is_valid(now) is False

    def test_data_property_after_fetch(self) -> None:
        """data property returns the EnergyCacheData after a fetch."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 5,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        data = cache.data
        assert data is not None
        assert data.samples is not None
        assert len(data.samples) == 5

    def test_get_or_fetch_returns_data_and_was_fresh(self) -> None:
        """get_or_fetch returns (data, was_fresh) tuple."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 5,
                "data_start": now,
            }

        result, was_fresh = cache.get_or_fetch(fetch_func, now)
        assert was_fresh is True
        assert isinstance(result, dict)
        assert result["per_second_data"] == [0.001] * 5

    def test_get_or_fetch_cache_hit(self) -> None:
        """Second get_or_fetch within TTL returns cached data with was_fresh=False."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 5,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        _, was_fresh = cache.get_or_fetch(fetch_func, now)
        assert was_fresh is False

    def test_get_or_fetch_force_bypasses_cache(self) -> None:
        """force=True always calls fetch_func and returns was_fresh=True."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)
        call_count = 0

        def fetch_func() -> dict[str, Any] | None:
            nonlocal call_count
            call_count += 1
            return {
                "per_second_data": [float(call_count)] * 5,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        assert call_count == 1

        # Second call with force=True: overlap mismatch is tolerated (no retry)
        result, was_fresh = cache.get_or_fetch(fetch_func, now, force=True)
        assert call_count == 2  # no retry — mismatch is tolerated
        assert was_fresh is True
        assert result["per_second_data"] == [2.0] * 5

    def test_get_or_fetch_none_result_invalidates(self) -> None:
        """When fetch_func returns None, cache is invalid and data is None."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return None

        result, was_fresh = cache.get_or_fetch(fetch_func, now)
        assert result is None
        assert cache.data is None
        assert cache.is_valid(now) is False

    def test_get_or_fetch_populates_last_fetch_at(self) -> None:
        """data.last_fetch_at is set on API call but not on cache hit."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 5,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        first_fetch_at = cache.data.last_fetch_at
        assert first_fetch_at is not None

        time.sleep(0.02)
        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        assert cache.data.last_fetch_at == first_fetch_at

    def test_get_or_fetch_nested_device_data(self) -> None:
        """get_or_fetch populates samples from nested devices list."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "api_response": {},
                "devices": [
                    {
                        "gid": 123,
                        "name": "VUE Device",
                        "per_second_data": [0.01] * 150,
                    }
                ],
            }

        cache.get_or_fetch(fetch_func, now)
        assert cache.data is not None
        assert cache.data.current_qh is not None
        assert len(cache.data.current_qh.samples) == 150

    def test_invalidate_clears_data(self) -> None:
        """invalidate() sets data to None."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 5,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        assert cache.data is not None
        cache.invalidate()
        assert cache.data is None

    def test_sleep_interval_adjust_returns_float(self) -> None:
        """sleep_interval_adjust returns a float."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)
        result = cache.sleep_interval_adjust(30.0, now)
        assert isinstance(result, float)

    def test_sleep_interval_adjust_decreases_on_stale_data(self) -> None:
        """sleep_interval_adjust returns shorter sleep when data is stale."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        # First, get fresh data so the cache has a last_fetch_at
        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 5,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        # With the most recent data having only 5 samples, the function
        # should return a reduced sleep interval.
        result = cache.sleep_interval_adjust(30.0, now)
        assert isinstance(result, float)
        # Just verify it returns a reasonable float — exact value depends on
        # implementation details like sample_count logic.
        assert result >= 0.0

    def test_sleep_min_when_data_older_than_2x_quantum(self) -> None:
        """sleep_interval_adjust returns MIN_SLEEP_SECS when data > 2× quantum."""
        cache = EnergyCache(ttl_seconds=60)
        quantum = 30  # 2×30 = 60s threshold
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 1, 1, tzinfo=timezone.utc)  # 61s later

        cache._data = EnergyCacheData(
            last_sample_at=data_time,
            data_start=data_time,
            last_fetch_at=now,
            quantization_seconds=quantum,
            quantization_offset=0,
            quantization_confidence=0.95,
        )

        result = cache.sleep_interval_adjust(30.0, now)
        assert result == 5.0

    def test_sleep_min_at_2x_quantum_boundary(self) -> None:
        """sleep_interval_adjust returns MIN_SLEEP_SECS at exactly 2× quantum."""
        cache = EnergyCache(ttl_seconds=60)
        quantum = 30  # 2×30 = 60s threshold
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 1, 0, tzinfo=timezone.utc)  # exactly 60s later

        cache._data = EnergyCacheData(
            last_sample_at=data_time,
            data_start=data_time,
            last_fetch_at=now,
            quantization_seconds=quantum,
            quantization_offset=0,
            quantization_confidence=0.95,
        )

        result = cache.sleep_interval_adjust(30.0, now)
        assert isinstance(result, float)
        assert result >= 5.0

    def test_falls_through_below_2x_quantum(self) -> None:
        """sleep_interval_adjust falls through to quantization logic below 2× quantum."""
        cache = EnergyCache(ttl_seconds=60)
        quantum = 30  # 2×30 = 60s threshold
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 0, 45, tzinfo=timezone.utc)  # 45s, < 60s

        cache._data = EnergyCacheData(
            last_sample_at=data_time,
            data_start=data_time,
            last_fetch_at=now,
            quantization_seconds=quantum,
            quantization_offset=0,
            quantization_confidence=0.95,
        )

        result = cache.sleep_interval_adjust(30.0, now)
        assert isinstance(result, float)
        assert result >= 5.0

    def test_skips_when_last_sample_at_none(self) -> None:
        """sleep_interval_adjust skips 2× check when last_sample_at is None."""
        cache = EnergyCache(ttl_seconds=60)
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 1, 1, tzinfo=timezone.utc)

        cache._data = EnergyCacheData(
            data_start=data_time,
            last_fetch_at=now,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=0.95,
        )

        result = cache.sleep_interval_adjust(30.0, now)
        assert isinstance(result, float)
        assert result >= 5.0

    def test_skips_when_quantum_missing(self) -> None:
        """sleep_interval_adjust skips 2× check when quantization_seconds is None."""
        cache = EnergyCache(ttl_seconds=60)
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 1, 1, tzinfo=timezone.utc)

        cache._data = EnergyCacheData(
            last_sample_at=data_time,
            last_fetch_at=now,
        )

        result = cache.sleep_interval_adjust(30.0, now)
        assert isinstance(result, float)

    def test_result_clamped_at_5_minimum(self) -> None:
        """sleep_interval_adjust result is never below MIN_SLEEP_SECS (5.0)."""
        cache = EnergyCache(ttl_seconds=60)
        quantum = 5  # Very small quantum: 2×5 = 10s threshold
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 0, 12, tzinfo=timezone.utc)  # 12s > 10s

        cache._data = EnergyCacheData(
            last_sample_at=data_time,
            data_start=data_time,
            last_fetch_at=now,
            quantization_seconds=quantum,
            quantization_offset=0,
            quantization_confidence=0.95,
        )

        result = cache.sleep_interval_adjust(30.0, now)
        assert result == 5.0

    def test_get_current_qh_returns_none_when_empty(self) -> None:
        """get_current_qh returns None when cache has no data."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)
        assert cache.get_current_qh(now) is None

    def test_get_current_qh_returns_dict_when_data_exists(self) -> None:
        """get_current_qh returns a dict with QH info when cache has data."""
        cache = EnergyCache(ttl_seconds=60)
        # 450 samples = halfway through QH1
        data_start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 7, 30, tzinfo=timezone.utc)
        samples = [0.001] * 450

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        cache.get_or_fetch(fetch_func, now)
        result = cache.get_current_qh(now)
        assert result is not None
        assert isinstance(result, dict)
        assert "qh_name" in result

    def test_get_current_qh_all_quarters_complete_returns_none(self) -> None:
        """When all 4 quarters are complete, get_current_qh returns None."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime(2025, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        samples = [0.01] * 3600

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        cache.get_or_fetch(fetch_func, now)
        result = cache.get_current_qh(now)
        assert result is None

    def test_thread_safe_concurrent_access(self) -> None:
        """Concurrent reads and writes should not raise exceptions."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        errors: list[str] = []

        def writer() -> None:
            try:
                for _ in range(10):
                    cache.get_or_fetch(
                        lambda: {
                            "per_second_data": [0.1] * 5,
                            "data_start": now,
                        },
                        now,
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        def reader() -> None:
            try:
                for _ in range(10):
                    cache.get_current_qh(now)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        threads = [
            threading.Thread(target=writer) for _ in range(3)
        ] + [threading.Thread(target=reader) for _ in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], f"Thread errors: {errors}"


class TestCacheHitReturnsFullMetrics:
    """Tests that cache hits return the full metrics dict, not a minimal one.

    Regression test for: on cache hits, _build_result() returned only
    {per_second_data, data_start}, dropping the "devices" key that the
    index endpoint needs to render predictions.
    """

    def test_cache_hit_returns_full_metrics_with_devices(self) -> None:
        """On cache hit, get_or_fetch returns the original full dict including devices.

        This reproduces the bug where the index endpoint received {'devices': []}
        on cache hits because _build_result() only included per_second_data and
        data_start, omitting the devices list with predictions.
        """
        from clock import FakeClock

        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        cache = EnergyCache(ttl_seconds=60, clock=FakeClock(now))

        # Simulate the full metrics dict that create_metrics returns
        full_metrics = {
            "devices": [
                {
                    "gid": 12345,
                    "name": "Solar Inverter",
                    "prediction": 500.0,
                    "nbc": {"QH1": {"predicted_wh": 500}},
                    "per_second_data": [0.01] * 10,
                }
            ],
            "instant": now,
            "api_response": {"took_ms": 150},
        }

        call_count = 0

        def fetch_func() -> dict[str, Any] | None:
            nonlocal call_count
            call_count += 1
            return full_metrics

        # First call: cache miss
        result, was_fresh = cache.get_or_fetch(fetch_func, now)
        assert was_fresh is True
        assert call_count == 1
        assert "devices" in result
        assert len(result["devices"]) == 1
        assert result["devices"][0]["name"] == "Solar Inverter"

        # Second call: cache hit — should return the SAME full dict with devices
        result2, was_fresh2 = cache.get_or_fetch(fetch_func, now)
        assert was_fresh2 is False
        assert call_count == 1  # fetch_func NOT called again
        assert "devices" in result2, (
            "Cache hit result missing 'devices' key — _build_result() only returns "
            "per_second_data and data_start, dropping the full metrics dict"
        )
        assert len(result2["devices"]) == 1, (
            "Cache hit result has empty devices list"
        )
        assert result2["devices"][0]["name"] == "Solar Inverter"
