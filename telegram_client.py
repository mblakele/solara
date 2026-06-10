"""Async Telegram Bot API client using aiohttp.

Minimal client — only implements send_message for status notifications.
No event loop, no polling, no handlers. Fire-and-forget.

Usage::

    config = TelegramConfig(bot_token="123:ABC", chat_id="-100")
    client = TelegramClient(config)
    await client.send_message("Hello")
    await client.close()
"""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


class TelegramConfig:
    """Configuration for Telegram notifications.

    Attributes:
        bot_token: Bot API token from devices.json.
        chat_id: Telegram chat ID (private or group).
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        """Initialize the Telegram configuration.

        Args:
            bot_token: Bot API token from devices.json.
            chat_id: Telegram chat ID (private or group).
        """
        self.bot_token = bot_token
        self.chat_id = chat_id

    def base_url(self) -> str:
        """Return the Telegram Bot API base URL.

        Returns:
            The base URL for API requests.
        """
        return f"https://api.telegram.org/bot{self.bot_token}"


class TelegramClient:
    """Async client for Telegram Bot API via aiohttp.

    Minimal — only implements what's needed for status notifications.
    Never raises exceptions from send_message; returns False on failure.

    Attributes:
        config: TelegramConfig with token and chat_id.
    """

    def __init__(self, config: TelegramConfig) -> None:
        """Initialize the Telegram client.

        Args:
            config: Bot token and chat ID.
        """
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return a cached aiohttp session, creating one if needed.

        If the cached session is closed (or its event loop was closed — which
        happens when ``asyncio.run()`` finishes and closes its temporary loop),
        discards it and creates a fresh one so the next call on a new loop
        succeeds.

        Returns:
            An aiohttp ClientSession instance.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def reset_session(self) -> None:
        """Reset cached aiohttp session and API client.

        Must be called before each ``asyncio.run()`` invocation, since
        ``asyncio.run()`` creates a fresh event loop and closes it afterward.
        A cached aiohttp ClientSession is bound to the loop it was created in
        and becomes unusable once that loop is closed. We discard the old
        session; Python's GC will clean it up.
        """
        self._session = None

    async def send_message(self, text: str) -> bool:
        """Send a message to the configured chat.

        Creates an aiohttp session on first use, reuses it for subsequent
        calls. If send fails for any reason (HTTP error, network error,
        JSON parse error), logs a warning and returns False.

        Args:
            text: Plain text message body.

        Returns:
            True on success, False on HTTP error or exception.
        """
        session = await self._get_session()
        url = f"{self.config.base_url()}/sendMessage"
        payload = {
            "chat_id": self.config.chat_id,
            "text": text,
        }
        try:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Telegram API returned status %d", resp.status
                    )
                    return False
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning("Telegram API error: %s", data)
                    return False
                return True
        except aiohttp.ClientError as e:
            logger.warning("Telegram send network error: %s", e)
            return False
        except (ValueError, OSError) as e:
            logger.warning("Telegram send error: %s", e)
            return False
        except RuntimeError as e:
            logger.warning("Telegram send failed (event loop issue): %s", e)
            return False

    async def close(self) -> None:
        """Release aiohttp session resources."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
