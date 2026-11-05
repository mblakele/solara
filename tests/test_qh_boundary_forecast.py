"""QH-boundary forecast regressions.

Two incidents share one boundary condition:

* 2026-10-14 (``bugs/2026-10-14-index-html-glitch.json``): all four NBC
  quarters are complete, so QH1.predicted_wh is None and the load manager
  reports ``no_incomplete_qh``. The index template must still render the
  forecast-period label (``waiting for data``) beside the placeholder.
* 2026-11-05 (``bugs/2026-11-05-qh-boundary-glitch.json``): the fetched
  window is the finished 16:15 quarter (899 samples) while the clock has
  moved to 16:30. The display path must not extrapolate the finished
  window with the new quarter's remainder. A near-full window is finalized
  from its measured sum into QH2; a short one leaves QH2 empty. Neither may
  move an older quarter into the finished slot, and injection must not
  duplicate the finalized quarter.
"""

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from flask import render_template

import app as app_mod
from app import app
from energy_cache import EnergyCache, EnergyCacheData
from load_models import CycleDiagnostics, CycleResult
from metrics import (
    DevicePrediction,
    HourlyProjection,
    MetricsBase,
    _PopulationResult,
)
from prediction import PredictionMixin
from util import CompletedNBCPeriod

from tests.test_app import mock_config, realistic_metrics


class _Projection(PredictionMixin):
    """Minimal prediction surface: ``_compute_nbc`` needs a clock and logger."""

    def __init__(self, now: datetime) -> None:
        self.instant = now
        self.energy_cache: EnergyCache | None = None
        self.logger = logging.getLogger("test_qh_boundary_forecast")


class TestQHBoundaryForecastLabel(unittest.TestCase):
    """Forecast period label is visible when QH1 is complete."""

    def setUp(self) -> None:
        self.app = app.test_client()

    def test_boundary_complete_qh_still_shows_waiting_label(self) -> None:
        """Complete QH1 (instant_minute>=60) shows its Wh as the headline."""
        mock_lm = MagicMock()
        mock_lm.enabled = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state = MagicMock()
        mock_lm.state.devices = {}
        mock_lm.state.pending_effects = []
        mock_lm.run_cycle.return_value = CycleResult(
            status="no_incomplete_qh",
            diagnostics=CycleDiagnostics(hysteresis_wh=0),
        )
        mock_lm.sentinel_names = []

        with mock_config(overrides={"MOCK": "True", "DEBUG": "True"}):
            from config import Config

            Config().set("LOAD_MANAGE_ENABLED", "True")
            app_mod._state.load_manager = mock_lm
            app_mod._state.last_cycle_result = mock_lm.run_cycle.return_value
            response = self.app.get(
                "/?instant_minute=65", headers={"Accept": "text/html"}
            )
        self.assertEqual(response.status_code, 200)
        data = response.data.decode("utf-8")
        self.assertIn("waiting for data", data)
        # The completed QH's Wh is surfaced as the headline number (instead
        # of a placeholder) — the most recent period's value is now visible
        # in the forecast card.
        self.assertNotIn("forecast__placeholder", data)
        self.assertIn("forecast__value--gen", data)


class TestStaleSliceForecast(unittest.TestCase):
    """Boundary regression: a finished trailing quarter is finalized, not extrapolated.

    At 16:30:25 UTC the fetched window is the *finished* 16:15 quarter (899
    samples). The unguarded display path applied the new 16:30 quarter's 875
    remaining seconds to it: ``remainingSeconds=875`` and
    ``predictedWh=50.18`` on a window that has already ended. A near-full
    finished window (at most ``FINALIZE_MAX_SHORTFALL_SECS`` short) must now
    become a measured QH2; a genuinely short one must leave QH2 empty.
    """

    INSTANT = datetime(2026, 11, 5, 16, 30, 25, tzinfo=timezone.utc)
    DATA_START = datetime(2026, 11, 5, 16, 15, 0, tzinfo=timezone.utc)
    SAMPLES = [0.001] * 899

    def test_stale_slice_reports_no_incomplete_qh(self) -> None:
        """Wall window of the trailing slice already ended → QH1 is None."""
        nbc = _Projection(self.INSTANT)._compute_nbc(  # pylint: disable=protected-access
            self.SAMPLES, 30, now=self.INSTANT, data_start=self.DATA_START
        )

        self.assertIsNone(nbc.qh1)

    def test_fresh_slice_still_extrapolates_with_wall_clock_remainder(self) -> None:
        """A fresh incomplete slice keeps the one-time-base behaviour."""
        instant = datetime(2026, 11, 5, 16, 15, 25, tzinfo=timezone.utc)
        data_start = datetime(2026, 11, 5, 16, 15, 0, tzinfo=timezone.utc)

        nbc = _Projection(instant)._compute_nbc(  # pylint: disable=protected-access
            [0.001] * 25, 30, now=instant, data_start=data_start
        )

        self.assertIsNotNone(nbc.qh1)
        self.assertFalse(nbc.qh1.complete)
        # Wall remainder: 900 - 25 = 875 seconds into the current quarter.
        self.assertEqual(nbc.qh1.remaining_seconds, 875)
        self.assertEqual(nbc.qh1.samples_used, 25)

    def test_stale_899_slice_finalizes_into_qh2(self) -> None:
        """The incident ledger: 16:15 finalizes, 16:00 moves to QH3."""
        nbc = _Projection(self.INSTANT)._compute_nbc(  # pylint: disable=protected-access
            self.SAMPLES, 30, now=self.INSTANT, data_start=self.DATA_START
        )

        self.assertIsNone(nbc.qh1)
        self.assertIsNotNone(nbc.qh2)
        self.assertTrue(nbc.qh2.complete)
        self.assertAlmostEqual(nbc.qh2.raw_wh, 899.0)
        self.assertEqual(nbc.qh2.samples_used, None)
        self.assertIsNone(nbc.qh3)
        self.assertIsNone(nbc.qh4)
        self.assertEqual(nbc.inject_before, self.DATA_START)
        self.assertEqual(nbc.backfill_from, "QH2")

    def test_stale_839_slice_leaves_qh2_empty(self) -> None:
        """A 61 s short window is unknown: QH2 stays empty, backfill is QH3."""
        instant = datetime(2026, 11, 5, 16, 30, 7, tzinfo=timezone.utc)
        nbc = _Projection(instant)._compute_nbc(  # pylint: disable=protected-access
            [0.001] * 839, 30, now=instant, data_start=self.DATA_START
        )

        self.assertIsNone(nbc.qh1)
        self.assertIsNone(nbc.qh2)
        self.assertIsNone(nbc.qh3)
        self.assertIsNone(nbc.qh4)
        self.assertEqual(nbc.inject_before, self.DATA_START)
        self.assertEqual(nbc.backfill_from, "QH3")

    def test_1799_slice_finalizes_1630_and_keeps_1615_in_qh3(self) -> None:
        """The trailing 16:30 chunk finalizes; the 16:15 prefix lands in QH3."""
        instant = datetime(2026, 11, 5, 16, 45, 5, tzinfo=timezone.utc)
        samples = [0.001] * 900 + [0.002] * 899

        nbc = _Projection(instant)._compute_nbc(  # pylint: disable=protected-access
            samples, 30, now=instant, data_start=self.DATA_START
        )

        self.assertIsNone(nbc.qh1)
        self.assertIsNotNone(nbc.qh2)
        self.assertAlmostEqual(nbc.qh2.raw_wh, 1798.0)
        self.assertIsNotNone(nbc.qh3)
        self.assertTrue(nbc.qh3.complete)
        self.assertAlmostEqual(nbc.qh3.raw_wh, 900.0)
        self.assertIsNone(nbc.qh4)

    def test_900_slice_at_complete_gap_is_not_mislabelled(self) -> None:
        """A whole missing 16:30 window shows as a gap, not as 16:15 in QH2."""
        instant = datetime(2026, 11, 5, 16, 45, 5, tzinfo=timezone.utc)

        nbc = _Projection(instant)._compute_nbc(  # pylint: disable=protected-access
            [0.001] * 900, 30, now=instant, data_start=self.DATA_START
        )

        self.assertIsNone(nbc.qh1)
        self.assertIsNone(nbc.qh2)
        self.assertIsNotNone(nbc.qh3)
        self.assertAlmostEqual(nbc.qh3.raw_wh, 900.0)
        self.assertEqual(nbc.inject_before, self.DATA_START)
        self.assertEqual(nbc.backfill_from, "QH3")

    def test_n900_at_boundary_matches_finalize(self) -> None:
        """A finished complete chunk shifts into QH3 instead of being QH2."""
        instant = datetime(2026, 11, 5, 16, 45, 5, tzinfo=timezone.utc)
        values = [0.001] * 900

        nbc = _Projection(instant)._compute_nbc(  # pylint: disable=protected-access
            values, 30, now=instant, data_start=self.DATA_START
        )

        self.assertIsNone(nbc.qh2)
        self.assertIsNotNone(nbc.qh3)
        self.assertAlmostEqual(nbc.qh3.raw_wh, 1000 * sum(values))

    def test_payload_finalizes_near_full_quarter_and_injects_older_periods(
        self,
    ) -> None:
        """The incident ledger through the production payload path."""
        result = _device_metrics(
            instant=self.INSTANT,
            data_start=self.DATA_START,
            samples=[-0.1367 / 899 / 1000] * 899,
            completed=[
                CompletedNBCPeriod(
                    start=datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc),
                    raw_wh=3.136,
                ),
                CompletedNBCPeriod(
                    start=datetime(2026, 11, 5, 15, 45, tzinfo=timezone.utc),
                    raw_wh=-1.469,
                ),
            ],
        )

        self.assertIsNone(result.nbc.qh1)
        self.assertIsNotNone(result.nbc.qh2)
        self.assertAlmostEqual(result.nbc.qh2.raw_wh, -0.1367)
        self.assertIsNotNone(result.nbc.qh3)
        self.assertAlmostEqual(result.nbc.qh3.raw_wh, 3.136)
        self.assertIsNotNone(result.nbc.qh4)
        self.assertAlmostEqual(result.nbc.qh4.raw_wh, -1.469)

    def test_short_payload_leaves_qh2_empty_and_starts_at_qh3(self) -> None:
        """The 839-sample case renders QH2 as unknown, QH3/QH4 from cache."""
        result = _device_metrics(
            instant=datetime(2026, 11, 5, 16, 30, 7, tzinfo=timezone.utc),
            data_start=self.DATA_START,
            samples=[0.001] * 839,
            completed=[
                CompletedNBCPeriod(
                    start=datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc),
                    raw_wh=3.136,
                ),
                CompletedNBCPeriod(
                    start=datetime(2026, 11, 5, 15, 45, tzinfo=timezone.utc),
                    raw_wh=-1.469,
                ),
            ],
        )

        self.assertIsNone(result.nbc.qh1)
        self.assertIsNone(result.nbc.qh2)
        self.assertIsNotNone(result.nbc.qh3)
        self.assertAlmostEqual(result.nbc.qh3.raw_wh, 3.136)
        self.assertIsNotNone(result.nbc.qh4)
        self.assertAlmostEqual(result.nbc.qh4.raw_wh, -1.469)

    def test_complete_899_plus_cached_period_does_not_duplicate(self) -> None:
        """A finalized 16:15 is not injected again from completed periods."""
        result = _device_metrics(
            instant=self.INSTANT,
            data_start=self.DATA_START,
            samples=[-0.1367 / 899 / 1000] * 899,
            completed=[
                CompletedNBCPeriod(
                    start=datetime(2026, 11, 5, 16, 15, tzinfo=timezone.utc),
                    raw_wh=999.0,
                ),
                CompletedNBCPeriod(
                    start=datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc),
                    raw_wh=3.136,
                ),
            ],
        )

        self.assertIsNotNone(result.nbc.qh2)
        self.assertAlmostEqual(result.nbc.qh2.raw_wh, -0.1367)
        self.assertIsNotNone(result.nbc.qh3)
        self.assertAlmostEqual(result.nbc.qh3.raw_wh, 3.136)

    def test_template_shows_finalized_quarter_once(self) -> None:
        """QH2 finalized: headline is its value; +3 appears only in Recent."""
        metrics = realistic_metrics()
        device = dict(metrics["devices"][0])
        nbc = dict(device["nbc"])
        nbc["QH1"] = None
        nbc["QH2"] = dict(
            nbc["QH2"] or {}, complete=True, raw_wh=-0.1367, wh=0.0
        )
        nbc["QH3"] = dict(
            nbc["QH3"] or {}, complete=True, raw_wh=3.136, wh=3.136
        )
        nbc["QH4"] = dict(
            nbc["QH4"] or {}, complete=True, raw_wh=-1.469, wh=0.0
        )
        device["nbc"] = nbc
        metrics = {"devices": [device]}
        load_management = {
            "last_cycle_result": {
                "status": "stale_data",
                "diagnostics": {"hysteresis_wh": 5.0, "gap_wh": None},
            },
            "target_wh": 0,
        }

        with app_mod.app.test_request_context():
            html = render_template(
                "_metrics.html",
                metrics=metrics,
                load_management=load_management,
                freshness=None,
            )

        # -0.1367 renders as 0, not -0.
        self.assertIn('0<span class="forecast__unit">Wh', html)
        self.assertNotIn("-0<span", html)
        self.assertEqual(html.count("+3"), 1)
        self.assertIn("-1", html)
        self.assertIn("<dd>—</dd>", html)
        self.assertIn("stale data", html)

    def test_template_shows_dash_when_finished_quarter_is_short(self) -> None:
        """A short finished quarter shows an em dash, not an older value."""
        metrics = realistic_metrics()
        device = dict(metrics["devices"][0])
        nbc = dict(device["nbc"])
        nbc["QH1"] = None
        nbc["QH2"] = None
        nbc["QH3"] = dict(
            nbc["QH3"] or {}, complete=True, raw_wh=3.136, wh=3.136
        )
        nbc["QH4"] = dict(
            nbc["QH4"] or {}, complete=True, raw_wh=-1.469, wh=0.0
        )
        device["nbc"] = nbc
        metrics = {"devices": [device]}
        load_management = {
            "last_cycle_result": {
                "status": "stale_data",
                "diagnostics": {"hysteresis_wh": 5.0, "gap_wh": None},
            },
            "target_wh": 0,
        }

        with app_mod.app.test_request_context():
            html = render_template(
                "_metrics.html",
                metrics=metrics,
                load_management=load_management,
                freshness=None,
            )

        self.assertIn("forecast__placeholder", html)
        self.assertEqual(html.count("+3"), 1)
        self.assertIn("-1", html)
        self.assertIn("<dd>—</dd>", html)
        self.assertIn("stale data", html)


def _device_metrics(
    instant: datetime,
    data_start: datetime,
    samples: list[float],
    completed: list[CompletedNBCPeriod],
):
    """Run the production payload path with a seeded cache."""
    cache = EnergyCache()
    cache._data = EnergyCacheData(  # pylint: disable=protected-access
        samples=[],
        data_start=data_start,
        last_sample_at=data_start,
        last_fetch_at=instant,
        sample_count=0,
        quantization_seconds=30,
        quantization_offset=0,
        quantization_confidence=1.0,
        completed_periods=completed,
    )
    with patch.object(MetricsBase, "vue_init"), patch.object(
        MetricsBase, "get_device_info"
    ):
        projection = HourlyProjection(
            instant=instant,
            logger_next=logging.getLogger("test_qh_boundary_forecast"),
            energy_cache=cache,
        )

    pop_result = _PopulationResult(
        per_second_data=list(samples),
        chart_data=[],
        nbc_seconds=list(samples),
        nbc_data_start=data_start,
        nbc_sample_count=len(samples),
    )
    pred_result = DevicePrediction(
        lag=timedelta(0),
        minute_predicted=0.0,
        prediction=0.0,
        prediction_min=0.0,
        prediction_max=0.0,
        seconds_remaining=0.0,
    )
    vdi = MagicMock()
    vdi.device_gid = 353600
    vdi.device_name = "M1208.24"
    vdi.time_zone = None

    return projection._compute_device_metrics(  # pylint: disable=protected-access
        vdi, pop_result, pred_result
    )
