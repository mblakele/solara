"""Meter-feedback replay: residual policy closed over two incident quarters.

Replays the full anchor series of quarter 23:15 from
``bugs/2026-10-14-water-heater-overshoot.log`` (actual ``+5.09``) and
quarter 00:15 from ``bugs/2026-10-14-no-overshoot.log`` (actual
``-28.65``) through a counterfactual meter model plus the proposed
policy (lumpy-first heater, small plugs on residual + margin via the
production ``GapMinder``). ``target_wh`` is -9 throughout.

Counterfactual model (first-order, documented deliberately):

* Each anchor is ``(samples_used, raw_wh, prediction_w, R_sample)`` in log
  order (duplicate data points dropped — the production tracker rejects
  repeats the same way).
* Both sides are step functions over quarter-relative time with exact
  logged command times (PendingEffect timestamps): history events are
  pinned per quarter; policy commands take effect at data-end plus the
  quarter's observed decision lag (8.63 s in A from c658, 14.41 s in B
  from c817). Same-step commands on both sides therefore produce
  exactly zero delta by construction — the interval-boundary slop of a
  fixed-step model cannot appear.
* Meter state at data-end ``T``: banked energy shifts by integrated
  ON-time divergence over ``[0, T]``; the trailing rate shifts by
  ON-time divergence over the trailing 30 s window. Landing is the
  logged actual plus integrated divergence over ``[0, 900]`` — no
  extrapolation, so history mode reproduces the actual exactly and
  improvement assertions are model-vs-model deltas off it, following the
  ``test_tesla_overshoot_replay`` convention.
* Divergences from production, by design: nameplate watts stand in for
  measured draw (duty-cycle error folds into the margin); solar-gust
  persistence is not modeled (the residual margin's job); Tesla and
  sentinel are out of scope (idle/reserved in both quarters).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from load_models import DeviceState, PlugConfig
from load_nbc import DecideContext, GapMinder, StateTracker

TARGET_WH = -9.0
HEATER_WATTS = 4857.0
SMALL_WATTS = 260.0
POWERS = {"water heater": HEATER_WATTS, "ecoflow": SMALL_WATTS, "jackery": SMALL_WATTS}
SMALLS = ("ecoflow", "jackery")
# Provisional residual margin (Wh): observed residual-error scale across
# the two quarters, NOT a sized parameter. Carried as a run argument so
# the margin=0 status-quo check and the margin=45 variant share one path.
PROVISIONAL_MARGIN_WH = 45.0

A_QH_START = datetime(2026, 10, 14, 23, 15, 0, tzinfo=timezone.utc)
A_ACTUAL_WH = 5.09
A_DECIDE_LAG = 8.63
# (t_eff seconds into quarter, plug, new ON state) from PendingEffects.
A_HIST_EVENTS: list[tuple[float, str, bool]] = [
    (68.63, "water heater", False),
    (68.63, "ecoflow", False),
    (183.21, "jackery", False),
]
# (samples_used, raw_wh, prediction_w, R_sample), unique data points.
A_ANCHORS: list[tuple[int, float, float, int]] = [
    (60, 74.31, 1.29500, 840),
    (90, 70.78, -0.11746, 810),
    (120, 67.03, -0.12498, 780),
    (150, 67.03, 0.00000, 750),
    (180, 66.81, -0.00750, 720),
    (210, 63.82, -0.09977, 690),
    (270, 57.82, -0.10000, 630),
    (300, 55.04, -0.09237, 600),
    (360, 49.64, -0.09250, 540),
    (390, 47.02, -0.08744, 510),
    (450, 41.92, -0.08250, 450),
    (480, 39.30, -0.08750, 420),
    (510, 36.66, -0.08782, 390),
    (540, 34.04, -0.08744, 360),
    (600, 29.09, -0.08250, 300),
    (630, 26.69, -0.07998, 270),
    (660, 24.29, -0.07994, 240),
    (690, 21.82, -0.08246, 210),
    (750, 17.02, -0.08000, 150),
    (780, 14.62, -0.08000, 120),
    (840, 9.96, -0.07890, 60),
    (870, 7.49, -0.08246, 30),
]

B_QH_START = datetime(2026, 10, 15, 0, 15, 0, tzinfo=timezone.utc)
B_ACTUAL_WH = -28.65
B_DECIDE_LAG = 14.41
B_HIST_EVENTS: list[tuple[float, str, bool]] = [
    (44.41, "water heater", False),
    (44.41, "ecoflow", False),
    (581.58, "ecoflow", True),
]
B_ANCHORS: list[tuple[int, float, float, int]] = [
    (30, 37.91, 1.26359, 870),
    (60, 35.88, -0.06743, 840),
    (120, 31.76, -0.08750, 780),
    (150, 28.91, -0.09497, 750),
    (180, 26.29, -0.08750, 720),
    (210, 23.41, -0.09591, 690),
    (270, 18.18, -0.08750, 630),
    (301, 15.45, -0.08786, 599),
    (330, 12.57, -0.09893, 570),
    (360, 9.95, -0.08745, 540),
    (420, 4.70, -0.08000, 480),
    (450, 2.08, -0.08742, 450),
    (480, -0.32, -0.07998, 420),
    (510, -2.59, -0.07571, 390),
    (570, -7.67, -0.08250, 330),
    (600, -8.95, -0.04251, 300),
    (630, -9.55, -0.02000, 270),
    (662, -9.39, 0.00496, 238),
    (690, -10.80, -0.04670, 210),
    (720, -13.28, -0.08256, 180),
    (750, -16.27, -0.09991, 150),
    (780, -18.52, -0.07499, 120),
    (840, -23.40, -0.07500, 60),
    (870, -26.02, -0.08740, 30),
]


def _on_at(
    events: list[tuple[float, str, bool]], name: str, t: float
) -> bool:
    """ON state of a plug at time t (initially on; midpoint-safe)."""
    state = True
    for t_eff, event_name, new_state in sorted(events):
        if t_eff <= t and event_name == name:
            state = new_state
    return state


def _on_seconds(
    events: list[tuple[float, str, bool]], name: str, t0: float, t1: float
) -> float:
    """Seconds a plug was ON over [t0, t1] (midpoint sampling)."""
    points = sorted({t0, t1} | {e[0] for e in events if t0 < e[0] < t1})
    return sum(
        b - a for a, b in zip(points, points[1:])
        if _on_at(events, name, (a + b) / 2)
    )


def _divergence_wh(
    cf_events: list[tuple[float, str, bool]],
    hist_events: list[tuple[float, str, bool]],
    t0: float,
    t1: float,
) -> float:
    """Counterfactual-minus-history energy over [t0, t1] in Wh."""
    return sum(
        POWERS[name]
        * (_on_seconds(cf_events, name, t0, t1) - _on_seconds(hist_events, name, t0, t1))
        / 3600.0
        for name in POWERS
    )


def simulate(
    anchors: list[tuple[int, float, float, int]],
    hist_events: list[tuple[float, str, bool]],
    actual_wh: float,
    qh_start: datetime,
    decide_lag: float,
    margin_wh: float,
    mode: str,
) -> tuple[list[set[tuple[str, str]]], float]:
    """Replay an anchor series under history or the residual policy.

    Args:
        anchors: Per-step ``(samples_used, raw_wh, prediction_w,
            R_sample)``.
        hist_events: Logged ``(t_eff, plug, new ON state)`` commands.
        actual_wh: Logged quarter total (history-mode landing anchor).
        qh_start: Quarter start (UTC).
        decide_lag: Data-end to actuation lag for policy commands.
        margin_wh: Residual margin added to the small-plug belief.
        mode: ``"history"`` forces logged commands; ``"residual"`` runs
            lumpy-first + residual-budget smalls via production decide().

    Returns:
        Per-step ``{(device, action)}`` sets and the landing Wh.
    """
    engine = GapMinder(hysteresis_wh=3)
    small_plugs = {
        name: PlugConfig(
            name=name, accessory_id=f"{name}-id", power_watts=SMALL_WATTS,
            priority=50 if name == "ecoflow" else 100,
        )
        for name in SMALLS
    }
    heater_rate = HEATER_WATTS / 3600.0
    policy_events: list[tuple[float, str, bool]] = []
    state = StateTracker()
    for name in SMALLS:
        state.devices[name] = DeviceState(
            name=name, desired_state=True, actual_state=True, last_toggle=None,
        )
    steps: list[set[tuple[str, str]]] = []
    landing = actual_wh
    for samples, raw_hist, w_hist, r_sample in anchors:
        t_data = float(samples)
        acted: set[tuple[str, str]] = set()
        if mode == "history":
            policy_events = list(hist_events)
        else:
            raw_cf = raw_hist + _divergence_wh(
                policy_events, hist_events, 0.0, t_data
            )
            w_cf = w_hist + _divergence_wh(
                policy_events, hist_events, max(0.0, t_data - 30.0), t_data
            ) / 30.0
            predicted_cf = raw_cf + w_cf * r_sample
            # Lumpy-first: the heater is implicated when its rated draw
            # dominates the observed rate, i.e. the forecast IS the heater.
            gap = TARGET_WH - predicted_cf
            heater_off = not _on_at(policy_events, "water heater", t_data)
            if (
                not heater_off
                and gap < -engine.HYSTERESIS_WH
                and heater_rate > 0.5 * abs(w_cf)
            ):
                policy_events.append((t_data + decide_lag, "water heater", False))
                acted.add(("water heater", "turn_off"))
                heater_off = True
            for name in SMALLS:
                on_now = _on_at(policy_events, name, t_data)
                state.devices[name].desired_state = on_now
                state.devices[name].actual_state = on_now
            # One-time net: on the step the lumpy load sheds, the metered
            # rate still carries its full draw while the remainder will
            # not, so strip exactly one rate-share. Afterwards the meter
            # has absorbed the shed and the residual IS the prediction --
            # netting again would double-subtract ~1100 Wh.
            just_shed_heater = ("water heater", "turn_off") in acted
            residual = raw_cf + (
                w_cf - (heater_rate if just_shed_heater else 0.0)
            ) * r_sample
            now = qh_start + timedelta(seconds=t_data + decide_lag)
            actions = engine.decide(
                ctx=DecideContext(
                    now=now,
                    seconds_remaining=r_sample - 6,
                    state=state,
                    plugs=small_plugs,
                    tesla=None,
                    data_point_at=qh_start + timedelta(seconds=t_data),
                ),
                predicted_wh=residual + margin_wh,
                target_wh=TARGET_WH,
            )
            for action in actions:
                policy_events.append(
                    (t_data + decide_lag, action.device_name, action.action == "turn_on")
                )
                acted.add((action.device_name, action.action))
        steps.append(acted)
    landing = actual_wh + _divergence_wh(policy_events, hist_events, 0.0, 900.0)
    return steps, landing


def test_status_quo_reproduces_quarter_a() -> None:
    """History-forced replay of A lands on the logged +5.09."""
    _, landing = simulate(
        A_ANCHORS, A_HIST_EVENTS, A_ACTUAL_WH, A_QH_START, A_DECIDE_LAG, 0.0, "history"
    )
    assert landing == pytest.approx(A_ACTUAL_WH, abs=0.5)


def test_status_quo_reproduces_quarter_b() -> None:
    """History-forced replay of B lands on the logged -28.65."""
    _, landing = simulate(
        B_ANCHORS, B_HIST_EVENTS, B_ACTUAL_WH, B_QH_START, B_DECIDE_LAG, 0.0, "history"
    )
    assert landing == pytest.approx(B_ACTUAL_WH, abs=0.5)


def test_marginless_matches_history_opening_a() -> None:
    """Residual policy at c658 sheds heater + ecoflow, like production."""
    steps, _ = simulate(
        A_ANCHORS, A_HIST_EVENTS, A_ACTUAL_WH, A_QH_START, A_DECIDE_LAG, 0.0, "residual"
    )
    assert steps[0] == {("water heater", "turn_off"), ("ecoflow", "turn_off")}


def test_marginless_matches_history_opening_b() -> None:
    """Residual policy at c817 sheds heater only, like production."""
    steps, _ = simulate(
        B_ANCHORS, B_HIST_EVENTS, B_ACTUAL_WH, B_QH_START, B_DECIDE_LAG, 0.0, "residual"
    )
    assert steps[0] == {("water heater", "turn_off")}


def test_margin45_sheds_both_smalls_at_opening_a() -> None:
    """45 Wh margin sheds jackery alongside at c658; landing ≈ -3.2."""
    steps, landing = simulate(
        A_ANCHORS, A_HIST_EVENTS, A_ACTUAL_WH,
        A_QH_START, A_DECIDE_LAG, PROVISIONAL_MARGIN_WH, "residual",
    )
    assert ("jackery", "turn_off") in steps[0]
    assert landing == pytest.approx(-3.2, abs=3.0)


def test_hold_then_confirm_in_quarter_b() -> None:
    """Ecoflow held at c817 sheds later on confirmation, landing bounded."""
    steps, landing = simulate(
        B_ANCHORS, B_HIST_EVENTS, B_ACTUAL_WH, B_QH_START, B_DECIDE_LAG, 0.0, "residual"
    )
    assert steps[0] == {("water heater", "turn_off")}
    shed_steps = [
        i for i, actions in enumerate(steps)
        if ("ecoflow", "turn_off") in actions
    ]
    assert shed_steps and shed_steps[0] > 0
    assert B_ACTUAL_WH - 2.0 <= landing <= 12.0


def test_provisional_margin_churns_quarter_b() -> None:
    """Naive symmetric margin flaps small plugs and deepens export in B.

    With +45 on the belief, step 0 sheds both smalls and the run then
    flip-flops (turn_on/turn_off jackery and ecoflow across six steps),
    landing ≈ -63 vs -28.65 history. Lesson for margin sizing (Subtask 2)
    and wiring (Subtask 4): the margin must apply to turn_off budgets
    only -- inflating turn_on gaps buys churn, not coverage. Pinned here
    so a future wiring change fails loudly if it reintroduces it.
    """
    steps, landing = simulate(
        B_ANCHORS, B_HIST_EVENTS, B_ACTUAL_WH,
        B_QH_START, B_DECIDE_LAG, PROVISIONAL_MARGIN_WH, "residual",
    )
    turn_ons = sum(
        1 for actions in steps for (_, action) in actions if action == "turn_on"
    )
    assert turn_ons >= 2
    assert landing < B_ACTUAL_WH
