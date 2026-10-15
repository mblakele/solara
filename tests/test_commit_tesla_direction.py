"""Regression: _stage_commit must classify Tesla direction from live amps.

Reproduces bugs/2026-10-08-tesla-chargeamps.log c38: the car was at 7A
(REST-arbitrated), last_commanded_amps had correctly expired to None, and
the manager commanded 5A. Commit logged "increase None -> 5A" with
direction=increase/suppress turn_off, when the actual 7->5 change is a
decrease (suppress turn_on, post-decrease bounce-back protection).
"""

from datetime import datetime, timezone

from load_manager import LoadManager, LoadManagerConfig
from load_models import CycleContext, PendingEffect, TeslaState


def _ctx_for_decrease() -> CycleContext:
    now = datetime(2026, 10, 8, 20, 51, 22, tzinfo=timezone.utc)
    ctx = CycleContext(now=now)
    ctx.sentinel_on = False
    ctx.qh_name = "QH1"
    ctx.predicted_wh = 55.8
    ctx.adjusted_wh = 55.8
    ctx.gap_wh = -64.8
    ctx.now_postfetch = now
    ctx.seconds_remaining = 522
    ctx.data_point_at = now
    ctx.tesla_state = TeslaState(
        is_charging=True, current_amps=7, plugged_in=True, at_home=True
    )
    ctx.succeeded_effects = [
        PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=now,
            data_point_at=now,
            power_watts=-480.0,
            target_amps=5,
        )
    ]
    return ctx


def test_commit_decrease_with_expired_command_uses_live_amps():
    """A 7->5 command with last_commanded=None is a decrease, not an increase."""
    lm = LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))
    assert lm.state.last_commanded_amps is None
    ctx = _ctx_for_decrease()
    lm._stage_commit(ctx)
    eff = ctx.succeeded_effects[0]
    assert eff.direction == "decrease"
    assert eff.suppress_action == "turn_on"
    assert eff.qh_name == "QH1"
    assert lm.state.last_commanded_amps == 5


def test_commit_first_command_without_live_state_stays_increase():
    """With no prior command and no live amps, a set_amps is an increase."""
    lm = LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))
    now = datetime(2026, 10, 8, 20, 50, 5, tzinfo=timezone.utc)
    ctx = CycleContext(now=now)
    ctx.sentinel_on = False
    ctx.qh_name = "QH1"
    ctx.predicted_wh = -50.0
    ctx.adjusted_wh = -50.0
    ctx.gap_wh = 41.5
    ctx.now_postfetch = now
    ctx.seconds_remaining = 599
    ctx.data_point_at = now
    ctx.tesla_state = None
    ctx.succeeded_effects = [
        PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=now,
            data_point_at=now,
            power_watts=240.0,
            target_amps=7,
        )
    ]
    lm._stage_commit(ctx)
    eff = ctx.succeeded_effects[0]
    assert eff.direction == "increase"
    assert eff.suppress_action == "turn_off"
