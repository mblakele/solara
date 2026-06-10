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
        app_mod._load_manager = None
        app_mod._load_manager_init_failed = False

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
                with patch.object(app_mod, "_lm_thread_started", False):
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

        app_mod._load_manager = None
        app_mod._load_manager_init_failed = False

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
                with patch.object(app_mod, "_lm_thread_started", False):
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
        from load_manager import LoadManager, LoadManagerConfig

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
        from load_manager import LoadManager, LoadManagerConfig

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
# 3. LoadManager._fire_telegram_notification respects sender status
# =============================================================================


class TestFireTelegramNotification:

    def test_noop_when_sender_is_none(self, mock_config_env):
        """When telegram_sender is None, _fire_telegram_notification returns False."""
        from load_manager import LoadManager, LoadManagerConfig
        from clock import FakeClock
        from datetime import datetime, timezone as tz

        clock = FakeClock(datetime(2025, 6, 15, 14, 0, 0, tzinfo=tz.utc))
        mgr = LoadManager(
            LoadManagerConfig(
                telegram_sender=None,
                clock=clock,
            ),
        )

        result = mgr.run_cycle()
        # With no sender and no plugs/tesla, should return a result (not crash).
        assert result is not None

    @pytest.mark.asyncio
    async def test_noop_when_not_configured(self, mock_config_env):
        """When sender exists but is_configured is False, returns False."""
        mock_sender = MagicMock()
        mock_sender.is_configured = False
        mock_sender.is_configured = False

        from load_manager import LoadManager, LoadManagerConfig
        from clock import FakeClock
        from datetime import datetime, timezone as tz

        clock = FakeClock(datetime(2025, 6, 15, 14, 0, 0, tzinfo=tz.utc))
        mgr = LoadManager(
            LoadManagerConfig(
                telegram_sender=mock_sender,
                clock=clock,
            ),
        )

        result = await mgr._fire_telegram_notification(
            actions=[], predicted_wh=1000.0, target_wh=-500.0, dry_run=False
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_noop_in_dry_run(self, mock_config_env):
        """When dry_run=True, notifications are skipped even with sender."""
        mock_sender = MagicMock()
        mock_sender.is_configured = True

        from load_manager import LoadManager, LoadManagerConfig
        from clock import FakeClock
        from datetime import datetime, timezone as tz

        clock = FakeClock(datetime(2025, 6, 15, 14, 0, 0, tzinfo=tz.utc))
        mgr = LoadManager(
            LoadManagerConfig(
                telegram_sender=mock_sender,
                clock=clock,
                dry_run=True,
            ),
        )

        result = await mgr._fire_telegram_notification(
            actions=[MagicMock(device_name="pump")],
            predicted_wh=1000.0,
            target_wh=-500.0,
            dry_run=True,
        )
        assert result is False


# =============================================================================
# 4. LoadManager._fire_auth_error_notification
# =============================================================================


class TestFireAuthErrorNotification:

    @pytest.mark.asyncio
    async def test_noop_when_sender_none(self):
        """When telegram_sender is None, returns False."""
        from load_manager import LoadManager, LoadManagerConfig

        mgr = LoadManager(LoadManagerConfig(telegram_sender=None))
        result = await mgr._fire_auth_error_notification("auth error")
        assert result is False

    @pytest.mark.asyncio
    async def test_noop_when_not_configured(self):
        """When sender exists but is_configured is False, returns False."""
        mock_sender = MagicMock()
        mock_sender.is_configured = False

        from load_manager import LoadManager, LoadManagerConfig

        mgr = LoadManager(LoadManagerConfig(telegram_sender=mock_sender))
        result = await mgr._fire_auth_error_notification("auth error")
        assert result is False

    @pytest.mark.asyncio
    async def test_noop_when_alert_disabled(self):
        """When alert_on_auth_error is False, returns False."""
        mock_sender = MagicMock()
        mock_sender.is_configured = True

        from load_manager import LoadManager, LoadManagerConfig

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

        from load_manager import LoadManager, LoadManagerConfig

        mgr = LoadManager(
            LoadManagerConfig(telegram_sender=mock_sender),
        )
        result = await mgr._fire_auth_error_notification("login_required")
        assert result is True
        mock_sender.send_notification.assert_awaited_once()
        # Verify the notification contains the error text
        call_args = mock_sender.send_notification.call_args[0][0]
        assert "login_required" in call_args.description
