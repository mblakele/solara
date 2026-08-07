"""Tests for EnergyCache quantization-aware behavior."""  # noqa: D01

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

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

        Expected predicted_wh with 30s window:
            prediction_w = 1000 * 0.003 = 3.0 W
            raw_wh = 1000 * (70*0.001 + 30*0.003) = 160 Wh
            predicted_wh = 160 + 800 * 3.0 = 2560 Wh

        With default 60s window, prediction_w would be 2.0 W and predicted_wh=1760.
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
        # 2560 from 30s window (not 1760 from 60s window)
        assert result["predicted_wh"] == pytest.approx(2560.0, abs=0.01), (
            f"Expected 2560 (30s window) but got {result['predicted_wh']}"
        )

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
        # 2560 from 30s window
        assert result["predicted_wh"] == pytest.approx(2560.0, abs=0.01), (
            f"Expected 1760 (60s window) but got {result['predicted_wh']}"
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
        # 2560 from 30s default (not 1760 from old 60s default)
        assert result["predicted_wh"] == pytest.approx(2560.0, abs=0.01), (
            f"Expected 2560 (30s default) but got {result['predicted_wh']}"
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


class TestGettersRaceWithInvalidate:
    """Lock-free members must never tear when invalidate() runs concurrently.

    ``invalidate()`` sets ``_data = None`` (metrics loop error path). A
    member that loads ``self._data`` twice — once for the None check, once
    for the attribute access — can see ``None`` on the second load and raise
    ``AttributeError``. Each member must bind the snapshot to a local once.

    The race window is a couple of bytecode ops, so a probabilistic hammer
    test is useless. Instead we reproduce the interleaving deterministically:
    the reader's FIRST ``_data`` load is paused (patched
    ``__getattribute__``) after the value is captured; the main thread then
    runs ``invalidate()``; the reader continues to its second load and tears.
    A single-load implementation returns the pre-invalidate snapshot cleanly.
    """

    def _run_torn_read(
        self, read_fn: Callable[[EnergyCache, datetime], object]
    ) -> object:
        """Run *read_fn* while invalidate() lands between two _data loads.

        Args:
            read_fn: Callable invoked by the reader thread with the cache
                and a reference ``now``.

        Returns:
            Whatever *read_fn* returned, or ``None`` on error paths.

        Raises:
            AssertionError: If the reader raised (i.e. the member tore).
        """
        import threading

        cache = EnergyCache()
        now = datetime(2026, 5, 7, 15, 20, 0, tzinfo=timezone.utc)
        cache._data = EnergyCacheData(
            samples=[0.1, 0.2, 0.3],
            data_start=now,
            last_sample_at=now,
            last_fetch_at=now,
            sample_count=3,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=0.9,
            data_lag_secs=5.0,
            full_metrics_dict={"devices": []},
            completed_periods=[],
        )

        original_getattribute = EnergyCache.__getattribute__
        first_load = threading.Event()
        proceed = threading.Event()
        result: dict[str, object] = {}
        reader_errors: list[BaseException] = []

        def racy_getattribute(self: object, name: str) -> object:
            value = original_getattribute(self, name)
            if name == "_data" and not first_load.is_set():
                first_load.set()
                if not proceed.wait(timeout=5):
                    raise TimeoutError("writer never invalidated")
            return value

        def reader() -> None:
            try:
                result["value"] = read_fn(cache, now)
            except BaseException as exc:  # pragma: no cover - failure path
                reader_errors.append(exc)

        EnergyCache.__getattribute__ = racy_getattribute  # type: ignore[method-assign]
        try:
            reader_thread = threading.Thread(target=reader)
            reader_thread.start()
            assert first_load.wait(timeout=5), "reader never started"
            # invalidate() lands between the reader's two _data loads.
            cache.invalidate()
            proceed.set()
            reader_thread.join(timeout=5)
        finally:
            EnergyCache.__getattribute__ = original_getattribute  # type: ignore[method-assign]

        assert reader_errors == [], f"member tore under invalidate: {reader_errors}"
        return result.get("value")

    @pytest.mark.parametrize(
        "member",
        [
            pytest.param(lambda c, n: c.samples, id="samples"),
            pytest.param(lambda c, n: c.data_start, id="data_start"),
            pytest.param(lambda c, n: c.last_sample_at, id="last_sample_at"),
            pytest.param(lambda c, n: c.last_fetch_at, id="last_fetch_at"),
            pytest.param(lambda c, n: c.sample_count, id="sample_count"),
            pytest.param(lambda c, n: c.quantization_seconds, id="quantization_seconds"),
            pytest.param(lambda c, n: c.quantization_offset, id="quantization_offset"),
            pytest.param(lambda c, n: c.quantization_confidence, id="quantization_confidence"),
            pytest.param(lambda c, n: c.full_metrics_dict, id="full_metrics_dict"),
            pytest.param(lambda c, n: c.completed_periods, id="completed_periods"),
            pytest.param(lambda c, n: c.data_lag_secs, id="data_lag_secs"),
            pytest.param(
                lambda c, n: c.sleep_interval_adjust(30.0, n),
                id="sleep_interval_adjust",
            ),
        ],
    )
    def test_member_never_tears_against_invalidate(
        self, member: Callable[[EnergyCache, datetime], object]
    ) -> None:
        """Member never raises when invalidate() lands between its loads."""
        value = self._run_torn_read(member)
        assert value is not None
