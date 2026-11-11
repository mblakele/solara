"""Red-phase tests for metrics fatal-error + stale-data telegram alerts.

Covers the 2026-09-16 production outage: VueAuthenticationError was
swallowed by EnergyCache (stale-serve) so run_cycle only saw stale data
and no telegram alert fired.

Scope (confirmed):
1. Fatal fetch errors (VueAuthenticationError / non-retryable) alert,
   at most one per QH.
2. Data age >300s (stale data_point_at, or empty cache since boot)
   alerts, at most one per QH.
Both bypass the telegram.devices whitelist and fire in dry-run. Fire-only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from clock import FakeClock
from load_manager import LoadManager, LoadManagerConfig


def _mgr(now: datetime, **kw) -> LoadManager:
    sender = MagicMock(is_configured=True)
    clock = FakeClock(now)
    return LoadManager(
        LoadManagerConfig(
            telegram_sender=sender,
            clock=clock,
            dry_run=True,
            config_interval_secs=30,
            **kw,
        )
    )


def _cycle_mgr(now: datetime) -> tuple[LoadManager, FakeClock]:
    """Manager wirable for run_cycle, with a controllable meter cache."""
    from energy_cache import EnergyCache

    sender = MagicMock(is_configured=True)
    clock = FakeClock(now)
    cache = EnergyCache(ttl_seconds=30, clock=clock)
    mgr = LoadManager(
        LoadManagerConfig(
            telegram_sender=sender, clock=clock, dry_run=True,
            config_interval_secs=30, energy_cache=cache, enabled=True,
        )
    )
    mgr._telegram_devices = None
    return mgr, clock


def _seed_cache(mgr: LoadManager, last_sample_at: datetime,
                now: datetime) -> None:
    """Seed the shared meter cache with one sample at *last_sample_at*."""
    from energy_cache import EnergyCacheData

    mgr.nbc_reader.energy_cache._data = EnergyCacheData(  # pylint: disable=protected-access
        samples=[-0.04],
        data_start=last_sample_at,
        last_sample_at=last_sample_at,
        last_fetch_at=now,
        sample_count=1,
        quantization_seconds=None,
        quantization_offset=None,
        quantization_confidence=None,
    )


class TestFetchFatalRecordsError:
    """EnergyCache must expose the last fetch failure for alerting."""
    def test_energy_cache_records_fatal_fetch_error(self):
        """EnergyCache must surface the last fetch exception for alerting."""
        from energy_cache import EnergyCache

        cache = EnergyCache(ttl_seconds=30)
        from metrics import VueAuthenticationError

        def _boom():
            raise VueAuthenticationError("Vue authentication failed")

        now = datetime(2026, 9, 16, 17, 12, tzinfo=timezone.utc)
        result, _ = cache.get_or_fetch(_boom, now, force=True)
        assert result is None
        assert isinstance(cache.last_fetch_error, VueAuthenticationError)

    def test_transient_retryable_is_not_fatal(self):
        """RetryableMetricsException must NOT trigger the fatal alert path."""
        from energy_cache import EnergyCache
        from metrics import RetryableMetricsException

        cache = EnergyCache(ttl_seconds=30)

        def _transient():
            raise RetryableMetricsException("No data for hour")

        now = datetime(2026, 9, 16, 17, 12, tzinfo=timezone.utc)
        cache.get_or_fetch(_transient, now, force=True)
        mgr = _mgr(now, energy_cache=cache)
        assert mgr._is_fatal_fetch_error(cache.last_fetch_error) is False


class TestFatalFetchAlert:
    """Fatal fetch errors queue one telegram alert per QH."""
    def test_fatal_fetch_queues_alert_once_per_qh(self):
        """VueAuthenticationError queues one error event per QH."""
        from metrics import VueAuthenticationError

        now = datetime(2026, 9, 16, 17, 12, tzinfo=timezone.utc)
        mgr = _mgr(now)
        # Bypass whitelist + dry-run: must still queue.
        mgr._telegram_devices = None
        assert mgr.dry_run is True

        err = VueAuthenticationError("Vue authentication failed")
        mgr._check_fetch_fatal_alert(err, now)
        assert len(mgr._pending_notifications) == 1

        # Same QH: no second alert.
        mgr._check_fetch_fatal_alert(err, now + timedelta(seconds=30))
        assert len(mgr._pending_notifications) == 1

        # Next QH: re-fire.
        mgr._check_fetch_fatal_alert(
            err, now + timedelta(seconds=900)
        )
        assert len(mgr._pending_notifications) == 2


class TestStaleDataAlert:
    """Data age over 300s queues one telegram alert per QH."""
    def test_stale_data_point_queues_alert_once_per_qh(self):
        """data_point_at older than 300s queues one alert per QH."""
        now = datetime(2026, 9, 16, 17, 12, tzinfo=timezone.utc)
        mgr = _mgr(now)
        mgr._telegram_devices = None
        old = now - timedelta(seconds=400)
        mgr._check_stale_data_alert(
            data_point_at=old, now=now, reason="stale_data"
        )
        assert len(mgr._pending_notifications) == 1
        mgr._check_stale_data_alert(
            data_point_at=old, now=now + timedelta(seconds=30),
            reason="stale_data",
        )
        assert len(mgr._pending_notifications) == 1
        mgr._check_stale_data_alert(
            data_point_at=old,
            now=now + timedelta(seconds=900),
            reason="stale_data",
        )
        assert len(mgr._pending_notifications) == 2

    def test_fresh_data_does_not_alert(self):
        """Recent data_point_at must not alert."""
        now = datetime(2026, 9, 16, 17, 12, tzinfo=timezone.utc)
        mgr = _mgr(now)
        mgr._check_stale_data_alert(
            data_point_at=now - timedelta(seconds=60),
            now=now, reason="stale_data",
        )
        assert not mgr._pending_notifications

    def test_empty_cache_since_boot_alerts_after_300s(self):
        """No data ever: age measured from boot, alerts after 300s."""
        boot = datetime(2026, 9, 16, 17, 0, tzinfo=timezone.utc)
        mgr = _mgr(boot)
        now = boot + timedelta(seconds=400)
        mgr._check_stale_data_alert(
            data_point_at=None, now=now, reason="no_data"
        )
        assert len(mgr._pending_notifications) == 1


class TestRunCycleDataHealth:
    """run_cycle wires the data-health checks into the NBC-fetch stage."""

    def test_run_cycle_queues_fatal_and_stale_on_fetch_miss(self):
        """A fetch miss with a stored fatal error alerts on the early exit."""
        from unittest.mock import patch

        from energy_cache import EnergyCache
        from metrics import VueAuthenticationError

        boot = datetime(2026, 9, 16, 17, 0, tzinfo=timezone.utc)
        clock = FakeClock(boot)
        sender = MagicMock(is_configured=True)
        cache = EnergyCache(ttl_seconds=30, clock=clock)
        cache._last_fetch_error = VueAuthenticationError("Vue auth failed")
        cache._last_fetch_error_at = boot
        mgr = LoadManager(
            LoadManagerConfig(
                telegram_sender=sender, clock=clock, dry_run=True,
                config_interval_secs=30, energy_cache=cache,
                enabled=True,
            )
        )
        mgr._telegram_devices = None
        clock.advance(400)
        with patch.object(
            mgr.nbc_reader, "get_current_qh", return_value=None
        ):
            result = mgr.run_cycle()
        assert result.status == "no_incomplete_qh"
        kinds = [e.description for e in mgr._pending_notifications]
        assert any("fetch failure" in d for d in kinds)
        assert any("unavailable" in d for d in kinds)
        # Second cycle, same QH: no duplicates.
        with patch.object(
            mgr.nbc_reader, "get_current_qh", return_value=None
        ):
            mgr.run_cycle()
        assert len(mgr._pending_notifications) == 2


class TestStaleSplit:
    """Split meter-staleness from pipeline-stall (bugs/2026-10-13 log).

    The 16:00:12 alert reported "last data point 369s old" while the
    meter was ~11s old. Meter health and prediction health must alert
    separately with honest text.

    The 2026-10-13 log has no pipeline page: the split post-dates it, so
    the prediction ages seeded here are synthetic. What these tests pin
    is the routing — a fresh meter plus an old *usable prediction* pages
    the pipeline, never the meter.
    """

    def _incident_mgr(self, now: datetime) -> LoadManager:
        from energy_cache import EnergyCache

        sender = MagicMock(is_configured=True)
        clock = FakeClock(now)
        cache = EnergyCache(ttl_seconds=30, clock=clock)
        mgr = LoadManager(
            LoadManagerConfig(
                telegram_sender=sender, clock=clock, dry_run=True,
                config_interval_secs=30, energy_cache=cache,
            )
        )
        mgr._telegram_devices = None
        return mgr

    def test_qh_boundary_fresh_meter_pages_pipeline_not_meter(self):
        """Fresh meter + stalled pipeline: one pipeline alert, no meter alert."""
        from load_models import CycleContext

        now = datetime(2026, 10, 13, 16, 0, 9, tzinfo=timezone.utc)
        mgr = self._incident_mgr(now)
        # No usable prediction since 15:54:01Z (368s before the check).
        mgr._last_usable_prediction_at = datetime(
            2026, 10, 13, 15, 54, 1, tzinfo=timezone.utc
        )
        # Meter just fetched OK: last sample 16:00:01Z (~8s old).
        _seed_cache(
            mgr, datetime(2026, 10, 13, 16, 0, 1, tzinfo=timezone.utc), now
        )
        ctx = CycleContext(now=now, data_point_at=None, now_postfetch=None)
        mgr._check_data_health_alerts(ctx)  # pylint: disable=protected-access
        descs = [e.description for e in mgr._pending_notifications]
        assert len(descs) == 1
        assert "prediction stalled" in descs[0]
        assert "meter data" in descs[0]
        assert "last meter data" not in descs[0]

    def test_true_outage_pages_meter_without_pipeline_duplicate(self):
        """Stale meter: exactly one meter alert, no pipeline duplicate."""
        from load_models import CycleContext

        now = datetime(2026, 10, 13, 16, 0, 9, tzinfo=timezone.utc)
        mgr = self._incident_mgr(now)
        mgr._last_usable_prediction_at = datetime(
            2026, 10, 13, 15, 54, 1, tzinfo=timezone.utc
        )
        _seed_cache(
            mgr, now - timedelta(seconds=400), now - timedelta(seconds=400)
        )
        ctx = CycleContext(now=now, data_point_at=None, now_postfetch=None)
        mgr._check_data_health_alerts(ctx)  # pylint: disable=protected-access
        descs = [e.description for e in mgr._pending_notifications]
        assert len(descs) == 1
        assert "last meter data" in descs[0]
        assert "prediction stalled" not in descs[0]

    def test_fresh_meter_and_pipeline_stays_quiet(self):
        """Fresh meter + recent usable prediction: no alert at all."""
        from load_models import CycleContext

        now = datetime(2026, 10, 13, 16, 0, 9, tzinfo=timezone.utc)
        mgr = self._incident_mgr(now)
        mgr._last_usable_prediction_at = now - timedelta(seconds=30)
        _seed_cache(mgr, now - timedelta(seconds=8), now)
        ctx = CycleContext(now=now, data_point_at=None, now_postfetch=None)
        mgr._check_data_health_alerts(ctx)  # pylint: disable=protected-access
        assert not mgr._pending_notifications


class TestPipelineUsablePrediction:
    """The pipeline page measures the last usable prediction, not a decision.

    The 2026-11-10 20:30:37 page fired at 304s on ``last_data_point_at``
    while a prediction had been produced 64s earlier and the loop was
    only waiting on its own pending effects.
    """

    def test_stage_nbc_fetch_records_usable_prediction(self):
        """A cycle past the sample gate records the post-fetch time."""
        from load_nbc import NBCFetchResult

        now = datetime(2026, 11, 10, 20, 29, 30, tzinfo=timezone.utc)
        mgr, _ = _cycle_mgr(now)
        _seed_cache(mgr, now - timedelta(seconds=30), now)
        fetched = NBCFetchResult(
            qh_name="QH1", predicted_wh=0.35, seconds_remaining=30,
            data_point_at=now - timedelta(seconds=28), samples_used=30,
        )
        with patch.object(
            mgr.nbc_reader, "get_current_qh", return_value=fetched
        ):
            mgr.run_cycle()
        tracked = mgr._last_usable_prediction_at
        assert tracked is not None
        assert abs((tracked - now).total_seconds()) < 1.0

    def test_incident_20261110_does_not_page(self):
        """304s on the old anchor, 64s on the usable-prediction anchor."""
        from load_models import CycleContext

        now = datetime(2026, 11, 10, 20, 30, 37, tzinfo=timezone.utc)
        mgr, _ = _cycle_mgr(now)
        _seed_cache(
            mgr, datetime(2026, 11, 10, 20, 30, 3, tzinfo=timezone.utc), now
        )
        # The old measure: last compute_gap at 20:25:33 (304s).
        mgr.state.last_data_point_at = datetime(
            2026, 11, 10, 20, 25, 33, tzinfo=timezone.utc
        )
        # The new measure: prediction produced at 20:29:33 (64s).
        mgr._last_usable_prediction_at = datetime(
            2026, 11, 10, 20, 29, 33, tzinfo=timezone.utc
        )
        ctx = CycleContext(now=now, data_point_at=None, now_postfetch=None)
        mgr._check_data_health_alerts(ctx)  # pylint: disable=protected-access
        assert not mgr._pending_notifications

    def test_previous_qh_prediction_counts_as_usable(self):
        """The 2026-10-17 shape: prediction produced 156s ago, no page."""
        from load_models import CycleContext

        now = datetime(2026, 10, 17, 15, 32, 47, tzinfo=timezone.utc)
        mgr, _ = _cycle_mgr(now)
        _seed_cache(
            mgr, datetime(2026, 10, 17, 15, 30, 5, tzinfo=timezone.utc), now
        )
        mgr.state.last_data_point_at = now - timedelta(seconds=306)
        mgr._last_usable_prediction_at = now - timedelta(seconds=156)
        ctx = CycleContext(now=now, data_point_at=None, now_postfetch=None)
        mgr._check_data_health_alerts(ctx)  # pylint: disable=protected-access
        assert not mgr._pending_notifications

    def test_real_stall_pages_with_stage2_reason(self):
        """No prediction for 400s pages once, with the real stage-2 reason."""
        from load_nbc import NBCFetchResult

        now = datetime(2026, 11, 10, 21, 0, 9, tzinfo=timezone.utc)
        mgr, clock = _cycle_mgr(now)
        _seed_cache(mgr, now - timedelta(seconds=30), now)
        mgr.state.last_data_point_at = now - timedelta(seconds=400)
        mgr._last_usable_prediction_at = now - timedelta(seconds=400)
        starved = NBCFetchResult(
            qh_name="QH1", predicted_wh=0.0, seconds_remaining=30,
            data_point_at=now, samples_used=1,
        )
        with patch.object(
            mgr.nbc_reader, "get_current_qh", return_value=starved
        ):
            result = mgr.run_cycle()
        assert result.status == "no_incomplete_qh"
        descs = [e.description for e in mgr._pending_notifications]
        assert len(descs) == 1
        assert "last usable prediction 400s old" in descs[0]
        assert "reason=insufficient_samples" in descs[0]
        assert "decision point" not in descs[0]
        # Same QH: the per-QH throttle still holds.
        clock.advance(30)
        _seed_cache(mgr, clock.now() - timedelta(seconds=30), clock.now())
        with patch.object(
            mgr.nbc_reader, "get_current_qh", return_value=starved
        ):
            mgr.run_cycle()
        assert len(mgr._pending_notifications) == 1
