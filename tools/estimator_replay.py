"""Closed-loop replay of the load manager's decisions over production logs.

Offline-only analysis: no production module imports anything here. The
forecast scorer (``forecast_log_scoring``) measures estimator error in the
*open* loop; this tool drives an estimator through production
``GapMinder.decide()`` against a counterfactual meter, so the quantities
that decide shipping — the quarter's miss and the actuator churn — are
measured with the loop closed.

Method: each anchor is a step over quarter-relative time, the quarter's
logged plug lines are history, policy commands land at the data end plus
the quarter's observed decision lag, and the landing is the logged actual
plus the integrated divergence over the quarter. Only ``rate_fn`` differs
between runs, so a variant is judged on the same decisions production would
have made with a different rate (``replay``/``rate_*``; the estimator
formulas are pinned against the scorer's ``estimate_prior_only`` /
``estimate_prior_blend`` by test).

The other axis is the decision policy itself (``Policy``), expressed only
through existing production surface — ``GapMinder``'s constructor:
``--turn-on-margin``, ``--turn-off-margin`` (whole Wh withheld from each
budget, the deliberate form of the hedge the loop currently gets by accident),
``--lumpy-first`` (production's ``LOAD_MANAGE_LUMPY_FIRST``), ``--amp-floor``
(round a Tesla reduction down to the amps the gap affords instead of up to the
amps it might need) and ``--dispatch-secs`` (price a starting load over one
meter dispatch instead of the whole remaining window). A candidate that wins
on this axis has a shipping path that is a flag, not a rewrite.

Fidelity note: ``DecideContext`` carries what the parsed anchors hold
(``banked_wh``, the quarter's accumulated raw energy, so the lumpy-first
gate's trailing-rate implication works). Whether the two *derived* gap
signals ride along is the guard arm (``--guards``):

* ``inert`` (the default) leaves ``gap_jitter_wh_per_s`` and
  ``gap_trend_wh_per_s`` dark — the machine every number in this docstring
  was measured on. The turn-on jitter guard, its banked-cover override and
  the ramp-aware Tesla stop cannot fire, so a policy whose whole job is
  suppressing actions (a margin) overlaps with the guard's job and its
  numbers are read as "outside the guard".
* ``live`` feeds one :class:`GapTrendTracker` per quarter in production's
  own order and inputs (``_stage_compute_gap``): this cycle's adjusted gap,
  the ``HYSTERESIS_WH / seconds_remaining`` noise floor, churn withheld
  until ``JITTER_MAX_REMAINING_SECS`` of the quarter has observed data, then
  ``churn_wh_per_s`` handed to ``gap_jitter_wh_per_s`` unconditionally
  (0.0 until measurable, exactly as production passes it) and the slope to
  ``gap_trend_wh_per_s`` when the tracker trusts it. The same
  ``predicted_wh`` that the tracker's gap is derived from is the value
  ``decide()`` is handed. One deviation, stated rather than hidden:
  production's tracker sees the *published* prediction minus its own
  pending-effect deltas, while the replay's gap reflects the counterfactual
  meter — its actions are already realized in ``raw_cf``/``w_cf`` and it
  tracks no pending effects — so the two churn series agree in shape, not
  sample for sample. The live arm is therefore a different machine: its
  absolute misses are read against the inert control the report runs
  alongside it, never against the pinned verdicts above, which are inert by
  construction.
* ``churn`` feeds the jitter signal but *not* the trend. The turn-on guard
  and the ramp rule read different context fields, so this is the control
  that separates them: ``churn`` minus ``inert`` is the jitter guard's own
  effect on the closed loop and ``live`` minus ``churn`` is the ramp rule's,
  which is how the pre-registered deletion rule for the ramp rule (zero
  production fires) gets a closed-loop half instead of an unattributable
  bundle.

The meter has two actuator populations. The plugs are ON/OFF steps at
nameplate watts. The car is a commanded-amps step: each cycle's *reported*
amps (``TeslaSample``) are its draw until the next one, and a policy
``set_amps``/Tesla ``turn_off`` replaces that step — ``nominal_voltage()``
watts per amp, the same conversion ``GapMinder`` prices the car with, so one
amp over the remaining window is worth ``240 * R / 3600`` Wh (the quantum the
2026-10-17 overshoots are made of). ``plugged_in``/``at_home`` ride along
from the log because production gates both charging directions on them, so
the counterfactual cannot charge an absent car — and in the idle-car
quarters it never commands the car at all, which keeps the plugs-only
baseline reproducible (see ``--regime``).

Quarter ingestion is deliberately conservative:

* quarters with no cycle that reports a Tesla state at all are dropped
  (``tesla_state=None`` means no state was read — the cycle either exited
  before the async phase or found the car unavailable: unknown, not idle);
* the opening ON/OFF state of each plug comes from the quarter's first
  candidate dump, back-inferred through any transition logged before it —
  assuming "all on" makes the policy shed loads that were never drawing,
  a nameplate-sized phantom divergence (heater: 4,857 W * R / 3600). The
  car's opening amps are back-inferred the same way, from the first sample;
* ``--regime {all,tesla,plug-only}`` splits the report by which actuators
  were in play: ``tesla`` is the quarters where the car drew or was
  commanded (the population the plugs-only model refused), ``plug-only`` the
  rest. Neither is a subset of the old model's, but the plug-only half
  measures identically because the policy never commands a car that is not
  drawing (0 of 1,269 corpus Tesla commands fall in it).

Nameplate watts stand in for measured draw, so absolute landings are model
values; the deltas between runs are the result.

Corpus verdict (13 logs, Oct 4-17 2026, 1,146 replayable quarters of 1,294
scored, of which 117 car-active): the shipped trailing window beats every
smoother variant closed-loop in *both* regimes, inverting the open-loop
ranking. Plug-only (1,029 quarters, the earlier baseline reproduced exactly):
status-quo 23.41 mean |miss| Wh against prior 27.71, prior-blend@60 26.26,
trend-gated 24.11. Car-active (117 quarters, 1,269 of the corpus's Tesla
commands): status-quo 17.99 against prior 31.99, prior-blend@60 19.31,
trend-gated 24.15. Every variant still roughly halves churn there
(15.73 -> 9.65-11.97 actions/quarter). No estimator variant shipped; the
single-log counter-example (2026-10-17, where prior-blend@60 beats
status-quo 6.88 to 10.54 on four car-active quarters) does not survive the
corpus.

Policy sweep verdict (closed loop, status-quo rate, both regimes): the
shipped zero-margin policy is the best or tied on mean |miss| everywhere, so
no margin and no ordering ships. Plug-only: 23.41 at margin 0, 23.41 at 10,
24.80 at 40, 26.85 at 60; the turn-on margin is worse still (25.64 at 10,
31.98 at 40). Car-active: 17.99 at 0 against 19.62 at 10 and 39.48 at 40. The
plug regime's mean signed miss (-4.87 Wh) is real bias — a 60 Wh turn-off
margin zeroes it (-0.00) — but the mean |miss| gets *worse*, so the
over-shedding is not waste: it hedges the forecast's own variance, and |miss|
punishes the hedge's removal. ``lumpy_first`` fires but nets out (plug-only
23.51, car-active 17.88, 2026-10-17 unchanged): it reorders sheds without
changing what the quarter books. The 2026-10-17 log disagrees with the corpus
again (turn-on margin 20 there: 4.78 vs 10.54 on its four car quarters,
23.40 vs 17.99 on the car corpus) — the same one-log trap the estimator line
fell into. The granularity cure therefore has to beat the shipped policy
*without* shedding less on average, which is what the quantum-aware form
(floor the amp step, time-box the shed) claims and a scalar budget cannot.

Amp-floor verdict (``--amp-floor``, car-active only — the plug-only regime is
bit-identical, as it must be with no car commanded): it *loses*, and the
mechanism is the point. Car-active 19.34 mean |miss| against 17.99 shipped,
with churn 19.30 against 15.73 actions/quarter and reversals 5.67 against
2.09 — and on 2026-10-17 itself 11.55 against 10.54 (worst miss 18.52 against
17.77). The floor's premise was that the residue gets closed on a later, finer
step: it does not, because the quantum is R-scaled and R shrinks ~30 s per
cycle, so an amp is 40.0 Wh at R=600 s and still 38.0 Wh one cycle later; the
quantum only falls below a 20 Wh gap once R < 300 s, two-thirds of the way
through the quarter. For most of a quarter no finer step exists, so declining
the step only routes the deficit to the plugs (hence +3.5 actions and +3.6
reversals — nearly one extra duty-cycle per declined step) while the worst
quarter is untouched (its 287 Wh miss is not amp-quantum-driven).

The duration form is closed as well, by the meter rather than by a measurement:
the minimum dispatch time IS the reporting quantization window (typically 30 s,
also the settle window; ``MIN_SECONDS_TO_ACT`` gates the tail), so a
minimum-length dispatch is one full observation step and the loop cannot
confirm it before re-deciding. A *scheduled* restore would be open-loop exactly
where the closed loop is blind — strictly worse than today, where the loop's
re-decide already performs the restore and the reversal is that mechanism
(2.09 reversals/quarter shipped, median 136 s ≈ 4-5 dispatch windows). The
dispatchable unit is ``power * quantization / 3600`` (2.2 Wh per 260 W plug,
2.0 Wh per amp, 40.5 Wh per heater run) — not the R-scaled 43.3 Wh production
books for that plug at R=600 s. The booking describes a duration the actuator
is never asked to hold; whether that mismatch feeds the over-shedding is the
open measurable question — its admissibility half is the candidate below (a
wash), the booking half is still open.

Dispatch-quantum verdict (``--dispatch-secs SECS``, the open question above
answered as a candidate): the *admissibility* horizon is the one place the
R-scaled belief forbids a real action — a starting load must fit the surplus
with its whole-window cost (43.3 Wh for a 260 W plug at R=600 s), which is
production's ``loads_too_large`` no-action reason (12-17% of worst-decile
cycles). ``--dispatch-secs`` admits a load whose *first* dispatch fits
instead, keeping the running debit and the pending-effect estimate
window-priced (that credit is what makes the loop undo a partial use). It is
a variance trade, not the resolution gain it looked like, and it nets out:
plug-only 23.41 -> 19.29 at 60 s, but **238 quarters improve and 160 get
worse** (median delta exactly 0.00, 61% untouched) — the mean is carried by
five rescues of 100-268 Wh each, the 400 Wh surpluses the whole-window fit
test declines outright. Car-active regresses the other way (17.99 -> 24.66 at
60 s; 30 improve, 73 worsen, median +2.66), and 2026-10-17 agrees (10.54 ->
23.84). The all-regime number only improves because 90% of the corpus is
plug-only. Best value is 60 s, the *toggle* debounce rather than the meter's
30 s quantum: 10 s (every load always fits) is worst in both regimes
(plug-only 31.41, car-active 45.72), and 300 s is the coarsest that still
helps the car side (17.10 against 17.99). So the whole-window fit test is not
a resolution defect but a conservative prior about whether a small predicted
surplus materializes — the same hedge, on the surplus side, that the turn-off
margin sweep found on the shed side. No ship.

Run from the repo root::

    uv run python tools/estimator_replay.py bugs/*.log
    uv run python tools/estimator_replay.py corpus/*.log --variant prior
    uv run python tools/estimator_replay.py corpus/*.log --regime tesla
    uv run python tools/estimator_replay.py corpus/*.log --date 2026-10-14
    uv run python tools/estimator_replay.py corpus/*.log --turn-off-margin 40
    uv run python tools/estimator_replay.py corpus/*.log --amp-floor --regime tesla
    uv run python tools/estimator_replay.py corpus/*.log --dispatch-secs 60
    uv run python tools/estimator_replay.py corpus/*.log --guards live
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping, Sequence

_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = Path(__file__).resolve().parent
# Both roots go on the path: the repo root for the production modules, and
# the tools directory so ``forecast_log_scoring`` resolves to one module
# whether this file runs as a script or is imported as
# ``tools.estimator_replay`` (the pattern divergence_audit.py already uses).
for _path in (str(_ROOT), str(_TOOLS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from forecast_log_scoring import (  # noqa: E402 # pylint: disable=wrong-import-position
    TURN_OFF_REVERSAL_WINDOW_SECS,
    CycleStateSnapshot,
    ForecastAnchor,
    TurnOffDecision,
    TurnOnRecord,
    _iter_log_texts,
    parse_cycle_states,
    parse_log,
    parse_turn_offs,
    parse_turn_ons,
)
from constants import (  # noqa: E402 # pylint: disable=wrong-import-position
    JITTER_MAX_REMAINING_SECS,
)
from gap_trend import GapTrendTracker  # noqa: E402 # pylint: disable=wrong-import-position
from load_models import (  # noqa: E402 # pylint: disable=wrong-import-position
    DeviceState,
    PendingEffect,
    PlugConfig,
    TeslaState,
)
from load_nbc import (  # noqa: E402 # pylint: disable=wrong-import-position
    DecideContext,
    GapMinder,
    StateTracker,
    nominal_voltage,
)
from util import floor_to_qh  # noqa: E402 # pylint: disable=wrong-import-position

TARGET_WH = -9.0
"""Per-quarter target the load manager aims at (Wh)."""

POWERS: dict[str, float] = {"water heater": 4857.0, "ecoflow": 260.0, "jackery": 260.0}
"""Nameplate watts per modelled plug, as the site's candidates report them."""

PRIORITIES: dict[str, int] = {"ecoflow": 50, "water heater": 60, "jackery": 100}
"""Turn-off order (lowest priority first), from this site's devices.json."""

HYSTERESIS_WH = 3
"""Production's deadband at this site (|target_wh| / 3, integral)."""

GUARD_MODES: tuple[str, ...] = ("inert", "churn", "live")
"""The guard arms — which derived gap signals reach ``decide()``.

``inert`` leaves both dark (the historical harness), ``churn`` feeds only the
jitter signal (the turn-on guard and its banked-cover override, with the ramp
rule's trend still dark), and ``live`` feeds both, as production does. The
jitter guard and the ramp rule read *different* context fields, so ``churn``
is the control that isolates the ramp rule: ``live`` minus ``churn`` is its
effect, and ``churn`` minus ``inert`` is the guard's own.
"""

DEFAULT_TREND_FLOOR_WH = 3.0
"""Trend-gated trust floor, mirroring production ``HYSTERESIS_WH``."""

MIN_ANCHORS_PER_QUARTER = 5
"""Fewer anchors than this cannot describe a quarter's trajectory."""

LAG_MIN_SECS = -5.0
"""A decision may precede the sample it read, barely."""

LAG_MAX_SECS = 90.0
"""Beyond this the fetch line and the data point are not one cycle."""

QUARTER_SECS = 900.0
"""Length of a quarter-hour, the integration window of every landing."""

RateFn = Callable[[float, ForecastAnchor], float]
"""Estimator under test: counterfactual window rate in, rate out."""


@dataclass(frozen=True)
class TeslaSample:
    """The car's reported draw at one instant of the quarter.

    The Tesla leg of the meter is the reported amps as a step function, the
    way a plug is its ON/OFF step function: one sample per cycle that
    reported a state, held until the next. ``plugged_in``/``at_home`` ride
    along because production gates both charging directions on them, so a
    counterfactual that dropped them would let the policy charge a car that
    is not there.
    """

    t: float
    """Seconds after the quarter start."""
    amps: float
    """Reported charge amps at ``t``."""
    plugged_in: bool
    """Whether the car was plugged in at ``t``."""
    at_home: bool
    """Whether the car was at home at ``t``."""


@dataclass(frozen=True)
class Quarter:
    """One replayable quarter: its anchor series and its logged history."""

    name: str
    qh_start: datetime
    actual_wh: float
    decide_lag: float
    anchors: tuple[ForecastAnchor, ...]
    events: tuple[tuple[float, str, bool], ...]
    initial_on: dict[str, bool] | None = None
    """ON state of each modelled plug at ``t = 0``; None means all on."""
    tesla_samples: tuple[TeslaSample, ...] = ()
    """The car's reported draw, in time order; empty means no car modelled.

    The first sample is also the open (back-inferred flat), the same
    back-inference ``opening_states`` applies to a plug.
    """
    car_active: bool = False
    """Whether the car drew power or took a command somewhere in the quarter.

    The regime discriminator for :func:`select_regime`: true is the
    population the plugs-only model used to refuse, false the one it used to
    replay — though neither is reproducible by the old model now that the car
    is commandable in every replayable quarter.
    """


@dataclass(frozen=True)
class GuardActivity:
    """What the guard arm's suppressing mechanisms did over a run.

    Counted with production's own public predicates (the guard's fire test
    and the bank override's cap), not by diffing action lists, so the numbers
    are the same events production logs as ``gapminder_jitter_guard`` and
    ``gapminder_banked_override``. All zeroes in the inert arm by
    construction: with no tracker there is no churn, no trend, and nothing
    for either mechanism to act on.

    ``trusted_trend_cycles`` counts the cycles whose slope cleared the
    tracker's trust rule — the ramp-aware Tesla stop can only hasten inside
    one of them, so it is that rule's reachability, not its firing.
    """

    declines: int = 0
    rescues: int = 0
    trusted_trend_cycles: int = 0


@dataclass(frozen=True)
class Replay:
    """One closed-loop run: what it decided and where it landed."""

    steps: tuple[tuple[tuple[float, str, str], ...], ...]
    landing_wh: float
    guard: GuardActivity = GuardActivity()

    @property
    def actions(self) -> tuple[tuple[str, str], ...]:
        """Every action taken, in step order, as ``(device, action)``."""
        return tuple(
            (device, action) for step in self.steps for _at, device, action in step
        )

    @property
    def action_count(self) -> int:
        """How many actuator commands the run issued."""
        return len(self.actions)

    @property
    def reversals(self) -> int:
        """Plug sheds a later turn-on of the same plug undid.

        Uses the log-side :data:`TURN_OFF_REVERSAL_WINDOW_SECS` window,
        so this is the same reversal currency the scorer's turn-off
        collection reports and the two numbers are comparable.
        """
        count = 0
        for step in self.steps:
            for shed_at, device, action in step:
                if action != "turn_off":
                    continue
                if any(
                    later_at - shed_at <= TURN_OFF_REVERSAL_WINDOW_SECS
                    for later in self.steps
                    for later_at, later_device, later_action in later
                    if later_device == device
                    and later_action == "turn_on"
                    and later_at >= shed_at
                ):
                    count += 1
        return count


# ── the decision policy ──────────────────────────────────────────────


@dataclass(frozen=True)
class Policy:
    """The decision-side knobs a run is measured under.

    The replay has two axes: the rate estimator (``rate_*``) and this — the
    policy the decisions are taken with. Every field is existing production
    surface (``GapMinder``'s constructor), so a candidate that wins here has
    a shipping path that is a flag rather than a rewrite, and the defaults
    reproduce the shipped policy exactly (which is what keeps the pinned
    quarter verdicts above from moving).

    The margins are the deliberate hedge the loop currently gets by accident:
    a shed books ``power * R / 3600`` Wh whether or not the gap needed that
    much, so a coarse load overshoots small gaps (the 2026-10-17 census: 8 of
    14 sheds, 131.5 Wh). Withholding a margin from the budget is the cheap
    way to decline those; it cannot see the load's quantum, which is what a
    quantum-aware rule would add.

    ``dispatch_secs`` is the other end of the same question: the *fit* test
    on a starting load. Shipped, a load must fit the surplus with its whole
    window's cost; dispatched, one dispatch is enough to start it, and the
    loop's next decision is the undo. Measured, it is a variance trade, not
    resolution (see the module docstring's verdict).
    """

    turn_on_margin_wh: int = 0
    """Wh withheld from the turn-on budget (0 aims at the target)."""
    turn_off_margin_wh: int = 0
    """Wh withheld from the turn-off budget (0 aims at the target)."""
    lumpy_first: bool = False
    """Shed an implicated lumpy load before the smalls (production flag)."""
    amp_floor: bool = False
    """Round a Tesla reduction down to the amps the gap affords, not up."""
    dispatch_secs: int | None = None
    """Pricing horizon for a *starting* load: one dispatch, or the window."""
    defer_stop: bool = True
    """Whether to defer stopping at minimum amps while a safe buffer remains
    (shipped). False stops immediately at minimum amps, relaxing the defer
    constraint and making the ramp rule unreachable."""
    max_defer_secs: int | None = None
    """Cap on the safe defer window in seconds (shipped 120). None keeps
    the shipped cap. Lower values stop sooner while a buffer remains."""
    ramp_aware: bool = False
    """Enable the ramp-aware Tesla stop hastener (shipped off). When on, a
    trusted rising deficit pulls the stop one cycle earlier."""

    @property
    def label(self) -> str:
        """One self-describing line, for the report header."""
        horizon = (
            "whole window" if self.dispatch_secs is None else f"{self.dispatch_secs} s"
        )
        return (
            f"turn-on margin {self.turn_on_margin_wh:g} Wh, "
            f"turn-off margin {self.turn_off_margin_wh:g} Wh, "
            f"lumpy-first {'on' if self.lumpy_first else 'off'}, "
            f"amp floor {'on' if self.amp_floor else 'off'}, "
            f"defer {'on' if self.defer_stop else 'off'}, "
            f"defer cap {self.max_defer_secs if self.max_defer_secs is not None else 120} s, "
            f"ramp-aware {'on' if self.ramp_aware else 'off'}, "
            f"dispatch {horizon}"
        )

    def engine(self) -> GapMinder:
        """A ``GapMinder`` constructed with this policy's knobs."""
        return GapMinder(
            hysteresis_wh=HYSTERESIS_WH,
            turn_on_margin_wh=self.turn_on_margin_wh,
            turn_off_margin_wh=self.turn_off_margin_wh,
            lumpy_first=self.lumpy_first,
            amp_floor=self.amp_floor,
            dispatch_secs=self.dispatch_secs,
            defer_stop=self.defer_stop,
            max_defer_secs=self.max_defer_secs,
            ramp_aware=self.ramp_aware,
        )


# ── the meter model ──────────────────────────────────────────────────


def _on_at(
    events: Sequence[tuple[float, str, bool]], name: str, t: float, initial: bool = True
) -> bool:
    """ON state of a plug at time ``t``; midpoint-safe."""
    state = initial
    for t_eff, event_name, new_state in sorted(events):
        if t_eff <= t and event_name == name:
            state = new_state
    return state


def _initial(initial_on: dict[str, bool] | None, name: str) -> bool:
    """ON state at ``t = 0`` for one plug (all-on unless told otherwise)."""
    if initial_on is None:
        return True
    return initial_on.get(name, True)


def _on_seconds(
    events: Sequence[tuple[float, str, bool]],
    name: str,
    t0: float,
    t1: float,
    initial_on: dict[str, bool] | None = None,
) -> float:
    """Seconds a plug was ON over ``[t0, t1]`` (midpoint sampling)."""
    points = sorted({t0, t1} | {event[0] for event in events if t0 < event[0] < t1})
    initial = _initial(initial_on, name)
    return sum(
        b - a
        for a, b in zip(points, points[1:])
        if _on_at(events, name, (a + b) / 2, initial)
    )


def _divergence_wh(
    counterfactual: Sequence[tuple[float, str, bool]],
    history: Sequence[tuple[float, str, bool]],
    t0: float,
    t1: float,
    initial_on: dict[str, bool] | None = None,
) -> float:
    """Counterfactual-minus-history plug energy over ``[t0, t1]`` in Wh."""
    return sum(
        watts
        * (
            _on_seconds(counterfactual, name, t0, t1, initial_on)
            - _on_seconds(history, name, t0, t1, initial_on)
        )
        / 3600.0
        for name, watts in POWERS.items()
    )


def _amps_at(samples: Sequence[TeslaSample], t: float, initial: float) -> float:
    """Car amps in force at ``t``; ``initial`` covers the open.

    ``initial`` is the quarter's opening amps (the first reported sample,
    which is also the initial of both the history and the policy series —
    they agree until the policy commands something).
    """
    amps = initial
    for sample in sorted(samples, key=lambda item: item.t):
        if sample.t <= t:
            amps = sample.amps
    return amps


def _amps_seconds(
    samples: Sequence[TeslaSample], t0: float, t1: float, initial: float
) -> float:
    """Amp-seconds the car drew over ``[t0, t1]`` (midpoint sampling)."""
    if not samples:
        return (t1 - t0) * initial
    points = sorted({t0, t1} | {sample.t for sample in samples if t0 < sample.t < t1})
    return sum(
        (b - a) * _amps_at(samples, (a + b) / 2, initial)
        for a, b in zip(points, points[1:])
    )


def _tesla_divergence_wh(
    counterfactual: Sequence[TeslaSample],
    history: Sequence[TeslaSample],
    t0: float,
    t1: float,
    initial: float,
) -> float:
    """Counterfactual-minus-history car energy over ``[t0, t1]`` in Wh.

    The car is the meter's second actuator population: a commanded amp
    delta draws ``nominal_voltage()`` watts per amp for as long as it is
    commanded, so one amp over the remaining window is worth
    ``240 * R / 3600`` Wh — the quantum the log-side overshoots are made of.
    """
    return (
        _amps_seconds(counterfactual, t0, t1, initial)
        - _amps_seconds(history, t0, t1, initial)
    ) * nominal_voltage() / 3600.0


def _tesla_sample_at(samples: Sequence[TeslaSample], t: float) -> TeslaSample:
    """The reported sample in force at ``t`` (the first one back-inferred)."""
    chosen = samples[0]
    for sample in sorted(samples, key=lambda item: item.t):
        if sample.t <= t:
            chosen = sample
    return chosen


def _counterfactual_tesla(
    quarter: Quarter, policy_tesla: Sequence[TeslaSample], t: float
) -> TeslaState | None:
    """The car as the policy has left it, or None when no car is modelled.

    Amps come from the counterfactual series (the logged draw where the
    policy has not commanded anything); ``plugged_in``/``at_home`` come from
    the log, because production gates both directions on them.
    """
    if not quarter.tesla_samples:
        return None
    sample = _tesla_sample_at(quarter.tesla_samples, t)
    amps = _amps_at(policy_tesla, t, quarter.tesla_samples[0].amps)
    return TeslaState(
        is_charging=amps > 0.0,
        current_amps=int(round(amps)),
        plugged_in=sample.plugged_in,
        at_home=sample.at_home,
    )


def _meter_divergence_wh(
    quarter: Quarter,
    commands: Sequence[tuple[float, str, bool]],
    policy_tesla: Sequence[TeslaSample],
    t0: float,
    t1: float,
) -> float:
    """Counterfactual-minus-history energy over ``[t0, t1]``, both legs.

    The plugs (nameplate watts) and the car (commanded amps) are the meter's
    two actuator populations, and their divergences add: the car is where
    most of a Tesla-heavy quarter's shed energy goes.
    """
    open_amps = quarter.tesla_samples[0].amps if quarter.tesla_samples else 0.0
    return _divergence_wh(
        commands, quarter.events, t0, t1, quarter.initial_on
    ) + _tesla_divergence_wh(policy_tesla, quarter.tesla_samples, t0, t1, open_amps)


def _apply_actions(
    quarter: Quarter,
    commanded_at: float,
    actions: Sequence[PendingEffect],
    commands: list[tuple[float, str, bool]],
    policy_tesla: list[TeslaSample],
) -> None:
    """Record decided commands on the counterfactual meter series.

    A car command replaces the policy's amp step (``turn_off`` means zero
    amps); a plug command appends an ON/OFF step. The car sample copies the
    logged ``plugged_in``/``at_home``, which the policy cannot change.
    """
    for action in actions:
        if action.device_name == "tesla":
            sample = _tesla_sample_at(quarter.tesla_samples, commanded_at)
            policy_tesla.append(
                TeslaSample(
                    t=commanded_at,
                    amps=0.0
                    if action.action == "turn_off"
                    else float(action.target_amps or 0),
                    plugged_in=sample.plugged_in,
                    at_home=sample.at_home,
                )
            )
        else:
            commands.append(
                (commanded_at, action.device_name, action.action == "turn_on")
            )


def _guard_activity(
    engine: GapMinder,
    gap_wh: float,
    jitter_wh_per_s: float | None,
    seconds_remaining: int,
    banked_wh: float,
) -> GuardActivity:
    """Count what the guard arm decided on one cycle, as production would.

    ``decide()`` only reaches the turn-on side past the deadband, and the
    guard only ever declines a *surplus* claim, so the same two gates are
    applied here: a cycle that never got that far cannot be counted. A fire
    the banked-cover override releases is a rescue, not a decline —
    production's own split between its ``gapminder_jitter_guard`` and
    ``gapminder_banked_override`` events.

    Args:
        engine: The constructed ``GapMinder`` (its hysteresis and predicates).
        gap_wh: The cycle's adjusted gap, as ``decide()`` computes it.
        jitter_wh_per_s: The tracker's churn signal (None in the inert arm).
        seconds_remaining: Seconds left in the quarter.
        banked_wh: The quarter's accumulated raw energy, as ``ctx.banked_wh``.

    Returns:
        This cycle's activity: a decline, a rescue, or nothing.
    """
    if abs(gap_wh) <= engine.HYSTERESIS_WH or gap_wh <= 0:
        return GuardActivity()
    if not engine.turn_on_jitter_guard_fires(
        gap_wh, jitter_wh_per_s, seconds_remaining
    ):
        return GuardActivity()
    if engine.banked_cover_cap_wh(banked_wh, TARGET_WH) is None:
        return GuardActivity(declines=1)
    return GuardActivity(rescues=1)


def _build_plugs() -> dict[str, PlugConfig]:
    """The modelled plugs, as configuration."""
    return {
        device: PlugConfig(
            name=device,
            accessory_id=f"{device}-id",
            power_watts=watts,
            priority=PRIORITIES[device],
        )
        for device, watts in POWERS.items()
    }


def _open_devices(quarter: Quarter) -> dict[str, DeviceState]:
    """Each plug's opening state, as the counterfactual meter has it."""
    return {
        device: DeviceState(
            name=device,
            desired_state=_initial(quarter.initial_on, device),
            actual_state=_initial(quarter.initial_on, device),
            last_toggle=None,
        )
        for device in POWERS
    }


def _pin_history(state: StateTracker, quarter: Quarter, t_data: float) -> None:
    """Pin every plug to the logged state at ``t_data`` (history mode)."""
    for device in POWERS:
        desired = _on_at(
            quarter.events, device, t_data, _initial(quarter.initial_on, device)
        )
        state.devices[device].desired_state = desired
        state.devices[device].actual_state = desired


def _sync_plug_states(
    state: StateTracker,
    quarter: Quarter,
    commands: Sequence[tuple[float, str, bool]],
    t_data: float,
) -> None:
    """Point every device at the counterfactual state the meter has now."""
    for device in POWERS:
        on_now = _on_at(commands, device, t_data, _initial(quarter.initial_on, device))
        state.devices[device].desired_state = on_now
        state.devices[device].actual_state = on_now


@dataclass(frozen=True)
class _Counterfactual:
    """The meter's own numbers for one cycle, before any rate swap."""

    raw_wh: float
    rate: float
    """Trailing-window rate (Wh/s) — the estimator's input, not its output."""


def _counterfactual(
    quarter: Quarter,
    anchor: ForecastAnchor,
    commands: Sequence[tuple[float, str, bool]],
    policy_tesla: Sequence[TeslaSample],
) -> _Counterfactual:
    """The counterfactual window's accumulated Wh and trailing rate."""
    t_data = float(anchor.samples_used)
    return _Counterfactual(
        raw_wh=anchor.raw_wh
        + _meter_divergence_wh(quarter, commands, policy_tesla, 0.0, t_data),
        rate=anchor.prediction_w
        + _meter_divergence_wh(
            quarter, commands, policy_tesla, max(0.0, t_data - 30.0), t_data
        )
        / 30.0,
    )


@dataclass(frozen=True)
class _GuardSignals:
    """One cycle's guard inputs, as the tracker measured them."""

    jitter_wh_per_s: float
    trend_wh_per_s: float | None
    activity: GuardActivity


def _cycle_guard_signals(
    tracker: GapTrendTracker,
    engine: GapMinder,
    anchor: ForecastAnchor,
    data_point_at: datetime,
    gap_wh: float,
    guards: str,
) -> _GuardSignals:
    """Feed this cycle's adjusted gap, then report what the tracker measured.

    Production's order in ``_stage_compute_gap``: the sample lands before
    ``decide()`` reads the signals. The trend is only *fed* in the live arm —
    the ramp rule reads that field, the jitter guard reads the other one —
    but trust in it is a fact about the series and is reported either way.
    """
    noise_floor = (
        engine.HYSTERESIS_WH / anchor.remaining_secs
        if anchor.remaining_secs > 0
        else 0.0
    )
    slope, trusted = tracker.update(
        data_point_at,
        gap_wh,
        noise_floor=noise_floor,
        churn_ready=anchor.remaining_secs <= JITTER_MAX_REMAINING_SECS,
    )
    jitter = tracker.churn_wh_per_s
    activity = _guard_activity(
        engine, gap_wh, jitter, anchor.remaining_secs, anchor.raw_wh
    )
    return _GuardSignals(
        jitter_wh_per_s=jitter,
        trend_wh_per_s=slope if (trusted and guards == "live") else None,
        activity=GuardActivity(
            declines=activity.declines,
            rescues=activity.rescues,
            trusted_trend_cycles=1 if trusted else 0,
        ),
    )


def replay(
    quarter: Quarter,
    rate_fn: RateFn,
    mode: str = "policy",
    *,
    policy: Policy = Policy(),
    guards: str = "inert",
) -> Replay:
    """Run one quarter closed-loop under ``rate_fn`` and ``policy``.

    Args:
        quarter: The quarter to replay.
        rate_fn: Rate estimator, taking the counterfactual window rate.
        mode: ``"history"`` pins the logged events on both sides (the
            framing control); ``"policy"`` starts from the counterfactual
            meter state and lets production ``decide()`` issue commands.
        policy: The decision knobs the engine is constructed with.
        guards: ``"inert"`` leaves the derived gap signals dark;
            ``"churn"`` feeds the jitter signal only (the turn-on guard and
            its override); ``"live"`` feeds both signals, as production
            does, which also makes the ramp rule reachable. History mode
            pins the logged events, so no arm can change it.

    Returns:
        The run's per-step actions, its landing Wh, and the guard arm's
        activity.

    Raises:
        ValueError: If ``guards`` is not one of :data:`GUARD_MODES`.
    """
    if guards not in GUARD_MODES:
        raise ValueError(f"unknown guard arm: {guards!r}; known: {GUARD_MODES}")
    engine = policy.engine()
    plugs = _build_plugs()
    state = StateTracker()
    state.devices.update(_open_devices(quarter))

    tracker = GapTrendTracker()
    commands: list[tuple[float, str, bool]] = []
    policy_tesla: list[TeslaSample] = []
    steps: list[tuple[tuple[float, str, str], ...]] = []
    activities: list[GuardActivity] = []
    for anchor in quarter.anchors:
        t_data = float(anchor.samples_used)
        if mode == "history":
            _pin_history(state, quarter, t_data)
            steps.append(())
            continue
        cf = _counterfactual(quarter, anchor, commands, policy_tesla)
        _sync_plug_states(state, quarter, commands, t_data)
        predicted_wh = cf.raw_wh + rate_fn(cf.rate, anchor) * anchor.remaining_secs
        adjusted_wh = state.estimated_current_wh(predicted_wh, anchor.remaining_secs)
        signals: _GuardSignals | None = None
        if guards != "inert":
            signals = _cycle_guard_signals(
                tracker,
                engine,
                anchor,
                quarter.qh_start + timedelta(seconds=t_data),
                TARGET_WH - adjusted_wh,
                guards,
            )
            activities.append(signals.activity)
        actions = engine.decide(
            ctx=DecideContext(
                now=quarter.qh_start + timedelta(seconds=t_data + quarter.decide_lag),
                seconds_remaining=anchor.remaining_secs,
                state=state,
                plugs=plugs,
                tesla=_counterfactual_tesla(quarter, policy_tesla, t_data),
                data_point_at=quarter.qh_start + timedelta(seconds=t_data),
                # The quarter's accumulated energy, as production passes it
                # (qh1.raw_wh): the lumpy-first gate's trailing-rate
                # implication, and the banked-cover override's certain side.
                banked_wh=anchor.raw_wh,
                gap_jitter_wh_per_s=signals.jitter_wh_per_s if signals else None,
                gap_trend_wh_per_s=signals.trend_wh_per_s if signals else None,
            ),
            predicted_wh=predicted_wh,
            target_wh=TARGET_WH,
        )
        commanded_at = t_data + quarter.decide_lag
        _apply_actions(quarter, commanded_at, actions, commands, policy_tesla)
        steps.append(
            tuple((commanded_at, action.device_name, action.action) for action in actions)
        )
    if mode == "history":
        return Replay(steps=tuple(steps), landing_wh=quarter.actual_wh)
    return Replay(
        steps=tuple(steps),
        landing_wh=quarter.actual_wh
        + _meter_divergence_wh(quarter, commands, policy_tesla, 0.0, QUARTER_SECS),
        guard=GuardActivity(
            declines=sum(activity.declines for activity in activities),
            rescues=sum(activity.rescues for activity in activities),
            trusted_trend_cycles=sum(
                activity.trusted_trend_cycles for activity in activities
            ),
        ),
    )


# ── the estimators under test ────────────────────────────────────────


def rate_status_quo(w_cf: float, _anchor: ForecastAnchor) -> float:
    """The shipped estimator: the trailing quantization window's rate."""
    return w_cf


def rate_prior(w_cf: float, anchor: ForecastAnchor) -> float:
    """The prior quarter's rate, falling back to the window as shipped."""
    if anchor.prior_rate is None:
        return w_cf
    return anchor.prior_rate


def rate_prior_blend(
    w_cf: float, anchor: ForecastAnchor, weight: float
) -> float:
    """Window/prior blend using the scorer's ``n``/``m`` weights."""
    if anchor.prior_rate is None:
        return w_cf
    samples = float(anchor.prediction_values)
    if samples + weight <= 0.0:
        return anchor.prior_rate
    return (samples * w_cf + weight * anchor.prior_rate) / (samples + weight)


def rate_trend_gated(noise_floor_wh: float = DEFAULT_TREND_FLOOR_WH) -> RateFn:
    """The prior, except while ``GapTrendTracker`` trusts a trend.

    Mirrors the scorer's trend-gated series and production's
    ``_stage_compute_gap``: one tracker per quarter, keyed on
    ``data_point_at``, fed the variant's own predicted total (a constant
    offset and sign flip both leave slopes unchanged), trust floor
    ``noise_floor_wh / remaining_secs``. Where a trend is trusted a genuine
    regime change is under way and the responsive window wins.

    Args:
        noise_floor_wh: Trust floor in Wh, divided by ``remaining_secs``.

    Returns:
        A rate estimator carrying its own per-quarter tracker state.
    """
    trackers: dict[datetime, GapTrendTracker] = {}

    def rate(w_cf: float, anchor: ForecastAnchor) -> float:
        tracker = trackers.setdefault(anchor.quarter_start, GapTrendTracker())
        floor = (
            noise_floor_wh / anchor.remaining_secs if anchor.remaining_secs else 0.0
        )
        predicted = anchor.raw_wh + w_cf * anchor.remaining_secs
        _, trusted = tracker.update(anchor.data_point_at, predicted, noise_floor=floor)
        if trusted:
            return w_cf
        return anchor.prior_rate if anchor.prior_rate is not None else w_cf

    return rate


def variants(trend_floor_wh: float = DEFAULT_TREND_FLOOR_WH) -> dict[str, RateFn]:
    """The estimator variants, freshly built.

    Trend-gated carries tracker state, so a caller that reuses the map
    across quarters would leak history between them — build one per run.

    Args:
        trend_floor_wh: Trust floor for the trend-gated variant.

    Returns:
        Variant name to rate estimator, in report order.
    """
    return {
        "status-quo": rate_status_quo,
        "prior": rate_prior,
        "prior-blend@60": lambda w_cf, anchor: rate_prior_blend(w_cf, anchor, 60.0),
        "trend-gated": rate_trend_gated(trend_floor_wh),
    }


# ── turning logs into quarters ───────────────────────────────────────


@dataclass
class IngestStats:
    """How many quarters a run replayed, and why the rest were dropped."""

    scored: int = 0
    clean: int = 0
    tesla: int = 0
    tesla_active: int = 0
    tesla_unknown: int = 0
    short: int = 0
    bad_lag: int = 0
    unknown_state: int = 0

    def merge(self, other: IngestStats) -> None:
        """Add another log's counts into this one."""
        self.scored += other.scored
        self.clean += other.clean
        self.tesla += other.tesla
        self.tesla_active += other.tesla_active
        self.tesla_unknown += other.tesla_unknown
        self.short += other.short
        self.bad_lag += other.bad_lag
        self.unknown_state += other.unknown_state


def quarter_transitions(
    decisions: Sequence[TurnOffDecision],
    ons: Sequence[TurnOnRecord],
    quarter_start: datetime,
) -> list[tuple[datetime, str, bool]]:
    """The quarter's logged plug transitions, as ``(wall time, device, on)``.

    Tesla legs are dropped: the car is modelled from its reported amps (see
    :func:`tesla_samples`), which are a direct reading of what it drew,
    rather than from the commands we sent it.

    Args:
        decisions: Every turn-off decision in the log.
        ons: Every plug turn-on record in the log.
        quarter_start: Quarter to collect for.

    Returns:
        Transitions in time order.
    """
    transitions: list[tuple[datetime, str, bool]] = []
    for decision in decisions:
        if decision.quarter_start != quarter_start:
            continue
        for action in decision.actions:
            if action.kind == "plug":
                transitions.append((decision.at, action.device_name, False))
    for record in ons:
        if floor_to_qh(record.at) == quarter_start:
            transitions.append((record.at, record.device_name, True))
    return sorted(transitions)


def events_from_transitions(
    transitions: Sequence[tuple[datetime, str, bool]], quarter_start: datetime
) -> tuple[tuple[float, str, bool], ...]:
    """Shift transitions onto quarter-relative seconds."""
    return tuple(
        ((at - quarter_start).total_seconds(), name, on)
        for at, name, on in transitions
    )


def opening_states(
    snapshots: Sequence[CycleStateSnapshot],
    transitions: Sequence[tuple[datetime, str, bool]],
    quarter_start: datetime,
) -> dict[str, bool] | None:
    """Each modelled plug's ON state at the quarter open, from the log.

    The candidate dump of the quarter's first cycle reports every configured
    plug's actual state. Anything that transitioned *before* that dump is
    back-inferred from its earliest event so the state is the open, not the
    state after the first decision.

    Args:
        snapshots: Every cycle's parsed dispatch state, in log order.
        transitions: The quarter's plug transitions, in time order.
        quarter_start: Quarter to report on.

    Returns:
        Plug name -> ON at open, or None when the quarter logs no candidate
        dump, or a modelled plug's state is entirely unknown.
    """
    for snapshot in snapshots:
        if snapshot.quarter_start != quarter_start or not snapshot.plugs:
            continue
        states: dict[str, bool] = {}
        for name, desired, actual in snapshot.plugs:
            on = actual if actual is not None else desired
            if on is None:
                return None
            states[name] = on
        if snapshot.at is None:
            return states
        for at, name, new_state in transitions:
            if at >= snapshot.at:
                break
            if name in states:
                states[name] = not new_state
        return states
    return None


def tesla_dirty_quarters(
    snapshots: Sequence[CycleStateSnapshot],
    decisions: Sequence[TurnOffDecision],
) -> set[datetime]:
    """Quarters where the Tesla may have drawn power.

    Historical name kept from when this disqualified a quarter; now it is
    the regime discriminator (``Quarter.car_active``), separating the
    quarters the car was in play in from the ones where the plugs were the
    only actuators.

    Args:
        snapshots: Every cycle's parsed dispatch state, in log order.
        decisions: Every turn-off decision in the log.

    Returns:
        Quarters with a charging cycle, a non-zero amp reading, or a
        non-plug (Tesla) decision.
    """
    dirty: set[datetime] = set()
    for snapshot in snapshots:
        if snapshot.quarter_start is None:
            continue
        charging = snapshot.tesla_charging or (snapshot.tesla_amps or 0.0) != 0.0
        if charging:
            dirty.add(snapshot.quarter_start)
    for decision in decisions:
        if any(action.kind != "plug" for action in decision.actions):
            dirty.add(decision.quarter_start)
    return dirty


def tesla_samples(
    snapshots: Sequence[CycleStateSnapshot], quarter_start: datetime
) -> tuple[TeslaSample, ...]:
    """The quarter's reported car draw, as ``TeslaSample`` steps.

    Every cycle that reported a Tesla state contributes one sample at its
    own line prefix; the first is also the open (back-inferred flat), the
    same back-inference ``opening_states`` applies to a plug. Cycles that
    logged no state are skipped — they exited before reading the car, which
    is not evidence that it was idle.

    Args:
        snapshots: Every cycle's parsed dispatch state, in log order.
        quarter_start: Quarter to collect for.

    Returns:
        Samples in time order (empty when no cycle reported a state).
    """
    samples: list[TeslaSample] = []
    for snapshot in snapshots:
        if snapshot.quarter_start != quarter_start or snapshot.at is None:
            continue
        if snapshot.tesla_amps is None:
            continue
        samples.append(
            TeslaSample(
                t=(snapshot.at - quarter_start).total_seconds(),
                amps=float(snapshot.tesla_amps),
                # An older or partial line without the flag cannot be read as
                # "away": unknown reads as plugged, and a non-zero reading is
                # proof of it regardless.
                plugged_in=snapshot.tesla_plugged_in is not False
                or snapshot.tesla_amps > 0,
                at_home=snapshot.tesla_at_home is not False,
            )
        )
    return tuple(sorted(samples, key=lambda sample: sample.t))


def tesla_unknown_quarters(
    snapshots: Sequence[CycleStateSnapshot],
) -> set[datetime]:
    """Quarters where no cycle reported a Tesla state at all.

    ``tesla_state=None`` means no state was read (the cycle exited before
    the async phase, or ran it and found the car unavailable — production
    records which in ``tesla_state_missing_reason``), so the meter model
    cannot certify these quarters.

    Args:
        snapshots: Every cycle's parsed dispatch state, in log order.

    Returns:
        Quarters whose every logged cycle left the Tesla state unknown.
    """
    known: set[datetime] = set()
    every: set[datetime] = set()
    for snapshot in snapshots:
        if snapshot.quarter_start is None:
            continue
        every.add(snapshot.quarter_start)
        if snapshot.tesla_charging is not None:
            known.add(snapshot.quarter_start)
    return every - known


def _scored_anchors(
    anchors: Sequence[ForecastAnchor],
) -> dict[datetime, list[ForecastAnchor]]:
    """Group anchors by quarter, keeping only replayable, distinct ones.

    A repeated ``samples_used`` is the same data point seen by a later cycle
    (the fetch is TTL-paced, the cycle is not), and production's tracker
    rejects the repeat rather than deciding twice — so the first anchor for a
    data point wins, as in the pinned two-quarter fixtures.
    """
    by_quarter: dict[datetime, list[ForecastAnchor]] = defaultdict(list)
    seen: set[tuple[datetime, int]] = set()
    for anchor in anchors:
        if anchor.actual_wh is None or anchor.prior_rate is None:
            continue
        key = (anchor.quarter_start, anchor.samples_used)
        if key in seen:
            continue
        seen.add(key)
        by_quarter[anchor.quarter_start].append(anchor)
    return by_quarter


def _quarter_lag(
    group: Sequence[ForecastAnchor], quarter_start: datetime
) -> float | None:
    """Median fetch-to-data-point lag for a quarter, or None when unusable.

    The fetch line's own timestamp places each command where production
    actually decided, so no hand-measured constant is needed.

    Args:
        group: The quarter's anchors.
        quarter_start: Quarter the anchors belong to.

    Returns:
        Seconds between the data end and the fetch line, or None when no
        anchor carries a wall time or the median falls outside
        ``[LAG_MIN_SECS, LAG_MAX_SECS]``.
    """
    lags = [
        (anchor.wall_at - quarter_start).total_seconds() - anchor.samples_used
        for anchor in group
        if anchor.wall_at is not None
    ]
    if not lags:
        return None
    lag = statistics.median(lags)
    if not LAG_MIN_SECS <= lag <= LAG_MAX_SECS:
        return None
    return lag


def ingest_log(path: Path, text: str) -> tuple[dict[datetime, Quarter], IngestStats]:
    """Turn one log's text into every quarter the meter model can replay.

    Args:
        path: Log file the text came from (recorded on each quarter).
        text: Full log contents.

    Returns:
        Replayable quarters keyed by quarter start, plus the drop counts.
    """
    decisions = parse_turn_offs(text, source=path.name)
    ons = parse_turn_ons(text, source=path.name)
    snapshots = parse_cycle_states(text, source=path.name)
    active = tesla_dirty_quarters(snapshots, decisions)
    unknown = tesla_unknown_quarters(snapshots)
    stats = IngestStats()
    quarters: dict[datetime, Quarter] = {}
    for quarter_start, group in _scored_anchors(
        parse_log(text, source=path.name)
    ).items():
        stats.scored += 1
        lag = _quarter_lag(group, quarter_start)
        if quarter_start in unknown:
            stats.tesla_unknown += 1
        elif len(group) < MIN_ANCHORS_PER_QUARTER:
            stats.short += 1
        elif lag is None:
            stats.bad_lag += 1
        else:
            transitions = quarter_transitions(decisions, ons, quarter_start)
            initial = opening_states(snapshots, transitions, quarter_start)
            if initial is None:
                stats.unknown_state += 1
                continue
            anchors = sorted(group, key=lambda anchor: anchor.samples_used)
            actual_wh = anchors[0].actual_wh
            if actual_wh is None:  # pragma: no cover - filtered above
                stats.unknown_state += 1
                continue
            samples = tesla_samples(snapshots, quarter_start)
            quarters[quarter_start] = Quarter(
                name=path.name,
                qh_start=quarter_start,
                actual_wh=actual_wh,
                decide_lag=lag,
                anchors=tuple(anchors),
                events=events_from_transitions(transitions, quarter_start),
                initial_on=initial,
                tesla_samples=samples,
                car_active=quarter_start in active,
            )
            stats.clean += 1
            if samples:
                stats.tesla += 1
                if quarter_start in active:
                    stats.tesla_active += 1
    return quarters, stats


def build_quarters(
    paths: Sequence[Path],
) -> tuple[dict[datetime, Quarter], IngestStats]:
    """Ingest every readable log, skipping the rest on stderr.

    Args:
        paths: Log files to read.

    Returns:
        Every replayable quarter across the logs, keyed by quarter start
        (a quarter logged twice is replayed once, from the later file), and
        the summed drop counts.
    """
    quarters: dict[datetime, Quarter] = {}
    stats = IngestStats()
    for path, text in _iter_log_texts(paths):
        log_quarters, log_stats = ingest_log(path, text)
        quarters.update(log_quarters)
        stats.merge(log_stats)
    return quarters, stats


# ── scoring and reporting ────────────────────────────────────────────

REGIMES: tuple[str, ...] = ("all", "tesla", "plug-only")
"""Metering regimes the report can be restricted to."""


def select_regime(
    quarters: Mapping[datetime, Quarter], regime: str
) -> dict[datetime, Quarter]:
    """Keep the quarters one metering regime covers.

    ``"tesla"`` keeps the quarters the car was in play in (it drew, or a
    non-plug decision was taken) — the population the plugs-only model used
    to refuse. ``"plug-only"`` keeps the rest. Neither is reproducible by
    the old model: the car is commandable in *every* replayable quarter now,
    so both populations have to be re-measured under the extended model.

    Args:
        quarters: The ingested quarter set.
        regime: One of :data:`REGIMES`.

    Returns:
        The quarters that regime covers.

    Raises:
        ValueError: If ``regime`` is not one of :data:`REGIMES`.
    """
    if regime not in REGIMES:
        raise ValueError(f"unknown regime: {regime!r} (known: {', '.join(REGIMES)})")
    if regime == "all":
        return dict(quarters)
    want_car = regime == "tesla"
    return {
        start: quarter
        for start, quarter in quarters.items()
        if quarter.car_active == want_car
    }


@dataclass(frozen=True)
class QuarterRun:
    """One quarter's outcome under one variant."""

    miss_wh: float
    actions: int
    reversals: int
    guard: GuardActivity = GuardActivity()


@dataclass(frozen=True)
class VariantSummary:
    """One variant's aggregate closed-loop outcome."""

    name: str
    quarters: int
    mean_abs_miss_wh: float
    median_abs_miss_wh: float
    mean_miss_wh: float
    mean_actions: float
    mean_reversals: float


@dataclass(frozen=True)
class Comparison:
    """One variant measured against the status quo, quarter by quarter."""

    better: int
    worse: int
    same: int
    changed: int
    deltas: tuple[float, ...]
    """Per-quarter ``|miss_variant| - |miss_status_quo|`` (negative wins)."""

    @property
    def mean_delta_wh(self) -> float:
        """Mean per-quarter |miss| change."""
        return statistics.mean(self.deltas) if self.deltas else 0.0

    @property
    def median_delta_wh(self) -> float:
        """Median per-quarter |miss| change."""
        return statistics.median(self.deltas) if self.deltas else 0.0

    @property
    def p90_delta_wh(self) -> float:
        """90th-percentile |miss| change (the worst decile's edge)."""
        if not self.deltas:
            return 0.0
        return sorted(self.deltas)[int(0.9 * len(self.deltas))]

    @property
    def worst_delta_wh(self) -> float:
        """Largest |miss| regression in any single quarter."""
        return max(self.deltas) if self.deltas else 0.0

    @property
    def best_delta_wh(self) -> float:
        """Largest |miss| improvement in any single quarter."""
        return min(self.deltas) if self.deltas else 0.0


def replay_all(
    quarters: Mapping[datetime, Quarter],
    rate_fn: RateFn,
    *,
    policy: Policy = Policy(),
    guards: str = "inert",
) -> dict[datetime, QuarterRun]:
    """Run one estimator over every quarter.

    Args:
        quarters: Quarters to replay, by start.
        rate_fn: The estimator under test.
        policy: The decision knobs the engine is constructed with.
        guards: Which guard arm to measure under (see :data:`GUARD_MODES`).

    Returns:
        Each quarter's miss against target, actions, reversals, and the
        guard arm's activity.
    """
    runs: dict[datetime, QuarterRun] = {}
    for quarter_start, quarter in quarters.items():
        run = replay(quarter, rate_fn, policy=policy, guards=guards)
        runs[quarter_start] = QuarterRun(
            miss_wh=run.landing_wh - TARGET_WH,
            actions=run.action_count,
            reversals=run.reversals,
            guard=run.guard,
        )
    return runs


def summarize_variant(name: str, runs: Mapping[datetime, QuarterRun]) -> VariantSummary:
    """Aggregate one variant's per-quarter outcomes."""
    misses = [abs(run.miss_wh) for run in runs.values()]
    return VariantSummary(
        name=name,
        quarters=len(runs),
        mean_abs_miss_wh=statistics.mean(misses) if misses else 0.0,
        median_abs_miss_wh=statistics.median(misses) if misses else 0.0,
        mean_miss_wh=(
            statistics.mean(run.miss_wh for run in runs.values()) if runs else 0.0
        ),
        mean_actions=(
            statistics.mean(run.actions for run in runs.values()) if runs else 0.0
        ),
        mean_reversals=(
            statistics.mean(run.reversals for run in runs.values()) if runs else 0.0
        ),
    )


def compare_variants(
    base: Mapping[datetime, QuarterRun], other: Mapping[datetime, QuarterRun]
) -> Comparison:
    """Measure one variant's |miss| against the status quo, per quarter."""
    better = worse = same = changed = 0
    deltas: list[float] = []
    for quarter_start, run in other.items():
        reference = base[quarter_start]
        delta = abs(run.miss_wh) - abs(reference.miss_wh)
        deltas.append(delta)
        if delta < -0.05:
            better += 1
        elif delta > 0.05:
            worse += 1
        else:
            same += 1
        if (run.actions, run.reversals) != (reference.actions, reference.reversals):
            changed += 1
    return Comparison(
        better=better, worse=worse, same=same, changed=changed, deltas=tuple(deltas)
    )


def rank_regressions(
    base: Mapping[datetime, QuarterRun],
    other: Mapping[datetime, QuarterRun],
    limit: int,
) -> list[tuple[datetime, QuarterRun, QuarterRun]]:
    """The worst |miss| regressions, as ``(quarter, base run, variant run)``."""
    ranked = sorted(
        other.items(),
        key=lambda item: abs(item[1].miss_wh) - abs(base[item[0]].miss_wh),
        reverse=True,
    )
    return [(quarter_start, base[quarter_start], run) for quarter_start, run in ranked[:limit]]


def format_stats(logs: int, stats: IngestStats) -> str:
    """The ingestion header: how many quarters, and what was dropped."""
    return "\n".join(
        [
            f"scanned {logs} log(s): {stats.scored} scored, {stats.clean} replayable",
            f"  of those: {stats.tesla} with a car in the meter "
            f"({stats.tesla_active} car-active)",
            f"  dropped: {stats.tesla_unknown} tesla-unknown, "
            f"{stats.short} short, {stats.bad_lag} bad lag, "
            f"{stats.unknown_state} no opening state",
        ]
    )


def format_summaries(summaries: Sequence[VariantSummary]) -> str:
    """The variant table."""
    lines = [
        f"{'variant':>16} {'quarters':>9} {'mean|miss|':>10} {'med|miss|':>10} "
        f"{'mean miss':>10} {'actions/q':>10} {'reversals/q':>12}"
    ]
    for summary in summaries:
        lines.append(
            f"{summary.name:>16} {summary.quarters:>9} "
            f"{summary.mean_abs_miss_wh:>10.2f} {summary.median_abs_miss_wh:>10.2f} "
            f"{summary.mean_miss_wh:>10.2f} {summary.mean_actions:>10.2f} "
            f"{summary.mean_reversals:>12.2f}"
        )
    return "\n".join(lines)


def format_comparison(name: str, comparison: Comparison) -> str:
    """One variant's paired verdict against the status quo."""
    return "\n".join(
        [
            f"{name} vs status-quo |miss|: better {comparison.better}, "
            f"worse {comparison.worse}, same {comparison.same} "
            f"(behaviour changed in {comparison.changed}/{len(comparison.deltas)})",
            f"  mean delta {comparison.mean_delta_wh:+.2f} Wh, "
            f"median {comparison.median_delta_wh:+.2f}, "
            f"p90 {comparison.p90_delta_wh:+.2f}, "
            f"worst {comparison.worst_delta_wh:+.2f}, "
            f"best {comparison.best_delta_wh:+.2f}",
        ]
    )


def format_guard_arm(
    live: Mapping[datetime, QuarterRun],
    *,
    churn: Mapping[datetime, QuarterRun],
    inert: Mapping[datetime, QuarterRun],
    rate_name: str,
    worst: int = 0,
) -> str:
    """The guard arm's verdict: what it did, against what controls.

    The paired comparison is what answers "did the guard withhold a real
    action?" — production's candidate dump lists every configured plug, so an
    *available* turn-on is near-universal and the log alone cannot tell a
    withheld action from one that was never there. The counterfactual can: a
    quarter the live arm left untouched while the inert arm commanded a load
    is the guard's work, and it shows up as a changed-quarter.

    The arm bundles three mechanisms, and the churn-only control splits them:
    ``churn`` vs ``inert`` is the turn-on guard and its banked-cover override
    (the ramp rule's trend stays dark there), ``live`` vs ``churn`` is the
    ramp rule alone — the pre-registered rule for deleting it needs exactly
    that number, since the production census (zero fires) only supplies half
    a verdict.

    Args:
        live: The status-quo rate's quarters under the live arm.
        churn: The same, with the jitter signal fed and the trend dark.
        inert: The same, with both signals dark (the shipped machine).
        rate_name: The rate the arm was measured with (the report's base).
        worst: How many changed quarters to list (0 lists none).

    Returns:
        The block, ready to print.
    """
    declines = sum(run.guard.declines for run in live.values())
    rescues = sum(run.guard.rescues for run in live.values())
    trusted = sum(run.guard.trusted_trend_cycles for run in live.values())
    quarters = len(live)
    combined = compare_variants(inert, live)
    guard_only = compare_variants(inert, churn)
    ramp = compare_variants(churn, live)
    lines = [
        f"guard arm: live ({rate_name} rate; churn-only and inert controls)",
        f"  jitter guard: {declines} decline(s) over {quarters} quarter(s) "
        f"({declines / quarters if quarters else 0.0:.2f}/quarter), "
        f"{rescues} banked-cover rescue(s)",
        f"  trusted-trend cycles: {trusted} "
        "(the ramp-aware Tesla stop's gate, not its firing)",
        f"live vs inert |miss|: better {combined.better}, worse "
        f"{combined.worse}, same {combined.same} (behaviour changed in "
        f"{combined.changed}/{quarters})",
        f"  mean delta {combined.mean_delta_wh:+.2f} Wh, median "
        f"{combined.median_delta_wh:+.2f}, worst {combined.worst_delta_wh:+.2f}, "
        f"best {combined.best_delta_wh:+.2f}",
        f"churn-only vs inert (the guard and its override, no ramp rule): "
        f"mean delta {guard_only.mean_delta_wh:+.2f} Wh, better "
        f"{guard_only.better}, worse {guard_only.worse}, changed "
        f"{guard_only.changed}/{quarters}",
        f"live vs churn-only (the ramp rule alone): mean delta "
        f"{ramp.mean_delta_wh:+.2f} Wh, better {ramp.better}, worse "
        f"{ramp.worse}, changed {ramp.changed}/{quarters}",
        "  cross-check: the decline rate must land in the same order as the "
        "scorer's `--guards` census over the same logs — a silent arm is a "
        "fidelity bug, not a calm corpus",
    ]
    if worst > 0:
        lines.append(format_guard_quarters(live, inert=inert, limit=worst))
    return "\n".join(lines)


def format_guard_quarters(
    live: Mapping[datetime, QuarterRun],
    *,
    inert: Mapping[datetime, QuarterRun],
    limit: int,
) -> str:
    """The quarters the live arm actually moved, worst |miss| delta first.

    Attribution for a wash: each row names the arms' action counts and what
    the guard's own counters saw, so a moved quarter can be read back to the
    mechanism rather than to the bundle.
    """
    moved = [
        (start, inert[start], run)
        for start, run in live.items()
        if (run.actions, run.reversals) != (inert[start].actions, inert[start].reversals)
    ]
    moved.sort(key=lambda row: abs(row[2].miss_wh) - abs(row[1].miss_wh), reverse=True)
    lines = [f"quarters the arm moved, worst {limit} of {len(moved)}:"]
    for start, base, run in moved[:limit]:
        lines.append(
            f"  {start:%Y-%m-%d %H:%M}  "
            f"|miss| {abs(base.miss_wh):6.2f} -> {abs(run.miss_wh):6.2f}  "
            f"({run.miss_wh - base.miss_wh:+7.2f})  "
            f"actions {base.actions} -> {run.actions}  "
            f"declines {run.guard.declines}, rescues {run.guard.rescues}, "
            f"trusted {run.guard.trusted_trend_cycles}"
        )
    return "\n".join(lines)


def format_regressions(
    name: str, rows: Sequence[tuple[datetime, QuarterRun, QuarterRun]]
) -> str:
    """The worst |miss| regressions under one variant."""
    lines = [f"worst regressions under {name}:"]
    for quarter_start, base, run in rows:
        lines.append(
            f"  {quarter_start:%Y-%m-%d %H:%M}  "
            f"|miss| {abs(base.miss_wh):6.2f} -> {abs(run.miss_wh):6.2f}  "
            f"({run.miss_wh - base.miss_wh:+7.2f})  "
            f"actions {base.actions} -> {run.actions}  "
            f"reversals {base.reversals} -> {run.reversals}"
        )
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────

_USAGE = (
    "usage: estimator_replay.py [LOG ...] [--variant NAME ...] "
    "[--date YYYY-MM-DD] [--worst N] [--trend-floor WH] "
    "[--regime {all,tesla,plug-only}] [--turn-on-margin WH] "
    "[--turn-off-margin WH] [--lumpy-first] [--amp-floor] "
    "[--dispatch-secs SECS] [--guards {inert,churn,live}]"
)


def build_parser() -> argparse.ArgumentParser:
    """The tool's argument parser."""
    parser = argparse.ArgumentParser(
        prog="estimator_replay.py",
        description="Replay load-management decisions over production logs "
        "with a swapped forecast rate, measuring the closed-loop miss.",
    )
    parser.add_argument(
        "logs",
        nargs="*",
        help="log files to replay; defaults to bugs/*.log",
    )
    parser.add_argument(
        "--variant",
        action="append",
        default=None,
        metavar="NAME",
        help="variant(s) to report; default all",
    )
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="restrict the run to one local day",
    )
    parser.add_argument(
        "--worst",
        type=int,
        default=8,
        metavar="N",
        help="show the N worst regressions under the reference variant",
    )
    parser.add_argument(
        "--trend-floor",
        type=float,
        default=DEFAULT_TREND_FLOOR_WH,
        metavar="WH",
        help="trend-gated trust floor (default: %(default)s)",
    )
    parser.add_argument(
        "--regime",
        choices=REGIMES,
        default="all",
        help="restrict the run to one metering regime (default: %(default)s); "
        "'tesla' is where the car was in play, 'plug-only' where it was idle",
    )
    parser.add_argument(
        "--turn-on-margin",
        type=int,
        default=0,
        metavar="WH",
        help="whole Wh withheld from the turn-on budget (default: %(default)s, "
        "aim at the target); a positive margin declines loads whose capacity "
        "exceeds the remaining surplus",
    )
    parser.add_argument(
        "--turn-off-margin",
        type=int,
        default=0,
        metavar="WH",
        help="whole Wh withheld from the turn-off budget (default: "
        "%(default)s, aim at the target); a positive margin declines sheds a "
        "coarse load would overshoot, at the price of leaving the gap open",
    )
    parser.add_argument(
        "--lumpy-first",
        action="store_true",
        help="shed an implicated lumpy load before the smalls "
        "(production's LOAD_MANAGE_LUMPY_FIRST)",
    )
    parser.add_argument(
        "--amp-floor",
        action="store_true",
        help="round a Tesla amp reduction down to the amps the gap affords "
        "instead of up to the amps it might need (the shipped ceil); the "
        "residue is left for a later, finer step",
    )
    parser.add_argument(
        "--dispatch-secs",
        type=int,
        default=None,
        metavar="SECS",
        help="price a starting load over one dispatch (the meter's "
        "quantization window, ~30 s) instead of the whole remaining window, "
        "so a load may start if its first dispatch fits the surplus; the "
        "running debit stays window-priced (default: %(default)s = whole "
        "window, shipped)",
    )
    parser.add_argument(
        "--defer",
        choices=["on", "off"],
        default="on",
        help="whether the Tesla stop is deferred while a safe buffer remains "
        "(shipped, 'on'); 'off' stops immediately at minimum amps, relaxing "
        "the defer constraint and making the ramp rule unreachable",
    )
    parser.add_argument(
        "--max-defer-secs",
        type=int,
        default=None,
        metavar="SECS",
        help="cap on the safe defer window in seconds (shipped 120); lower "
        "values stop sooner while a buffer remains. None keeps the shipped cap",
    )
    parser.add_argument(
        "--ramp-aware",
        action="store_true",
        help="enable the ramp-aware Tesla stop hastener (shipped off): a "
        "trusted rising deficit pulls the stop one cycle earlier",
    )
    parser.add_argument(
        "--guards",
        choices=GUARD_MODES,
        default="inert",
        help="which guard arm the run measures: 'inert' (default) leaves the "
        "derived gap signals dark, reproducing the published numbers; 'live' "
        "feeds a per-quarter trend tracker so the jitter guard, its "
        "banked-cover override and the ramp-aware Tesla stop can fire; "
        "'churn' feeds only the jitter signal, the control that isolates the "
        "ramp rule. Any non-inert arm prints the verdict against both controls",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Replay the given logs and report the closed-loop variant comparison.

    Args:
        argv: Log paths plus the optional flags. Defaults to ``sys.argv``
            and, for the log paths themselves, to every file under
            ``bugs/``.

    Returns:
        Process exit status: 0 when at least one quarter replayed, 1 when
        an argument, a variant name, or the input itself yields nothing.
    """
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    available = variants(args.trend_floor)
    display = list(args.variant) if args.variant else list(available)
    unknown = [name for name in display if name not in available]
    if unknown:
        print(f"error: unknown variant(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"known: {', '.join(available)}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 1
    paths = [Path(path) for path in args.logs] or sorted(
        (_ROOT / "bugs").glob("*.log")
    )
    if not paths:
        print(_USAGE, file=sys.stderr)
        return 1
    quarters, stats = build_quarters(paths)
    if args.date:
        quarters = {
            start: quarter
            for start, quarter in quarters.items()
            if start.strftime("%Y-%m-%d") == args.date
        }
    quarters = select_regime(quarters, args.regime)
    if not quarters:
        print(f"error: no replayable quarters in {len(paths)} log(s)", file=sys.stderr)
        return 1

    names = list(dict.fromkeys([*display, "status-quo"]))
    policy = Policy(
        turn_on_margin_wh=args.turn_on_margin,
        turn_off_margin_wh=args.turn_off_margin,
        lumpy_first=args.lumpy_first,
        amp_floor=args.amp_floor,
        dispatch_secs=args.dispatch_secs,
        defer_stop=(args.defer == "on"),
        max_defer_secs=args.max_defer_secs,
        ramp_aware=args.ramp_aware,
    )
    runs = {
        name: replay_all(quarters, available[name], policy=policy, guards=args.guards)
        for name in names
    }
    base = runs["status-quo"]
    summaries = [summarize_variant(name, runs[name]) for name in display]
    comparisons = [
        (name, compare_variants(base, runs[name]))
        for name in display
        if name != "status-quo"
    ]
    reference = next((name for name in display if name != "status-quo"), None)

    print(format_stats(len(paths), stats))
    print(f"  regime: {args.regime} ({len(quarters)} quarter(s))")
    print(f"  policy: {policy.label}")
    if args.date:
        print(f"  restricted to {args.date}: {len(quarters)} quarters")
    print()
    print(f"target Wh/quarter: {TARGET_WH:.1f}\n")
    print(format_summaries(summaries))
    for name, comparison in comparisons:
        print()
        print(format_comparison(name, comparison))
    if reference is not None and args.worst > 0:
        print()
        print(
            format_regressions(
                reference,
                rank_regressions(base, runs[reference], args.worst),
            )
        )
    if args.guards != "inert":
        # The controls are the same logs, rate and policy — only the derived
        # gap signals differ, so every delta is the arm's own work.
        print()
        print(
            format_guard_arm(
                replay_all(
                    quarters, available["status-quo"], policy=policy, guards="live"
                ),
                churn=replay_all(
                    quarters, available["status-quo"], policy=policy, guards="churn"
                ),
                inert=replay_all(quarters, available["status-quo"], policy=policy),
                rate_name="status-quo",
                worst=args.worst,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
