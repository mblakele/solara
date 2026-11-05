"""Telegram notification policy and the deferred send queue.

Extracted from ``load_manager.py`` (architecture-v2 D.2): one file owns
when a notification fires, which gates apply, and how the event is built.
``LoadManager`` keeps only the QH throttles and the send loop.
"""

from __future__ import annotations

from datetime import datetime, time
import logging
from typing import Any, Protocol

from telegram import (
    NotificationEvent,
    build_error_notification,
    build_notification,
)


logger = logging.getLogger(__name__)


class NotifierOwner(Protocol):
    """Live attributes the notifier reads at call time.

    Reading through the owner (instead of copying values in ``__init__``)
    keeps test seams that set ``telegram_sender`` or the whitelist after
    construction working.
    """

    telegram_sender: Any
    _telegram_devices: dict[str, set[str]] | None
    _telegram_alert_on_auth_error: bool


class Notifier:
    """Notification policy plus the deferred send queue.

    Args:
        owner: The ``LoadManager`` (or a test double) that supplies the
            sender, whitelist, auth-alert switch, and clock.
    """

    def __init__(self, owner: NotifierOwner) -> None:
        self._owner = owner
        self.events: list[NotificationEvent] = []

    # ── Queue plumbing ────────────────────────────────────────────────

    def replace(self, events: list[NotificationEvent]) -> None:
        """Replace the pending queue (test seam)."""
        self.events = list(events)

    def drain(self) -> list[NotificationEvent]:
        """Return every pending event and empty the queue."""
        events = list(self.events)
        self.events.clear()
        return events

    def _sender_ready(self, label: str) -> bool:
        """Return True when a configured sender is available."""
        if self._owner.telegram_sender is None:
            logger.info("%s notification skipped: sender not configured", label)
            return False
        if not self._owner.telegram_sender.is_configured:
            logger.info(
                "%s notification skipped: sender not configured "
                "(is_configured=False)",
                label,
            )
            return False
        return True

    # ── Gated surplus notification ────────────────────────────────────

    def surplus(
        self,
        actions: list[Any],
        predicted_wh: float,
        target_wh: float,
        dry_run: bool,
        now: datetime,
        runtime_today_secs: dict[str, float] | None = None,
    ) -> bool:
        """Queue a surplus notification when every gate allows it.

        Skips when the sender is missing, dry-run is active, there are no
        actions, or no action matches the ``telegram.devices`` whitelist.

        Returns:
            True when an event was queued.
        """
        if not self._sender_ready("Telegram"):
            return False
        if dry_run:
            logger.info("Telegram notification skipped: dry-run mode")
            return False
        if not actions:
            # Operator-facing sign convention (deliberate, do NOT "fix"):
            # NBC-signed — negative = excess solar, positive = grid draw —
            # matching how predictions are displayed everywhere else.
            # This is the OPPOSITE of the engine-internal control gap
            # (target_wh - predicted_wh) in CycleDiagnostics/gapminder logs.
            gap_wh = predicted_wh - target_wh
            logger.info(
                "Telegram notification skipped: no actions "
                "(gap=%+.1f Wh, positive=grid draw)",
                gap_wh,
            )
            return False

        event = build_notification(
            actions=actions,
            predicted_wh=predicted_wh,
            target_wh=target_wh,
            now=now,
            runtime_today_secs=runtime_today_secs,
        )

        devices = self._owner._telegram_devices
        if devices is None:
            logger.info(
                "Telegram notification skipped: telegram.devices "
                "whitelist not configured",
            )
            return False

        matches = any(
            action.device_name.lower() in devices
            and action.action in devices[action.device_name.lower()]
            for action in actions
        )
        if not matches:
            logger.info(
                "Telegram notification skipped: no actions match "
                "telegram.devices whitelist",
            )
            return False

        self.events.append(event)
        device_names = ", ".join(action.device_name for action in actions)
        logger.info(
            "Telegram notification queued: devices=%s gap=%+.1f Wh",
            device_names,
            predicted_wh - target_wh,
        )
        return True

    # ── Bypass notifications (data health, auth, out-of-range) ────────

    def auth_error(
        self,
        error_msg: str,
        login_url: str | None,
        now: datetime,
    ) -> bool:
        """Queue an auth error, honouring the auth-alert switch only."""
        if not self._sender_ready("Auth error"):
            return False
        if not self._owner._telegram_alert_on_auth_error:
            logger.info(
                "Auth error notification skipped: alert_on_auth_error "
                "is False",
            )
            return False
        event = build_error_notification(error_msg, now=now, login_url=login_url)
        self.events.append(event)
        logger.info("Auth error notification queued: error=%s", error_msg)
        return True

    def drift(self, alert: Any, now: datetime) -> bool:
        """Queue a persistent Emporia drift alert."""
        if not self._sender_ready("Drift alert"):
            return False
        message = (
            f"Emporia VUE drift: channel {alert.channel_num} QH starting "
            f"{alert.chart_start.isoformat()} rejected {alert.count} "
            f"consecutive fetches (API data_start "
            f"{alert.data_start.isoformat()}); head of window appears "
            "permanently missing"
        )
        event = build_error_notification(message, now=now)
        self.events.append(event)
        logger.error("Drift alert notification queued: %s", message)
        return True

    def fetch_fatal(self, error_msg: str, now: datetime) -> bool:
        """Queue a fatal metrics-fetch alert."""
        if not self._sender_ready("Fetch-fatal"):
            return False
        message = (
            f"Emporia VUE fetch failure: {error_msg} "
            "(no fresh data this cycle)"
        )
        event = build_error_notification(message, now=now)
        self.events.append(event)
        logger.error("Fetch-fatal notification queued: %s", message)
        return True

    def stale_data(self, message: str, now: datetime) -> bool:
        """Queue a stale/missing meter-data alert."""
        if not self._sender_ready("Stale-data"):
            return False
        event = build_error_notification(message, now=now)
        self.events.append(event)
        logger.error("Stale-data notification queued: %s", message)
        return True

    def pipeline_stall(self, message: str, now: datetime) -> bool:
        """Queue a stalled decision-pipeline alert."""
        if not self._sender_ready("Pipeline-stall"):
            return False
        event = build_error_notification(message, now=now)
        self.events.append(event)
        logger.error("Pipeline-stall notification queued: %s", message)
        return True

    def out_of_range(
        self,
        device_name: str,
        time_range: tuple[time, time],
        now: datetime,
    ) -> bool:
        """Queue a plug-ON-outside-its-window alert."""
        if not self._sender_ready("Out-of-range"):
            return False
        start_str = time_range[0].strftime("%H:%M")
        end_str = time_range[1].strftime("%H:%M")
        message = (
            f"{device_name} is ON outside its time range "
            f"{start_str}-{end_str}"
        )
        event = build_error_notification(message, now=now)
        self.events.append(event)
        logger.warning("Out-of-range notification queued: %s", message)
        return True
