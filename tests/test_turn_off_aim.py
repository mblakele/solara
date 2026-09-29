"""Turn-off aims at target (not deadband edge).

Mirrors tests/test_turn_on_aim.py: with predicted=2000 / target=500 /
hysteresis=1000 and Tesla at 48 A with 900 s remaining, the old edge
budget (500 Wh) reduces to 39 A while the full-gap budget (1500 Wh)
reduces to 23 A.
"""

from datetime import datetime, timezone

from load_models import TeslaState
from load_nbc import DecideContext, GapMinder, StateTracker

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


def _tesla_ctx(state: StateTracker) -> DecideContext:
    """Build a turn-off DecideContext with Tesla charging at 48 A."""
    return DecideContext(
        now=fixed_now,
        seconds_remaining=900,
        state=state,
        plugs={},
        tesla=TeslaState(
            is_charging=True,
            current_amps=48,
            plugged_in=True,
            at_home=True,
        ),
    )


def test_turn_off_aims_at_target() -> None:
    """Default budget uses the full gap, reducing Tesla to 23 A."""
    engine = GapMinder(hysteresis_wh=1000)
    state = StateTracker()
    actions = engine.decide(
        ctx=_tesla_ctx(state),
        predicted_wh=2000.0,
        target_wh=500.0,
    )
    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    # Full-gap budget: 1500 Wh -> ceil(1500*3600/(240*900)) = 25 A drop -> 23 A.
    assert actions[0].target_amps == 23


def test_turn_off_margin_restores_edge_behavior() -> None:
    """Passing the hysteresis as turn-off margin keeps the old 39 A result."""
    engine = GapMinder(hysteresis_wh=1000, turn_off_margin_wh=1000)
    state = StateTracker()
    actions = engine.decide(
        ctx=_tesla_ctx(state),
        predicted_wh=2000.0,
        target_wh=500.0,
    )
    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    # Edge budget: 1500 - 1000 = 500 Wh -> 9 A drop -> 39 A.
    assert actions[0].target_amps == 39
