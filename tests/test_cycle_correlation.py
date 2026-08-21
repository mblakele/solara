"""Cycle correlation IDs + surfaced diagnostics (plan subtasks 1.3 + 1.4).

Red-phase tests:

- 1.3: every cycle carries a ``cycle_id`` (boot-uuid prefix + monotonic
  counter) through CycleContext, CycleResult, boundary-log extras,
  recent_cycles entries, and serialized payloads. LM-layer scope only:
  EnergyCache/fetch-layer logs stay untagged by design.
- 1.4: CycleResult carries stage ``timings``; /api/v1/load/status surfaces
  health counters (consecutive_error_count, last_error_type, SSE subscriber
  count, cache freshness, MQTT freshness); Emporia channel timing switches
  from wall clock to a monotonic seam.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from load_manager import LoadManager, LoadManagerConfig
from load_models import CycleContext, CycleResult


@pytest.fixture
def lm() -> LoadManager:
    """Default LoadManager with minimal config, no real controllers."""
    return LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))


def _now() -> datetime:
    return datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestCycleIdModels:
    """cycle_id fields on the shared models."""

    def test_cycle_context_defaults_to_empty_cycle_id(self):
        """CycleContext.cycle_id defaults to empty string."""
        ctx = CycleContext(now=_now())
        assert ctx.cycle_id == ""

    def test_cycle_result_to_dict_includes_cycle_id_and_timings(self):
        """CycleResult.to_dict() serializes cycle_id and timings."""
        result = CycleResult(
            status="ok",
            cycle_id="c1-abcd",
            timings={"enabled_check": 0.001, "nbc_fetch": 0.5},
        )
        data = result.to_dict()
        assert data["cycle_id"] == "c1-abcd"
        assert data["timings"] == {"enabled_check": 0.001, "nbc_fetch": 0.5}


class TestRunCycleCorrelation:
    """run_cycle attaches cycle_id + timings to every returned result."""

    def test_early_exit_result_carries_cycle_id_and_timings(self, lm, caplog):
        """Early-exit results carry the id and the stages timed so far."""
        early = CycleResult(status="stale")
        with (
            patch.object(lm, "_stage_enabled_check", return_value=None),
            patch.object(lm, "_stage_nbc_fetch", return_value=early),
        ):
            with caplog.at_level(logging.INFO, logger="load_manager"):
                result = lm.run_cycle()

        assert result.status == "stale"
        assert result.cycle_id and re.fullmatch(r"c\d+-[0-9a-f]{4}", result.cycle_id), (
            f"cycle_id {result.cycle_id!r} must match c<counter>-<boot4>"
        )
        assert result.timings is not None
        assert set(result.timings) >= {"enabled_check", "nbc_fetch"}

        early_logs = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "cycle_early_exit"
        ]
        assert early_logs, "early exit must be logged"
        assert getattr(early_logs[0], "cycle_id", None) == result.cycle_id, (
            "boundary log extras must carry the same cycle_id as the result"
        )

    def test_cycle_ids_differ_between_cycles(self, lm):
        """Each run_cycle invocation gets a fresh id."""
        early = CycleResult(status="stale")
        with (
            patch.object(lm, "_stage_enabled_check", return_value=None),
            patch.object(lm, "_stage_nbc_fetch", return_value=early),
        ):
            first = lm.run_cycle()
            second = lm.run_cycle()

        assert first.cycle_id != second.cycle_id

    def test_final_result_carries_full_timings(self, lm):
        """The ok-path result includes every stage through build_result."""
        final = CycleResult(status="ok")
        with (
            patch.object(lm, "_stage_enabled_check", return_value=None),
            patch.object(lm, "_stage_nbc_fetch", return_value=None),
            patch.object(lm, "_stage_pending_check", return_value=None),
            patch.object(lm, "_stage_compute_gap", return_value=None),
            patch.object(lm, "_stage_async_phase", return_value=None),
            patch.object(lm, "_stage_commit", return_value=None),
            patch.object(lm, "_stage_build_result", return_value=final),
        ):
            result = lm.run_cycle()

        assert result.timings is not None
        assert set(result.timings) >= {
            "enabled_check",
            "nbc_fetch",
            "pending_check",
            "compute_gap",
            "async_phase",
            "commit",
            "build_result",
        }
        assert result.cycle_id


class TestRecentCyclesCarryCycleId:
    """recent_cycles entries expose the cycle_id for alert correlation."""

    def test_recent_cycles_include_cycle_id(self):
        import app as app_mod

        mock_lm = MagicMock()
        mock_lm.run_cycle.return_value = CycleResult(
            status="ok", sleep_hint=30.0, cycle_id="c7-beef"
        )
        mock_lm._send_pending_notifications_sync = MagicMock()

        app_mod._state.telegram_sender = None
        try:
            with patch("app._get_load_manager", return_value=mock_lm):
                with patch(
                    "app.time.sleep", side_effect=InterruptedError("stop")
                ):
                    with pytest.raises(InterruptedError):
                        app_mod._load_management_loop()

            latest = app_mod._state.recent_cycles[-1]
            assert latest["cycle_id"] == "c7-beef"
        finally:
            app_mod._state.consecutive_error_count = 0
            app_mod._state.last_error_type = None


class TestLoadStatusHealthCounters:
    """/api/v1/load/status surfaces counters computed but never served."""

    def test_payload_includes_health_counters(self):
        import app as app_mod

        client = app_mod.app.test_client()
        client.testing = True

        mock_lm = MagicMock()
        mock_lm.enabled = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_state = MagicMock()
        mock_state.devices = {}
        mock_state.pending_effects = []
        mock_lm.state = mock_state

        app_mod._state.consecutive_error_count = 3
        app_mod._state.last_error_type = "RuntimeError"
        try:
            with patch("app._get_load_manager", return_value=mock_lm):
                response = client.get("/api/v1/load/status")

            assert response.status_code == 200
            data = response.get_json()
            assert data["consecutiveErrorCount"] == 3
            assert data["lastErrorType"] == "RuntimeError"
            assert isinstance(data["sseSubscriberCount"], int)
            assert "cache" in data
            assert "mqtt" in data
        finally:
            app_mod._state.consecutive_error_count = 0
            app_mod._state.last_error_type = None


class TestMonotonicFetchTiming:
    """Emporia channel timing must use a monotonic seam, not wall clock."""

    def test_fetch_channel_data_uses_monotonic_clock(self, monkeypatch):
        """api_response records monotonic elapsed seconds for the API call."""
        import metrics

        hp = metrics.HourlyProjection.__new__(metrics.HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()
        hp.metrics = {"api_response": {}}

        chart_start = now.replace(minute=0)
        hp.vue.get_chart_usage.return_value = ([0.1] * 60, chart_start)

        clock_values = iter([100.0, 100.25])
        monkeypatch.setattr(
            metrics,
            "_monotonic",
            lambda: next(clock_values),
            raising=False,
        )

        chan_mock = MagicMock()
        chan_mock.channel_num = 7

        usage, data_start, channel_num = hp._fetch_channel_data(
            chan_mock, chart_start, now
        )
        assert usage == [0.1] * 60
        assert channel_num == 7

        recorded = hp.metrics["api_response"]["get_chart_usage/7"]
        assert isinstance(recorded, float), (
            "fetch timing must be monotonic seconds (float), not a "
            "wall-clock timedelta vulnerable to NTP steps"
        )
        assert recorded == pytest.approx(0.25)

    def test_monotonic_not_wall_clock_drives_duration(self, monkeypatch):
        """Advancing the wall clock must NOT change recorded durations."""
        import metrics
        from clock import FakeClock

        hp = metrics.HourlyProjection.__new__(metrics.HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()
        hp.metrics = {"api_response": {}}

        chart_start = now.replace(minute=0)
        fake_clock = FakeClock(now)
        old_clock = metrics._CLOCK
        metrics.set_clock(fake_clock)
        monkeypatch.setattr(metrics, "_monotonic", lambda: 55.0, raising=False)

        def _slow_get_chart_usage(_channel, _start, _end, **_kwargs):
            fake_clock.advance(120)  # wall-clock jump must be ignored
            return ([0.1] * 60, chart_start)

        hp.vue.get_chart_usage.side_effect = _slow_get_chart_usage

        chan_mock = MagicMock()
        chan_mock.channel_num = 9

        hp._fetch_channel_data(chan_mock, chart_start, now)
        recorded = hp.metrics["api_response"]["get_chart_usage/9"]
        assert recorded == pytest.approx(0.0), (
            "wall-clock advancement (NTP step) must not inflate the "
            "recorded fetch duration"
        )
        del old_clock
