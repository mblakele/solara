"""Tests for telegram_client.py — TelegramConfig and TelegramClient.

Covers:
  - TelegramConfig base_url construction
  - TelegramClient.send_message success path
  - TelegramClient.send_message HTTP error (4xx/5xx)
  - TelegramClient.send_message network error (ClientError)
  - TelegramClient.send_message JSON parse error (ValueError)
  - TelegramClient.send_message OSError
  - TelegramClient.send_message Telegram API error response
  - TelegramClient.close (closes session, no-op if already closed)
  - TelegramClient._get_session (creates and reuses session)
  - TelegramClient.send_message reuses session across multiple calls
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from telegram_client import TelegramClient, TelegramConfig


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def telegram_config():
    """Standard TelegramConfig for tests."""
    return TelegramConfig(bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", chat_id="-100123456")


@pytest.fixture()
def telegram_client(telegram_config):
    """TelegramClient with session mocked out."""
    ctrl = TelegramClient(telegram_config)
    ctrl._session = MagicMock()
    ctrl._session.close = AsyncMock()  # close() is async
    return ctrl


# =============================================================================
# 1. TelegramConfig (lines ~53-66)
# =============================================================================


class TestTelegramConfig:

    def test_base_url_returns_correct_format(self, telegram_config):
        """base_url includes the token in the standard Bot API format."""
        assert telegram_config.base_url() == (
            "https://api.telegram.org/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        )

    def test_base_url_with_simple_token(self):
        """base_url works with a simple numeric token."""
        config = TelegramConfig(bot_token="111:BBB", chat_id="12345")
        assert config.base_url() == "https://api.telegram.org/bot111:BBB"


# =============================================================================
# 2. TelegramClient.send_message — success (lines ~97-132)
# =============================================================================


class TestSendMessageSuccess:

    def test_send_message_success(self, telegram_config):
        """Successful API response returns True."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"ok": True, "result": {}}
        )

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_response)
        cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post.return_value = cm
        mock_session.close = AsyncMock()

        ctrl = TelegramClient(telegram_config)
        ctrl._session = mock_session
        result = asyncio.run(ctrl.send_message("test message"))
        assert result is True
        mock_session.post.assert_called_once()

    def test_send_message_text_content(self, telegram_config):
        """Message body is sent as 'text' field in JSON payload."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"ok": True, "result": {}}
        )

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_response)
        cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post.return_value = cm
        mock_session.close = AsyncMock()

        ctrl = TelegramClient(telegram_config)
        ctrl._session = mock_session
        asyncio.run(ctrl.send_message("Hello, world!"))

        call_args = mock_session.post.call_args
        assert call_args.kwargs["json"]["text"] == "Hello, world!"
        assert call_args.kwargs["json"]["chat_id"] == "-100123456"

    def test_send_message_with_emoji(self, telegram_config):
        """Unicode characters in message are sent correctly."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"ok": True, "result": {}}
        )

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_response)
        cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post.return_value = cm
        mock_session.close = AsyncMock()

        ctrl = TelegramClient(telegram_config)
        ctrl._session = mock_session
        result = asyncio.run(ctrl.send_message("Solar ⚡ alert"))
        assert result is True


# =============================================================================
# 3. TelegramClient.send_message — HTTP error (lines ~103-105)
# =============================================================================


class TestSendMessageHTTPError:

    def _setup_mock_response(self, client, status=403):
        """Set up session.post to return a mock response with given status."""
        mock_response = MagicMock()
        mock_response.status = status
        mock_response.json = AsyncMock(return_value={})

        post_mock = MagicMock()
        post_mock.return_value = AsyncMock(return_value=mock_response)
        post_mock.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        post_mock.return_value.__aexit__ = AsyncMock(return_value=False)
        client._session.post = post_mock

    def test_non_200_status_returns_false(self, telegram_client):
        """HTTP status != 200 returns False."""
        self._setup_mock_response(telegram_client, status=403)

        result = asyncio.run(telegram_client.send_message("should fail"))
        assert result is False

    def test_500_status_returns_false(self, telegram_client):
        """Server error 500 returns False."""
        self._setup_mock_response(telegram_client, status=500)

        result = asyncio.run(telegram_client.send_message("should fail"))
        assert result is False


# =============================================================================
# 4. TelegramClient.send_message — Telegram API error response (lines ~106-110)
# =============================================================================


class TestSendMessageAPIError:

    def _setup_mock_response(self, client, body=None):
        """Set up session.post to return a mock API error response."""
        if body is None:
            body = {"ok": False, "description": "Chat not found"}
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=body)

        post_mock = MagicMock()
        post_mock.return_value = AsyncMock(return_value=mock_response)
        post_mock.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        post_mock.return_value.__aexit__ = AsyncMock(return_value=False)
        client._session.post = post_mock

    def test_api_error_response_returns_false(self, telegram_client):
        """API returns ok=False — returns False."""
        self._setup_mock_response(
            telegram_client, body={"ok": False, "description": "Chat not found"}
        )

        result = asyncio.run(telegram_client.send_message("should fail"))
        assert result is False


# =============================================================================
# 5. TelegramClient.send_message — network error (lines ~113-115)
# =============================================================================


class TestSendMessageNetworkError:

    def test_client_error_returns_false(self, telegram_client):
        """aiohttp.ClientError returns False."""
        telegram_client._session.post.side_effect = aiohttp.ClientError("connection refused")

        result = asyncio.run(telegram_client.send_message("should fail"))
        assert result is False


# =============================================================================
# 6. TelegramClient.send_message — parse error (lines ~116-118)
# =============================================================================


class TestSendMessageParseError:

    def test_value_error_returns_false(self, telegram_client):
        """JSON parse ValueError returns False."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(side_effect=ValueError("invalid json"))
        telegram_client._session.post = AsyncMock(return_value=mock_response)

        result = asyncio.run(telegram_client.send_message("should fail"))
        assert result is False

    def test_os_error_returns_false(self, telegram_client):
        """OSError returns False."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(side_effect=OSError("disk full"))
        telegram_client._session.post = AsyncMock(return_value=mock_response)

        result = asyncio.run(telegram_client.send_message("should fail"))
        assert result is False


# =============================================================================
# 7. TelegramClient.close (lines ~121-125)
# =============================================================================


class TestClose:

    def test_close_closes_session(self, telegram_client):
        """close() calls session.close() when session exists."""
        mock_session = telegram_client._session
        mock_session.closed = False
        # close() is async
        asyncio.run(telegram_client.close())
        mock_session.close.assert_awaited_once()

    def test_close_noop_if_none(self):
        """close() does not raise when _session is None."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )
        asyncio.run(client.close())  # Should not raise

    def test_close_noop_if_already_closed(self):
        """close() does not call close() if session is already closed."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )
        mock_session = MagicMock()
        mock_session.closed = True
        mock_session.close = AsyncMock()  # ensure close is async
        client._session = mock_session
        asyncio.run(client.close())
        mock_session.close.assert_not_called()


# =============================================================================
# 8. TelegramClient._get_session — session creation and reuse (lines ~78-85)
# =============================================================================


class TestGetSession:

    def test_creates_session_when_none(self):
        """First call creates a new aiohttp session."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )
        # Before creation, _session should be None
        assert client._session is None

        asyncio.run(client._get_session())

        assert client._session is not None

    def test_reuses_session_on_second_call(self):
        """Second call returns the same session, doesn't create a new one."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )

        session1 = asyncio.run(client._get_session())
        session2 = asyncio.run(client._get_session())

        assert session1 is session2

    def test_recreates_on_closed_session(self):
        """A closed session is recreated on next call."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )

        session1 = asyncio.run(client._get_session())
        # Close the session properly using asyncio.run
        asyncio.run(session1.close())

        session2 = asyncio.run(client._get_session())
        assert session2 is not session1

    def test_reuses_session_when_closed_is_false(self):
        """Session is reused when __aenter__ has not been called (closed=False)."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )

        session1 = asyncio.run(client._get_session())
        assert session1.closed is False

        session2 = asyncio.run(client._get_session())
        assert session1 is session2


# =============================================================================
# 9. TelegramClient.send_message — session reuse across multiple calls
# =============================================================================


class TestSendMessageReuse:

    def test_reuses_session_across_calls(self, telegram_config):
        """Multiple send_message calls reuse the same session via session.post."""
        client = TelegramClient(telegram_config)

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"ok": True, "result": {}})

        post_mock = MagicMock()
        post_mock.return_value = AsyncMock(return_value=mock_response)
        post_mock.return_value.__aenter__ = AsyncMock(return_value=mock_response)
        post_mock.return_value.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = post_mock
        session.closed = False

        client._session = session

        # Two sends — should only ever create one session
        asyncio.run(client._get_session())
        result1 = asyncio.run(client.send_message("first"))
        result2 = asyncio.run(client.send_message("second"))

        # Both succeed with reused session
        assert result1 is True
        assert result2 is True

        # session.post was called exactly twice (once per send)
        assert session.post.call_count == 2


# =============================================================================
# 10. TelegramClient.reset_session — session reset (lines ~78-85)
# =============================================================================


class TestResetSession:

    def test_reset_session_clears_cached_session(self):
        """reset_session() nullifies _session so a new one is created next time."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )

        # Create a session first
        session1 = asyncio.run(client._get_session())
        assert session1 is not None
        assert client._session is session1

        # Reset should nullify the session
        client.reset_session()
        assert client._session is None

    def test_reset_session_is_safe_when_session_none(self):
        """reset_session() does not raise when _session is None."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )
        client.reset_session()  # Should not raise
        assert client._session is None

    def test_reset_session_is_safe_when_session_closed(self):
        """reset_session() does not raise when _session is already closed."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )
        session1 = asyncio.run(client._get_session())
        asyncio.run(session1.close())
        client.reset_session()  # Should not raise
        assert client._session is None


# =============================================================================
# 11. Regression: event loop closure across asyncio.run() cycles
# =============================================================================


class TestEventLoopClosureRegression:

    def test_send_fails_without_reset_session_across_asyncio_run_cycles(self):
        """REPRODUCTION: Without reset_session(), second asyncio.run() cycle fails.

        This test reproduces the production bug where a TelegramClient reused
        across multiple asyncio.run() calls (one per load management cycle)
        triggers "Event loop is closed" on the second cycle, because aiohttp
        ClientSession binds to the event loop at creation time.

        asyncio.run() creates a fresh event loop and closes it after each call.
        A cached ClientSession from loop A is unusable on loop B.
        """
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )

        # Cycle 1: create a real aiohttp session via asyncio.run()
        session1 = asyncio.run(client._get_session())
        assert session1 is not None
        assert session1.closed is False

        # Cycle 2: asyncio.run() creates a fresh event loop.
        # The cached session is bound to the old loop — this should fail.
        result2 = asyncio.run(client.send_message("second cycle"))
        assert result2 is False

    def test_send_succeeds_with_reset_session_between_asyncio_run_cycles(self):
        """With reset_session() between cycles, second asyncio.run() succeeds."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )

        # Cycle 1: create a real aiohttp session via asyncio.run()
        session1 = asyncio.run(client._get_session())
        assert session1 is not None

        # Reset between cycles (this is what _stage_async_phase should do)
        client.reset_session()

        # Cycle 2: fresh session created on new loop
        session2 = asyncio.run(client._get_session())
        assert session2 is not None
        assert session2 is not session1

    def test_send_succeeds_without_reset_when_session_is_closed(self):
        """When session.closed is True, _get_session() recreates it automatically."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )

        # Cycle 1: create a real aiohttp session
        session1 = asyncio.run(client._get_session())
        assert session1 is not None

        # Close the session (simulating what aiohttp does when the event loop closes)
        asyncio.run(session1.close())

        # Cycle 2: _get_session() should detect closed session and recreate
        session2 = asyncio.run(client._get_session())
        assert session2 is not session1


# =============================================================================
# 12. TelegramClient._get_session — event loop mismatch detection
# =============================================================================


class TestGetSessionLoopMismatch:

    def test_get_session_detects_closed_session(self):
        """When session.closed is True, _get_session() returns a fresh session."""
        client = TelegramClient(
            TelegramConfig(bot_token="111:BBB", chat_id="999")
        )

        session1 = asyncio.run(client._get_session())
        assert session1 is not None
        assert session1.closed is False

        # Simulate the session being closed (as aiohttp does when the loop closes)
        session1._closed = True  # type: ignore[attr-defined]
        # Also set closed property to True so the check passes
        type(session1).closed = property(lambda self: True)

        session2 = asyncio.run(client._get_session())
        assert session2 is not session1
