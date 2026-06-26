"""Tests for EnergyCache quantization-aware behavior."""  # noqa: D01

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from energy_cache import EnergyCache, EnergyCacheData
from util import ceil_to_qh


class TestEnergyCacheLowConfidenceLog:
    """Tests for low-confidence quantization warning log."""

    def test_low_confidence_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """When detect_quantization returns confidence below the threshold, a warning is emitted.

        Mocks detect_quantization to return N=20, offset=0, confidence=0.60
        which is below QUANTIZATION_CONFIDENCE_THRESHOLD (0.7).
        """
        cache = EnergyCache()
        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        empty = EnergyCacheData(
            samples=[],
            data_start=None,
            last_sample_at=None,
            last_fetch_at=None,
            sample_count=None,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )

        new_samples = [0.0] * 7 + [1.0] * 20 + [2.0] * 20 + [3.0] * 20

        from unittest.mock import patch
        with patch("energy_cache.detect_quantization", return_value=(20, 0, 0.50)):
            with caplog.at_level("WARNING", logger="energy_cache"):
                cache._merge_samples(empty, new_samples, data_start, now)

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

        empty = EnergyCacheData(
            samples=[],
            data_start=None,
            last_sample_at=None,
            last_fetch_at=None,
            sample_count=None,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )

        # Full hour with exact 30-second samples — confidence = 1.0
        new_samples: list[float] = []
        for i in range(120):
            new_samples.extend([float(i)] * 30)

        with caplog.at_level("WARNING", logger="energy_cache"):
            cache._merge_samples(empty, new_samples, data_start, now)

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


class TestOverlapVerification:
    """Tests for incremental fetch overlap verification."""

    def _make_cache(
        self,
        samples: list[float],
        data_start: datetime,
        quantization_seconds: int | None = None,
    ) -> EnergyCache:
        """Create an EnergyCache with pre-set data."""
        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=samples,
            data_start=data_start,
            last_sample_at=data_start + timedelta(seconds=len(samples) - 1),
            last_fetch_at=data_start,
            sample_count=len(samples),
            quantization_seconds=quantization_seconds,
            quantization_offset=0,
            quantization_confidence=1.0 if quantization_seconds else None,
        )
        return cache

    def test_merge_incremental_overlap_match(self) -> None:
        """When all overlapping samples match, merge succeeds."""
        from energy_cache import EnergyCacheData
        from datetime import timedelta

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        existing = EnergyCacheData(
            samples=[0.1, 0.2, 0.3, 0.4, 0.5],
            data_start=base,
            last_sample_at=base + timedelta(seconds=4),
            last_fetch_at=base,
            sample_count=5,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=1.0,
        )
        # New samples start 2 seconds before cache end → 3 samples overlap
        # Cache: [0.1, 0.2, 0.3, 0.4, 0.5] at times 0,1,2,3,4
        # New:   [0.3, 0.4, 0.5, 0.6, 0.7] at times 2,3,4,5,6
        # Overlap at times 2,3,4 — values must match
        new_samples = [0.3, 0.4, 0.5, 0.6, 0.7]
        merged = EnergyCache.merge_incremental(
            existing, new_samples, base + timedelta(seconds=2)
        )
        assert merged is not None
        assert merged.samples == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]

    def test_merge_incremental_overlap_mismatch(self, caplog: pytest.LogCaptureFixture) -> None:
        """When overlapping samples differ, merge succeeds with a warning."""
        from energy_cache import EnergyCacheData
        from datetime import timedelta

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        existing = EnergyCacheData(
            samples=[0.1, 0.2, 0.3, 0.4, 0.5],
            data_start=base,
            last_sample_at=base + timedelta(seconds=4),
            last_fetch_at=base,
            sample_count=5,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=1.0,
        )
        # New sample at time 2 differs from cached value (0.3 vs 0.99)
        # Merge keeps cached values for overlap, appends only new-after-cache-end
        new_samples = [0.99, 0.4, 0.5, 0.6, 0.7]
        with caplog.at_level("WARNING", logger="energy_cache"):
            merged = EnergyCache.merge_incremental(
                existing, new_samples, base + timedelta(seconds=2)
            )
        assert merged is not None
        # Cached values kept for overlap; new samples after cache end appended
        assert merged.samples == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
        assert "Overlap mismatch" in caplog.text

    def test_merge_incremental_overlap_tiny_difference(self) -> None:
        """Reproduces the real-world mismatch: -1.25e-5 vs 0.0."""
        from energy_cache import EnergyCacheData
        from datetime import timedelta

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        existing = EnergyCacheData(
            samples=[-1.2537916666666667e-05],
            data_start=base,
            last_sample_at=base,
            last_fetch_at=base,
            sample_count=1,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=1.0,
        )
        new_samples = [0.0]
        merged = EnergyCache.merge_incremental(
            existing, new_samples, base
        )
        assert merged is not None

    def test_merge_incremental_overlap_real_world_values(self) -> None:
        """Reproduces the second real-world mismatch: 0.001487 vs 0.001482."""
        from energy_cache import EnergyCacheData
        from datetime import timedelta

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        existing = EnergyCacheData(
            samples=[0.0014865579166666667],
            data_start=base,
            last_sample_at=base,
            last_fetch_at=base,
            sample_count=1,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=1.0,
        )
        new_samples = [0.0014823023333333334]
        merged = EnergyCache.merge_incremental(
            existing, new_samples, base
        )
        assert merged is not None

    def test_merge_incremental_no_overlap(self) -> None:
        """When new samples start after cache end, no verification occurs."""
        from energy_cache import EnergyCacheData
        from datetime import timedelta

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        existing = EnergyCacheData(
            samples=[0.1, 0.2, 0.3],
            data_start=base,
            last_sample_at=base + timedelta(seconds=2),
            last_fetch_at=base,
            sample_count=3,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=1.0,
        )
        # New samples start after cache end — no overlap
        new_samples = [0.4, 0.5, 0.6]
        merged = EnergyCache.merge_incremental(
            existing, new_samples, base + timedelta(seconds=3)
        )
        assert merged is not None
        assert merged.samples == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]

    def test_get_or_fetch_mismatch_succeeds_on_first_fetch(
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

    def test_build_incremental_fetch_expands_by_quantum(self) -> None:
        """_build_incremental_fetch shifts start_time back by quantization_seconds."""
        from metrics import _build_incremental_fetch
        from datetime import timedelta

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=[0.1] * 100,
            data_start=base,
            last_sample_at=base + timedelta(seconds=99),
            last_fetch_at=base,
            sample_count=100,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=1.0,
        )

        captured_start = {}

        class FakeVue:
            def get_chart_usage(self, gid, start, end, **kwargs):
                captured_start["start"] = start
                return ([0.001] * 60, start)

        fetcher = _build_incremental_fetch(cache, FakeVue(), 12345, base + timedelta(minutes=5))
        fetcher()

        # Should start 30 seconds before the end of cache (100 - 30 = 70)
        expected_start = base + timedelta(seconds=70)
        assert captured_start["start"] == expected_start

    def test_build_incremental_fetch_no_quantization(self) -> None:
        """When quantization_seconds is None, no expansion occurs."""
        from metrics import _build_incremental_fetch
        from datetime import timedelta

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=[0.1] * 100,
            data_start=base,
            last_sample_at=base + timedelta(seconds=99),
            last_fetch_at=base,
            sample_count=100,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )

        captured_start = {}

        class FakeVue:
            def get_chart_usage(self, gid, start, end, **kwargs):
                captured_start["start"] = start
                return ([0.001] * 60, start)

        fetcher = _build_incremental_fetch(cache, FakeVue(), 12345, base + timedelta(minutes=5))
        fetcher()

        # Without quantization, should start at exactly the end of cache (100)
        expected_start = base + timedelta(seconds=100)
        assert captured_start["start"] == expected_start


class TestGetOrFetchFetchFuncOverlapMismatch:
    """When fetch_func() itself raises OverlapMismatchError."""

    def test_fetch_func_overlap_clears_cache_and_retries(self) -> None:
        """fetch_func raising OverlapMismatchError clears cache and retries once."""
        from datetime import timedelta
        from energy_cache import OverlapMismatchError

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
            if call_count == 1:
                raise OverlapMismatchError(
                    mismatch_count=1, overlap_count=1,
                    first_idx=0, cached_val=0.001, new_val=0.0,
                )
            return {
                "per_second_data": [1.0] * 100,
                "data_start": base,
            }

        now = base + timedelta(minutes=5)
        result, was_fresh = cache.get_or_fetch(fetcher, now)
        assert was_fresh is True
        assert result is not None
        assert call_count == 2, "Should have retried after overlap mismatch in fetch_func"

    def test_fetch_func_overlap_clears_cache_data(self) -> None:
        """After overlap mismatch from fetch_func, cache is cleared."""
        from datetime import timedelta
        from energy_cache import OverlapMismatchError

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

        def fetcher() -> dict[str, Any] | None:
            raise OverlapMismatchError(
                mismatch_count=1, overlap_count=1,
                first_idx=0, cached_val=0.001, new_val=0.0,
            )

        now = base + timedelta(minutes=5)
        with pytest.raises(OverlapMismatchError):
            cache.get_or_fetch(fetcher, now)
        # Cache should be cleared even if retry also fails
        assert cache._data is None or cache._data.samples is None
