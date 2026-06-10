"""Telegram notification sender for Solara.

Wraps telegram_client.py's TelegramConfig and TelegramClient with
configuration loading from environment variables and devices.json,
and a high-level async send interface.

Config loading order (highest priority first):
  1. Environment variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
  2. devices.json "telegram" section

Usage::

    sender = TelegramSender.from_config()
    success = await sender.send("Solar surplus alert!")
    await sender.close()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone


import pytz

from config import Config
from load_models import PendingEffect
from telegram_client import TelegramClient, TelegramConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationEvent:
    """A notification event to be sent via Telegram.

    Attributes:
        event_type: One of the predefined event type strings.
        timestamp: When the event occurred.
        description: Human-readable summary of the event.
        actions: Actions that were executed for this event.
        predicted_wh: The predicted Wh for the current quarter-hour.
        target_wh: The target Wh threshold.
        surplus_wh: Computed surplus/deficit (predicted - target).
    """

    event_type: str
    timestamp: datetime
    description: str
    actions: list[PendingEffect] = field(default_factory=list)
    predicted_wh: float = 0.0
    target_wh: float = 0.0
    surplus_wh: float = field(init=False)

    def __post_init__(self) -> None:
        """Compute the surplus_wh from predicted and target."""
        object.__setattr__(self, "surplus_wh", self.predicted_wh - self.target_wh)

    def format_message(self) -> str:
        """Format the notification as a human-readable message.

        Returns:
            A Markdown-formatted string suitable for Telegram.
        """
        action_list = ""
        if self.actions:
            action_lines = []
            for action in self.actions:
                if isinstance(action, dict):
                    device = action.get("device", "unknown")
                    action_type = action.get("type", "")
                else:
                    device = action.device_name
                    action_type = action.action
                bullet = "🔘" if action_type == "turn_off" else "🟢"
                action_lines.append(f"  {bullet} {device}")
            action_list = "\n".join(action_lines)

        return (
            f"☀️ {self.description} {int(self.predicted_wh)}-Wh at {self.timestamp.strftime('%H:%M:%S')}\n"
            f"{action_list if self.actions else 'ℹ️ No actions'}"
        )


# Predefined event type constants for consistency across the system.
EVENT_TYPE_SURPLUS = "surplus"
EVENT_TYPE_DEFICIT = "deficit"
EVENT_TYPE_TESLA = "tesla"
EVENT_TYPE_DISCHARGE = "discharge"
EVENT_TYPE_ERROR = "error"
EVENT_TYPE_SYSTEM = "system"


def load_telegram_config(config: Config | None = None) -> dict[str, str] | None:
    """Load Telegram configuration from env vars and devices.json.

    Reads from environment variables first, then falls back to
    devices.json "telegram" section. Returns None if no config found.

    Args:
        config: Optional Config instance (unused — Telegram config is
            loaded directly from env vars and devices.json). Accepted
            for API consistency across config loading functions.

    Returns:
        A dict with "bot_token" and "chat_id" keys, or None if
        no Telegram config is available from any source.
    """
    _ = config  # accepted for API consistency
    from decouple import config as _decouple_config

    # Priority 1: environment variables
    bot_token = _decouple_config("TELEGRAM_BOT_TOKEN", default=None)
    chat_id = _decouple_config("TELEGRAM_CHAT_ID", default=None)

    if bot_token and chat_id:
        return {"bot_token": bot_token, "chat_id": chat_id}

    # Priority 2: devices.json "telegram" section
    try:
        import device_config

        section = device_config._load().get("telegram")  # pylint: disable=W0212
        if section and section.get("bot_token") and section.get("chat_id"):
            return {
                "bot_token": section["bot_token"],
                "chat_id": str(section["chat_id"]),
            }
    except ImportError:
        pass

    return None


class TelegramSender:
    """High-level Telegram notification sender.

    Wraps telegram_client.py's TelegramClient with configuration
    loading and message formatting. Never raises exceptions from
    send() — returns False on failure.

    The sender is created once and reused across multiple sends.
    A single aiohttp session is shared for all requests.

    Attributes:
        bot_token: The Telegram bot token (from config).
        chat_id: The target chat ID (from config).
    """

    def __init__(self, config: TelegramConfig) -> None:
        """Initialize the Telegram sender.

        Args:
            config: TelegramConfig with bot token and chat ID.
        """
        self.config = config
        self._telegram_client: TelegramClient | None = None

    @classmethod
    def from_config(cls) -> TelegramSender | None:
        """Create a TelegramSender from environment and devices.json config.

        Loads configuration using load_telegram_config(). Returns None
        if no Telegram configuration is available from any source.

        Returns:
            A configured TelegramSender instance, or None if no config found.
        """
        raw_config = load_telegram_config()
        if raw_config is None:
            return None

        bot_token = raw_config["bot_token"]
        chat_id = raw_config["chat_id"]
        telegram_config = TelegramConfig(bot_token=bot_token, chat_id=chat_id)
        logger.info("Telegram configured %s", chat_id)
        return cls(telegram_config)

    @classmethod
    def from_raw(
        cls, bot_token: str, chat_id: str
    ) -> TelegramSender | None:
        """Create a TelegramSender from raw string values.

        Convenience constructor for tests and external callers
        that already have the config values available.

        Args:
            bot_token: Telegram Bot API token.
            chat_id: Telegram chat ID.

        Returns:
            A configured TelegramSender, or None if either value
            is empty.
        """
        if not bot_token or not chat_id:
            return None
        telegram_config = TelegramConfig(
            bot_token=bot_token, chat_id=chat_id
        )
        return cls(telegram_config)

    @property
    def is_configured(self) -> bool:
        """Return True if a Telegram bot token and chat ID are configured.

        Returns:
            True if sender has valid config, False otherwise.
        """
        return (
            bool(self.config)
            and bool(self.config.bot_token)
            and bool(self.config.chat_id)
        )

    async def send(
        self,
        text: str,
    ) -> bool:
        """Send a plain text message to the configured chat.

        Creates a TelegramClient on first use, reuses it for subsequent
        calls. If send fails for any reason, logs a warning and returns
        False.

        Args:
            text: Plain text message body.

        Returns:
            True on success, False on failure.
        """
        if not self.is_configured:
            logger.warning("Telegram not configured — skipping send")
            return False

        client = self._telegram_client
        if client is None:
            client = TelegramClient(self.config)
            object.__setattr__(self, "_telegram_client", client)

        try:
            result = await client.send_message(text)
            if result:
                logger.info("Telegram send successful")
            else:
                logger.warning("Telegram send returned False (API rejection or network error)")
            return result
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Telegram send failed: %s", e)
            return False

    async def send_notification(
        self,
        event: NotificationEvent,
    ) -> bool:
        """Send a formatted notification event.

        Convenience wrapper that formats the event into a message
        and sends it via send().

        Args:
            event: The notification event to send.

        Returns:
            True on success, False on failure.
        """
        message = event.format_message()
        return await self.send(message)

    async def close(self) -> None:
        """Release the underlying async client resources."""
        client = self._telegram_client
        if client is not None:
            await client.close()
            object.__setattr__(self, "_telegram_client", None)

    def reset_session(self) -> None:
        """Reset the underlying async client session.

        Must be called before each ``asyncio.run()`` invocation, since
        ``asyncio.run()`` creates a fresh event loop and closes it after.
        Discarding the cached client means a new one is created on the
        next send() call, bound to the new event loop.
        """
        if self._telegram_client is not None:
            self._telegram_client.reset_session()
            object.__setattr__(self, "_telegram_client", None)


def build_notification(
    actions: list[PendingEffect],
    predicted_wh: float,
    target_wh: float,
    now: datetime | None = None,
    config: Config | None = None,
) -> NotificationEvent:
    """Build a notification event for load management actions.

    Args:
        actions: List of PendingEffect objects describing what was done.
        predicted_wh: The predicted Wh for the current quarter-hour.
        target_wh: The target Wh threshold.
        now: Current time, or the current time in UTC if None.
        config: Optional Config instance for timezone resolution. Falls
            back to module-level _config singleton when None.

    Returns:
        A formatted NotificationEvent with surplus context.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Convert to device timezone for display
    tz_name: str = "America/Los_Angeles"
    if config is not None:
        tz_name = config.timezone
    else:
        try:
            from config import _config

            tz_name = _config.timezone
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    try:
        local_tz = pytz.timezone(tz_name)
        display_time = now.astimezone(local_tz)
    except pytz.exceptions.UnknownTimeZoneError:
        display_time = now

    return NotificationEvent(
        event_type=EVENT_TYPE_SURPLUS,
        timestamp=display_time,
        description=("Solara"),
        actions=actions,
        predicted_wh=predicted_wh,
        target_wh=target_wh,
    )


def build_error_notification(
    error_msg: str,
    now: datetime | None = None,
    config: Config | None = None,
    login_url: str | None = None,
) -> NotificationEvent:
    """Build a notification event for a load management error.

    Args:
        error_msg: The error message text.
        now: Current time, or the current time in UTC if None.
        config: Optional Config instance for timezone resolution. Falls
            back to module-level _config singleton when None.
        login_url: Optional Tesla OAuth login URL to include in the description.

    Returns:
        A formatted NotificationEvent with error context.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    tz_name: str = "America/Los_Angeles"
    if config is not None:
        tz_name = config.timezone
    else:
        try:
            from config import _config

            tz_name = _config.timezone
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    try:
        local_tz = pytz.timezone(tz_name)
        display_time = now.astimezone(local_tz)
    except pytz.exceptions.UnknownTimeZoneError:
        display_time = now

    desc = f"⚠️ Error: {error_msg}"
    if login_url:
        desc += f"\n🔑 Re-authenticate: {login_url}"

    return NotificationEvent(
        event_type=EVENT_TYPE_ERROR,
        timestamp=display_time,
        description=desc,
    )
