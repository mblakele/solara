"""The meter's dispatch quantum as a turn-on admissibility horizon.

Production prices a load's fit over the whole remaining window
(``power * R / 3600``): a 260 W plug is 43.3 Wh at R=600 s, so a 21 Wh
surplus is declined outright even though the loop could run that plug for
one dispatch — the meter's quantization window, ~30 s — and undo it at the
next decision. ``dispatch_secs`` admits a load whose *first* dispatch fits
the surplus instead of requiring the whole window to fit.

Only the admissibility changes. The booking stays window-priced, because
:meth:`StateTracker.estimated_current_wh` credits a pending plug effect
``power * R / 3600`` Wh: that same number is what the loop sees as absorbed
on the next cycle, and it is what makes the loop undo a partial use rather
than hold it to the quarter's end. The shed side and the car's amp
economics are deliberately untouched (the unguarded turn-off path is a
documented decision; a dispatched amp step saturates into an irreversible
stop, which has no restart path within a quarter).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from load_models import DeviceState, PlugConfig, TeslaState
from load_nbc import DecideContext, GapMinder, StateTracker

NOW = datetime(2026, 10, 18, 23, 10, 0, tzinfo=timezone.utc)
TARGET_WH = -9.0

# The site's plugs: 100 = most important, so turn-on order is jackery,
# water heater, ecoflow and turn-off order is the reverse.
PLUGS = {
    "ecoflow": (260.0, 50),
    "water heater": (4857.0, 60),
    "jackery": (260.0, 100),
}


def _configs() -> dict[str, PlugConfig]:
    """The three site plugs as production configs."""
    return {
        name: PlugConfig(
            name=name, accessory_id=f"{name}-id", power_watts=watts, priority=priority
        )
        for name, (watts, priority) in PLUGS.items()
    }


def _ctx(
    seconds_remaining: int,
    *,
    on: bool = True,
    tesla: TeslaState | None = None,
) -> DecideContext:
    """Decision context with every plug in the same opening state."""
    plugs = _configs()
    state = StateTracker()
    for name in plugs:
        state.devices[name] = DeviceState(
            name=name, desired_state=on, actual_state=on, last_toggle=None
        )
    return DecideContext(
        now=NOW,
        seconds_remaining=seconds_remaining,
        state=state,
        plugs=plugs,
        tesla=tesla,
    )


def _decide(engine: GapMinder, ctx: DecideContext, gap_wh: float) -> list[object]:
    """Decide a surplus (positive) or deficit (negative) ``gap_wh``."""
    return engine.decide(
        ctx=ctx, predicted_wh=TARGET_WH - gap_wh, target_wh=TARGET_WH
    )


def _named(actions: list[object]) -> list[tuple[str, str]]:
    """(device, action) pairs, in decision order."""
    return [(a.device_name, a.action) for a in actions]  # type: ignore[attr-defined]


def test_shipped_fit_requires_the_whole_window_to_fit() -> None:
    """The shipped knob is None and prices the load over all of R.

    A 21 Wh surplus cannot hold a 260 W plug for the remaining 600 s
    (43.3 Wh), so the shipped engine declines it — the ``loads_too_large``
    no-action reason, 12-17% of cycles.
    """
    engine = GapMinder(hysteresis_wh=3)
    assert engine.dispatch_secs is None
    assert _decide(engine, _ctx(600, on=False), 21.0) == []


def test_dispatch_quantum_admits_a_load_whose_first_dispatch_fits() -> None:
    """30 s of the most important plug (2.2 Wh) fits the same surplus."""
    engine = GapMinder(hysteresis_wh=3, dispatch_secs=30)
    actions = _decide(engine, _ctx(600, on=False), 21.0)
    assert _named(actions) == [("jackery", "turn_on")]


def test_dispatch_quantum_booking_stays_window_priced() -> None:
    """The running debit is the held cost, so one command still ends the loop.

    Jackery's 30 s (2.2 Wh) admits it, but the debit is the whole-window
    43.3 Wh the pending-effect estimate will credit: the 100 Wh surplus is
    down to 56.7 Wh, the heater's first dispatch (40.5 Wh) fits, and its
    809.5 Wh debit closes the budget. A dispatch-priced debit would also
    admit ecoflow — three actions, not two.
    """
    engine = GapMinder(hysteresis_wh=3, dispatch_secs=30)
    actions = _decide(engine, _ctx(600, on=False), 100.0)
    assert _named(actions) == [
        ("jackery", "turn_on"),
        ("water heater", "turn_on"),
    ]


def test_dispatch_quantum_is_capped_by_the_remaining_window() -> None:
    """A command cannot be held longer than the quarter has left.

    At R=30 s the 60 s horizon is capped to 30 (2.2 Wh) and the plug is
    admitted; the same 3 Wh surplus at R=600 s prices 60 s (4.3 Wh) and
    declines it.
    """
    engine = GapMinder(hysteresis_wh=0, dispatch_secs=60)
    assert _named(_decide(engine, _ctx(30, on=False), 3.0)) == [
        ("jackery", "turn_on")
    ]
    assert _decide(engine, _ctx(600, on=False), 3.0) == []


def test_dispatch_quantum_leaves_the_shed_side_alone() -> None:
    """The turn-off path is unguarded by design: same shed, same booking.

    A 14 Wh deficit sheds the least important plug for the whole window in
    both engines — dispatch pricing must not add an overshoot guard to a
    path whose acceptance criterion is the reversal rate (plan §5.2).
    """
    shipped = _decide(GapMinder(hysteresis_wh=3), _ctx(600), -14.0)
    dispatched = _decide(
        GapMinder(hysteresis_wh=3, dispatch_secs=30), _ctx(600), -14.0
    )
    assert _named(shipped) == _named(dispatched) == [("ecoflow", "turn_off")]


def test_dispatch_quantum_leaves_the_car_alone() -> None:
    """The car's amp economics stay window-priced.

    A 21 Wh surplus buys 21/(240*600/3600) = 0.5 A — under one amp in both
    engines, so neither commands the car. The car's own quantum (1 A = 40 Wh
    over the window, 2 Wh per dispatch) is a separate axis because a
    dispatch-priced amp step saturates into an irreversible stop.
    """
    tesla = TeslaState(
        is_charging=True, current_amps=10, plugged_in=True, at_home=True
    )
    shipped = _decide(GapMinder(hysteresis_wh=3), _ctx(600, tesla=tesla), 21.0)
    dispatched = _decide(
        GapMinder(hysteresis_wh=3, dispatch_secs=30), _ctx(600, tesla=tesla), 21.0
    )
    assert _named(shipped) == _named(dispatched) == []


@pytest.mark.parametrize("bad", [0, -1, -30])
def test_non_positive_dispatch_quantum_is_rejected(bad: int) -> None:
    """A zero-second horizon would admit every load unconditionally."""
    with pytest.raises(ValueError):
        GapMinder(dispatch_secs=bad)
