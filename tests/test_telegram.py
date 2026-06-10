"""Tests for telegram.py — TelegramSender, NotificationEvent, helpers.

Covers:
  - load_telegram_config() env var and devices.json loading
  - NotificationEvent creation and formatting
  - TelegramSender.from_config(), from_raw(), is_configured
  - TelegramSender.send(), send_notification(), close()
  - build_notification(), build_error_notification()
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytz
import pytest

from telegram import (
    EVENT_TYPE_DEFICIT,
    EVENT_TYPE_ERROR,
    EVENT_TYPE_SURPLUS,
    EVENT_TYPE_SYSTEM,
    NotificationEvent,
    TelegramSender,
    build_error_notification,
    build_notification,
    load_telegram_config,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def sample_event():
    """A NotificationEvent for testing."""
    return NotificationEvent(
        event_type=EVENT_TYPE_SURPLUS,
        timestamp=datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
        description="Test surplus event",
        actions=[{"device": "Pool Pump", "type": "turn_on"}],
        predicted_wh=1200.0,
        target_wh=-500.0,
    )


# =============================================================================
# 1. load_telegram_config()
# =============================================================================


class TestLoadTelegramConfig:

    def test_env_vars_take_priority(self, monkeypatch):
        """Env vars override devices.json when both are configured."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat")
        result = load_telegram_config()
        assert result is not None
        assert result["bot_token"] == "env-token"
        assert result["chat_id"] == "env-chat"

    def test_devices_json_fallback(self, monkeypatch):
        """devices.json telegram section used when env vars are missing."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

        def mock_load():
            return {
                "telegram": {"bot_token": "dj-token", "chat_id": 98765}
            }

        monkeypatch.setattr(
            "device_config._load",
            mock_load,
        )
        result = load_telegram_config()
        assert result is not None
        assert result["bot_token"] == "dj-token"
        assert result["chat_id"] == "98765"

    def test_no_config_returns_none(self, monkeypatch):
        """Returns None when neither env vars nor devices.json have config."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

        def mock_load():
            return {}

        monkeypatch.setattr("device_config._load", mock_load)
        result = load_telegram_config()
        assert result is None

    def test_partial_config_returns_none(self, monkeypatch):
        """Returns None when only one of token/chat_id is set."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "only-token")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setattr("device_config._load", lambda: {})
        result = load_telegram_config()
        assert result is None

    def test_env_bot_token_only(self, monkeypatch):
        """bot_token set but no chat_id returns None."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setattr("device_config._load", lambda: {})
        result = load_telegram_config()
        assert result is None


# =============================================================================
# 2. NotificationEvent
# =============================================================================


class TestNotificationEvent:

    def test_surplus_wh_computed(self):
        """surplus_wh is predicted_wh - target_wh."""
        event = NotificationEvent(
            event_type=EVENT_TYPE_SURPLUS,
            timestamp=datetime.now(timezone.utc),
            description="Test",
            predicted_wh=1500.0,
            target_wh=-500.0,
        )
        assert event.surplus_wh == 2000.0

    def test_deficit_surplus_wh(self):
        """surplus_wh is negative when below target."""
        event = NotificationEvent(
            event_type=EVENT_TYPE_DEFICIT,
            timestamp=datetime.now(timezone.utc),
            description="Test",
            predicted_wh=-100.0,
            target_wh=-500.0,
        )
        assert event.surplus_wh == 400.0

    def test_format_message_with_actions(self, sample_event):
        """format_message includes actions in output."""
        msg = sample_event.format_message()
        assert "🟢 Pool Pump" in msg

    def test_format_message_without_actions(self):
        """format_message omits actions section when empty."""
        event = NotificationEvent(
            event_type=EVENT_TYPE_SYSTEM,
            timestamp=datetime.now(timezone.utc),
            description="System check",
            predicted_wh=500.0,
            target_wh=-500.0,
        )
        msg = event.format_message()
        assert "No actions" in msg
        assert "Actions taken" not in msg

    def test_format_message_includes_timestamp(self, sample_event):
        """format_message includes HH:MM:SS timestamp."""
        msg = sample_event.format_message()
        assert "14:30:00" in msg

    def test_format_message_includes_wh_values(self, sample_event):
        """format_message includes predicted and target Wh values."""
        msg = sample_event.format_message()
        assert "1200-Wh" in msg

    def test_default_actions_is_empty_list(self):
        """Actions defaults to empty list."""
        event = NotificationEvent(
            event_type=EVENT_TYPE_SURPLUS,
            timestamp=datetime.now(timezone.utc),
            description="Test",
        )
        assert event.actions == []

    def test_frozen_dataclass(self):
        """NotificationEvent is immutable (frozen)."""
        event = NotificationEvent(
            event_type=EVENT_TYPE_SURPLUS,
            timestamp=datetime.now(timezone.utc),
            description="Test",
        )
        with pytest.raises(Exception):
            event.event_type = "new"


# =============================================================================
# 3. TelegramSender
# =============================================================================


class TestTelegramSender:

    def test_from_raw_success(self):
        """from_raw creates a sender with valid values."""
        sender = TelegramSender.from_raw("token123", "chat456")
        assert sender is not None
        assert sender.is_configured is True
        assert sender.config.bot_token == "token123"
        assert sender.config.chat_id == "chat456"

    def test_from_raw_empty_token(self):
        """from_raw returns None for empty token."""
        assert TelegramSender.from_raw("", "chat456") is None

    def test_from_raw_empty_chat(self):
        """from_raw returns None for empty chat_id."""
        assert TelegramSender.from_raw("token123", "") is None

    def test_from_raw_none_values(self):
        """from_raw returns None for None values."""
        assert TelegramSender.from_raw(None, "chat456") is None  # type: ignore[arg-type]

    def test_is_configured_false_when_no_config(self):
        """is_configured is False when config is None."""
        sender = TelegramSender.__new__(TelegramSender)
        sender.config = None  # type: ignore[attr-defined]
        assert sender.is_configured is False

    def test_is_configured_false_when_empty_values(self):
        """is_configured is False when token/chat_id are empty."""
        from telegram_client import TelegramConfig

        config = TelegramConfig(bot_token="", chat_id="")
        sender = TelegramSender(config)
        assert sender.is_configured is False

    @pytest.mark.asyncio
    async def test_send_when_not_configured(self):
        """send returns False when not configured."""
        sender = TelegramSender.__new__(TelegramSender)
        sender.config = None  # type: ignore[attr-defined]
        result = await sender.send("Hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_success(self):
        """send returns True on successful message delivery."""
        from telegram_client import TelegramConfig

        config = TelegramConfig(bot_token="token", chat_id="chat")
        sender = TelegramSender(config)

        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_client.close = AsyncMock()
        object.__setattr__(sender, "_telegram_client", mock_client)

        result = await sender.send("Test message")
        assert result is True
        mock_client.send_message.assert_called_once_with("Test message")

    @pytest.mark.asyncio
    async def test_send_failure_returns_false(self):
        """send returns False when underlying client raises."""
        from telegram_client import TelegramConfig

        config = TelegramConfig(bot_token="token", chat_id="chat")
        sender = TelegramSender(config)

        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(side_effect=RuntimeError("fail"))
        mock_client.close = AsyncMock()
        object.__setattr__(sender, "_telegram_client", mock_client)

        result = await sender.send("Test")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_notification(self):
        """send_notification formats and sends the event."""
        from telegram_client import TelegramConfig

        config = TelegramConfig(bot_token="token", chat_id="chat")
        sender = TelegramSender(config)

        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_client.close = AsyncMock()
        object.__setattr__(sender, "_telegram_client", mock_client)

        event = NotificationEvent(
            event_type=EVENT_TYPE_SURPLUS,
            timestamp=datetime.now(timezone.utc),
            description="Surplus alert",
            predicted_wh=1000.0,
            target_wh=-500.0,
        )

        result = await sender.send_notification(event)
        assert result is True

    @pytest.mark.asyncio
    async def test_close(self):
        """close releases the underlying client."""
        from telegram_client import TelegramConfig

        config = TelegramConfig(bot_token="token", chat_id="chat")
        sender = TelegramSender(config)

        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_client.close = AsyncMock()
        object.__setattr__(sender, "_telegram_client", mock_client)

        await sender.close()
        mock_client.close.assert_called_once()
        assert sender._telegram_client is None

    @pytest.mark.asyncio
    async def test_close_noop_when_none(self):
        """close is a no-op when client is already None."""
        from telegram_client import TelegramConfig

        config = TelegramConfig(bot_token="token", chat_id="chat")
        sender = TelegramSender(config)
        # _telegram_client should be None by default
        await sender.close()  # Should not raise


# =============================================================================
# 4. build_notification()
# =============================================================================


class TestBuildSurplusNotification:

    def test_default_timestamp_uses_utc(self):
        """When now is None, uses current UTC time."""
        event = build_notification([], 1000.0, -500.0)
        assert event.timestamp.tzinfo is not None
        assert event.event_type == EVENT_TYPE_SURPLUS

    def test_description_includes_values(self):
        """Description includes predicted and target Wh values."""
        event = build_notification(
            [{"device": "Pump", "type": "turn_on"}],
            1200.0,
            -500.0,
            datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
        )
        assert 1200.0 == event.predicted_wh
        assert -500.0 == event.target_wh

    def test_actions_included(self):
        """Actions list is passed through."""
        actions = [{"device": "Pool Pump", "type": "turn_on"}]
        event = build_notification(
            actions, 1000.0, -500.0,
            datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
        )
        assert event.actions == actions


# =============================================================================
# 5. build_error_notification()
# =============================================================================


class TestBuildErrorNotification:

    def test_default_timestamp_uses_utc(self):
        """When now is None, uses current UTC time."""
        event = build_error_notification("Connection lost")
        assert event.timestamp.tzinfo is not None
        assert event.event_type == EVENT_TYPE_ERROR

    def test_description_includes_error(self):
        """Description includes the error message."""
        event = build_error_notification("API timeout")
        assert "API timeout" in event.description

    def test_auth_error_format(self):
        """Auth error text appears in description."""
        event = build_error_notification("login_required: refresh_token is invalid")
        assert "login_required" in event.description
        assert "refresh_token" in event.description

    def test_auth_error_with_login_url(self):
        """Login URL is appended to description when provided."""
        event = build_error_notification(
            "login_required: refresh_token is invalid",
            login_url="https://auth.tesla.com/oauth2/v3/authorize?client_id=test",
        )
        assert "login_required" in event.description
        assert "Re-authenticate" in event.description
        assert "auth.tesla.com" in event.description
