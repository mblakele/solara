"""Turn-on aims at target (not deadband edge).

Red-phase for the 2026-09-26 decide-margin experiment: with
predicted=-32.821 / target=-9 / hysteresis=3 and a 260 W plug with
297 s remaining (capacity ~21.4 Wh), the old edge budget (20.8 Wh)
rejected the plug while the full-gap budget (23.8 Wh) accepts it.

Turn-off edge coverage moved to tests/test_turn_off_aim.py.
"""

from datetime import datetime, timezone

from load_models import PlugConfig
from load_nbc import DecideContext, GapMinder, StateTracker

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


def _ctx(state: StateTracker, plugs: dict[str, PlugConfig], seconds: int) -> DecideContext:
    """Build a turn-on DecideContext with the given remaining time."""
    return DecideContext(
        now=fixed_now,
        seconds_remaining=seconds,
        state=state,
        plugs=plugs,
        tesla=None,
    )


def test_turn_on_aims_at_target() -> None:
    """A plug fitting in the full gap but not the edge gap turns on."""
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs = {
        "jackery": PlugConfig(name="jackery", accessory_id="j1", power_watts=260.0),
    }
    actions = engine.decide(
        ctx=_ctx(state, plugs, 297),
        predicted_wh=-32.8210135,
        target_wh=-9.0,
    )
    assert len(actions) == 1
    assert actions[0].device_name == "jackery"
    assert actions[0].action == "turn_on"


def test_turn_on_margin_restores_edge_behavior() -> None:
    """Passing the hysteresis as turn-on margin keeps the old rejection."""
    engine = GapMinder(hysteresis_wh=3, turn_on_margin_wh=3)
    state = StateTracker()
    plugs = {
        "jackery": PlugConfig(name="jackery", accessory_id="j1", power_watts=260.0),
    }
    actions = engine.decide(
        ctx=_ctx(state, plugs, 297),
        predicted_wh=-32.8210135,
        target_wh=-9.0,
    )
    assert actions == []
