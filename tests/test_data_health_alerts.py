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
from unittest.mock import MagicMock

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
            )
        )
        mgr.enabled = True
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
