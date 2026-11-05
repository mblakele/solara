"""C.2 contract tests: one TTL, one ``predicted_wh``, one QH helper set.

The contract is that every cache/reader default agrees on one TTL, that the
display path (``PredictionMixin._compute_nbc``), the cache path
(``EnergyCache.get_current_qh``) and the loader path
(``NBCReader._parse_metrics``) extrapolate ``predicted_wh`` with the same
wall-clock remainder, and that the quarter-hour remainder has one definition
in ``util.qh_seconds_remaining``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
import pytz

import app as app_mod
from constants import DEFAULT_CACHE_TTL_SECS
from energy_cache import EnergyCache, EnergyCacheData
from load_nbc import NBCReader
from prediction import PredictionMixin
from util import qh_seconds_remaining


class _Projection(PredictionMixin):
    """Minimal stand-in for ``HourlyProjection`` (avoids VUE auth)."""

    def __init__(
        self, instant: datetime, energy_cache: EnergyCache | None = None
    ) -> None:
        self.instant = instant
        self.energy_cache = energy_cache
        self.logger = logging.getLogger("test_cache_time_contract")


def _seeded_cache(
    samples: list[float],
    data_start: datetime,
    now: datetime,
    *,
    quantization_seconds: int | None = 30,
    quantization_confidence: float | None = 1.0,
) -> EnergyCache:
    """Return a cache holding *samples* as one aligned fetch."""
    cache = EnergyCache()
    cache._data = EnergyCacheData(
        samples=list(samples),
        data_start=data_start,
        last_sample_at=data_start + timedelta(seconds=len(samples) - 1),
        last_fetch_at=now,
        sample_count=len(samples),
        quantization_seconds=quantization_seconds,
        quantization_offset=0 if quantization_seconds is not None else None,
        quantization_confidence=quantization_confidence,
    )
    return cache


class TestSingleTtl:
    """One TTL value is shared by the app cache, EnergyCache, and NBCReader."""

    def test_energy_cache_default_uses_the_shared_ttl(self) -> None:
        assert EnergyCache().ttl_seconds == DEFAULT_CACHE_TTL_SECS

    def test_nbc_reader_default_uses_the_shared_ttl(self) -> None:
        assert NBCReader().energy_cache.ttl_seconds == DEFAULT_CACHE_TTL_SECS

    def test_app_cache_uses_the_shared_ttl(self) -> None:
        assert app_mod._state.energy_cache.ttl_seconds == DEFAULT_CACHE_TTL_SECS


def _fixtures() -> list[tuple[str, list[float], datetime]]:
    """Return (name, samples, now) rows where sample count != wall clock."""
    early_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
    late_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
    mixed_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
    return [
        # 100 samples at 14:05: wall says 600 s left, samples say 800 s.
        ("early-qh", [0.001] * 100, early_start + timedelta(minutes=5)),
        # 800 samples at 14:14: wall says 60 s left, samples say 100 s.
        ("late-qh", [0.001] * 800, late_start + timedelta(minutes=14)),
        # Mixed rate: the 30 s window is 3.0 W, the raw sum is 160 Wh.
        (
            "mixed-rate",
            [0.001] * 70 + [0.003] * 30,
            mixed_start + timedelta(minutes=1),
        ),
    ]


class TestSinglePredictedWh:
    """Display, cache, and loader paths use one wall-clock ``predicted_wh``."""

    @pytest.mark.parametrize("name,samples,now", _fixtures())
    def test_display_path_matches_cache_path(
        self, name: str, samples: list[float], now: datetime
    ) -> None:
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        cache = _seeded_cache(samples, data_start, now)
        cache_result = cache.get_current_qh(now)
        assert cache_result is not None

        projection = _Projection(now, cache)
        display = projection._compute_nbc(  # pylint: disable=protected-access
            samples, 30, now=now
        )

        assert display.qh1 is not None, name
        assert display.qh1.predicted_wh == pytest.approx(
            cache_result["predicted_wh"], abs=0.01
        ), name
        assert display.qh1.remaining_seconds == cache_result["seconds_remaining"], name

    @pytest.mark.parametrize("name,samples,now", _fixtures())
    def test_loader_path_matches_cache_path(
        self, name: str, samples: list[float], now: datetime
    ) -> None:
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        cache = _seeded_cache(samples, data_start, now)
        cache_result = cache.get_current_qh(now)
        assert cache_result is not None

        projection = _Projection(now, cache)
        nbc = projection._compute_nbc(  # pylint: disable=protected-access
            samples, 30, now=now
        )
        metrics_data = {
            "_now": now,
            "_data_lag_secs": 0.0,
            "devices": [{"name": "main_panel", "nbc": nbc.to_dict()}],
        }

        reader = NBCReader(energy_cache=cache)
        parsed = reader._parse_metrics(  # pylint: disable=protected-access
            "main_panel", metrics_data
        )

        assert parsed is not None, name
        assert parsed.predicted_wh == pytest.approx(
            cache_result["predicted_wh"], abs=0.01
        ), name
        assert parsed.seconds_remaining == cache_result["seconds_remaining"], name

    def test_wall_clock_beats_sample_count(self) -> None:
        """The 100-sample/14:05 fixture differs from sample-count extrapolation."""
        now = datetime(2025, 6, 15, 14, 5, 0, tzinfo=timezone.utc)
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 100
        cache = _seeded_cache(samples, data_start, now)

        result = cache.get_current_qh(now)

        assert result is not None
        # 100 Wh raw + 600 s * 1.0 W = 700 Wh (sample count would give 900).
        assert result["predicted_wh"] == pytest.approx(700.0, abs=0.01)
        assert result["seconds_remaining"] == 600


class TestQhHelperEquivalence:
    """``qh_seconds_remaining`` is the one quarter-hour remainder definition."""

    @pytest.mark.parametrize(
        "instant",
        [
            # Midnight rollover.
            datetime(2025, 6, 15, 0, 0, 0),
            datetime(2025, 6, 15, 0, 14, 59),
            datetime(2025, 6, 15, 23, 45, 0),
            datetime(2025, 6, 15, 23, 59, 59),
            # Spring-forward DST (America/Los_Angeles skips 02:00-03:00).
            datetime(2026, 3, 8, 1, 59, 59, tzinfo=pytz.timezone("America/Los_Angeles")),
            datetime(2026, 3, 8, 3, 0, 0, tzinfo=pytz.timezone("America/Los_Angeles")),
            # Fall-back DST (01:00-02:00 repeats).
            datetime(2026, 11, 1, 1, 59, 59, tzinfo=pytz.timezone("America/Los_Angeles")),
            datetime(2026, 11, 1, 2, 0, 0, tzinfo=pytz.timezone("America/Los_Angeles")),
        ],
    )
    def test_matches_inline_reference(self, instant: datetime) -> None:
        expected = 900 - (instant.second + (instant.minute % 15) * 60)
        assert qh_seconds_remaining(instant) == expected

    @pytest.mark.parametrize(
        "instant",
        [
            datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2025, 6, 15, 0, 14, 59, tzinfo=timezone.utc),
            datetime(2025, 6, 15, 23, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 3, 8, 10, 7, 30, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 9, 52, 15, tzinfo=timezone.utc),
        ],
    )
    def test_parse_metrics_uses_the_helper(self, instant: datetime) -> None:
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 100
        cache = _seeded_cache(samples, data_start, instant)
        projection = _Projection(instant, cache)
        nbc = projection._compute_nbc(  # pylint: disable=protected-access
            samples, 30, now=instant
        )
        metrics_data = {
            "_now": instant,
            "devices": [{"name": "main_panel", "nbc": nbc.to_dict()}],
        }

        parsed = NBCReader(energy_cache=cache)._parse_metrics(  # pylint: disable=protected-access
            "main_panel", metrics_data
        )

        assert parsed is not None
        assert parsed.seconds_remaining == qh_seconds_remaining(instant)
