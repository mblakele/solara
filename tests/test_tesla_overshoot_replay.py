"""Incident replay: bugs/2026-10-01-tesla-overshoot.log, quarter 20:30-20:45.

Characterizes the shipped turn-on behavior for the 2026-10-01 overshoot
(actual ``+1.739 Wh`` vs ``target_wh=-9``, miss ``+10.7``) by stepping the
logged decision-time gaps through the production classes:
``GapTrendTracker`` + ``GapMinder.decide``.

Logged decision cycles (see the log's ``gapminder_decide`` /
``action=set_amps`` lines)::

    cycle  data point  gap Wh    seconds  amps  outcome
    c574   20:40:31    +12.732   239 s    10    no action (Tesla delta 0)
    c575   20:41:01    +28.282   209 s    10    set 12 A (+2)
    c578   20:42:01     +3.418   119 s    12    no action (Tesla delta 0)
    c579   20:43:01     +7.688   111 s    12    set 13 A (+1)
    c582   20:44:01     -1.223    29 s    13    no action (hysteresis)

Both increases ran while ``ctx.gap_trend_wh_per_s`` was ``None`` — the
adjusted-gap series alternates sign (+0.52, -0.42, +0.07, -0.15 Wh/s), so
the mixed-sign trust rule never confirms a ramp.

Counterfactual model (first-order, documented deliberately):

* The logged gap embeds the real commands' energy through the meter
  (``predicted_wh == adjusted_wh`` at every logged step — no
  pending-effect credit applied), so the no-action gap would have been
  ``gap_obs + E_obs(t)`` where ``E`` is cumulative command draw. A
  replayed variant re-scores each observed gap:
  ``gap' = gap_obs + E_obs(t) - E'(t)``.
* Command energy integrates the effect's own ``seconds_remaining`` window
  (command -> quarter end). That overstates the logged wall-time
  integration by 0.6 Wh total (209 s vs 206 s, 111 s vs 108 s) — pinned
  by ``test_command_energy_model_matches_logged_wall_times``.
* The do-nothing baseline is anchored so a status-quo replay reproduces
  the logged actual: ``DO_NOTHING = ACTUAL - sum(logged command energy)``.
  Counterfactuals are then model-vs-model deltas off that baseline.

Divergences from production, by design:

* The log starts at 20:40:04 — mid-quarter. The tracker's pre-c574 seed
  samples are not in the file, so this replay seeds fresh at c574 while
  production reported a trusted ``-0.765 Wh/s`` there. The c574 decision
  (Tesla delta 0) is unaffected by the trend either way, and every cycle
  after c574 ends up untrusted in both the replay and the log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from gap_trend import GapTrendTracker
from load_models import DeviceState, PendingEffect, PlugConfig, TeslaState
from load_nbc import DecideContext, GapMinder, StateTracker
from util import floor_to_qh

TARGET_WH = -9.0
#: qh2.raw_wh on the fetch after the boundary (log's 20:45:36 nbc_set).
ACTUAL_QH_WH = 1.7394091666666602
QH_END = datetime(2026, 10, 1, 20, 45, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class FixtureStep:
    """One logged decision cycle.

    Attributes:
        cycle: Logged cycle id (e.g. ``"c574"``).
        data_point_at: NBC data point the gap was computed from (UTC).
        gap_wh: Logged adjusted gap (target - predicted).
        seconds_remaining: Decision-time seconds left in the quarter.
        amps_before: Logged Tesla charge amps at the decision.
        wall: Logged cycle wall-clock time (UTC).
    """

    cycle: str
    data_point_at: datetime
    gap_wh: float
    seconds_remaining: int
    amps_before: int
    wall: datetime


#: c574 -> c582 from the log; every step is a fresh-data decision cycle.
FIXTURE: tuple[FixtureStep, ...] = (
    FixtureStep("c574", datetime(2026, 10, 1, 20, 40, 31, tzinfo=timezone.utc),
                12.731666250000025, 239, 10, datetime(2026, 10, 1, 20, 41, 4, tzinfo=timezone.utc)),
    FixtureStep("c575", datetime(2026, 10, 1, 20, 41, 1, tzinfo=timezone.utc),
                28.281547499999995, 209, 10, datetime(2026, 10, 1, 20, 41, 34, tzinfo=timezone.utc)),
    FixtureStep("c578", datetime(2026, 10, 1, 20, 42, 1, tzinfo=timezone.utc),
                3.4183974999999975, 119, 12, datetime(2026, 10, 1, 20, 43, 4, tzinfo=timezone.utc)),
    FixtureStep("c579", datetime(2026, 10, 1, 20, 43, 1, tzinfo=timezone.utc),
                7.68822333333334, 111, 12, datetime(2026, 10, 1, 20, 43, 12, tzinfo=timezone.utc)),
    FixtureStep("c582", datetime(2026, 10, 1, 20, 44, 1, tzinfo=timezone.utc),
                -1.223295833333328, 29, 13, datetime(2026, 10, 1, 20, 44, 34, tzinfo=timezone.utc)),
)

#: Status-quo commands as (delta watts, decision seconds): +2 A at c575,
#: +1 A at c579. Command wall time is derived from its own
#: seconds_remaining (see ``_command_wall``), mirroring the replay.
LOGGED_COMMANDS: tuple[tuple[float, int], ...] = ((480.0, 209), (240.0, 111))

#: Full-window Wh of the status-quo commands under the harness model
#: (480*209 + 240*111) / 3600 = 35.27 Wh; the log's wall-time integration
#: is 34.67 Wh, hence the -33.53 do-nothing baseline vs the -32.93
#: wall-time derivation.
DO_NOTHING_WH = ACTUAL_QH_WH - sum(p * s for p, s in LOGGED_COMMANDS) / 3600.0


@dataclass(frozen=True)
class ReplayStep:
    """One replayed decision.

    Attributes:
        cycle: Fixture cycle id being replayed.
        data_point_at: Data point fed to the trend tracker.
        gap_wh: Gap fed to ``decide`` (observed, counterfactually adjusted).
        trend_wh_per_s: Trusted trend passed in the ctx, or None.
        current_amps: Tesla amps seen by the decision.
        seconds_remaining: Decision-time seconds left in the quarter.
        actions: Actions ``GapMinder.decide`` produced.
        jitter_guard_fired: Whether the turn-on jitter guard declined.
    """

    cycle: str
    data_point_at: datetime
    gap_wh: float
    trend_wh_per_s: float | None
    current_amps: int
    seconds_remaining: int
    actions: tuple[PendingEffect, ...]
    jitter_guard_fired: bool = False


def _command_wall(seconds: int) -> datetime:
    """Wall time a command with this decision window was issued.

    Args:
        seconds: ``seconds_remaining`` at the command's decision.

    Returns:
        Quarter end minus the decision window (UTC).
    """
    return QH_END - timedelta(seconds=seconds)


def _step_wall(seconds: int) -> datetime:
    """Modeled wall time for a decision with this window.

    Args:
        seconds: ``seconds_remaining`` at the decision.

    Returns:
        Quarter end minus the decision window (UTC).
    """
    return _command_wall(seconds)


def _elapsed_wh(commands: tuple[tuple[float, int], ...], at: datetime) -> float:
    """Cumulative draw (Wh) of commands between their issue time and ``at``.

    Args:
        commands: (delta watts, decision seconds) pairs.
        at: Evaluate cumulative energy at this instant.

    Returns:
        Signed watt-hours consumed (positive = more house draw).
    """
    total = 0.0
    for power_watts, seconds in commands:
        issued = _command_wall(seconds)
        if at > issued:
            total += power_watts * (at - issued).total_seconds() / 3600.0
    return total


def _commands_wh(commands: tuple[tuple[float, int], ...]) -> float:
    """Full-window Wh of commands running until the quarter ends.

    Args:
        commands: (delta watts, decision seconds) pairs.

    Returns:
        Signed watt-hours.
    """
    return sum(p * s for p, s in commands) / 3600.0


def _landed(commands: tuple[tuple[float, int], ...]) -> float:
    """Quarter total (Wh) for a replayed command sequence.

    Args:
        commands: (delta watts, decision seconds) pairs the replay took.

    Returns:
        Do-nothing baseline plus the replayed commands' draw.
    """
    return DO_NOTHING_WH + _commands_wh(commands)


def _commands_from(steps: list[ReplayStep]) -> tuple[tuple[float, int], ...]:
    """Extract (delta watts, decision seconds) from replayed actions.

    Args:
        steps: Replay result steps.

    Returns:
        Command pairs in step order.
    """
    commands: list[tuple[float, int]] = []
    for step in steps:
        for effect in step.actions:
            if effect.action in ("set_amps", "turn_on", "turn_off"):
                commands.append((effect.power_watts, step.seconds_remaining))
    return tuple(commands)


def _replay(plumb_jitter: bool = False) -> list[ReplayStep]:
    """Run the incident sequence through the production decision path.

    Feeds each step's (counterfactually adjusted) gap to a fresh
    ``GapTrendTracker`` and calls ``GapMinder.decide`` exactly like
    ``_stage_compute_gap`` + the async phase do, tracking the replayed
    Tesla amp level so later steps see the variant's own state.

    Args:
        plumb_jitter: When True, pass the tracker's churn into the
            DecideContext — production as shipped after the jitter guard
            landed. When False (default), model production as it ran
            during the incident: no jitter value existed, so the guard
            is inapplicable and decisions reproduce the log.

    Returns:
        One ReplayStep per fixture step.
    """
    tracker = GapTrendTracker()
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    # Plug states from the log's c574 CycleDiagnostics candidates: jackery
    # and ecoflow on, water heater off; the sentinel plug never reaches the
    # engine (load_manager filters it out before building the ctx).
    state.devices["jackery"] = DeviceState(
        name="jackery", desired_state=True, actual_state=True,
    )
    state.devices["ecoflow"] = DeviceState(
        name="ecoflow", desired_state=True, actual_state=True,
    )
    state.devices["water heater"] = DeviceState(
        name="water heater", desired_state=False, actual_state=False,
    )
    plugs: dict[str, PlugConfig] = {
        "jackery": PlugConfig(name="jackery", accessory_id="jackery",
                              power_watts=260.0, priority=100),
        "ecoflow": PlugConfig(name="ecoflow", accessory_id="ecoflow",
                              power_watts=260.0, priority=50),
        "water heater": PlugConfig(name="water heater", accessory_id="water-heater",
                                   power_watts=4857.0, priority=0),
    }

    replayed: list[tuple[float, int]] = []
    amps = FIXTURE[0].amps_before
    results: list[ReplayStep] = []

    for step in FIXTURE:
        wall = _step_wall(step.seconds_remaining)
        # Re-score the observed gap for this variant's own command energy:
        # gap' = gap_obs + E_logged(t) - E_replayed(t).
        gap = (
            step.gap_wh
            + _elapsed_wh(LOGGED_COMMANDS, wall)
            - _elapsed_wh(tuple(replayed), wall)
        )
        rate, trusted = tracker.update(
            step.data_point_at, gap,
            noise_floor=engine.HYSTERESIS_WH / step.seconds_remaining,
        )
        trend = rate if trusted else None
        churn = tracker.churn_wh_per_s
        ctx = DecideContext(
            now=wall,
            seconds_remaining=step.seconds_remaining,
            state=state,
            plugs=plugs,
            tesla=TeslaState(is_charging=True, current_amps=amps,
                             plugged_in=True, at_home=True),
            data_point_at=step.data_point_at,
            gap_trend_wh_per_s=trend,
            gap_jitter_wh_per_s=(churn if plumb_jitter else None),
            cycle_secs=30,
        )
        actions = engine.decide(
            ctx, predicted_wh=TARGET_WH - gap, target_wh=TARGET_WH,
        )
        for effect in actions:
            if effect.action == "set_amps" and effect.target_amps is not None:
                replayed.append((effect.power_watts, step.seconds_remaining))
                amps = effect.target_amps
            elif effect.action == "turn_off" and effect.device_name == "tesla":
                replayed.append((effect.power_watts, step.seconds_remaining))
                amps = 0
            elif effect.action == "turn_on":
                replayed.append((effect.power_watts, step.seconds_remaining))
        results.append(
            ReplayStep(
                cycle=step.cycle,
                data_point_at=step.data_point_at,
                gap_wh=gap,
                trend_wh_per_s=trend,
                current_amps=ctx.tesla.current_amps if ctx.tesla else 0,
                seconds_remaining=step.seconds_remaining,
                actions=tuple(actions),
                jitter_guard_fired=engine.turn_on_jitter_guard_fires(
                    gap,
                    churn if plumb_jitter else None,
                    step.seconds_remaining,
                ),
            )
        )
    return results


def test_fixture_stays_within_one_quarter_hour() -> None:
    """Fixture guard: the whole incident belongs to a single QH.

    GapTrendTracker resets on a quarter-hour boundary, so a fixture that
    straddled one would silently lose its history (and with it the
    untrusted-trend behavior this file exists to characterize).
    """
    assert len({floor_to_qh(step.data_point_at) for step in FIXTURE}) == 1


def test_modeled_wall_times_match_logged_cycle_times() -> None:
    """Seconds-remaining mapping lands within 5 s of each logged cycle."""
    for step in FIXTURE:
        wall = _step_wall(step.seconds_remaining)
        assert abs((wall - step.wall).total_seconds()) <= 5.0, step.cycle


def test_status_quo_reproduces_logged_decisions() -> None:
    """Status-quo replay takes exactly the logged actions."""
    steps = _replay()
    outcomes = [
        [(e.action, e.target_amps) for e in step.actions]
        for step in steps
    ]
    assert outcomes == [
        [],                      # c574: Tesla delta 0 (threshold)
        [("set_amps", 12)],      # c575: +2 A
        [],                      # c578: Tesla delta 0
        [("set_amps", 13)],      # c579: +1 A
        [],                      # c582: hysteresis
    ]
    # Amps seen by each decision follow the logged progression.
    assert [step.current_amps for step in steps] == [10, 10, 12, 12, 13]


def test_status_quo_gap_series_is_identity() -> None:
    """Without counterfactuals the re-scoring is an identity transform.

    E_logged == E_replayed at every step, so each gap fed to decide must
    equal the logged gap exactly. Guards the gap-adjustment plumbing: a
    sign or offset error here would silently skew every counterfactual.
    """
    steps = _replay()
    for step, fixture in zip(steps, FIXTURE):
        assert step.gap_wh == pytest.approx(fixture.gap_wh), step.cycle


def test_trend_is_none_at_every_replayed_decision() -> None:
    """The mixed-sign series never confirms — matching the log.

    The log's diagnostics show ``gap_trend_wh_per_s=None`` after c574
    (c574 itself reported a trusted -0.765 from seed samples that predate
    the file; the fresh-seeded replay reports None there instead, with no
    decision impact). Both increase decisions — c575 and c579 — ran with
    no trend either way.
    """
    steps = _replay()
    assert [step.trend_wh_per_s for step in steps] == [None, None, None, None, None]


def test_status_quo_landing_matches_logged_actual() -> None:
    """Status-quo commands land the harness on the logged quarter total."""
    landed = _landed(_commands_from(_replay()))
    assert landed == pytest.approx(ACTUAL_QH_WH)
    # ... and the incident itself: target -9, actual +1.74, miss +10.7.
    assert abs(landed - TARGET_WH) > 10.0


def test_command_energy_model_matches_logged_wall_times() -> None:
    """Decision-window energy is within 1 Wh of the logged wall times.

    The log's commands ran 206 s (c575, 20:41:34 -> 20:45:00) and 108 s
    (c579, 20:43:12 -> 20:45:00): 34.67 Wh vs the harness's 35.27 Wh.
    """
    wall_energy = (480.0 * 206 + 240.0 * 108) / 3600.0
    assert abs(_commands_wh(LOGGED_COMMANDS) - wall_energy) < 1.0


def test_jitter_guarded_replay_beats_status_quo() -> None:
    """Subtask-4 gate: the shipped guard must beat the logged miss.

    With jitter plumbed (current production), the churn swing blocks the
    c579 +1 A increase — the guard fires exactly where the incident
    overshot — and the quarter lands toward the target instead of
    +1.74. Acceptance: |landed - target| < 10.7 (the logged miss).
    """
    history = _replay()
    guarded = _replay(plumb_jitter=True)

    # The first increase (c575) happens before churn is measurable — the
    # guard must only decline from the oscillation's measurable onset.
    assert guarded[1].jitter_guard_fired is False
    assert guarded[3].jitter_guard_fired is True
    # c579's +1 A is the overshooting command; only the c575 +2 A remains.
    assert _commands_from(guarded) == ((480.0, 209),)

    miss_history = abs(_landed(_commands_from(history)) - TARGET_WH)
    miss_guarded = abs(_landed(_commands_from(guarded)) - TARGET_WH)
    assert miss_guarded < 10.7
    assert miss_guarded < miss_history
