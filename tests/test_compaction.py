"""Tests for EnergyCache compaction and CompletedNBCPeriod.

Covers:
  - CompletedNBCPeriod dataclass
  - EnergyCache.compact()
  - util.inject_completed_qh()
  - EnergyCache._merge_samples_replace()
  - get_or_fetch replace-not-merge behavior
  - boundary fetch compacting a missed QH
  - _compute_device_metrics injecting completed QH
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from energy_cache import EnergyCache, EnergyCacheData
from metrics import MetricsBase
from util import (
    CompletedNBCPeriod,
    NBCQuarter,
    NBCQuarterSet,
)


# ---------------------------------------------------------------------------
# Phase 1: CompletedNBCPeriod dataclass
# ---------------------------------------------------------------------------

class TestCompletedNBCPeriod:
    """Tests for the CompletedNBCPeriod frozen dataclass."""

    def test_is_frozen(self) -> None:
        """CompletedNBCPeriod must be immutable."""
        start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        p = CompletedNBCPeriod(start=start, raw_wh=123.45)
        with pytest.raises(AttributeError):
            p.start = datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_data(
    samples: list[float],
    data_start: datetime,
    now: datetime,
    completed_periods: list[CompletedNBCPeriod] | None = None,
    full_metrics_dict: dict | None = None,
) -> EnergyCacheData:
    """Create an EnergyCacheData with metadata derived from the samples."""
    return EnergyCacheData(
        samples=samples,
        data_start=data_start,
        last_sample_at=data_start + timedelta(seconds=len(samples) - 1)
        if samples
        else data_start,
        last_fetch_at=now,
        sample_count=len(samples),
        quantization_seconds=None,
        quantization_offset=None,
        quantization_confidence=None,
        completed_periods=completed_periods,
        full_metrics_dict=full_metrics_dict,
    )


def _make_cache(
    samples: list[float],
    data_start: datetime,
    now: datetime,
    completed_periods: list[CompletedNBCPeriod] | None = None,
    full_metrics_dict: dict | None = None,
    last_fetch_at: datetime | None = None,
) -> EnergyCache:
    """Create an EnergyCache pre-populated with the given window."""
    cache = EnergyCache()
    cache._data = _make_data(
        samples=samples,
        data_start=data_start,
        now=last_fetch_at if last_fetch_at is not None else now,
        completed_periods=completed_periods,
        full_metrics_dict=full_metrics_dict,
    )
    return cache


# ---------------------------------------------------------------------------
# Phase 2: EnergyCache.compact()
# ---------------------------------------------------------------------------

class TestCompact:
    """Tests for EnergyCache.compact()."""

    def test_no_data_noop(self) -> None:
        """compact() is a no-op when cache is empty."""
        cache = EnergyCache()
        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        with cache._lock:
            cache.compact(now)
        assert cache._data is None

    def test_no_completed_periods_noop(self) -> None:
        """compact() doesn't compact when no complete QH periods exist (< 900 samples)."""
        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)
        # Only 500 samples — less than one QH
        cache = _make_cache(
            samples=[0.001] * 500,
            data_start=data_start,
            now=now,
        )
        with cache._lock:
            cache.compact(now)
        assert cache._data is not None
        assert len(cache._data.samples) == 500
        assert cache._data.completed_periods is None

    def test_compacts_completed_periods(self) -> None:
        """compact() identifies and stores completed QH periods."""
        # 2700 samples starting at 14:00 → covers QH2 (14:00-14:15), QH3 (14:15-14:30),
        # QH4 (14:30-14:45), and partial QH1 (14:45-14:50)
        # At now=14:50, QH2-QH4 are complete, QH1 is incomplete
        now = datetime(2025, 6, 15, 14, 50, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        # 2700 samples = 3 complete QH (900 each) + 0 extra
        # Actually let's do 2700 + 300 = 3000 samples to have a partial QH1
        samples = [0.001] * 2700 + [0.002] * 300
        cache = _make_cache(samples=samples, data_start=data_start, now=now)
        with cache._lock:
            cache.compact(now)
        assert cache._data is not None
        assert cache._data.completed_periods is not None
        # 3 completed QH periods (QH2, QH3, QH4)
        assert len(cache._data.completed_periods) == 3
        # Current QH data preserved (300 samples)
        assert len(cache._data.samples) == 300

    def test_preserves_current_qh(self) -> None:
        """compact() preserves per-second data for current QH."""
        now = datetime(2025, 6, 15, 14, 20, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        # 1200 samples = 1 complete QH (900) + 300 in current QH
        samples = [0.001] * 900 + [0.002] * 300
        cache = _make_cache(samples=samples, data_start=data_start, now=now)
        with cache._lock:
            cache.compact(now)
        assert cache._data is not None
        assert len(cache._data.samples) == 300
        assert cache._data.data_start == datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)

    def test_prunes_old_completed_periods(self) -> None:
        """compact() removes CompletedNBCPeriod objects older than 1 hour."""
        now = datetime(2025, 6, 15, 15, 30, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 15, 15, 0, tzinfo=timezone.utc)
        # 300 samples in current QH
        samples = [0.001] * 300
        # Existing completed period from 2 hours ago (should be pruned)
        old_period = CompletedNBCPeriod(
            start=datetime(2025, 6, 15, 13, 0, 0, tzinfo=timezone.utc),
            raw_wh=500.0,
        )
        cache = _make_cache(
            samples=samples,
            data_start=data_start,
            now=now,
            completed_periods=[old_period],
        )
        with cache._lock:
            cache.compact(now)
        assert cache._data is not None
        # Old period should be pruned
        if cache._data.completed_periods:
            for p in cache._data.completed_periods:
                assert p.start >= now - timedelta(seconds=3600)

    def test_deduplicates_completed_periods(self) -> None:
        """compact() deduplicates by start time."""
        now = datetime(2025, 6, 15, 14, 50, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 2700 + [0.002] * 300
        # Existing period with same start as one we'll compact
        existing = CompletedNBCPeriod(
            start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
            raw_wh=999.0,  # different raw_wh
        )
        cache = _make_cache(
            samples=samples,
            data_start=data_start,
            now=now,
            completed_periods=[existing],
        )
        with cache._lock:
            cache.compact(now)
        assert cache._data is not None
        assert cache._data.completed_periods is not None
        # Should have 3 periods (QH2, QH3, QH4), deduplicated
        assert len(cache._data.completed_periods) == 3
        # The one with start=14:00 should use the new raw_wh from samples
        qh2 = next(p for p in cache._data.completed_periods
                   if p.start == datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc))
        expected_raw_wh = 900 * 0.001 * 1000  # 900 samples * 0.001 kWh * 1000
        assert qh2.raw_wh == pytest.approx(expected_raw_wh, rel=1e-6)

    def test_keeps_at_most_3(self) -> None:
        """compact() keeps at most 3 completed periods."""
        now = datetime(2025, 6, 15, 14, 55, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        # 3600 samples = 4 complete QH periods
        # At now=14:55, all 4 are complete (end times: 14:15, 14:30, 14:45, 15:00)
        # Wait, 14:00 + 3600s = 15:00. ceil_to_qh(14:55) = 15:00.
        # So QH4 ends at 14:59:59, which is < 15:00 = ceil_to_qh(now).
        # All 4 are complete. But we keep at most 3.
        samples = [0.001] * 3600
        cache = _make_cache(samples=samples, data_start=data_start, now=now)
        with cache._lock:
            cache.compact(now)
        assert cache._data is not None
        assert cache._data.completed_periods is not None
        assert len(cache._data.completed_periods) <= 3


# ---------------------------------------------------------------------------
# Phase 2: inject_completed_qh()
# ---------------------------------------------------------------------------

class TestInjectCompletedQH:
    """Tests for inject_completed_qh() helper."""

    def test_injects_qh2_qh4_from_completed(self) -> None:
        """inject_completed_qh() fills QH2-QH4 from completed periods."""
        from util import inject_completed_qh

        # Start with NBC that has only QH1
        qh1 = NBCQuarter(
            complete=False,
            raw_wh=100.0,
            wh=100.0,
            predicted_wh=500.0,
            remaining_seconds=600,
            samples_used=300,
        )
        nbc = NBCQuarterSet(qh1=qh1, qh2=None, qh3=None, qh4=None)

        completed = [
            CompletedNBCPeriod(
                start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
                raw_wh=200.0,
            ),
            CompletedNBCPeriod(
                start=datetime(2025, 6, 15, 13, 45, 0, tzinfo=timezone.utc),
                raw_wh=300.0,
            ),
        ]

        result = inject_completed_qh(nbc, completed)
        assert result.qh1 is not None
        assert result.qh1.raw_wh == 100.0  # QH1 preserved
        assert result.qh2 is not None
        assert result.qh2.complete is True
        assert result.qh2.raw_wh == 200.0  # Most recent completed
        assert result.qh3 is not None
        assert result.qh3.complete is True
        assert result.qh3.raw_wh == 300.0
        assert result.qh4 is None  # Only 2 completed periods

    def test_injects_qh3_qh4_when_qh2_present(self) -> None:
        """When QH2 is already present from per-second data, inject QH3/QH4 from completed periods.

        The most recent completed period goes into QH3 and the next most
        recent goes into QH4, rather than skipping the most recent period.
        """
        from util import inject_completed_qh

        qh1 = NBCQuarter(
            complete=False,
            raw_wh=100.0,
            wh=100.0,
            predicted_wh=500.0,
            remaining_seconds=600,
            samples_used=300,
        )
        qh2 = NBCQuarter(complete=True, raw_wh=999.0, wh=999.0)
        nbc = NBCQuarterSet(qh1=qh1, qh2=qh2, qh3=None, qh4=None)

        completed = [
            CompletedNBCPeriod(
                start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
                raw_wh=200.0,
            ),
            CompletedNBCPeriod(
                start=datetime(2025, 6, 15, 13, 45, 0, tzinfo=timezone.utc),
                raw_wh=300.0,
            ),
        ]

        result = inject_completed_qh(nbc, completed)
        assert result.qh1 is not None
        assert result.qh1.raw_wh == 100.0  # QH1 preserved
        assert result.qh2 is not None
        assert result.qh2.raw_wh == 999.0  # QH2 preserved from per-second
        assert result.qh3 is not None
        assert result.qh3.complete is True
        assert result.qh3.raw_wh == 200.0  # Most recent completed → QH3
        assert result.qh4 is not None
        assert result.qh4.complete is True
        assert result.qh4.raw_wh == 300.0  # Second most recent → QH4

    def test_preserves_qh1_from_per_second(self) -> None:
        """inject_completed_qh() preserves QH1 from per-second data."""
        from util import inject_completed_qh

        qh1 = NBCQuarter(
            complete=False,
            raw_wh=50.0,
            wh=50.0,
            predicted_wh=200.0,
            remaining_seconds=300,
            samples_used=600,
        )
        nbc = NBCQuarterSet(qh1=qh1, qh2=None, qh3=None, qh4=None)

        result = inject_completed_qh(nbc, [])
        assert result.qh1 is not None
        assert result.qh1.raw_wh == 50.0

    def test_empty_completed_periods(self) -> None:
        """inject_completed_qh() returns original NBC when no completed periods."""
        from util import inject_completed_qh

        qh1 = NBCQuarter(
            complete=False,
            raw_wh=100.0,
            wh=100.0,
            predicted_wh=500.0,
            remaining_seconds=600,
            samples_used=300,
        )
        nbc = NBCQuarterSet(qh1=qh1, qh2=None, qh3=None, qh4=None)

        result = inject_completed_qh(nbc, [])
        assert result == nbc

    def test_does_not_overwrite_existing_qh2(self) -> None:
        """inject_completed_qh() does not overwrite QH2 if already present."""
        from util import inject_completed_qh

        qh1 = NBCQuarter(
            complete=False,
            raw_wh=100.0,
            wh=100.0,
            predicted_wh=500.0,
            remaining_seconds=600,
            samples_used=300,
        )
        qh2_existing = NBCQuarter(
            complete=True,
            raw_wh=999.0,
            wh=999.0,
        )
        nbc = NBCQuarterSet(qh1=qh1, qh2=qh2_existing, qh3=None, qh4=None)

        completed = [
            CompletedNBCPeriod(
                start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
                raw_wh=200.0,
            ),
        ]

        result = inject_completed_qh(nbc, completed)
        assert result.qh2 is not None
        assert result.qh2.raw_wh == 999.0  # Existing preserved, not overwritten


# ---------------------------------------------------------------------------
# Phase 2: _merge_samples_replace()
# ---------------------------------------------------------------------------

class TestMergeSamplesReplace:
    """Tests for EnergyCache._merge_samples_replace()."""

    def test_replaces_per_second_data(self) -> None:
        """_merge_samples_replace() discards old samples, stores new."""
        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)

        cache = _make_cache(
            samples=[0.001] * 900,
            data_start=data_start,
            now=now,
            completed_periods=[
                CompletedNBCPeriod(
                    start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
                    raw_wh=100.0,
                ),
            ],
        )

        new_samples = [0.002] * 300
        result = cache._merge_samples_replace(new_samples, data_start, now)

        assert result.samples == [0.002] * 300
        assert result.data_start == data_start
        assert result.sample_count == 300
        # Completed periods preserved
        assert result.completed_periods is not None
        assert len(result.completed_periods) == 1
        assert result.completed_periods[0].raw_wh == 100.0

    def test_preserves_completed_periods(self) -> None:
        """_merge_samples_replace() preserves completed_periods from existing _data."""
        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)

        periods = [
            CompletedNBCPeriod(
                start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
                raw_wh=100.0,
            ),
            CompletedNBCPeriod(
                start=datetime(2025, 6, 15, 13, 45, 0, tzinfo=timezone.utc),
                raw_wh=200.0,
            ),
        ]
        cache = _make_cache(
            samples=[0.001] * 300,
            data_start=data_start,
            now=now,
            completed_periods=periods,
        )

        new_samples = [0.003] * 100
        result = cache._merge_samples_replace(new_samples, data_start, now)

        assert result.completed_periods is not None
        assert len(result.completed_periods) == 2


# ---------------------------------------------------------------------------
# Phase 2: get_or_fetch replace-not-merge
# ---------------------------------------------------------------------------

class TestGetOrFetchReplace:
    """Tests for get_or_fetch replace-not-merge behavior."""

    def test_replace_when_data_start_matches(self) -> None:
        """get_or_fetch replaces per-second data when data_start matches."""
        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)

        cache = _make_cache(
            samples=[0.001] * 300,
            data_start=data_start,
            last_fetch_at=now - timedelta(seconds=60),
            now=now,
            completed_periods=[
                CompletedNBCPeriod(
                    start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
                    raw_wh=100.0,
                ),
            ],
        )

        # Fetch returns data starting at the same data_start
        fetch_result = {
            "per_second_data": [0.002] * 500,
            "data_start": data_start,
        }
        fetch_func = MagicMock(return_value=fetch_result)

        _, was_fresh = cache.get_or_fetch(fetch_func, now, force=True)
        assert was_fresh is True
        assert cache._data is not None
        # Should be replaced, not merged (500 new, not 300+500=800)
        assert len(cache._data.samples) == 500
        # Completed periods preserved
        assert cache._data.completed_periods is not None
        assert len(cache._data.completed_periods) == 1

    def test_replace_when_data_start_differs(self) -> None:
        """get_or_fetch replaces when data_start differs (always-replace semantics)."""
        now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        cache = _make_cache(
            samples=[0.001] * 500,
            data_start=data_start,
            last_fetch_at=now - timedelta(seconds=60),
            now=now,
        )

        new_data_start = data_start + timedelta(seconds=500)
        fetch_result = {
            "per_second_data": [0.002] * 60,
            "data_start": new_data_start,
        }
        fetch_func = MagicMock(return_value=fetch_result)

        _, was_fresh = cache.get_or_fetch(fetch_func, now, force=True)
        assert was_fresh is True
        assert cache._data is not None
        # Replace semantics: only new 60 samples stored
        assert len(cache._data.samples) == 60
        assert cache._data.data_start == new_data_start


# ---------------------------------------------------------------------------
# Phase 3: boundary fetch compacting a missed QH
# ---------------------------------------------------------------------------

class TestBoundaryFetchCompaction:
    """Tests for fetching across a QH boundary to compact a missed QH."""

    def test_boundary_fetch_compacts_missed_qh(self) -> None:
        """Fetching across a QH boundary completes and compacts the missed QH.

        If the last fetch before a QH boundary held <900 samples of the
        just-completed QH (e.g. 890), the next fetch must start from the old
        QH-aligned data_start so the window reaches 900 samples and compact()
        materializes a CompletedNBCPeriod.  Starting from floor_to_qh(now)
        would replace the un-compacted window and lose the QH.
        """
        now = datetime(2026, 7, 31, 18, 45, 40, tzinfo=timezone.utc)
        data_start = datetime(2026, 7, 31, 18, 30, 0, tzinfo=timezone.utc)

        cache = _make_cache(
            samples=[0.001] * 890,
            data_start=data_start,
            now=now,
        )

        def fetcher() -> dict[str, Any]:
            # Mirrors the production fetcher contract: the request starts at
            # the old QH-aligned data_start and the API returns data starting
            # at the requested start, covering the completed QH (940 seconds).
            return {
                "per_second_data": [0.001] * 940,
                "data_start": data_start,
            }

        cache.get_or_fetch(fetcher, now, force=True)

        assert cache._data is not None
        completed = cache._data.completed_periods or []
        assert any(
            p.start == data_start for p in completed
        ), (
            f"expected a CompletedNBCPeriod at {data_start}, "
            f"got {[p.start for p in completed]}"
        )


# ---------------------------------------------------------------------------
# Phase 3: _compute_device_metrics injecting completed QH
# ---------------------------------------------------------------------------

class TestComputeDeviceMetricsCompletedPeriods:
    """Tests for _compute_device_metrics injecting completed QH."""

    def test_injects_qh2_qh4_from_completed(self) -> None:
        """_compute_device_metrics injects QH2-QH4 from completed periods."""
        from metrics import HourlyProjection

        now = datetime(2025, 6, 15, 14, 20, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)

        cache = _make_cache(
            samples=[0.001] * 300,
            data_start=data_start,
            now=now,
            completed_periods=[
                CompletedNBCPeriod(
                    start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
                    raw_wh=200.0,
                ),
                CompletedNBCPeriod(
                    start=datetime(2025, 6, 15, 13, 45, 0, tzinfo=timezone.utc),
                    raw_wh=300.0,
                ),
            ],
        )

        # Patch base-class network calls so the constructor never touches
        # the real Emporia API (same pattern as test_metrics.py).
        with patch.object(MetricsBase, "vue_init"), \
             patch.object(MetricsBase, "get_device_info"):
            hp = HourlyProjection(now, MagicMock(), energy_cache=cache)

        # Create a mock vdi
        vdi = MagicMock()
        vdi.device_gid = 12345
        vdi.device_name = "test_device"

        # Create a mock pop_result with per-second data for QH1 only
        pop_result = MagicMock()
        pop_result.per_second_data = [0.001] * 300
        pop_result.nbc_seconds = [0.001] * 300
        pop_result.nbc_data_start = data_start
        pop_result.chart_data = [0.001] * 300

        # Create a mock pred_result
        pred_result = MagicMock()
        pred_result.prediction = 100.0
        pred_result.prediction_min = 80.0
        pred_result.prediction_max = 120.0
        pred_result.lag = timedelta(seconds=5)
        pred_result.minute_predicted = 10.0
        pred_result.seconds_remaining = 600

        result = hp._compute_device_metrics(vdi, pop_result, pred_result)

        # NBC should have QH1 from per-second data and QH2-QH4 from completed periods
        assert result.nbc.qh1 is not None
        assert result.nbc.qh1.complete is False  # QH1 is incomplete
        assert result.nbc.qh2 is not None
        assert result.nbc.qh2.complete is True
        assert result.nbc.qh2.raw_wh == 200.0
        assert result.nbc.qh3 is not None
        assert result.nbc.qh3.complete is True
        assert result.nbc.qh3.raw_wh == 300.0
        assert result.nbc.qh4 is None  # Only 2 completed periods


# ---------------------------------------------------------------------------
# Integration: full compaction cycle
# ---------------------------------------------------------------------------

class TestCompactionIntegration:
    """Integration tests for the full compaction cycle."""

    def test_full_cycle_fetch_compact_replace(self) -> None:
        """Full cycle: initial fetch → compact → subsequent fetch → verify."""
        # now=14:20 means ceil_to_qh(14:20) = 14:30. Only QH starting at 14:00
        # ends before 14:30, so only 1 QH gets compacted.
        now = datetime(2025, 6, 15, 14, 20, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

        cache = EnergyCache()

        # Step 1: Initial fetch (1200 samples = 1 complete QH + 300 in current QH)
        initial_samples = [0.001] * 900 + [0.002] * 300
        fetch_result = {
            "per_second_data": initial_samples,
            "data_start": data_start,
        }
        fetch_func = MagicMock(return_value=fetch_result)
        cache.get_or_fetch(fetch_func, now, force=True)

        assert cache._data is not None
        # After compact: 300 samples in current QH (900 compacted)
        assert len(cache._data.samples) == 300
        assert cache._data.completed_periods is not None
        assert len(cache._data.completed_periods) == 1

        # Step 2: Subsequent fetch (new QH data — same data_start after compact)
        new_data_start = datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)
        new_samples = [0.003] * 400
        fetch_result_2 = {
            "per_second_data": new_samples,
            "data_start": new_data_start,
        }
        fetch_func_2 = MagicMock(return_value=fetch_result_2)
        cache.get_or_fetch(fetch_func_2, now, force=True)

        assert cache._data is not None
        # Should be replaced, not merged (same data_start)
        assert len(cache._data.samples) == 400
        # Completed periods preserved
        assert cache._data.completed_periods is not None
        assert len(cache._data.completed_periods) == 1


# ---------------------------------------------------------------------------
# data_start QH-alignment after compaction
# ---------------------------------------------------------------------------

class TestCompactPartialChunkWarning:
    """compact() must not silently drop a misaligned leading chunk.

    When data_start is misaligned and the leading partial chunk cannot
    form a full QH (``offset + 900 > len(samples)``), compact() trims it
    with no CompletedNBCPeriod materialized — up to 899 seconds of data
    vanish.  The trim itself is correct (the partial chunk predates the
    window's first QH boundary and can never be completed; keeping it
    would leave a misaligned data_start that trips
    EnergyCacheAlignmentError), but the loss must be loud, not silent.
    """

    def test_warns_when_trimming_partial_chunk_without_materializing(
        self, caplog
    ) -> None:
        """A misaligned leading chunk trimmed with no completed period warns."""
        import logging

        now = datetime(2026, 7, 30, 2, 50, 0, tzinfo=timezone.utc)
        data_start = datetime(2026, 7, 30, 2, 34, 1, tzinfo=timezone.utc)

        # 1200 samples: offset = 659 (02:34:01 -> 02:45:00) but
        # offset + 900 = 1559 > 1200, so the while loop runs zero times
        # and the leading partial chunk is trimmed without materializing
        # a CompletedNBCPeriod.
        samples = [0.001] * 1200

        cache = _make_cache(
            samples=samples,
            data_start=data_start,
            now=now,
        )

        with caplog.at_level(logging.WARNING, logger="energy_cache"):
            with cache._lock:
                cache.compact(now)

        assert any(
            "partial chunk" in record.message
            for record in caplog.records
        ), "compact() should warn when trimming a partial chunk"

        # Trim behavior unchanged: current-QH tail preserved and aligned.
        assert cache._data is not None
        assert len(cache._data.samples) == 541
        assert cache._data.data_start == datetime(
            2026, 7, 30, 2, 45, 0, tzinfo=timezone.utc
        )
        # data_start must never jump past the last sample time.
        assert cache._data.data_start <= cache._data.last_sample_at
        assert not cache._data.completed_periods

    def test_no_warning_when_aligned_and_nothing_to_do(self, caplog) -> None:
        """QH-aligned data_start with no completed periods logs nothing."""
        import logging

        now = datetime(2025, 6, 15, 14, 20, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)

        cache = _make_cache(
            samples=[0.001] * 300,
            data_start=data_start,
            now=now,
        )

        with caplog.at_level(logging.WARNING, logger="energy_cache"):
            with cache._lock:
                cache.compact(now)

        assert not any(
            "partial chunk" in record.message
            for record in caplog.records
        )
        # Already QH-aligned and nothing to do — data stays unchanged.
        assert cache._data is not None
        assert len(cache._data.samples) == 300
        assert cache._data.data_start == data_start
