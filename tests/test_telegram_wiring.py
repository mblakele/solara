"""Tests for TelegramSender wiring into LoadManager via app.py.

Verifies that:
  - app._get_load_manager() passes a telegram_sender to LoadManagerConfig
  - load_manager.py logs when telegram sender is configured or not
  - LoadManager._fire_telegram_notification respects the sender status
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clock import FakeClock
from load_manager import LoadManager, LoadManagerConfig


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def mock_config_env(monkeypatch):
    """Set Telegram env vars so TelegramSender.from_config() returns a sender."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")


# =============================================================================
# 1. app.py wiring: telegram_sender is passed to LoadManagerConfig
# =============================================================================


class TestAppTelegramWiring:

    def test_load_manager_config_receives_telegram_sender(self, mock_config_env):
        """_get_load_manager() should pass telegram_sender into LoadManagerConfig."""
        import app as app_mod

        # Reset the singleton so _get_load_manager runs fresh init logic.
        app_mod._state.load_manager = None
        app_mod._state.load_manager_init_failed = False

        created_config = None

        def capture_config(config):
            nonlocal created_config
            created_config = config
            # Return a minimal LoadManager that won't crash.
            mock_lm = MagicMock()
            mock_lm.enabled = False
            mock_lm.dry_run = True
            mock_lm.target_wh = -500
            mock_lm.nbc_device = "test"
            mock_lm.state.devices = {}
            mock_lm.plugs = {}
            mock_lm.sentinel_names = frozenset()
            mock_lm.config_interval_secs = 30
            mock_lm.tesla_ctrl = None
            mock_lm.tesla_config = None
            return mock_lm

        with patch(
            "load_manager.LoadManager",
            side_effect=capture_config,
            autospec=False,
        ):
            # Also need to suppress the background thread start.
            with patch.object(app_mod, "_load_management_loop"):
                with patch.object(app_mod._state, "lm_thread_started", False):
                    lm = app_mod._get_load_manager()

        assert lm is not None
        assert created_config is not None
        assert created_config.telegram_sender is not None
        assert created_config.telegram_sender.is_configured is True

    def test_load_manager_config_telegram_sender_is_none_when_not_configured(
        self,
    ):
        """When no Telegram env vars or devices.json, telegram_sender should be None."""
        import app as app_mod

        app_mod._state.load_manager = None
        app_mod._state.load_manager_init_failed = False

        created_config = None

        def capture_config(config):
            nonlocal created_config
            created_config = config
            mock_lm = MagicMock()
            mock_lm.enabled = False
            mock_lm.dry_run = True
            mock_lm.target_wh = -500
            mock_lm.nbc_device = "test"
            mock_lm.state.devices = {}
            mock_lm.plugs = {}
            mock_lm.sentinel_names = frozenset()
            mock_lm.config_interval_secs = 30
            mock_lm.tesla_ctrl = None
            mock_lm.tesla_config = None
            return mock_lm

        with patch(
            "load_manager.LoadManager",
            side_effect=capture_config,
            autospec=False,
        ):
            with patch.object(app_mod, "_load_management_loop"):
                with patch.object(app_mod._state, "lm_thread_started", False):
                    lm = app_mod._get_load_manager()

        assert lm is not None
        assert created_config is not None
        # Should be None since no Telegram credentials are available.
        assert created_config.telegram_sender is None


# =============================================================================
# 2. load_manager.py logging: sender status is reported at init
# =============================================================================


class TestLoadManagerTelegramLogging:

    def test_logger_reports_configured_sender(self, mock_config_env, caplog):
        """LoadManager.__init__ logs when telegram sender is configured."""

        with caplog.at_level(logging.INFO):
            mgr = LoadManager(
                LoadManagerConfig(
                    telegram_sender=MagicMock(is_configured=True),
                ),
            )

        assert mgr.telegram_sender is not None
        # The log should mention telegram sender status at INFO level.
        telegram_logs = [r for r in caplog.records if "telegram" in r.name.lower() or "telegram" in r.message.lower()]
        assert any("configured" in r.message.lower() for r in telegram_logs), (
            "Expected log message about telegram sender being configured, "
            f"got: {[r.message for r in telegram_logs]}"
        )

    def test_logger_reports_none_sender(self, caplog):
        """LoadManager.__init__ logs when no telegram sender is provided."""

        with caplog.at_level(logging.INFO):
            mgr = LoadManager(
                LoadManagerConfig(
                    telegram_sender=None,
                ),
            )

        assert mgr.telegram_sender is None
        telegram_logs = [r for r in caplog.records if "telegram" in r.name.lower() or "telegram" in r.message.lower()]
        assert any("not configured" in r.message.lower() for r in telegram_logs), (
            "Expected log message about telegram sender not being configured, "
            f"got: {[r.message for r in telegram_logs]}"
        )


# =============================================================================
# 4. LoadManager._fire_auth_error_notification
# =============================================================================


class TestFireAuthErrorNotification:

    @pytest.mark.asyncio
    async def test_noop_when_sender_none(self):
        """When telegram_sender is None, returns False."""

        mgr = LoadManager(LoadManagerConfig(telegram_sender=None))
        result = await mgr._fire_auth_error_notification("auth error")
        assert result is False

    @pytest.mark.asyncio
    async def test_noop_when_not_configured(self):
        """When sender exists but is_configured is False, returns False."""
        mock_sender = MagicMock()
        mock_sender.is_configured = False


        mgr = LoadManager(LoadManagerConfig(telegram_sender=mock_sender))
        result = await mgr._fire_auth_error_notification("auth error")
        assert result is False

    @pytest.mark.asyncio
    async def test_noop_when_alert_disabled(self):
        """When alert_on_auth_error is False, returns False."""
        mock_sender = MagicMock()
        mock_sender.is_configured = True


        mgr = LoadManager(
            LoadManagerConfig(telegram_sender=mock_sender),
        )
        mgr._telegram_alert_on_auth_error = False
        result = await mgr._fire_auth_error_notification("auth error")
        assert result is False

    @pytest.mark.asyncio
    async def test_sends_when_alert_enabled(self):
        """When alert_on_auth_error is True, sends notification."""
        mock_sender = MagicMock()
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=True)


        mgr = LoadManager(
            LoadManagerConfig(telegram_sender=mock_sender),
        )
        result = await mgr._fire_auth_error_notification("login_required")
        assert result is True
        mock_sender.send_notification.assert_awaited_once()
        # Verify the notification contains the error text
        call_args = mock_sender.send_notification.call_args[0][0]
        assert "login_required" in call_args.description


# =============================================================================
# 5. LoadManager drift alerts (_queue_drift_error_notification / _drain_drift_alerts)
# =============================================================================


class TestDriftErrorAlerts:

    @staticmethod
    def _make_alert():

        from metrics import DriftAlert

        now = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        return DriftAlert(
            channel_num=5,
            chart_start=now,
            data_start=now.replace(minute=16),
            count=5,
        )

    def test_queue_drift_notification_skipped_when_sender_none(self):
        """With no TelegramSender, drift alerts are not queued."""

        mgr = LoadManager(
            LoadManagerConfig(telegram_sender=None, dry_run=True, config_interval_secs=30)
        )
        mgr._queue_drift_error_notification(self._make_alert())
        assert mgr._pending_notifications == []

    def test_queue_drift_notification_skipped_when_not_configured(self):
        """With an unconfigured sender, drift alerts are not queued."""

        mock_sender = MagicMock(is_configured=False)
        mgr = LoadManager(
            LoadManagerConfig(telegram_sender=mock_sender, dry_run=True, config_interval_secs=30)
        )
        mgr._queue_drift_error_notification(self._make_alert())
        assert mgr._pending_notifications == []

    def test_queue_drift_notification_appends_when_configured(self):
        """With a configured sender, drift alerts queue an error event."""


        mock_sender = MagicMock(is_configured=True)
        clock = FakeClock(datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc))
        mgr = LoadManager(
            LoadManagerConfig(
                telegram_sender=mock_sender,
                clock=clock,
                dry_run=True,
                config_interval_secs=30,
            )
        )
        mgr._queue_drift_error_notification(self._make_alert())
        assert len(mgr._pending_notifications) == 1
        event = mgr._pending_notifications[0]
        assert event.event_type == "error"
        assert "Emporia VUE drift" in event.description
        assert "channel 5" in event.description

    def test_drain_drift_alerts_queues_each_alert(self):
        """_drain_drift_alerts forwards drained alerts to the queue method."""

        mgr = LoadManager(
            LoadManagerConfig(telegram_sender=None, dry_run=True, config_interval_secs=30)
        )
        alert = self._make_alert()
        with patch("load_manager.drain_drift_alerts", return_value=[alert]):
            with patch.object(mgr, "_queue_drift_error_notification") as mock_queue:
                mgr._drain_drift_alerts()
        mock_queue.assert_called_once_with(alert)

    def test_run_cycle_drains_drift_alerts_after_nbc_fetch(self, mock_config_env):
        """run_cycle calls _drain_drift_alerts right after the NBC fetch stage."""
        from config import Config
        from load_models import CycleDiagnostics, CycleResult

        Config().set("LOAD_MANAGE_ENABLED", "True")
        mgr = LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))
        early = CycleResult(
            status="no_incomplete_qh",
            diagnostics=CycleDiagnostics(reason="no_incomplete_qh"),
            sleep_hint=30,
            sleep_hint_at="2025-06-15T14:00:00+00:00",
        )
        with patch.object(mgr, "_stage_nbc_fetch", return_value=early):
            with patch.object(mgr, "_drain_drift_alerts") as mock_drain:
                result = mgr.run_cycle(force=True)
        mock_drain.assert_called_once()
        # run_cycle finalizes results (attaches cycle_id + timings), so the
        # returned object is an enriched copy of the stage result rather
        # than the identical instance.
        assert result.status == early.status
        assert result.cycle_id
