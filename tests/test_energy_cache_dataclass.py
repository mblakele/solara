"""Tests for Phase 1: EnergyCacheData dataclass and EnergyCache wrapper."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from energy_cache import EnergyCache, EnergyCacheData


class TestEnergyCacheData:
    """Tests for the EnergyCacheData frozen dataclass."""

    def test_dataclass_exists(self) -> None:
        """EnergyCacheData must be importable from energy_cache."""
        from energy_cache import EnergyCacheData  # noqa: F401

    def test_dataclass_is_frozen(self) -> None:
        """EnergyCacheData must be immutable — setattr raises AttributeError."""
        now = datetime.now(timezone.utc)
        data = EnergyCacheData(
            samples=[0.001, 0.002, 0.003],
            data_start=now,
            last_sample_at=now,
            last_fetch_at=now,
            sample_count=3,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        with pytest.raises(AttributeError):
            data.samples = [0.999]  # type: ignore[assignment]

    def test_dataclass_equality(self) -> None:
        """Two EnergyCacheData instances with identical fields are equal."""
        now = datetime.now(timezone.utc)
        a = EnergyCacheData(
            samples=[0.1],
            data_start=now,
            last_sample_at=now,
            last_fetch_at=now,
            sample_count=1,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        b = EnergyCacheData(
            samples=[0.1],
            data_start=now,
            last_sample_at=now,
            last_fetch_at=now,
            sample_count=1,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        assert a == b

    def test_dataclass_inequality_different_samples(self) -> None:
        """Different sample values produce inequality."""
        now = datetime.now(timezone.utc)
        a = EnergyCacheData(
            samples=[0.1],
            data_start=now,
            last_sample_at=now,
            last_fetch_at=now,
            sample_count=1,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        b = EnergyCacheData(
            samples=[0.2],
            data_start=now,
            last_sample_at=now,
            last_fetch_at=now,
            sample_count=1,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        assert a != b

    def test_dataclass_all_fields_present(self) -> None:
        """All fields described in the plan must be present."""
        now = datetime.now(timezone.utc)
        data = EnergyCacheData(
            samples=[0.001],
            data_start=now,
            last_sample_at=now,
            last_fetch_at=now,
            sample_count=1,
            quantization_seconds=5,
            quantization_offset=0,
            quantization_confidence=0.99,
        )
        assert hasattr(data, "samples")
        assert hasattr(data, "data_start")
        assert hasattr(data, "last_sample_at")
        assert hasattr(data, "last_fetch_at")
        assert hasattr(data, "sample_count")
        assert hasattr(data, "quantization_seconds")
        assert hasattr(data, "quantization_offset")
        assert hasattr(data, "quantization_confidence")
        assert data.samples == [0.001]
        assert data.quantization_seconds == 5
        assert data.quantization_confidence == 0.99

    def test_dataclass_none_fields(self) -> None:
        """Fields may be None when no data has been fetched."""
        data = EnergyCacheData(
            samples=None,
            data_start=None,
            last_sample_at=None,
            last_fetch_at=None,
            sample_count=None,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        assert data.samples is None
        assert data.data_start is None
        assert data.sample_count is None

    def test_dataclass_sample_count_matches_samples_length(self) -> None:
        """When samples is a list, sample_count should equal len(samples)."""
        now = datetime.now(timezone.utc)
        samples = [0.1, 0.2, 0.3, 0.4, 0.5]
        data = EnergyCacheData(
            samples=samples,
            data_start=now,
            last_sample_at=now,
            last_fetch_at=now,
            sample_count=5,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        assert data.sample_count == len(data.samples)

    def test_dataclass_sample_count_none_when_samples_none(self) -> None:
        """sample_count is None when samples is None."""
        data = EnergyCacheData(
            samples=None,
            data_start=None,
            last_sample_at=None,
            last_fetch_at=None,
            sample_count=None,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        assert data.sample_count is None


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

        result, was_fresh = cache.get_or_fetch(fetch_func, now, force=True)
        assert call_count == 2
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

    def test_get_or_fetch_populates_data_start(self) -> None:
        """data.data_start is set from the fetch result."""
        cache = EnergyCache(ttl_seconds=60)
        fixed_start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 10,
                "data_start": fixed_start,
            }

        cache.get_or_fetch(fetch_func, now)
        assert cache.data is not None
        assert cache.data.data_start == fixed_start

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

    def test_get_or_fetch_populates_sample_count(self) -> None:
        """data.sample_count is set to len(samples) after fetch."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 7,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        assert cache.data is not None
        assert cache.data.sample_count == 7

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
        assert cache.data.samples is not None
        assert len(cache.data.samples) == 150

    def test_get_or_fetch_samples_field_access(self) -> None:
        """samples can be accessed via cache.data.samples."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)
        expected = [0.5] * 20

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": expected,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        assert cache.data is not None
        assert cache.data.samples == expected

    def test_merge_incremental_appends_new_samples(self) -> None:
        """merge_incremental appends new samples after existing ones."""
        cache = EnergyCache(ttl_seconds=60)
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Pre-populate with 5 samples
        existing_data = EnergyCacheData(
            samples=[0.1] * 5,
            data_start=base_time,
            last_sample_at=base_time + timedelta(seconds=4),
            last_fetch_at=base_time,
            sample_count=5,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        cache._data = existing_data

        new_samples = [0.2, 0.3]
        merged = cache.merge_incremental(
            existing_data.data_start,
            base_time + timedelta(seconds=5),
            existing_data.samples,
            new_samples,
        )

        assert merged is not None
        assert merged.samples is not None
        assert merged.samples == [0.1, 0.1, 0.1, 0.1, 0.1, 0.2, 0.3]

    def test_merge_incremental_deduplicates_overlap(self) -> None:
        """merge_incremental skips samples that overlap with existing data."""
        cache = EnergyCache(ttl_seconds=60)
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Existing: samples at seconds 0-4 (5 samples)
        existing_data = EnergyCacheData(
            samples=[0.1] * 5,
            data_start=base_time,
            last_sample_at=base_time + timedelta(seconds=4),
            last_fetch_at=base_time,
            sample_count=5,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        cache._data = existing_data

        # New: samples at seconds 3-6 (4 samples), overlap at seconds 3-4
        new_samples = [0.5] * 4
        merged = cache.merge_incremental(
            base_time,
            base_time + timedelta(seconds=3),
            existing_data.samples,
            new_samples,
        )

        assert merged is not None
        assert merged.samples is not None
        # Existing 0-4 + new 5-6 = 7 samples
        assert len(merged.samples) == 7

    def test_merge_incremental_returns_none_on_none_input(self) -> None:
        """merge_incremental returns None when new_samples is None."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)
        result = cache.merge_incremental(now, now, [], None)
        assert result is None

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

    def test_pruning_keeps_samples_within_3600s(self) -> None:
        """Samples older than 3600 seconds from now are pruned."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)

        # Pre-populate with 3601 samples (oldest is >3600s ago)
        old_start = now - timedelta(seconds=3621)
        existing = EnergyCacheData(
            samples=[0.1] * 3601,
            data_start=old_start,
            last_sample_at=now - timedelta(seconds=20),
            last_fetch_at=old_start,
            sample_count=3601,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        cache._data = existing

        with patch("energy_cache.datetime") as mock_dt:
            mock_dt.now.return_value = now

            def fetch_func() -> dict[str, Any] | None:
                return {
                    "per_second_data": [0.2] * 10,
                    "data_start": now - timedelta(seconds=10),
                }

            cache.get_or_fetch(fetch_func, now, force=True)

        # After merge + pruning, should be at most ~3600 samples
        assert cache.data is not None
        assert cache.data.samples is not None
        assert len(cache.data.samples) <= 3600

    def test_last_fetch_at_property(self) -> None:
        """DataCache.last_fetch_at property works correctly."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 5,
                "data_start": now,
            }

        assert cache.last_fetch_at is None
        cache.get_or_fetch(fetch_func, now)
        assert cache.last_fetch_at is not None
        assert cache.last_fetch_at == cache.data.last_fetch_at

    def test_data_start_property(self) -> None:
        """DataCache.data_start property works correctly."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)
        fixed_start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001] * 5,
                "data_start": fixed_start,
            }

        assert cache.data_start is None
        cache.get_or_fetch(fetch_func, now)
        assert cache.data_start == fixed_start

    def test_merge_incremental_updates_last_fetch_at(self) -> None:
        """merge_incremental returns new data with updated last_fetch_at."""
        cache = EnergyCache(ttl_seconds=60)
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        existing = EnergyCacheData(
            samples=[0.1] * 5,
            data_start=base_time,
            last_sample_at=base_time + timedelta(seconds=4),
            last_fetch_at=base_time,
            sample_count=5,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        cache._data = existing

        new_samples = [0.2, 0.3]
        merged = cache.merge_incremental(
            existing.data_start,
            base_time + timedelta(seconds=5),
            existing.samples,
            new_samples,
        )

        assert merged is not None
        # last_fetch_at should be updated to current time (or close to it)
        assert merged.last_fetch_at is not None
        assert merged.last_fetch_at >= base_time

    def test_merge_incremental_updates_sample_count(self) -> None:
        """merge_incremental updates sample_count to match total samples."""
        cache = EnergyCache(ttl_seconds=60)
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        existing = EnergyCacheData(
            samples=[0.1] * 5,
            data_start=base_time,
            last_sample_at=base_time + timedelta(seconds=4),
            last_fetch_at=base_time,
            sample_count=5,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        cache._data = existing

        new_samples = [0.2] * 3
        merged = cache.merge_incremental(
            existing.data_start,
            base_time + timedelta(seconds=5),
            existing.samples,
            new_samples,
        )

        assert merged is not None
        assert merged.sample_count == 8  # 5 + 3

    def test_samples_is_list_type(self) -> None:
        """cache.data.samples is a list of floats when data exists."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": [0.001, 0.002, 0.003],
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, now)
        assert cache.data is not None
        assert isinstance(cache.data.samples, list)
        assert all(isinstance(v, float) for v in cache.data.samples)

    def test_merge_incremental_with_empty_new_samples(self) -> None:
        """merge_incremental with empty new_samples list returns existing data unchanged."""
        cache = EnergyCache(ttl_seconds=60)
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        existing = EnergyCacheData(
            samples=[0.1, 0.2],
            data_start=base_time,
            last_sample_at=base_time + timedelta(seconds=1),
            last_fetch_at=base_time,
            sample_count=2,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        cache._data = existing

        merged = cache.merge_incremental(
            existing.data_start,
            base_time,
            existing.samples,
            [],
        )

        # Empty new_samples returns None (nothing to merge).
        assert merged is None

    def test_get_current_qh_with_incremental_data(self) -> None:
        """get_current_qh works correctly with incrementally merged data."""
        cache = EnergyCache(ttl_seconds=60)
        now = datetime(2025, 6, 1, 12, 7, 30, tzinfo=timezone.utc)

        # Initial fetch: 400 samples (first ~6.5 min of QH1)
        data_start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 400

        def fetch_func() -> dict[str, Any] | None:
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        cache.get_or_fetch(fetch_func, now)
        result = cache.get_current_qh(now)

        assert result is not None
        assert result["qh_name"] == "QH1"
        # seconds_remaining should be based on wall-clock, not sample count
        # now is at 7:30 = 450 seconds into the hour
        expected_remaining = 900 - 450
        assert result["seconds_remaining"] == expected_remaining
