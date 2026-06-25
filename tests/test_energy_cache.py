"""Tests for EnergyCache quantization-aware behavior."""  # noqa: D01

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from energy_cache import EnergyCache, EnergyCacheData


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
        from energy_cache import CurrentQH
        cache = EnergyCache()
        cache._data = EnergyCacheData(
            current_qh=CurrentQH(data_start=data_start, samples=list(samples)),
            last_fetch_at=data_start,
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

    def test_get_current_qh_falls_back_to_60_when_no_quantization(self):
        """get_current_qh falls back to 60s window when no quantization data.

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
        # 1760 from 60s window
        assert result["predicted_wh"] == pytest.approx(1760.0, abs=0.01), (
            f"Expected 1760 (60s window) but got {result['predicted_wh']}"
        )

    def test_get_current_qh_falls_back_when_confidence_below_threshold(self):
        """get_current_qh falls back to 60s window when confidence < 0.9.

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
        # 1760 from 60s fallback (not 2560 from 30s window)
        assert result["predicted_wh"] == pytest.approx(1760.0, abs=0.01), (
            f"Expected 1760 (60s fallback) but got {result['predicted_wh']}"
        )

    def test_get_current_qh_returns_none_when_no_data(self):
        """get_current_qh returns None when cache has no data."""
        cache = EnergyCache()
        result = cache.get_current_qh(datetime(2025, 6, 15, 14, 1, 0, tzinfo=timezone.utc))
        assert result is None

    def test_get_current_qh_returns_none_when_qh1_complete(self):
        """get_current_qh returns None when QH1 is complete (stale data)."""
        from energy_cache import CurrentQH
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        # 900 samples = complete QH1
        samples = [0.001] * 900
        now = datetime(2025, 6, 15, 14, 15, 1, tzinfo=timezone.utc)

        cache = EnergyCache()
        cache._data = EnergyCacheData(
            current_qh=CurrentQH(data_start=data_start, samples=samples),
            last_fetch_at=now,
            quantization_seconds=30,
            quantization_confidence=1.0,
        )
        result = cache.get_current_qh(now)
        assert result is None

    def test_get_current_qh_reads_from_current_qh_block(self):
        """get_current_qh computes NBC from current_qh samples directly."""
        from energy_cache import CurrentQH
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 100  # 100 samples = incomplete QH
        now = datetime(2025, 6, 15, 14, 1, 40, tzinfo=timezone.utc)

        cache = EnergyCache()
        cache._data = EnergyCacheData(
            current_qh=CurrentQH(data_start=data_start, samples=samples),
            last_fetch_at=now,
            quantization_seconds=30,
            quantization_confidence=1.0,
        )
        result = cache.get_current_qh(now)

        assert result is not None
        assert result["qh_name"] == "QH1"
        assert result["predicted_wh"] > 0
        assert result["data_start"] == data_start
