"""StateTracker transition writers for the plug lifecycle.

The writers own the stored facts. The derived ``PlugState`` is a projection
over those facts, so each transition is checked through both.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from load_nbc import StateTracker
from load_models import DeviceState, PlugState

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _tracker(**state: object) -> StateTracker:
    tracker = StateTracker()
    tracker.set_device_state(
        "heater", DeviceState(name="heater", **state)  # type: ignore[arg-type]
    )
    return tracker


def test_command_from_off_is_pending_on() -> None:
    """A turn-on command moves OFF to PENDING_ON and starts the window."""
    tracker = _tracker(desired_state=False, actual_state=False)
    state = tracker.record_command("heater", True, NOW)
    assert state is PlugState.PENDING_ON
    dev = tracker.devices["heater"]
    assert dev.desired_state is True
    assert dev.last_toggle == NOW


def test_observation_confirms_pending_on() -> None:
    """The controller reporting on confirms PENDING_ON to ON."""
    tracker = _tracker(desired_state=True, actual_state=False)
    state = tracker.record_observation("heater", True)
    assert state is PlugState.ON
    assert tracker.devices["heater"].actual_state is True


def test_observation_does_not_move_last_toggle() -> None:
    """Only a command or an external adoption starts the debounce window."""
    tracker = _tracker(
        desired_state=True, actual_state=False, last_toggle=NOW
    )
    tracker.record_observation("heater", True)
    assert tracker.devices["heater"].last_toggle == NOW


def test_external_turn_on_adopts_to_on() -> None:
    """Sync adopts an external flip; the end state is ON."""
    tracker = _tracker(desired_state=False, actual_state=False)
    tracker.record_observation("heater", True)
    assert tracker.note_desired_transition("heater", True, NOW) is True
    tracker.record_toggle("heater", NOW)
    assert tracker.plug_states()["heater"] is PlugState.ON
    assert tracker.devices["heater"].last_toggle == NOW


def test_command_from_on_is_pending_off() -> None:
    """A turn-off command moves ON to PENDING_OFF and starts the window."""
    tracker = _tracker(desired_state=True, actual_state=True)
    state = tracker.record_command("heater", False, NOW)
    assert state is PlugState.PENDING_OFF
    assert tracker.devices["heater"].last_toggle == NOW


def test_observation_confirms_pending_off() -> None:
    """The controller reporting off confirms PENDING_OFF to OFF."""
    tracker = _tracker(desired_state=False, actual_state=True)
    state = tracker.record_observation("heater", False)
    assert state is PlugState.OFF


def test_repeat_command_does_not_move_toggle() -> None:
    """A command that changes nothing does not restart the debounce window."""
    earlier = NOW - timedelta(seconds=120)
    tracker = _tracker(
        desired_state=True, actual_state=True, last_toggle=earlier
    )
    state = tracker.record_command("heater", True, NOW)
    assert state is PlugState.ON
    assert tracker.devices["heater"].last_toggle == earlier


def test_command_off_credits_daily_runtime() -> None:
    """The wrapper keeps note_desired_transition's runtime bookkeeping."""
    earlier = NOW - timedelta(seconds=120)
    tracker = _tracker(
        desired_state=True,
        actual_state=True,
        on_since=earlier,
        runtime_day=NOW.date(),
    )
    tracker.record_command("heater", False, NOW)
    dev = tracker.devices["heater"]
    assert dev.runtime_today_secs == pytest.approx(120.0)
    assert dev.on_since is None


def test_observation_on_unknown_device_creates_it() -> None:
    """A first observation is stored without inventing a desired state."""
    tracker = StateTracker()
    state = tracker.record_observation("heater", False)
    assert state is PlugState.OFF
    dev = tracker.devices["heater"]
    assert dev.desired_state is None
    assert dev.actual_state is False
