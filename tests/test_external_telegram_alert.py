"""External plug flips raise Telegram alerts with daily runtimes.

When _sync_plug_states reconciles an external on/off flip it already
records a synthetic pending effect (NBC math + debounce) and updates the
daily runtime books via note_desired_transition. The reconciled flip must
also queue a Telegram surplus notification (whitelist-gated, like decided
actions) so externally toggled plugs alert exactly as managed ones do —
turn_off lines carrying "(MM:SS today)".
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from clock import FakeClock
from load_controllers import PlugController
from load_manager import LoadManager, LoadManagerConfig
from load_models import DeviceState, PlugConfig
from telegram import TelegramSender


def _make_mgr(now: datetime, dry_run: bool = False) -> LoadManager:
    """Build a LoadManager with stub plugs + configured Telegram sender."""
    plugs = {
        "pool_pump": PlugConfig(
            name="pool_pump", accessory_id="p1",
            power_watts=1500.0, priority=10,
        ),
    }
    mock_sender = MagicMock(spec=TelegramSender)
    mock_sender.is_configured = True
    mgr = LoadManager(LoadManagerConfig(
        plug_ctrl=PlugController(plugs),
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=dry_run,
        telegram_sender=mock_sender,
        clock=FakeClock(now),
    ))
    mgr._telegram_devices = {"pool_pump": {"turn_on", "turn_off"}}  # noqa: SLF001
    return mgr


def test_sync_returns_external_effect() -> None:
    """_sync_plug_states hands back the synthetic effect for alerting."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    mgr = _make_mgr(now)
    mgr.plug_ctrl._state["pool_pump"] = False  # type: ignore[attr-defined]
    mgr.state.devices["pool_pump"] = DeviceState(
        name="pool_pump", desired_state=True, actual_state=True,
        last_toggle=None,
    )

    external = asyncio.run(mgr._sync_plug_states())

    assert len(external) == 1
    assert external[0].device_name == "pool_pump"
    assert external[0].action == "turn_off"


def test_external_turn_off_queues_alert_with_runtime() -> None:
    """Externally stopped plug alerts with today's ON-time, like managed."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    mgr = _make_mgr(now)
    mgr.plug_ctrl._state["pool_pump"] = False  # type: ignore[attr-defined]
    mgr.state.devices["pool_pump"] = DeviceState(
        name="pool_pump", desired_state=True, actual_state=True,
        last_toggle=None, on_since=now - timedelta(seconds=372),
    )

    with patch.object(mgr, "_decide_actions", return_value=[]):
        asyncio.run(mgr._cycle_async_phase_body(
            9500.0, -10000.0, now, 600, False,
            qh_name="QH1", data_point_at=now,
        ))

    assert len(mgr._pending_notifications) == 1
    message = mgr._pending_notifications[0].format_message()
    assert "🔘 pool_pump" in message
    assert "06:12 today" in message


def test_external_turn_on_queues_alert_without_runtime() -> None:
    """Externally started plug alerts ON with no runtime suffix."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    mgr = _make_mgr(now)
    mgr.plug_ctrl._state["pool_pump"] = True  # type: ignore[attr-defined]
    mgr.state.devices["pool_pump"] = DeviceState(
        name="pool_pump", desired_state=False, actual_state=False,
        last_toggle=None,
    )

    with patch.object(mgr, "_decide_actions", return_value=[]):
        asyncio.run(mgr._cycle_async_phase_body(
            9500.0, -10000.0, now, 600, False,
            qh_name="QH1", data_point_at=now,
        ))

    assert len(mgr._pending_notifications) == 1
    message = mgr._pending_notifications[0].format_message()
    assert "🟢 pool_pump" in message
    assert "today" not in message


def test_external_flip_no_alert_without_whitelist() -> None:
    """Without a telegram.devices whitelist the external flip stays silent."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    mgr = _make_mgr(now)
    mgr._telegram_devices = None  # noqa: SLF001
    mgr.plug_ctrl._state["pool_pump"] = False  # type: ignore[attr-defined]
    mgr.state.devices["pool_pump"] = DeviceState(
        name="pool_pump", desired_state=True, actual_state=True,
        last_toggle=None,
    )

    with patch.object(mgr, "_decide_actions", return_value=[]):
        asyncio.run(mgr._cycle_async_phase_body(
            9500.0, -10000.0, now, 600, False,
            qh_name="QH1", data_point_at=now,
        ))

    assert mgr._pending_notifications == []


def test_external_flip_no_alert_in_dry_run() -> None:
    """Dry-run records no effect and queues no external alert."""
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    mgr = _make_mgr(now, dry_run=True)
    mgr.plug_ctrl._state["pool_pump"] = False  # type: ignore[attr-defined]
    mgr.state.devices["pool_pump"] = DeviceState(
        name="pool_pump", desired_state=True, actual_state=True,
        last_toggle=None,
    )

    with patch.object(mgr, "_decide_actions", return_value=[]):
        asyncio.run(mgr._cycle_async_phase_body(
            9500.0, -10000.0, now, 600, True,
            qh_name="QH1", data_point_at=now,
        ))

    assert mgr._pending_notifications == []
    assert mgr.state.snapshot_effects() == []
