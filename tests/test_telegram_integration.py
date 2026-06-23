"""Tests for Telegram notification integration in LoadManager.

Covers:
  - LoadManager accepts telegram_sender parameter (via __init__)
  - _fire_telegram_notification builds and sends the notification
  - Dry-run mode skips Telegram notifications
  - Missing sender gracefully skips notification
  - LoadManager.close() closes the telegram sender
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from energy_cache import EnergyCache
from load_controllers import PlugConfig, PlugController, TeslaController
from load_manager import LoadManager, LoadManagerConfig
from load_models import PendingEffect, TeslaConfig
from telegram import NotificationEvent, TelegramSender
from telegram_client import TelegramConfig


# =============================================================================
# Fixtures
# =============================================================================


def _make_energy_cache(predicted_wh: float = -2000.0, now: datetime | None = None) -> EnergyCache:
    """Create an EnergyCache pre-populated with samples."""
    if now is None:
        now = datetime(2026, 5, 6, 7, 8, 0, tzinfo=timezone.utc)

    sample_value = predicted_wh / 900_000.0
    qh_minute = (now.minute // 15) * 15
    data_start = now.replace(minute=qh_minute, second=0, microsecond=0) - timedelta(minutes=15)
    sample_count = int((now - data_start).total_seconds())

    cache = EnergyCache(ttl_seconds=30)
    samples = [sample_value] * sample_count
    with cache._lock:
        cache._samples = samples
        cache._data_start = data_start
        cache._last_sample_at = now - timedelta(seconds=1)
        cache._sample_count = sample_count
        cache._last_fetch_at = now - timedelta(seconds=0)
    return cache


def _make_nbc_metrics(predicted_wh: float) -> dict:
    """Create mock NBC metrics data with predicted_wh."""
    qh_data = {}
    for i in range(4):
        if i == 0:
            qh_data[f"QH{i+1}"] = {
                "wh": predicted_wh,
                "complete": False,
                "raw_wh": predicted_wh * 0.8,
                "predicted_wh": predicted_wh,
                "samples_used": 600,
                "remaining_seconds": 300,
            }
        else:
            qh_data[f"QH{i+1}"] = {
                "wh": 100.0,
                "complete": True,
                "raw_wh": 80.0,
                "predicted_wh": 100.0,
                "samples_used": 900,
            }
    return {"devices": [{"name": "main_panel", "nbc": qh_data}]}


def _make_manager(
    telegram_sender: TelegramSender | None = None,
    dry_run: bool = False,
    predicted_wh: float = -2000.0,
    now: datetime | None = None,
) -> LoadManager:
    """Create a LoadManager with optional TelegramSender."""
    if now is None:
        now = datetime.now(timezone.utc)

    plugs = {
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="xyz789",
            power_watts=1500.0,
            priority=200,
        ),
    }
    plug_ctrl = PlugController(plugs)
    tesla_ctrl = TeslaController(
        TeslaConfig(
            client_id="test-id",
            client_secret="test-secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="vehicle-123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
        )
    )

    mgr = LoadManager(
        metrics_fetch=lambda: _make_nbc_metrics(predicted_wh),
        energy_cache=_make_energy_cache(predicted_wh, now),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=tesla_ctrl,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=dry_run,
        telegram_sender=telegram_sender,
    )

    return mgr


# =============================================================================
# Tests: __init__ accepts telegram_sender
# =============================================================================


class TestInitTelegramSender:

    def test_default_telegram_sender_is_none(self):
        """When no telegram_sender is passed, it defaults to None."""
        mgr = _make_manager(telegram_sender=None)
        assert mgr.telegram_sender is None

    def test_telegram_sender_can_be_set(self):
        """telegram_sender can be assigned after construction."""
        config = TelegramConfig(bot_token="t", chat_id="c")
        sender = TelegramSender(config)
        mgr = _make_manager(telegram_sender=sender)
        assert mgr.telegram_sender is sender

    def test_load_manager_config_accepts_telegram_sender(self):
        """LoadManagerConfig can accept a telegram_sender argument."""
        config = TelegramConfig(bot_token="t", chat_id="c")
        sender = TelegramSender(config)

        mgr = LoadManager(
            config=LoadManagerConfig(
                metrics_fetch=lambda: None,
                plug_ctrl=PlugController({}),
                tesla_ctrl=None,
                telegram_sender=sender,
            )
        )
        assert mgr.telegram_sender is sender


# =============================================================================
# Tests: _fire_telegram_notification
# =============================================================================


class TestFireTelegramNotification:

    @pytest.mark.asyncio
    async def test_noop_when_sender_is_none(self):
        """When telegram_sender is None, notification is skipped silently."""
        mgr = _make_manager(telegram_sender=None)
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[
                PendingEffect(
                    device_name="pool_pump",
                    action="turn_on",
                    timestamp=now,
                    data_point_at=now,
                    power_watts=2000.0,
                    target_amps=None,
                )
            ],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_sends_notification_on_successful_actions(self):
        """When sender is configured, whitelist matches, and actions exist, notification is sent."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=True)

        mgr = _make_manager_with_telegram_devices(
            telegram_sender=mock_sender,
            telegram_devices={"pool_pump": ["turn_on"]},
        )
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[
                PendingEffect(
                    device_name="pool_pump",
                    action="turn_on",
                    timestamp=now,
                    data_point_at=now,
                    power_watts=2000.0,
                    target_amps=None,
                )
            ],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )

        assert result is True
        mock_sender.send_notification.assert_called_once()
        event = mock_sender.send_notification.call_args[0][0]
        assert isinstance(event, NotificationEvent)
        assert event.event_type == "surplus"

    @pytest.mark.asyncio
    async def test_noop_when_sender_not_configured(self):
        """When sender exists but is not configured, notification is skipped."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = False

        mgr = _make_manager(telegram_sender=mock_sender)
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[{"device": "pool_pump", "action": "turn_on"}],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )

        assert result is False
        mock_sender.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_in_dry_run_mode(self):
        """When dry_run is True, notification is skipped even if actions exist."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=True)

        mgr = _make_manager(telegram_sender=mock_sender, dry_run=True)
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[
                PendingEffect(
                    device_name="pool_pump",
                    action="turn_on",
                    timestamp=now,
                    data_point_at=now,
                    power_watts=2000.0,
                    target_amps=None,
                )
            ],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=True,
            now=now,
        )

        assert result is False
        mock_sender.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_on_send_failure(self):
        """Returns False when the underlying send fails."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=False)

        mgr = _make_manager(telegram_sender=mock_sender)
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[
                PendingEffect(
                    device_name="pool_pump",
                    action="turn_on",
                    timestamp=now,
                    data_point_at=now,
                    power_watts=2000.0,
                    target_amps=None,
                )
            ],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )

        assert result is False


# =============================================================================
# Tests: LoadManager.close() closes telegram sender
# =============================================================================


class TestCloseTelegramSender:

    def test_close_calls_sender_close(self):
        """LoadManager.close() calls close() on the telegram sender."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.close = AsyncMock()

        mgr = _make_manager(telegram_sender=mock_sender)
        mgr.close()

        mock_sender.close.assert_called_once()

    def test_close_noop_when_sender_is_none(self):
        """LoadManager.close() is a no-op when no telegram sender is set."""
        mgr = _make_manager(telegram_sender=None)
        # Should not raise
        mgr.close()


# =============================================================================
# Tests: _cycle_async_phase queues surplus notification
# =============================================================================


class TestCycleCallsNotification:
    """Verify _cycle_async_phase queues surplus notification."""

    @pytest.mark.asyncio
    async def test_async_phase_queues_notification(self):
        """_cycle_async_phase queues surplus notification after actions."""
        mock_sender = MagicMock(spec=TelegramSender)
        mock_sender.is_configured = True

        mgr = _make_manager_with_telegram_devices(
            telegram_sender=mock_sender,
            telegram_devices={"pool_pump": ["turn_on"]},
        )

        now = datetime.now(timezone.utc)

        with (
            patch.object(mgr, "_sync_plug_states"),
            patch.object(mgr, "_fetch_tesla_state_async", return_value=(None, None, None)),
            patch.object(mgr, "_execute_action", return_value=True),
            patch("load_manager.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = await mgr._cycle_async_phase(
                gap_wh=-10000.0,
                adjusted_wh=-10000.0,
                now=now,
                seconds_remaining=600,
                dry_run=False,
            )

        # Notification should be queued (not sent async)
        assert len(mgr._pending_notifications) > 0
        event = mgr._pending_notifications[0]
        assert event.event_type == "surplus"
        # The async send should NOT have been called
        mock_sender.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_phase_skips_notification_in_dry_run(self):
        """_cycle_async_phase does not queue notification in dry-run mode."""
        mock_sender = MagicMock(spec=TelegramSender)
        mock_sender.is_configured = True

        mgr = _make_manager(telegram_sender=mock_sender, dry_run=True)

        now = datetime.now(timezone.utc)

        with (
            patch.object(mgr, "_sync_plug_states"),
            patch.object(mgr, "_fetch_tesla_state_async", return_value=(None, None, None)),
            patch.object(mgr, "_execute_action", return_value=True),
            patch("load_manager.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = await mgr._cycle_async_phase(
                gap_wh=-10000.0,
                adjusted_wh=-10000.0,
                now=now,
                seconds_remaining=600,
                dry_run=True,
            )

        # No notification should be queued in dry-run mode
        assert mgr._pending_notifications == []


# =============================================================================
# Tests: _send_pending_notifications_sync — flush queued notifications
# =============================================================================


class TestPendingNotifications:
    """Tests for deferred notification queue + flush."""

    def test_pending_notifications_flushed_sync(self):
        """_send_pending_notifications_sync drains and sends queued notifications."""
        mock_sender = MagicMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification_sync = MagicMock(return_value=True)

        mgr = _make_manager(telegram_sender=mock_sender)
        mgr._pending_notifications = [
            NotificationEvent(
                event_type="surplus",
                timestamp=datetime.now(timezone.utc),
                description="Test",
                predicted_wh=-500.0,
                target_wh=-500.0,
            ),
        ]

        mgr._send_pending_notifications_sync()

        assert mgr._pending_notifications == []
        mock_sender.send_notification_sync.assert_called_once()

    def test_pending_notifications_noop_when_empty(self):
        """_send_pending_notifications_sync is a no-op when list is empty."""
        mock_sender = MagicMock(spec=TelegramSender)
        mock_sender.is_configured = True

        mgr = _make_manager(telegram_sender=mock_sender)
        mgr._send_pending_notifications_sync()  # should not raise
        assert mgr._pending_notifications == []
        mock_sender.send_notification_sync.assert_not_called()

    def test_pending_notifications_noop_when_sender_none(self):
        """_send_pending_notifications_sync is a no-op when sender is None."""
        mgr = _make_manager(telegram_sender=None)
        mgr._pending_notifications = [
            NotificationEvent(
                event_type="surplus",
                timestamp=datetime.now(timezone.utc),
                description="Test",
                predicted_wh=-500.0,
                target_wh=-500.0,
            ),
        ]
        mgr._send_pending_notifications_sync()  # should not raise
        assert mgr._pending_notifications == []

    def test_pending_notifications_clears_even_on_error(self):
        """_send_pending_notifications_sync clears list even if send raises."""
        mock_sender = MagicMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification_sync = MagicMock(
            side_effect=RuntimeError("fail"),
        )

        mgr = _make_manager(telegram_sender=mock_sender)
        mgr._pending_notifications = [
            NotificationEvent(
                event_type="surplus",
                timestamp=datetime.now(timezone.utc),
                description="Test",
                predicted_wh=-500.0,
                target_wh=-500.0,
            ),
        ]

        mgr._send_pending_notifications_sync()  # should not raise
        assert mgr._pending_notifications == []

    def test_pending_notifications_flushed_multiple(self):
        """Multiple queued notifications are all flushed."""
        mock_sender = MagicMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification_sync = MagicMock(return_value=True)

        mgr = _make_manager(telegram_sender=mock_sender)
        now = datetime.now(timezone.utc)
        mgr._pending_notifications = [
            NotificationEvent(
                event_type="surplus",
                timestamp=now,
                description="First",
                predicted_wh=-500.0,
                target_wh=-500.0,
            ),
            NotificationEvent(
                event_type="error",
                timestamp=now,
                description="Second",
            ),
        ]

        mgr._send_pending_notifications_sync()
        assert mgr._pending_notifications == []
        assert mock_sender.send_notification_sync.call_count == 2


# =============================================================================
# Tests: telegram.devices whitelist gating
# =============================================================================


def _make_manager_with_telegram_devices(
    telegram_sender: TelegramSender | None = None,
    telegram_devices: dict[str, list[str]] | None = None,
    dry_run: bool = False,
    predicted_wh: float = -2000.0,
    now: datetime | None = None,
) -> LoadManager:
    """Create a LoadManager with a controlled telegram.devices whitelist.

    Args:
        telegram_sender: Optional injected TelegramSender.
        telegram_devices: Dict mapping device name → allowed actions, or None
            to simulate no telegram.devices config.
        dry_run: Whether load management is in dry-run mode.
        predicted_wh: NBC predicted Wh value.
        now: Current time reference.

    Returns:
        A LoadManager instance with the desired telegram.devices setting.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    plugs = {
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="xyz789",
            power_watts=1500.0,
            priority=200,
        ),
        "jackery": PlugConfig(
            name="jackery",
            accessory_id="jack01",
            power_watts=500.0,
            priority=100,
        ),
    }
    plug_ctrl = PlugController(plugs)
    tesla_ctrl = TeslaController(
        TeslaConfig(
            client_id="test-id",
            client_secret="test-secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="vehicle-123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
        )
    )

    mgr = LoadManager(
        metrics_fetch=lambda: _make_nbc_metrics(predicted_wh),
        energy_cache=_make_energy_cache(predicted_wh, now),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=tesla_ctrl,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=dry_run,
        telegram_sender=telegram_sender,
    )

    # Override the whitelist loaded during __init__ so tests have full control.
    if telegram_devices is not None:
        mgr._telegram_devices = {
            name.lower(): set(actions) for name, actions in telegram_devices.items()
        }
    else:
        mgr._telegram_devices = None

    return mgr


class TestTelegramDeviceWhitelist:
    """Tests for telegram.devices whitelist gating in _fire_telegram_notification."""

    @pytest.mark.asyncio
    async def test_no_notification_when_whitelist_not_configured(self):
        """When telegram.devices is not configured, no notification is sent."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=True)

        mgr = _make_manager_with_telegram_devices(
            telegram_sender=mock_sender, telegram_devices=None
        )
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[
                PendingEffect(
                    device_name="pool_pump",
                    action="turn_on",
                    timestamp=now,
                    data_point_at=now,
                    power_watts=1500.0,
                    target_amps=None,
                )
            ],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )

        # No whitelist configured → no notification sent
        assert result is False
        mock_sender.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_sent_when_whitelist_matches(self):
        """When whitelist is configured and action device+type matches, notification is sent."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=True)

        mgr = _make_manager_with_telegram_devices(
            telegram_sender=mock_sender,
            telegram_devices={"pool_pump": ["turn_on", "turn_off"]},
        )
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[
                PendingEffect(
                    device_name="pool_pump",
                    action="turn_on",
                    timestamp=now,
                    data_point_at=now,
                    power_watts=1500.0,
                    target_amps=None,
                )
            ],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )

        assert result is True
        mock_sender.send_notification.assert_called_once()
        event = mock_sender.send_notification.call_args[0][0]
        assert isinstance(event, NotificationEvent)
        assert event.event_type == "surplus"

    @pytest.mark.asyncio
    async def test_not_sent_when_whitelist_no_match(self):
        """When whitelist is configured but device+type does not match, no notification."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=True)

        # Whitelist only allows "pool_pump" — "jackery" is not listed
        mgr = _make_manager_with_telegram_devices(
            telegram_sender=mock_sender,
            telegram_devices={"pool_pump": ["turn_on", "turn_off"]},
        )
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[
                PendingEffect(
                    device_name="jackery",
                    action="turn_on",
                    timestamp=now,
                    data_point_at=now,
                    power_watts=500.0,
                    target_amps=None,
                )
            ],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )

        assert result is False
        mock_sender.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_sent_when_action_type_not_allowed(self):
        """When action type is not in the whitelist, no notification is sent."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=True)

        # Whitelist only allows "turn_on" for pool_pump — turn_off is not allowed
        mgr = _make_manager_with_telegram_devices(
            telegram_sender=mock_sender,
            telegram_devices={"pool_pump": ["turn_on"]},
        )
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[
                PendingEffect(
                    device_name="pool_pump",
                    action="turn_off",
                    timestamp=now,
                    data_point_at=now,
                    power_watts=1500.0,
                    target_amps=None,
                )
            ],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )

        assert result is False
        mock_sender.send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_contains_all_actions_when_sent(self):
        """When whitelist matches at least one action, notification includes all actions."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=True)

        # Whitelist allows pool_pump but not jackery
        mgr = _make_manager_with_telegram_devices(
            telegram_sender=mock_sender,
            telegram_devices={"pool_pump": ["turn_on", "turn_off"]},
        )
        now = datetime.now(timezone.utc)

        actions = [
            PendingEffect(
                device_name="pool_pump",
                action="turn_on",
                timestamp=now,
                data_point_at=now,
                power_watts=1500.0,
                target_amps=None,
            ),
            PendingEffect(
                device_name="jackery",
                action="turn_on",
                timestamp=now,
                data_point_at=now,
                power_watts=500.0,
                target_amps=None,
            ),
        ]

        result = await mgr._fire_telegram_notification(
            actions=actions,
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )

        assert result is True
        mock_sender.send_notification.assert_called_once()
        event = mock_sender.send_notification.call_args[0][0]
        # Notification must include ALL actions, not just the matching ones
        assert len(event.actions) == 2
        device_names = [a.device_name for a in event.actions]
        assert "pool_pump" in device_names
        assert "jackery" in device_names

    @pytest.mark.asyncio
    async def test_case_insensitive_device_matching(self):
        """Device name matching in whitelist is case-insensitive."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=True)

        # Whitelist uses lowercase
        mgr = _make_manager_with_telegram_devices(
            telegram_sender=mock_sender,
            telegram_devices={"POOL_PUMP": ["turn_on", "turn_off"]},
        )
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[
                PendingEffect(
                    device_name="pool_pump",
                    action="turn_on",
                    timestamp=now,
                    data_point_at=now,
                    power_watts=1500.0,
                    target_amps=None,
                )
            ],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )

        assert result is True
        mock_sender.send_notification.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_notification_when_all_actions_filtered(self):
        """When whitelist is configured but no action matches, no notification."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.is_configured = True
        mock_sender.send_notification = AsyncMock(return_value=True)

        # Whitelist is empty dict → nothing matches
        mgr = _make_manager_with_telegram_devices(
            telegram_sender=mock_sender,
            telegram_devices={},
        )
        now = datetime.now(timezone.utc)

        result = await mgr._fire_telegram_notification(
            actions=[
                PendingEffect(
                    device_name="pool_pump",
                    action="turn_on",
                    timestamp=now,
                    data_point_at=now,
                    power_watts=1500.0,
                    target_amps=None,
                )
            ],
            predicted_wh=-2000.0,
            target_wh=-500.0,
            dry_run=False,
            now=now,
        )

        assert result is False
        mock_sender.send_notification.assert_not_called()
