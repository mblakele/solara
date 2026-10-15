"""Incident replay: controllable-load-decomposed residual rate.

Replays the opening decisions of two quarters through the residual
prototype plus the production ``GapMinder``:

* ``bugs/2026-10-14-water-heater-overshoot.log`` ``c658`` (quarter 23:15,
  ``target_wh=-9``): ``raw_wh=74.3072625``, ``prediction_w=1.295``,
  wall-clock ``seconds_remaining=835``, all of ecoflow / water heater /
  jackery on. Production shed ecoflow + heater (residual -15.7, stop) and
  chased jackery a cycle later on the +67 blip; the quarter closed
  ``+5.09`` (miss ``+14.09``).
* ``bugs/2026-10-14-no-overshoot.log`` ``c817`` (quarter 00:15,
  ``target_wh=-9``): ``raw_wh=37.907825``, ``prediction_w=1.2635941666``,
  wall-clock ``seconds_remaining=859`` (ecoflow savings 62.0388 Wh at
  260 W), same three plugs on. Production shed ecoflow + heater
  (residual -74.7, stop); the quarter closed ``-28.65`` (miss ``-19.65``).

Hand calcs (heater 4857 W = 1.3491667 Wh/s):

* overshoot: ``74.3072625 + (1.295 - 1.3491667) * 835 = +29.08``,
  small-plug gap ``-9 - 29.08 = -38.08`` (turn_off).
* no-overshoot: ``37.907825 + (1.2635942 - 1.3491667) * 859 = -35.60``,
  small-plug gap ``+26.6`` (turn_on side: hold, both smalls already on).

Counterfactual model (first-order, documented deliberately): the replay
scores each quarter's opening belief state only. It does not simulate the
metered-data feedback of acting differently (loads left on raise later
trailing rates), so landings quoted elsewhere are booked-savings shifts,
not simulated totals. A meter-feedback harness is still required before
any production wiring.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from load_models import DeviceState, PlugConfig
from load_nbc import (
    DecideContext,
    GapMinder,
    StateTracker,
    on_sheddable_watts,
    residual_prediction_wh,
)

TARGET_WH = -9.0
HEATER_WATTS = 4857.0
SMALL_WATTS = 260.0

# bugs/2026-10-14-water-heater-overshoot.log c658.
A_RAW_WH = 74.3072625
A_PREDICTION_W = 1.295
A_SECONDS_REMAINING = 835
# bugs/2026-10-14-no-overshoot.log c817.
B_RAW_WH = 37.907825
B_PREDICTION_W = 1.2635941666666668
B_SECONDS_REMAINING = 859


def _smalls_state(all_on: bool) -> tuple[dict[str, PlugConfig], StateTracker]:
    """Smalls-only context: ecoflow + jackery, both on or both off."""
    plugs = {
        "ecoflow": PlugConfig(
            name="ecoflow", accessory_id="e1",
            power_watts=SMALL_WATTS, priority=50,
        ),
        "jackery": PlugConfig(
            name="jackery", accessory_id="j1",
            power_watts=SMALL_WATTS, priority=100,
        ),
    }
    state = StateTracker()
    for name in plugs:
        state.devices[name] = DeviceState(
            name=name, desired_state=all_on, actual_state=all_on,
            last_toggle=None,
        )
    return plugs, state


def _decide_smalls(
    plugs: dict[str, PlugConfig],
    state: StateTracker,
    predicted_wh: float,
    seconds_remaining: int,
) -> list[str]:
    """Run production decide() over small plugs; return turn_off names."""
    engine = GapMinder(hysteresis_wh=3)
    now = datetime(2026, 10, 14, 23, 16, 5, tzinfo=timezone.utc)
    actions = engine.decide(
        ctx=DecideContext(
            now=now,
            seconds_remaining=seconds_remaining,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=predicted_wh,
        target_wh=TARGET_WH,
    )
    return [a.device_name for a in actions if a.action == "turn_off"]


def test_on_sheddable_watts_collects_on_plugs() -> None:
    """ON plugs contribute rated watts; off plugs contribute nothing."""
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {
        "ecoflow": PlugConfig(
            name="ecoflow", accessory_id="e1",
            power_watts=260.0, priority=50,
        ),
        "water heater": PlugConfig(
            name="water heater", accessory_id="w1",
            power_watts=4857.0, priority=60,
        ),
        "jackery": PlugConfig(
            name="jackery", accessory_id="j1",
            power_watts=260.0, priority=100,
        ),
    }
    state.devices["ecoflow"] = DeviceState(
        name="ecoflow", desired_state=True, actual_state=True, last_toggle=None,
    )
    state.devices["water heater"] = DeviceState(
        name="water heater", desired_state=True, actual_state=True,
        last_toggle=None,
    )
    state.devices["jackery"] = DeviceState(
        name="jackery", desired_state=False, actual_state=False,
        last_toggle=None,
    )
    assert sorted(on_sheddable_watts(plugs, state)) == [260.0, 4857.0]


def test_residual_prediction_matches_hand_calc_overshoot() -> None:
    """c658 residual: +29.08 (small-plug gap -38.08, turn_off)."""
    residual = residual_prediction_wh(
        A_RAW_WH, A_PREDICTION_W, [HEATER_WATTS], A_SECONDS_REMAINING,
    )
    assert residual == pytest.approx(29.08, abs=0.01)
    assert TARGET_WH - residual == pytest.approx(-38.08, abs=0.01)


def test_residual_prediction_matches_hand_calc_no_overshoot() -> None:
    """c817 residual: -35.60 (small-plug gap +26.6, hold side)."""
    residual = residual_prediction_wh(
        B_RAW_WH, B_PREDICTION_W, [HEATER_WATTS], B_SECONDS_REMAINING,
    )
    assert residual == pytest.approx(-35.60, abs=0.01)
    assert TARGET_WH - residual == pytest.approx(26.6, abs=0.1)


def test_residual_budget_sheds_one_small_plug_marginless() -> None:
    """Margin-less residual at c658 sheds ecoflow only, like production.

    Budget 38.08 covers one 60.31 Wh small plug with residual -22.2, so
    bin-packing stops: decomposition alone does not change the c658
    outcome. Shedding both smalls upfront needs a residual margin (see
    the provisional-margin test below).
    """
    plugs, state = _smalls_state(all_on=True)
    residual = residual_prediction_wh(
        A_RAW_WH, A_PREDICTION_W, [HEATER_WATTS], A_SECONDS_REMAINING,
    )
    assert _decide_smalls(plugs, state, residual, A_SECONDS_REMAINING) == [
        "ecoflow"
    ]


def test_provisional_margin_sheds_both_smalls_overshoot() -> None:
    """A 45 Wh residual margin sheds both smalls at c658.

    Budget 38.08 + 45 = 83.08: ecoflow (60.31) leaves 22.77, so jackery
    follows (turn_off has no too-large guard). The 45 Wh figure is
    PROVISIONAL -- the observed residual-error scale across these two
    quarters, not a sized parameter. Sizing it needs the scored-anchor
    corpus (>=50 quarters per the forecast-uncertainty-action-band plan);
    do not wire this into production on two samples.
    """
    plugs, state = _smalls_state(all_on=True)
    residual = residual_prediction_wh(
        A_RAW_WH, A_PREDICTION_W, [HEATER_WATTS], A_SECONDS_REMAINING,
    )
    assert _decide_smalls(
        plugs, state, residual + 45.0, A_SECONDS_REMAINING
    ) == ["ecoflow", "jackery"]


def test_residual_holds_smalls_no_overshoot() -> None:
    """c817 residual sits on the hold side: no small-plug turn_off."""
    plugs, state = _smalls_state(all_on=True)
    residual = residual_prediction_wh(
        B_RAW_WH, B_PREDICTION_W, [HEATER_WATTS], B_SECONDS_REMAINING,
    )
    assert _decide_smalls(plugs, state, residual, B_SECONDS_REMAINING) == []


def test_post_shed_blip_with_nothing_eligible_takes_no_action() -> None:
    """With all smalls already off, the +67 blip (c661) cannot chase."""
    plugs, state = _smalls_state(all_on=False)
    assert _decide_smalls(plugs, state, 67.0340625, 720) == []
