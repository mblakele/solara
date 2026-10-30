"""Explicit Tesla priority against plug on/off decisions.

The legacy order is implicit: plugs win the surplus side, Tesla wins the
deficit side. These tests pin the requested explicit order when Tesla is
the highest-priority device. They use a strict priority gap (Tesla 100,
plug 0), so they do not depend on the equal-priority tie rule.
"""

from __future__ import annotations

from datetime import datetime, timezone

from load_models import DeviceState, PlugConfig, TeslaState
from load_nbc import DecideContext, GapMinder, StateTracker

NOW = datetime(2026, 10, 30, 18, 0, 0, tzinfo=timezone.utc)


def test_high_priority_tesla_sheds_plug_before_reducing_amps():
    """A low-priority plug turns off before a high-priority Tesla is trimmed.

    Tesla is at 10 A and the deficit is 100 Wh. One amp over the 600 s
    window is 40 Wh, so the legacy order would trim 3 A (120 Wh) and leave
    the plug on. With Tesla at priority 100 and the plug at 0, the plug
    sheds first (166.7 Wh) and Tesla is untouched.
    """
    engine = GapMinder(tesla_priority=100)
    state = StateTracker()
    state.devices["pool"] = DeviceState(name="pool", desired_state=True)
    plugs = {
        "pool": PlugConfig(
            name="pool",
            accessory_id="pool-1",
            power_watts=1000.0,
            priority=0,
        ),
    }
    tesla = TeslaState(
        is_charging=True,
        current_amps=10,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=NOW,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=50.0,
        target_wh=-50.0,
    )

    assert len(actions) == 1
    assert actions[0].device_name == "pool"
    assert actions[0].action == "turn_off"


def test_high_priority_tesla_increases_amps_before_turning_on_plug():
    """A high-priority Tesla takes amps before a low-priority plug turns on.

    The surplus is 460 Wh over 600 s. Tesla goes from 5 A to 16 A (11 A,
    440 Wh), which leaves 20 Wh. The 100 W lamp needs 16.7 Wh, so it still
    turns on, but only after the Tesla action.
    """
    engine = GapMinder(tesla_priority=100)
    state = StateTracker()
    plugs = {
        "lamp": PlugConfig(
            name="lamp",
            accessory_id="lamp-1",
            power_watts=100.0,
            priority=0,
        ),
    }
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=NOW,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-510.0,
        target_wh=-50.0,
    )

    assert len(actions) == 2
    assert actions[0].device_name == "tesla"
    assert actions[0].action == "set_amps"
    assert actions[0].target_amps == 16
    assert actions[1].device_name == "lamp"
    assert actions[1].action == "turn_on"
