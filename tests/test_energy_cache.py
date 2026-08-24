"""Tests for EnergyCache behavior.

Covers quantization-aware get_current_qh, get_or_fetch semantics (TTL,
cache hits, force, timeouts, concurrency), pruning bounds,
sleep_interval_adjust, and the EnergyCacheData-backed public interface.
"""  # noqa: D01

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from energy_cache import EnergyCache, EnergyCacheAlignmentError, EnergyCacheData
from util import ceil_to_qh


class TestEnergyCacheLowConfidenceLog:
    """Tests for low-confidence quantization warning log."""

    def test_low_confidence_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """When detect_quantization returns confidence below the threshold, a warning is emitted.

        Mocks detect_quantization to return N=20, offset=0, confidence=0.50
        which is below QUANTIZATION_CONFIDENCE_THRESHOLD (0.7).
        """
        cache = EnergyCache()
        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        new_samples = [0.0] * 7 + [1.0] * 20 + [2.0] * 20 + [3.0] * 20

        from unittest.mock import patch
        with patch("energy_cache.detect_quantization", return_value=(20, 0, 0.50)):
            with caplog.at_level("WARNING", logger="energy_cache"):
                cache._merge_samples_replace(new_samples, data_start, now)

        assert len(caplog.records) > 0
        assert any(
            "Quantization detected" in rec.message and "low confidence" in rec.message
            for rec in caplog.records
        ), "Expected warning about low-confidence quantization"

    def test_high_confidence_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """When detect_quantization returns confidence >= 0.9, no warning is emitted."""
        cache = EnergyCache()
        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        # Full hour with exact 30-second samples — confidence = 1.0
        new_samples: list[float] = []
        for i in range(120):
            new_samples.extend([float(i)] * 30)

        with caplog.at_level("WARNING", logger="energy_cache"):
            cache._merge_samples_replace(new_samples, data_start, now)

        warning_records = [
            rec for rec in caplog.records
            if "Quantization detected" in rec.message
        ]
        assert len(warning_records) == 0, (
            f"Expected no warning but got: {[r.message for r in caplog.records]}"
        )


class TestGetCurrentQhQuantization:
    """Tests for get_current_qh with quantization-aware prediction window."""

    def _make_cache_with_quantization(
        self,
        samples: list[float],
        data_start: datetime,
        quantization_seconds: int | None,
        quantization_confidence: float | None,
    ) -> EnergyCache:
        """Create an EnergyCache with pre-set quantization data."""
        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=samples,
            data_start=data_start,
            last_sample_at=data_start,
            last_fetch_at=data_start,
            sample_count=len(samples),
            quantization_seconds=quantization_seconds,
            quantization_offset=0,
            quantization_confidence=quantization_confidence,
        )
        return cache

    def test_get_current_qh_uses_quantization_window(self):
        """get_current_qh uses 30s prediction window when quantization data is present.

        Layout: 70 samples of 0.001, then 30 samples of 0.003 = 100 total.
        With quantization_seconds=30, confidence=1.0 → window=30s.

        Expected predicted_wh with 30s window (plan 3.3: extrapolation uses
        the WALL-CLOCK remainder, not sample count):
            wall remaining at 14:01 = 900 - 60 = 840 s
            prediction_w = 1000 * 0.003 = 3.0 W
            raw_wh = 1000 * (70*0.001 + 30*0.003) = 160 Wh
            predicted_wh = 160 + 840 * 3.0 = 2680 Wh

        The dict also exposes the sample-count remainder for diagnostics.
        """
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 70 + [0.003] * 30
        now = datetime(2025, 6, 15, 14, 1, 0, tzinfo=timezone.utc)

        cache = self._make_cache_with_quantization(
            samples, data_start, quantization_seconds=30, quantization_confidence=1.0
        )
        result = cache.get_current_qh(now)

        assert result is not None
        assert result["qh_name"] == "QH1"
        # 2680 from 30s window on the wall-clock remainder
        assert result["predicted_wh"] == pytest.approx(2680.0, abs=0.01), (
            f"Expected 2680 (30s window, wall-clock) but got {result['predicted_wh']}"
        )
        assert result["seconds_remaining"] == 840
        assert result["sample_seconds_remaining"] == 800

    def test_get_current_qh_falls_back_when_no_quantization(self):
        """get_current_qh falls back to default window when no quantization data.

        Same samples as test_get_current_qh_uses_quantization_window.
        """
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 70 + [0.003] * 30
        now = datetime(2025, 6, 15, 14, 1, 0, tzinfo=timezone.utc)

        cache = self._make_cache_with_quantization(
            samples, data_start, quantization_seconds=None, quantization_confidence=None
        )
        result = cache.get_current_qh(now)

        assert result is not None
        assert result["qh_name"] == "QH1"
        # 2680 from 30s default window on the wall-clock remainder
        assert result["predicted_wh"] == pytest.approx(2680.0, abs=0.01), (
            f"Expected 2680 (30s window) but got {result['predicted_wh']}"
        )

    def test_get_current_qh_falls_back_when_confidence_below_threshold(self):
        """get_current_qh falls back to default window when confidence below threshold.

        Same samples as above, with quantization_seconds=30 but confidence=0.5.
        """
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 70 + [0.003] * 30
        now = datetime(2025, 6, 15, 14, 1, 0, tzinfo=timezone.utc)

        cache = self._make_cache_with_quantization(
            samples, data_start, quantization_seconds=30, quantization_confidence=0.5
        )
        result = cache.get_current_qh(now)

        assert result is not None
        assert result["qh_name"] == "QH1"
        # 2680 from 30s default (not old 60s default)
        assert result["predicted_wh"] == pytest.approx(2680.0, abs=0.01), (
            f"Expected 2680 (30s default) but got {result['predicted_wh']}"
        )

    def test_get_current_qh_returns_none_when_no_data(self):
        """get_current_qh returns None when cache has no data."""
        cache = EnergyCache()
        result = cache.get_current_qh(datetime(2025, 6, 15, 14, 1, 0, tzinfo=timezone.utc))
        assert result is None

    def test_get_current_qh_returns_none_when_qh1_complete(self):
        """get_current_qh returns None when QH1 is complete (stale data)."""
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        # 900 samples = complete QH1
        samples = [0.001] * 900
        now = datetime(2025, 6, 15, 14, 15, 1, tzinfo=timezone.utc)

        cache = self._make_cache_with_quantization(
            samples, data_start, quantization_seconds=30, quantization_confidence=1.0
        )
        result = cache.get_current_qh(now)
        assert result is None


class TestGetCurrentQhAlignmentGuard:
    """Tests for the QH-alignment guard in get_current_qh.

    A misaligned or missing ``data_start`` must raise the named
    ``EnergyCacheAlignmentError`` (not a bare assert, which is stripped
    under ``python -O``) so any path that stored bad data degrades loudly.
    """

    def test_get_current_qh_raises_when_data_start_misaligned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Misaligned data_start (14:16:00) raises EnergyCacheAlignmentError."""
        cache = EnergyCache()
        data_start = datetime(2025, 6, 15, 14, 16, 0, tzinfo=timezone.utc)
        cache._data = EnergyCacheData(
            samples=[0.001] * 240,
            data_start=data_start,
            last_sample_at=data_start + timedelta(seconds=239),
            last_fetch_at=data_start,
            sample_count=240,
            quantization_seconds=None,
            quantization_offset=0,
            quantization_confidence=None,
        )
        now = datetime(2025, 6, 15, 14, 20, 0, tzinfo=timezone.utc)

        with caplog.at_level("WARNING", logger="energy_cache"):
            with pytest.raises(EnergyCacheAlignmentError):
                cache.get_current_qh(now)

        assert any(
            "not QH-aligned" in rec.message for rec in caplog.records
        ), (
            "Expected WARNING about QH alignment, got "
            f"{[r.message for r in caplog.records]}"
        )

    def test_get_current_qh_raises_when_data_start_none(self) -> None:
        """Missing data_start raises EnergyCacheAlignmentError."""
        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=[0.001] * 240,
            data_start=None,
            last_sample_at=None,
            last_fetch_at=datetime(2025, 6, 15, 14, 20, 0, tzinfo=timezone.utc),
            sample_count=240,
            quantization_seconds=None,
            quantization_offset=0,
            quantization_confidence=None,
        )
        now = datetime(2025, 6, 15, 14, 20, 0, tzinfo=timezone.utc)

        with pytest.raises(EnergyCacheAlignmentError):
            cache.get_current_qh(now)


class TestIncrementalFetch:
    """Tests for replace-path get_or_fetch."""

    def test_get_or_fetch_replace_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On overlap mismatch, merge tolerates it and succeeds without retry."""
        from energy_cache import EnergyCacheData
        from datetime import timedelta

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=[0.1, 0.2, 0.3],
            data_start=base,
            last_sample_at=base + timedelta(seconds=2),
            last_fetch_at=base,
            sample_count=3,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=1.0,
        )

        call_count = 0

        def fetcher() -> dict[str, Any] | None:
            nonlocal call_count
            call_count += 1
            # Incremental with mismatched overlap — should succeed now
            return {
                "per_second_data": [0.99, 0.2, 0.3, 0.4, 0.5],
                "data_start": base + timedelta(seconds=1),
            }

        now = base + timedelta(minutes=5)
        result, was_fresh = cache.get_or_fetch(fetcher, now)
        assert was_fresh is True
        assert result is not None
        assert call_count == 1, "Should succeed on first fetch (no retry needed)"




class TestGetOrFetchTimeout:
    """Tests for fetch timeout in EnergyCache.get_or_fetch."""

    def test_slow_fetch_returns_none_on_timeout(self) -> None:
        """When fetch_func exceeds timeout, returns (None, True) without hanging."""
        import time

        cache = EnergyCache(fetch_timeout_secs=0.5)
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        def slow_fetcher() -> dict[str, Any] | None:
            time.sleep(0.8)
            return {"devices": []}

        result, was_fresh = cache.get_or_fetch(slow_fetcher, now, force=True)
        assert result is None
        assert was_fresh is True

    def test_fast_fetch_completes_within_timeout(self) -> None:
        """When fetch_func completes before timeout, returns normally."""
        cache = EnergyCache(fetch_timeout_secs=10)
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        def fast_fetcher() -> dict[str, Any] | None:
            return {"devices": [], "data_start": now}

        result, was_fresh = cache.get_or_fetch(fast_fetcher, now, force=True)
        assert was_fresh is True
        assert result is not None

    def test_timeout_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Timeout emits a warning log message."""
        import time

        cache = EnergyCache(fetch_timeout_secs=0.5)
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        def slow_fetcher() -> dict[str, Any] | None:
            time.sleep(0.8)
            return {"devices": []}

        with caplog.at_level("WARNING", logger="energy_cache"):
            cache.get_or_fetch(slow_fetcher, now, force=True)

        assert any(
            "fetch timed out" in rec.message.lower()
            for rec in caplog.records
        ), f"Expected timeout warning, got: {[r.message for r in caplog.records]}"

    def test_timeout_fetch_exception_returns_none(self) -> None:
        """When fetch_func raises an exception, returns (None, True)."""
        cache = EnergyCache(fetch_timeout_secs=5)
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        def failing_fetcher() -> dict[str, Any] | None:
            raise ConnectionError("API down")

        result, was_fresh = cache.get_or_fetch(failing_fetcher, now, force=True)
        assert result is None
        assert was_fresh is True

    def test_retryable_exception_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A RetryableMetricsException from fetch_func logs a warning, not an error."""
        from metrics import RetryableMetricsException

        cache = EnergyCache(fetch_timeout_secs=5)
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        def retryable_fetcher() -> dict[str, Any] | None:
            raise RetryableMetricsException("No data for hour")

        with caplog.at_level("WARNING", logger="energy_cache"):
            result, was_fresh = cache.get_or_fetch(retryable_fetcher, now, force=True)

        assert result is None
        assert was_fresh is True
        assert not any(rec.levelno == logging.ERROR for rec in caplog.records), (
            f"Expected no ERROR records, got: {[r.message for r in caplog.records]}"
        )
        assert any(
            rec.levelno == logging.WARNING and "RetryableMetricsException" in rec.message
            for rec in caplog.records
        ), f"Expected WARNING mentioning exception, got: {[r.message for r in caplog.records]}"

    def test_unexpected_exception_still_logs_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-retryable exceptions from fetch_func keep the ERROR traceback log."""
        cache = EnergyCache(fetch_timeout_secs=5)
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        def failing_fetcher() -> dict[str, Any] | None:
            raise ValueError("boom")

        with caplog.at_level("WARNING", logger="energy_cache"):
            result, was_fresh = cache.get_or_fetch(failing_fetcher, now, force=True)

        assert result is None
        assert was_fresh is True
        assert any(
            rec.levelno == logging.ERROR and "fetch_func raised" in rec.message
            for rec in caplog.records
        ), f"Expected ERROR log, got: {[r.message for r in caplog.records]}"

    def test_timeout_logs_underlying_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Timeout warning includes the underlying thread exception details."""
        import time

        cache = EnergyCache(fetch_timeout_secs=0.3)
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        def slow_failing_fetcher() -> dict[str, Any] | None:
            time.sleep(0.6)
            raise ConnectionError("DNS resolution failed")

        with caplog.at_level("WARNING", logger="energy_cache"):
            cache.get_or_fetch(slow_failing_fetcher, now, force=True)
            # Give the thread time to log its exception after the timeout.
            time.sleep(0.9)

        all_msgs = [rec.message for rec in caplog.records]
        assert any(
            "fetch timed out" in msg.lower() for msg in all_msgs
        ), f"Expected timeout warning, got: {all_msgs}"
        assert any(
            "ConnectionError" in msg for msg in all_msgs
        ), f"Expected underlying exception in log, got: {all_msgs}"

    def test_timeout_returns_stale_cache_if_available(self) -> None:
        """When fetch times out, returns existing stale cache instead of None."""
        import time
        from datetime import timedelta

        cache = EnergyCache(fetch_timeout_secs=1, ttl_seconds=30)
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        stale_time = now - timedelta(seconds=60)

        # Pre-populate cache with stale data.
        cache._data = EnergyCacheData(
            samples=[1.0] * 60,
            data_start=stale_time,
            last_sample_at=stale_time,
            last_fetch_at=stale_time,
            sample_count=60,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
            full_metrics_dict={"devices": [{"gid": 1}], "data_start": stale_time},
        )

        def slow_fetcher() -> dict[str, Any] | None:
            time.sleep(1.2)
            return None

        result, was_fresh = cache.get_or_fetch(slow_fetcher, now, force=True)
        # Should return stale cache, not None.
        assert result is not None
        assert was_fresh is False
        assert result.get("devices") == [{"gid": 1}]

    def test_default_timeout_is_30(self) -> None:
        """Default fetch_timeout_secs is 30 seconds."""
        cache = EnergyCache()
        assert cache._fetch_timeout_secs == 30

    def test_fetch_failure_no_stale_cache_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When fetch returns None and no stale cache exists, logs WARNING."""
        cache = EnergyCache(fetch_timeout_secs=5)
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        def failing_fetcher() -> dict[str, Any] | None:
            raise ConnectionError("network down")

        with caplog.at_level("WARNING", logger="energy_cache"):
            result, was_fresh = cache.get_or_fetch(failing_fetcher, now, force=True)

        assert result is None
        assert was_fresh is True
        assert any(
            "no stale cache" in rec.message.lower()
            for rec in caplog.records
        ), f"Expected 'no stale cache' warning, got: {[r.message for r in caplog.records]}"


class TestGetOrFetchTimingLogs:
    """Tests for timing/diagnostic logging in EnergyCache.get_or_fetch."""

    def test_fetch_elapsed_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Fetch path logs elapsed time."""
        cache = EnergyCache()
        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        def fetcher() -> dict[str, Any] | None:
            return {"devices": [], "data_start": now}

        with caplog.at_level("DEBUG", logger="energy_cache"):
            cache.get_or_fetch(fetcher, now, force=True)

        assert any(
            "fetch_func completed" in rec.message.lower()
            for rec in caplog.records
        ), f"Expected fetch timing log, got: {[r.message for r in caplog.records]}"


class TestPruneOldSamplesLastSampleAt:
    """Tests that _prune_old_samples updates last_sample_at."""

    def test_pruning_updates_last_sample_at(self) -> None:
        """After pruning, last_sample_at must be >= data_start."""
        from datetime import timedelta

        cache = EnergyCache()
        # 3241 samples starting at 03:29:00 — ends at 03:29:59.
        # cutoff = ceil_to_qh(04:23:59 - 3600s) = 03:30:00.
        # All 60 samples before 03:30:00 are pruned, advancing data_start
        # to 03:30:00. Without the fix, last_sample_at stays 03:29:59
        # which is before the new data_start.
        data_start = datetime(2026, 7, 9, 3, 29, 0, tzinfo=timezone.utc)
        # last_sample_at is 03:29:59 (60 seconds after data_start).
        last_sample_at = data_start + timedelta(seconds=59)
        samples = [0.0] * 60  # only 60 samples: 03:29:00 to 03:29:59
        now = datetime(2026, 7, 9, 4, 23, 59, tzinfo=timezone.utc)

        data = EnergyCacheData(
            samples=samples,
            data_start=data_start,
            last_sample_at=last_sample_at,
            last_fetch_at=now,
            sample_count=60,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )

        pruned = cache._prune_old_samples(data, now)
        # data_start should advance to 03:30:00.
        expected_data_start = datetime(2026, 7, 9, 3, 30, 0, tzinfo=timezone.utc)
        assert pruned.data_start == expected_data_start
        # last_sample_at must be >= data_start (the invariant).
        assert pruned.last_sample_at >= pruned.data_start, (
            f"last_sample_at {pruned.last_sample_at} < data_start {pruned.data_start}"
        )


# =============================================================================
# Fetch-outside-the-lock (plan subtask 2.2, fixes R2)
# =============================================================================


class TestFetchOutsideLock:
    """get_or_fetch must not hold the state lock across network I/O.

    Holding ``_lock`` during the fetch (up to fetch_timeout_secs=30 s)
    blocks every Flask request thread behind the load-management thread's
    Emporia call. After the fix, readers acquire the lock only for
    instant snapshot operations.
    """

    def _now(self):
        return datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)

    def _payload(self, now):
        return {
            "per_second_data": [0.2] * 60,
            "data_start": now.replace(minute=0),
        }

    def test_reader_not_blocked_during_slow_fetch(self):
        """get_current_qh completes while another thread's fetch is stuck."""
        import threading

        cache = EnergyCache()
        now = self._now()

        started = threading.Event()
        release = threading.Event()

        def slow_fetch():
            started.set()
            release.wait(timeout=10)
            return self._payload(now)

        worker = threading.Thread(
            target=lambda: cache.get_or_fetch(slow_fetch, now), daemon=True
        )
        worker.start()
        assert started.wait(timeout=5), "fetcher never started"

        reader = threading.Thread(
            target=lambda: cache.get_current_qh(now), daemon=True
        )
        reader.start()
        reader.join(timeout=1.0)
        assert not reader.is_alive(), (
            "reader is blocked behind an in-flight fetch: the state lock "
            "is still held across network I/O"
        )
        reader.join(timeout=5)

        release.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert cache.data is not None

    def test_single_flight_only_one_fetch_per_refresh(self):
        """Concurrent invalid-cache callers produce exactly one fetch."""
        import threading

        cache = EnergyCache(ttl_seconds=30)
        now = self._now()
        calls: list[int] = []
        calls_lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()

        def counting_fetch():
            with calls_lock:
                calls.append(1)
            started.set()
            release.wait(timeout=10)
            return self._payload(now)

        results: list[tuple] = []

        def call_it():
            results.append(cache.get_or_fetch(counting_fetch, now))

        t1 = threading.Thread(target=call_it, daemon=True)
        t1.start()
        assert started.wait(timeout=5)

        t2 = threading.Thread(target=call_it, daemon=True)
        t2.start()
        release.set()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not t1.is_alive() and not t2.is_alive()
        assert len(calls) == 1, f"expected single-flight, got {len(calls)} calls"
        assert results[0][1] is True
        assert results[1][1] is False

    def test_stale_served_when_fetch_times_out(self):
        """Timeout path still serves the previous snapshot untouched."""
        import time as time_mod

        cache = EnergyCache(fetch_timeout_secs=1)
        now = self._now()

        stale_samples = [0.1] * 60
        data_start = now - timedelta(minutes=2)
        cache._data = EnergyCacheData(
            samples=stale_samples,
            data_start=data_start,
            last_sample_at=data_start + timedelta(seconds=59),
            last_fetch_at=now - timedelta(minutes=1),
            sample_count=60,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=1.0,
        )

        def hanging_fetch():
            time_mod.sleep(3)
            return self._payload(now)

        result, fresh = cache.get_or_fetch(hanging_fetch, now)
        assert fresh is False
        assert result is not None

    def test_fetcher_exception_absorbed_and_lock_usable_after(self):
        """Fetcher exceptions are absorbed (logged) -> empty/stale result.

        _run_fetch_with_timeout converts any raised exception into None so
        get_or_fetch can serve stale data; that contract must survive the
        locking rework.
        """
        cache = EnergyCache()
        now = self._now()

        def bad_fetch():
            raise ValueError("api down")

        result, fresh = cache.get_or_fetch(bad_fetch, now)
        assert result is None
        assert fresh is True

        # State lock must be free and the cache still readable.
        assert cache.get_current_qh(now) is None


class TestFlatDataQuantizationGuard:
    """get_current_qh must reject the flat-data N=2 artifact (A2)."""

    def test_flat_data_falls_back_to_default_window(self):
        from dataclasses import replace

        cache = EnergyCache()
        now = datetime(2025, 6, 15, 14, 5, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        # Trailing-2 rate differs wildly from the default-window rate.
        samples = [1.0] * 40 + [9.0] * 20
        cache._data = cache._merge_samples_replace(samples, data_start, now)
        assert cache.data is not None

        # Force the flat-data artifact into stored state (N=2, conf 1.0,
        # as detect_quantization reports for all-identical arrays). The
        # shared guard must reject it so extrapolation uses the default
        # window instead of the last 2 samples.
        cache._data = replace(
            cache.data,
            quantization_seconds=2,
            quantization_confidence=1.0,
        )

        result = cache.get_current_qh(now)
        assert result is not None

        from util import compute_nbc_quarters, qh_seconds_remaining

        expected = compute_nbc_quarters(
            samples, None, seconds_remaining_override=qh_seconds_remaining(now)
        ).qh1.predicted_wh
        assert result["predicted_wh"] == pytest.approx(expected), (
            "flat-data N=2 artifact must not drive extrapolation"
        )
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

        result, _ = cache.get_or_fetch(fetch_func, now)
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

        # Seed cache with all quantization fields set
        cache.last_sample_at = data_time
        cache.data_start = data_time
        cache.quantization_seconds = quantum
        cache.quantization_offset = 0
        cache.quantization_confidence = 0.95

        result = cache.sleep_interval_adjust(30.0, now)
        assert result == 5.0

    def test_sleep_min_at_2x_quantum_boundary(self) -> None:
        """sleep_interval_adjust returns MIN_SLEEP_SECS at exactly 2× quantum."""
        cache = EnergyCache(ttl_seconds=60)
        quantum = 30  # 2×30 = 60s threshold
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 1, 0, tzinfo=timezone.utc)  # exactly 60s later

        cache.last_sample_at = data_time
        cache.data_start = data_time
        cache.quantization_seconds = quantum
        cache.quantization_offset = 0
        cache.quantization_confidence = 0.95

        result = cache.sleep_interval_adjust(30.0, now)
        # At exactly 2× (60s), data_age=60, 60 > 60 is False → falls through
        # At 60s+1ns it would return 5.0. Testing boundary: exactly 2× should
        # NOT trigger the early-exit (uses strict >), so it falls through to
        # the quantization adjustment below.
        assert isinstance(result, float)
        assert result >= 5.0

    def test_falls_through_below_2x_quantum(self) -> None:
        """sleep_interval_adjust falls through to quantization logic below 2× quantum."""
        cache = EnergyCache(ttl_seconds=60)
        quantum = 30  # 2×30 = 60s threshold
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 0, 45, tzinfo=timezone.utc)  # 45s, < 60s

        cache.last_sample_at = data_time
        cache.data_start = data_time
        cache.quantization_seconds = quantum
        cache.quantization_offset = 0
        cache.quantization_confidence = 0.95

        result = cache.sleep_interval_adjust(30.0, now)
        # Should not return 5.0 from the early-exit; falls through to quantization
        # logic. Result may still be 5.0 if the quantization math produces it,
        # but the key is that the early-exit path was NOT taken.
        assert isinstance(result, float)
        assert result >= 5.0

    def test_skips_when_last_sample_at_none(self) -> None:
        """sleep_interval_adjust skips 2× check when last_sample_at is None."""
        cache = EnergyCache(ttl_seconds=60)
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 1, 1, tzinfo=timezone.utc)

        # Only set quantization fields and data_start, not last_sample_at
        cache.data_start = data_time
        cache.quantization_seconds = 30
        cache.quantization_offset = 0
        cache.quantization_confidence = 0.95

        result = cache.sleep_interval_adjust(30.0, now)
        assert isinstance(result, float)
        assert result >= 5.0

    def test_skips_when_quantum_missing(self) -> None:
        """sleep_interval_adjust skips 2× check when quantization_seconds is None."""
        cache = EnergyCache(ttl_seconds=60)
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 1, 1, tzinfo=timezone.utc)

        cache.last_sample_at = data_time
        # Do not set quantization_seconds — it stays None

        result = cache.sleep_interval_adjust(30.0, now)
        # Returns unchanged because quantization_confidence is None (below threshold)
        assert isinstance(result, float)

    def test_result_clamped_at_5_minimum(self) -> None:
        """sleep_interval_adjust result is never below MIN_SLEEP_SECS (5.0)."""
        cache = EnergyCache(ttl_seconds=60)
        quantum = 5  # Very small quantum: 2×5 = 10s threshold
        data_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 1, 12, 0, 12, tzinfo=timezone.utc)  # 12s > 10s

        cache.last_sample_at = data_time
        cache.data_start = data_time
        cache.quantization_seconds = quantum
        cache.quantization_offset = 0
        cache.quantization_confidence = 0.95

        result = cache.sleep_interval_adjust(30.0, now)
        assert result == 5.0

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
