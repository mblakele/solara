"""Closed-loop replay: does a variance-reduced rate change what we do?

The model lives in :mod:`tools.estimator_replay` (moved out of this file when
the corpus scale-up made it a tool); these tests are its executable spec. The
scorer measures forecast error in the open loop; the replay drives the same
estimator through production ``GapMinder.decide()`` against a counterfactual
meter, so the quantities that matter — the quarter's miss and the actuator
churn — are measured with a closed loop.

Data: the anchor series and plug events are **checked in** (``QUARTER_A_*`` /
``QUARTER_B_*``), not read from ``bugs/``. That directory is gitignored, so a
clone would otherwise skip every test here and the verdict would silently
evaporate; a guarded drift test re-parses the logs and fails if the fixtures
go stale. The same reason ``tests/test_residual_feedback_replay.py`` and
``tests/test_tesla_ramp_replay.py`` carry their data as literals. Ingestion
itself is covered on synthetic log text, so it runs without any log files.

Method (the step-function meter model is the one
``tests/test_residual_feedback_replay.py`` documents, reused here rather than
hand-fixtured): each anchor is a step over quarter-relative time, history
events are the quarter's logged plug lines, policy commands land at the data
end plus the quarter's observed decision lag, and the landing is the logged
actual plus the integrated divergence over the quarter. Only ``rate_fn``
differs between runs, and it takes the counterfactual window rate ``w_cf`` so
the variant's formula is identical to
``tools.forecast_log_scoring.estimate_prior_only`` / ``estimate_prior_blend``
(pinned by test, so the replay cannot evaluate a different variant than the
scorer scored).

Runs:
  * ``history`` — both sides see the logged events, so the landing is the
    logged actual by construction. The framing control.
  * ``status-quo`` — production's trailing-window rate through ``decide()``.
  * variant — the estimator's rate through the same ``decide()``.

The two axes are independent: a run is an estimator *and* a ``Policy`` (the
decision knobs, built from ``GapMinder``'s constructor — margins and
``lumpy_first``). The default policy has to reproduce the shipped engine
exactly, which is what keeps the pinned verdicts below from moving; the
policy tests drive it on synthetic quarters where a coarse quantum (43.3 Wh
of 260 W plug at R=600 s) makes the margin's effect legible.

Scope: the two plug-only quarters the checked-in fixtures cover (no Tesla
activity), plus a synthetic car leg. The meter's second actuator population
is the car: ``TeslaSample`` steps of *reported* amps, which a policy command
replaces, priced at ``nominal_voltage()`` watts per amp so one amp over the
remaining window is ``240 * R / 3600`` Wh — the quantum behind the
2026-10-17 overshoots. ``plugged_in`` comes from the log, so the
counterfactual can never charge an absent car. Nameplate watts stand in for
measured draw, so absolute landings are model values; the *deltas against the
status-quo run* are the result, following the ``test_tesla_overshoot_replay``
convention.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import pytest

from constants import JITTER_FLOOR_WH_PER_S
from tools import estimator_replay as er
from tools import forecast_log_scoring as fls

_BUGS = Path(__file__).resolve().parents[1] / "bugs"
# (log filename, quarter label, observed decision lag secs). The lag is
# measured from the log's PendingEffect timestamps (see
# test_residual_feedback_replay); everything else is checked in below.
_LOG_A = ("2026-10-14-water-heater-overshoot.log", "2026-10-14 23:15", 8.63)
_LOG_B = ("2026-10-14-no-overshoot.log", "2026-10-15 00:15", 14.41)
_NEEDS_LOGS = pytest.mark.skipif(
    not (_BUGS / _LOG_A[0]).exists(),
    reason="bugs/ is gitignored; logs not present (fixtures still cover the verdict)",
)

# Anchor tuples: (samples_used, remaining_secs, prediction_values, raw_wh,
# prediction_w, prior_rate). Extracted from the logs named above; the
# drift test at the bottom checks these still match.
QUARTER_A_ACTUAL = 5.091345000000002
QUARTER_A_ANCHORS: tuple[tuple[int, int, int, float, float, float], ...] = (
    (60, 840, 30, 74.307262, 1.295, -0.009664397),
    (90, 810, 30, 70.783438, -0.117461, -0.009664397),
    (120, 780, 30, 67.034063, -0.124979, -0.009664397),
    (150, 750, 30, 67.034063, 0.0, -0.009664397),
    (180, 720, 30, 66.809153, -0.007497, -0.009664397),
    (210, 690, 30, 63.815953, -0.099773, -0.009664397),
    (270, 630, 30, 57.815453, -0.100003, -0.009664397),
    (300, 600, 30, 55.044338, -0.09237, -0.009664397),
    (360, 540, 30, 49.644693, -0.092497, -0.009664397),
    (390, 510, 30, 47.021443, -0.087442, -0.009664397),
    (450, 450, 30, 41.92293, -0.0825, -0.009664397),
    (480, 420, 30, 39.297843, -0.087503, -0.009664397),
    (510, 390, 30, 36.663218, -0.087821, -0.009664397),
    (540, 360, 30, 34.039968, -0.087442, -0.009664397),
    (600, 300, 30, 29.09038, -0.0825, -0.009664397),
    (630, 270, 30, 26.6911, -0.079976, -0.009664397),
    (660, 240, 30, 24.29278, -0.079944, -0.009664397),
    (690, 210, 30, 21.819018, -0.082459, -0.009664397),
    (750, 150, 30, 17.019018, -0.08, -0.009664397),
    (780, 120, 30, 14.618858, -0.080005, -0.009664397),
    (840, 60, 30, 9.963428, -0.078936, -0.009664397),
    (870, 30, 30, 7.489665, -0.082459, -0.009664397),
)
QUARTER_A_EVENTS: tuple[tuple[float, str, bool], ...] = (
    (68.0, "ecoflow", False),
    (68.0, "water heater", False),
    (183.0, "jackery", False),
)

QUARTER_B_ACTUAL = -28.64531
QUARTER_B_ANCHORS: tuple[tuple[int, int, int, float, float, float], ...] = (
    (30, 870, 30, 37.907825, 1.263594, -0.030956458),
    (60, 840, 30, 35.884918, -0.06743, -0.030956458),
    (120, 780, 30, 31.760668, -0.0875, -0.030956458),
    (150, 750, 30, 28.911618, -0.094968, -0.030956458),
    (180, 720, 30, 26.286617, -0.0875, -0.030956458),
    (210, 690, 30, 23.409447, -0.095906, -0.030956458),
    (270, 630, 30, 18.177385, -0.087497, -0.030956458),
    (301, 599, 30, 15.453597, -0.087864, -0.030956458),
    (330, 570, 30, 12.573498, -0.098932, -0.030956458),
    (360, 540, 30, 9.949898, -0.087453, -0.030956458),
    (420, 480, 30, 4.700737, -0.079997, -0.030956458),
    (450, 450, 30, 2.0781, -0.087421, -0.030956458),
    (480, 420, 30, -0.32126, -0.079979, -0.030956458),
    (510, 390, 30, -2.59256, -0.07571, -0.030956458),
    (570, 330, 30, -7.673748, -0.0825, -0.030956458),
    (600, 300, 30, -8.948918, -0.042506, -0.030956458),
    (630, 270, 30, -9.549037, -0.020004, -0.030956458),
    (662, 238, 30, -9.390063, 0.004964, -0.030956458),
    (690, 210, 30, -10.798913, -0.046695, -0.030956458),
    (720, 180, 30, -13.27581, -0.082563, -0.030956458),
    (750, 150, 30, -16.27321, -0.099913, -0.030956458),
    (780, 120, 30, -18.52276, -0.074985, -0.030956458),
    (840, 60, 30, -23.397498, -0.075, -0.030956458),
    (870, 30, 30, -26.019523, -0.087401, -0.030956458),
)
QUARTER_B_EVENTS: tuple[tuple[float, str, bool], ...] = (
    (44.0, "ecoflow", False),
    (44.0, "water heater", False),
    (581.0, "ecoflow", True),
)


def _build_quarter(
    name: str,
    label: str,
    lag: float,
    actual_wh: float,
    anchors: Sequence[tuple[int, int, int, float, float, float]],
    events: Sequence[tuple[float, str, bool]],
    initial_on: dict[str, bool] | None = None,
) -> er.Quarter:
    """Build a replayable quarter from checked-in anchor and event tuples.

    Fixture plumbing: the tool builds its quarters from parsed logs, but the
    two pinned quarters above carry their data as literals so the verdict
    survives a clone without ``bugs/``.
    """
    qh_start = datetime.strptime(label, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return er.Quarter(
        name=name,
        qh_start=qh_start,
        actual_wh=actual_wh,
        decide_lag=lag,
        initial_on=initial_on,
        anchors=tuple(
            fls.ForecastAnchor(
                source=name,
                quarter_start=qh_start,
                data_point_at=qh_start + timedelta(seconds=samples_used),
                remaining_secs=remaining_secs,
                samples_used=samples_used,
                prediction_values=prediction_values,
                raw_wh=raw_wh,
                prediction_w=prediction_w,
                predicted_wh=raw_wh + prediction_w * remaining_secs,
                actual_wh=actual_wh,
                wall_at=None,
                prior_rate=prior_rate,
            )
            for (
                samples_used,
                remaining_secs,
                prediction_values,
                raw_wh,
                prediction_w,
                prior_rate,
            ) in anchors
        ),
        events=tuple(sorted(events)),
    )


@pytest.fixture(scope="module")
def quarter_a() -> er.Quarter:
    """The 23:15 water-heater quarter (checked-in fixture)."""
    return _build_quarter(
        "A", _LOG_A[1], _LOG_A[2], QUARTER_A_ACTUAL, QUARTER_A_ANCHORS, QUARTER_A_EVENTS
    )


@pytest.fixture(scope="module")
def quarter_b() -> er.Quarter:
    """The 00:15 quarter (checked-in fixture)."""
    return _build_quarter(
        "B", _LOG_B[1], _LOG_B[2], QUARTER_B_ACTUAL, QUARTER_B_ANCHORS, QUARTER_B_EVENTS
    )


def test_fixture_events_include_the_multi_word_plug(quarter_a: er.Quarter) -> None:
    """The heater's shed is in the event set, not dropped for its space.

    ``device=water heater`` broke a ``\\S+`` name pattern once. The parser fix
    is pinned in test_forecast_log_scoring.py; here it matters because the
    replay depends on the whole event set, so a dropped shed would silently
    mis-model the meter instead of failing.
    """
    offs = [(t, name) for t, name, state in quarter_a.events if not state]
    assert [name for _, name in offs] == ["ecoflow", "water heater", "jackery"]
    assert [t for t, _ in offs] == pytest.approx([68.0, 68.0, 183.0], abs=1.0)


def test_history_mode_lands_on_the_logged_actual(
    quarter_a: er.Quarter, quarter_b: er.Quarter
) -> None:
    """History pins both sides, so the landing is the logged total exactly."""
    for quarter in (quarter_a, quarter_b):
        run = er.replay(quarter, er.rate_status_quo, mode="history")
        assert run.landing_wh == pytest.approx(quarter.actual_wh)
        assert run.action_count == 0


def test_status_quo_closed_loop_reproduces_productions_opening_shed(
    quarter_a: er.Quarter, quarter_b: er.Quarter
) -> None:
    """The control: production's rate makes production's own opening decision."""
    first_a = er.replay(quarter_a, er.rate_status_quo).steps[0]
    assert ("water heater", "turn_off") in [(d, a) for _t, d, a in first_a]
    first_b = er.replay(quarter_b, er.rate_status_quo).steps[0]
    assert ("water heater", "turn_off") in [(d, a) for _t, d, a in first_b]


def test_rate_functions_reproduce_the_scorers_estimates(quarter_a: er.Quarter) -> None:
    """The replay's variant is the scorer's variant, same formula."""
    for anchor in quarter_a.anchors:
        priors = (
            er.rate_prior(anchor.prediction_w, anchor) * anchor.remaining_secs
            + anchor.raw_wh
        )
        assert priors == pytest.approx(fls.estimate_prior_only(anchor))
        blended = er.rate_prior_blend(anchor.prediction_w, anchor, 60.0)
        assert anchor.raw_wh + blended * anchor.remaining_secs == pytest.approx(
            fls.estimate_prior_blend(anchor, 60.0)
        )


def test_variant_rate_is_available_for_every_anchor(
    quarter_a: er.Quarter, quarter_b: er.Quarter
) -> None:
    """Both quarters report a prior rate, so the variant is never a no-op."""
    for quarter in (quarter_a, quarter_b):
        assert all(a.prior_rate is not None for a in quarter.anchors)


def test_quarter_can_open_with_plugs_already_off() -> None:
    """A plug that was off at the open must not be "shed" by the model.

    Model limitation this pins: the first two fixtures both open with every
    plug on, so the all-ON default was never exercised. Corpus quarters do not
    all open that way, and assuming they do makes the policy shed loads that
    were never running — a heater-sized phantom divergence (4,857 W * R / 3600)
    that swamps the real miss.
    """
    anchors = ((60, 840, 30, 74.0, 1.2, -0.01),)
    dark = _build_quarter(
        "synthetic",
        "2026-10-14 23:15",
        8.0,
        40.0,
        anchors,
        (),
        initial_on=dict.fromkeys(er.POWERS, False),
    )
    assert er.replay(dark, er.rate_status_quo).action_count == 0
    lit = _build_quarter("synthetic", "2026-10-14 23:15", 8.0, 40.0, anchors, ())
    assert er.replay(lit, er.rate_status_quo).action_count > 0


def test_reversals_respect_the_log_side_window() -> None:
    """A turn-on beyond the window is not a reversal (same rule as the tool)."""
    window = fls.TURN_OFF_REVERSAL_WINDOW_SECS
    shed = ((100.0, "ecoflow", "turn_off"),)
    just_inside = er.Replay(
        steps=(shed, ((100.0 + window, "ecoflow", "turn_on"),)), landing_wh=0.0
    )
    just_outside = er.Replay(
        steps=(shed, ((100.0 + window + 1.0, "ecoflow", "turn_on"),)), landing_wh=0.0
    )
    assert just_inside.reversals == 1
    assert just_outside.reversals == 0


# ── the verdict ──────────────────────────────────────────────────────
#
# Measured over the two quarters the plug-only model covers, closed loop:
#
#   quarter                 run         landing      miss   actions  reversals
#   A 2026-10-14 23:15      status-quo   +2.06      +11.06      5          1
#   A 2026-10-14 23:15      prior        -0.11       +8.89      3          0
#   B 2026-10-15 00:15      status-quo  -28.31      -19.31      7          3
#   B 2026-10-15 00:15      prior       -39.84      -30.84      5          2
#
# The flat prior cuts churn in both quarters and fixes A outright, but
# over-sheds B into deeper export: B's trailing rate (~-0.087 W/s) was right
# and the prior quarter's (-0.031 W/s) was a different regime, so the prior
# under-states export and the loop sheds too much. Mean |miss| therefore gets
# *worse* (19.87 vs 15.19), which fails Subtask 2's acceptance ("must improve
# the mean absolute miss"). Do not ship the flat prior; the trend-gated mix is
# the variant that has to earn B back.
#
# The corpus replay (1,029 plug-only quarters of 13 logs,
# tools/estimator_replay.py) confirmed it at scale and killed the whole line:
# status-quo 23.41 mean |miss| against prior 27.71, prior-blend@60 26.26 and
# trend-gated 24.11, with churn roughly halved (2.07 -> 1.41 actions/quarter).
# Every smoother variant is worse closed-loop even though the shipped window is
# 2x worse open-loop — a 30 s loop pays for responsiveness, not smoothness.
#
# The car regime agrees (--regime tesla: 117 car-active quarters carrying
# 1,269 Tesla commands): status-quo 17.99 against prior 31.99,
# prior-blend@60 19.31 and trend-gated 24.15, churn 15.73 -> 9.65-11.97
# actions/quarter. The 2026-10-17 log is the counter-example that does not
# survive the corpus (prior-blend@60 6.88 vs status-quo 10.54 on its four
# car-active quarters).



def test_prior_rate_cuts_churn_in_both_quarters(
    quarter_a: er.Quarter, quarter_b: er.Quarter
) -> None:
    """The variant's one robust effect: fewer commands and fewer reversals."""
    for quarter in (quarter_a, quarter_b):
        status_quo = er.replay(quarter, er.rate_status_quo)
        variant = er.replay(quarter, er.rate_prior)
        assert variant.action_count < status_quo.action_count
        assert variant.reversals < status_quo.reversals


def test_prior_rate_improves_quarter_a(quarter_a: er.Quarter) -> None:
    """Stable regime: the smoother rate lands nearer target, with no reversal."""
    status_quo = er.replay(quarter_a, er.rate_status_quo)
    variant = er.replay(quarter_a, er.rate_prior)
    assert status_quo.landing_wh == pytest.approx(2.06, abs=0.5)
    assert variant.landing_wh == pytest.approx(-0.11, abs=0.5)
    assert abs(variant.landing_wh - er.TARGET_WH) < abs(
        status_quo.landing_wh - er.TARGET_WH
    )
    assert variant.reversals == 0
    assert status_quo.reversals == 1


def test_prior_rate_worsens_quarter_b(quarter_b: er.Quarter) -> None:
    """Regime change: the stale prior over-sheds into deeper export.

    This is the finding Subtask 2 exists to produce. Pinned so it cannot be
    quietly reversed by a later estimator edit.
    """
    status_quo = er.replay(quarter_b, er.rate_status_quo)
    variant = er.replay(quarter_b, er.rate_prior)
    assert status_quo.landing_wh == pytest.approx(-28.31, abs=0.5)
    assert variant.landing_wh == pytest.approx(-39.84, abs=0.5)
    assert abs(variant.landing_wh - er.TARGET_WH) > abs(
        status_quo.landing_wh - er.TARGET_WH
    )


def test_prior_loses_on_quarter_b_at_any_plausible_lag(
    quarter_b: er.Quarter,
) -> None:
    """The regime-change verdict is a rate fact, not a timing artifact.

    The fixture's lag (14.41 s) is reconstructed from the log's decision
    timestamps, while the tool's ingestion derives its own from the fetch line
    (median 33 s for this quarter) — the two models disagree on the *size* of
    the loss, and the tool's own run on the log reads a 5.06 -> 56.13 Wh swing
    where the fixture reads 19.31 -> 30.84. So pin the *sign* across the whole
    plausible band: whatever the true command time is, the stale prior
    over-sheds a quarter whose trailing rate has just moved.
    """
    for lag in (5.0, 10.0, 14.41, 20.0, 25.0, 30.0, 33.0, 40.0):
        quarter = dataclasses.replace(quarter_b, decide_lag=lag)
        status_quo = er.replay(quarter, er.rate_status_quo)
        prior = er.replay(quarter, er.rate_prior)
        assert abs(prior.landing_wh - er.TARGET_WH) > abs(
            status_quo.landing_wh - er.TARGET_WH
        ), f"prior should lose on quarter B at lag {lag}"


def test_flat_prior_fails_the_mean_absolute_miss_gate(
    quarter_a: er.Quarter, quarter_b: er.Quarter
) -> None:
    """Aggregate verdict: churn down, mean |miss| up -> not shippable."""

    def mean_miss(rate_fn: er.RateFn) -> float:
        landing = [
            er.replay(q, rate_fn).landing_wh for q in (quarter_a, quarter_b)
        ]
        return sum(abs(value - er.TARGET_WH) for value in landing) / len(landing)

    status_quo = mean_miss(er.rate_status_quo)
    variant = mean_miss(er.rate_prior)
    assert status_quo == pytest.approx(15.19, abs=0.5)
    assert variant == pytest.approx(19.87, abs=0.5)
    assert variant > status_quo


# ── ingestion, on synthetic log text ─────────────────────────────────
#
# Held here rather than in bugs/ so a fresh clone still exercises the loader.
# The shapes mirror the real reprs exactly (pinned by the scorer's tests);
# only the field values are chosen.

_SYNTH_Q = datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)
_SYNTH_START = "2026-10-01 20:30:00"
_COMPLETED_TAIL = (
    "prediction_values=None, prediction_w=None, predicted_wh=None, "
    "remaining_seconds=None, samples_used=None"
)
_IDLE_TESLA = (
    "{'is_charging': False, 'current_amps': 0, 'plugged_in': False, 'at_home': True}"
)
# 13:39:31 PDT is 20:39:31 UTC, inside the 20:30 quarter.
_DP = (
    "datetime.datetime(2026, 10, 1, 13, 39, 31, "
    "tzinfo=<DstTzInfo 'America/Los_Angeles' PDT-1 day, 17:00:00 DST>)"
)


def _anchor_fetch(used: int, lag_bias: int = 0) -> str:
    """An nbc_set + create_metrics pair for one anchor of the 20:30 quarter.

    The completed qh2 supplies the anchor's prior rate, exactly as a real
    fetch's neighbouring completed quarter does.
    """
    stamp = (_SYNTH_Q + timedelta(seconds=used + 8 + lag_bias)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    remaining = 900 - used
    return "\n".join(
        [
            f"[{stamp} +0000] [980472] [DEBUG] app nbc_set len {used} "
            f"(NBCQuarterSet(qh1=NBCQuarter(complete=False, raw_wh=70.0, wh=0, "
            f"prediction_values=30, prediction_w=-0.1, "
            f"predicted_wh={70.0 - 0.1 * remaining!r}, "
            f"remaining_seconds={remaining}, samples_used={used}), "
            f"qh2=NBCQuarter(complete=True, raw_wh=-8.7, wh=0.0, "
            f"{_COMPLETED_TAIL}), "
            f"qh3=NBCQuarter(complete=True, raw_wh=None, wh=0.0, "
            f"{_COMPLETED_TAIL})))",
            f"[{stamp} +0000] [980472] [DEBUG] app create_metrics result: "
            f"devices=1, data_start={_SYNTH_START}+00:00, "
            f"per_second_data_total={used}",
        ]
    )


def _actual_fetch(actual_wh: float) -> str:
    """A later fetch that completes the 20:30 quarter as its qh2."""
    return "\n".join(
        [
            "[2026-10-01 20:46:00 +0000] [980472] [DEBUG] app nbc_set len 60 "
            f"(NBCQuarterSet(qh1=NBCQuarter(complete=True, raw_wh=None, wh=0, "
            f"prediction_values=None, prediction_w=None, predicted_wh=None, "
            f"remaining_seconds=None, samples_used=None), "
            f"qh2=NBCQuarter(complete=True, raw_wh={actual_wh!r}, wh=0.0, "
            f"{_COMPLETED_TAIL}), "
            f"qh3=NBCQuarter(complete=True, raw_wh=None, wh=0.0, "
            f"{_COMPLETED_TAIL})))",
            "[2026-10-01 20:46:00 +0000] [980472] [DEBUG] app create_metrics "
            "result: devices=1, data_start=2026-10-01 20:45:00+00:00, "
            "per_second_data_total=60",
        ]
    )


def _candidate(name: str, actual: bool) -> str:
    """One CandidateDetail entry in production field order."""
    return (
        f"CandidateDetail(device_type='plug', name='{name}', power_watts=260.0, "
        f"capacity_wh=21.6, can_toggle=True, desired_state={actual}, "
        f"actual_state={actual})"
    )


def _cycle_line(
    stamp: str = "2026-10-01 20:39:31",
    candidates: str = "",
    tesla: str = _IDLE_TESLA,
) -> str:
    """A cycle-result line for the 20:30 quarter."""
    return (
        f"[{stamp} +0000] [980472] [DEBUG] app Load management cycle result: "
        f"CycleResult(status='ok', qh='QH1', predicted_wh=-1.0, adjusted_wh=-1.0, "
        f"target_wh=-9, actions=[], diagnostics=CycleDiagnostics(gap_wh=7.7, "
        f"hysteresis_wh=3, seconds_remaining=269, data_point_at={_DP}, "
        f"reason='ok', candidates=[{candidates}], tesla_state={tesla})"
    )


def _shed(stamp: str, device: str) -> str:
    """A turn-off entry and the plug shed it booked, in the same second."""
    return "\n".join(
        [
            f"[2026-10-01 {stamp} +0000] [3224] [DEBUG] load_nbc "
            f"[_decide_turn_off] gap=60.0 Wh, seconds_remaining=600",
            f"[2026-10-01 {stamp} +0000] [3224] [INFO] load_nbc "
            f"action=turn_off device={device} savings=60.0 gap=60.0 priority=60",
        ]
    )


def _replayable_log() -> str:
    """A five-anchor quarter that survives every ingestion filter."""
    return "\n".join(
        [
            "\n".join(_anchor_fetch(60 * (index + 1)) for index in range(5)),
            _actual_fetch(12.5),
            _cycle_line(
                candidates=", ".join(
                    _candidate(name, True) for name in er.POWERS
                )
            ),
            _shed("20:30:45", "water heater"),
        ]
    )


def test_ingest_log_builds_a_replayable_quarter() -> None:
    """Anchors, actual, lag, events and the opening state all come from the log."""
    quarters, stats = er.ingest_log(Path("synth.log"), _replayable_log())
    (quarter,) = quarters.values()
    assert (stats.scored, stats.clean) == (1, 1)
    assert quarter.name == "synth.log"
    assert quarter.qh_start == _SYNTH_Q
    assert quarter.actual_wh == pytest.approx(12.5)
    assert quarter.anchors[0].samples_used == 60
    assert [a.samples_used for a in quarter.anchors] == [60, 120, 180, 240, 300]
    assert quarter.anchors[0].prior_rate == pytest.approx(-8.7 / 900)
    assert quarter.decide_lag == pytest.approx(8.0)
    assert quarter.events == ((45.0, "water heater", False),)
    assert quarter.initial_on == dict.fromkeys(er.POWERS, True)


def test_ingest_log_uses_the_fetch_line_lag_not_a_constant() -> None:
    """Command times follow each quarter's own fetch line."""
    late = "\n".join(
        [
            "\n".join(_anchor_fetch(60 * (i + 1), lag_bias=33) for i in range(5)),
            _actual_fetch(12.5),
            _cycle_line(candidates=_candidate("jackery", True)),
        ]
    )
    quarters, _ = er.ingest_log(Path("synth.log"), late)
    (quarter,) = quarters.values()
    assert quarter.decide_lag == pytest.approx(41.0)


def test_ingest_log_replays_a_quarter_the_model_can_act_on() -> None:
    """The ingested quarter is replayable end to end, not just parseable."""
    quarters, _ = er.ingest_log(Path("synth.log"), _replayable_log())
    (quarter,) = quarters.values()
    assert er.replay(quarter, er.rate_status_quo).action_count >= 0
    assert er.replay(quarter, er.rate_status_quo, mode="history").landing_wh == (
        pytest.approx(quarter.actual_wh)
    )


def test_ingest_log_drops_a_repeated_data_point() -> None:
    """A later cycle reading the same sample is the same data point.

    Production's fetch is TTL-paced while the cycle is not, so one data point
    is seen by several cycles; production's tracker rejects the repeat rather
    than deciding twice, and the pinned quarter A/B fixtures were extracted
    with that dedup (22 anchors, not the 37 the log holds).
    """
    log = "\n".join(
        [
            "\n".join(_anchor_fetch(60 * (i + 1)) for i in range(5)),
            _anchor_fetch(300),
            _actual_fetch(12.5),
            _cycle_line(candidates=_candidate("jackery", True)),
        ]
    )
    quarters, _ = er.ingest_log(Path("synth.log"), log)
    (quarter,) = quarters.values()
    assert [a.samples_used for a in quarter.anchors] == [60, 120, 180, 240, 300]


def test_ingest_log_models_a_quarter_where_the_tesla_charges() -> None:
    """A charging car is an actuator now, not a disqualifier.

    The meter carries a commanded-amps leg, so the quarters the plugs-only
    model had to refuse (every cycle in the corpus where the car drew) are
    replayable, and the reported amps become their history.
    """
    log = "\n".join(
        [
            "\n".join(_anchor_fetch(60 * (i + 1)) for i in range(5)),
            _actual_fetch(12.5),
            _cycle_line(
                candidates=_candidate("jackery", True),
                tesla="{'is_charging': True, 'current_amps': 12, "
                "'plugged_in': True, 'at_home': True}",
            ),
        ]
    )
    quarters, stats = er.ingest_log(Path("synth.log"), log)
    (quarter,) = quarters.values()
    assert (stats.clean, stats.tesla, stats.tesla_active) == (1, 1, 1)
    assert quarter.tesla_samples == (er.TeslaSample(571.0, 12.0, True, True),)
    assert quarter.car_active is True
    assert er.replay(quarter, er.rate_status_quo, mode="history").landing_wh == (
        pytest.approx(quarter.actual_wh)
    )


def test_ingest_log_reads_the_amp_history_in_time_order() -> None:
    """Each cycle's reported amps become one step of the car's draw."""
    log = "\n".join(
        [
            "\n".join(_anchor_fetch(60 * (i + 1)) for i in range(5)),
            _actual_fetch(12.5),
            _cycle_line(
                candidates=_candidate("jackery", True),
                tesla="{'is_charging': True, 'current_amps': 12, "
                "'plugged_in': True, 'at_home': True}",
            ),
            _cycle_line(
                stamp="2026-10-01 20:44:31",
                candidates=_candidate("jackery", True),
                tesla="{'is_charging': True, 'current_amps': 6, "
                "'plugged_in': True, 'at_home': True}",
            ),
        ]
    )
    quarters, _ = er.ingest_log(Path("synth.log"), log)
    (quarter,) = quarters.values()
    assert [(s.t, s.amps) for s in quarter.tesla_samples] == [(571.0, 12.0), (871.0, 6.0)]


def test_ingest_log_marks_an_idle_car_as_modelled_but_not_active() -> None:
    """Replayed with a car in the meter, but the car never drew."""
    quarters, stats = er.ingest_log(Path("synth.log"), _replayable_log())
    (quarter,) = quarters.values()
    assert (stats.clean, stats.tesla, stats.tesla_active) == (1, 1, 0)
    assert quarter.tesla_samples == (er.TeslaSample(571.0, 0.0, False, True),)
    assert quarter.car_active is False


def test_ingest_log_drops_a_quarter_with_unknown_tesla_state() -> None:
    """``tesla_state=None`` is an early exit, not a certified-idle car."""
    log = "\n".join(
        [
            "\n".join(_anchor_fetch(60 * (i + 1)) for i in range(5)),
            _actual_fetch(12.5),
            _cycle_line(candidates=_candidate("jackery", True), tesla="None"),
        ]
    )
    quarters, stats = er.ingest_log(Path("synth.log"), log)
    assert quarters == {}
    assert stats.tesla_unknown == 1


def test_ingest_log_drops_a_quarter_without_a_candidate_dump() -> None:
    """No candidate dump means no opening state, so no honest model."""
    log = "\n".join(
        [
            "\n".join(_anchor_fetch(60 * (i + 1)) for i in range(5)),
            _actual_fetch(12.5),
        ]
    )
    quarters, stats = er.ingest_log(Path("synth.log"), log)
    assert quarters == {}
    assert stats.unknown_state == 1


def test_ingest_log_drops_a_quarter_with_too_few_anchors() -> None:
    """Three anchors cannot describe a trajectory."""
    log = "\n".join(
        [
            "\n".join(_anchor_fetch(60 * (i + 1)) for i in range(3)),
            _actual_fetch(12.5),
            _cycle_line(candidates=_candidate("jackery", True)),
        ]
    )
    quarters, stats = er.ingest_log(Path("synth.log"), log)
    assert quarters == {}
    assert stats.short == 1


def test_ingest_log_drops_a_quarter_whose_fetch_line_is_too_far_off() -> None:
    """A line this stale is not the cycle that read the data point.

    Command times come from the fetch line, so a lag that far outside the
    observed band would place every decision in the wrong minute.
    """
    log = "\n".join(
        [
            "\n".join(_anchor_fetch(60 * (i + 1), lag_bias=200) for i in range(5)),
            _actual_fetch(12.5),
            _cycle_line(candidates=_candidate("jackery", True)),
        ]
    )
    quarters, stats = er.ingest_log(Path("synth.log"), log)
    assert quarters == {}
    assert stats.bad_lag == 1


def test_ingest_log_counts_a_merge_across_logs() -> None:
    """Two logs' counts add up, and the quarters are keyed by start."""
    first, _ = er.ingest_log(Path("a.log"), _replayable_log())
    second, _ = er.ingest_log(Path("b.log"), _replayable_log())
    assert first.keys() == second.keys()


def test_opening_states_back_infers_through_a_pre_dump_transition() -> None:
    """A dump taken after the quarter's first shed still reports the open.

    Production's controller reports the state it last synced, but when the
    dump does reflect the command the transition has to be undone — otherwise
    every acting quarter opens with its loads already off.
    """
    text = "\n".join(
        [
            _shed("20:30:10", "water heater"),
            _cycle_line(candidates=_candidate("water heater", False)),
        ]
    )
    snapshots = fls.parse_cycle_states(text, source="s.log")
    transitions = er.quarter_transitions(
        fls.parse_turn_offs(text, source="s.log"),
        fls.parse_turn_ons(text, source="s.log"),
        _SYNTH_Q,
    )
    assert er.opening_states(snapshots, transitions, _SYNTH_Q) == {
        "water heater": True
    }


def test_opening_states_ignores_a_transition_after_the_dump() -> None:
    """Only what happened before the dump describes the open."""
    text = "\n".join(
        [
            _cycle_line(candidates=_candidate("water heater", True)),
            _shed("20:39:59", "water heater"),
        ]
    )
    snapshots = fls.parse_cycle_states(text, source="s.log")
    transitions = er.quarter_transitions(
        fls.parse_turn_offs(text, source="s.log"),
        fls.parse_turn_ons(text, source="s.log"),
        _SYNTH_Q,
    )
    assert er.opening_states(snapshots, transitions, _SYNTH_Q) == {
        "water heater": True
    }


def test_ingest_log_skips_an_unreadable_path(tmp_path: Path, capsys) -> None:
    """A missing log warns and is skipped; the rest still loads."""
    missing = tmp_path / "nope.log"
    quarters, _ = er.build_quarters([missing])
    assert quarters == {}
    assert "skipping" in capsys.readouterr().err


# ── the Tesla leg of the meter ───────────────────────────────────────


def _tesla_quarter(
    amps: float,
    *,
    predicted_wh: float,
    remaining_secs: int,
    plugged_in: bool = True,
    at_home: bool = True,
    actual_wh: float = 0.0,
    car_active: bool = True,
) -> er.Quarter:
    """One anchor with every plug already off: the car in isolation.

    Plugs off leaves the car as the only actuator, so a landing other than
    ``actual_wh`` is exactly the car's own commanded deviation — the same
    isolation the log-side ``--turn-offs`` collection relies on. ``amps`` is
    also the opening state: one reported sample is back-inferred flat, the
    way ``opening_states`` back-infers a plug.
    """
    samples_used = 900 - remaining_secs
    return er.Quarter(
        name="tesla-synth",
        qh_start=_SYNTH_Q,
        actual_wh=actual_wh,
        decide_lag=8.0,
        initial_on=dict.fromkeys(er.POWERS, False),
        anchors=(
            fls.ForecastAnchor(
                source="tesla-synth",
                quarter_start=_SYNTH_Q,
                data_point_at=_SYNTH_Q + timedelta(seconds=samples_used),
                remaining_secs=remaining_secs,
                samples_used=samples_used,
                prediction_values=30,
                raw_wh=predicted_wh,
                prediction_w=0.0,
                predicted_wh=predicted_wh,
                actual_wh=actual_wh,
                wall_at=None,
                prior_rate=0.0,
            ),
        ),
        events=(),
        tesla_samples=(
            er.TeslaSample(
                t=float(samples_used), amps=amps, plugged_in=plugged_in, at_home=at_home
            ),
        ),
        car_active=car_active,
    )


def test_replay_lands_the_tesla_amp_step_by_its_quantum() -> None:
    """One amp for the remaining window is ``240 W * R / 3600`` Wh.

    11.0 Wh predicted against a -9 Wh target is a 20 Wh deficit; at R=600 s
    one amp is worth 40 Wh, so ``ceil`` books one amp. The command lands one
    decision lag (8 s) after the data point, so the car's draw falls by
    ``240 * (600 - 8) / 3600`` — the lag is part of the model, not slop.
    """
    quarter = _tesla_quarter(6.0, predicted_wh=11.0, remaining_secs=600)
    run = er.replay(quarter, er.rate_status_quo)
    assert run.actions == (("tesla", "set_amps"),)
    assert run.landing_wh == pytest.approx(-240.0 * (600 - 8.0) / 3600.0)


def test_replay_reproduces_the_tesla_ceil_overshoot() -> None:
    """The model books the step it can, not the gap it was asked for.

    22:01 in ``bugs/2026-10-17-small-overshoots.log``: a 76.5 Wh gap at
    R=841 s with the car at 8 A becomes a 2-amp step (production logs
    ``savings=112.1``), 35.6 Wh past the target at decision time. The landing
    integrates from where the command lands, so it books
    ``2 * 240 * (841 - 8) / 3600`` = 111.07 Wh — still 33.6 Wh past a 76.5 Wh
    gap. Pinning it here keeps the overshoot a measured property of the model
    rather than a claim read off an untracked log.
    """
    quarter = _tesla_quarter(8.0, predicted_wh=67.5, remaining_secs=841)
    run = er.replay(quarter, er.rate_status_quo)
    assert run.actions == (("tesla", "set_amps"),)
    assert run.landing_wh == pytest.approx(-2 * 240.0 * (841 - 8.0) / 3600.0)
    assert abs(run.landing_wh) - 76.5 == pytest.approx(34.57, abs=0.01)


def test_replay_never_commands_an_unplugged_car() -> None:
    """A car that is away cannot be asked to draw.

    Production gates both directions on ``plugged_in``; a model that let the
    policy charge an absent car would manufacture the energy out of nothing.
    """
    quarter = _tesla_quarter(
        0.0, predicted_wh=67.5, remaining_secs=841, plugged_in=False
    )
    run = er.replay(quarter, er.rate_status_quo)
    assert run.actions == ()
    assert run.landing_wh == pytest.approx(0.0)


def test_history_mode_lands_on_the_logged_actual_with_a_car() -> None:
    """History pins the car too, so a driven quarter is a framing control."""
    quarter = _tesla_quarter(
        6.0, predicted_wh=11.0, remaining_secs=600, actual_wh=-12.0
    )
    run = er.replay(quarter, er.rate_status_quo, mode="history")
    assert run.landing_wh == pytest.approx(-12.0)
    assert run.action_count == 0


def test_plug_only_quarters_never_command_the_tesla(
    quarter_a: er.Quarter, quarter_b: er.Quarter
) -> None:
    """No car in the meter means no car command, whatever the gap.

    The pinned A/B fixtures carry no ``tesla_samples``, so the Tesla leg must
    stay inert and their verdicts unchanged.
    """
    for quarter in (quarter_a, quarter_b):
        assert quarter.tesla_samples == ()
        run = er.replay(quarter, er.rate_status_quo)
        assert all(device != "tesla" for device, _ in run.actions)


# ── the decision policy ──────────────────────────────────────────────
#
# The estimator is one axis of the replay; the decision knobs are the other
# and are *also* production surface (GapMinder's constructor), so a candidate
# that wins here has a shipping path that is a flag rather than a rewrite.
# Defaults must reproduce the shipped engine exactly, which is what keeps the
# pinned A/B verdicts above from moving.


def _plug_quarter(
    *,
    predicted_wh: float,
    remaining_secs: int,
    all_on: bool,
    prediction_w: float = 0.0,
    actual_wh: float = 0.0,
) -> er.Quarter:
    """One anchor with no car: the plugs in isolation.

    ``all_on`` sets every modelled plug's opening state, so a turn-off test
    starts with candidates to shed and a turn-on test with candidates to
    bring on. There are no logged events, so the counterfactual and the
    history agree until the policy touches something. ``predicted_wh`` is the
    total the engine sees; ``prediction_w`` sets the window rate and the raw
    bank behind it.
    """
    samples_used = 900 - remaining_secs
    raw_wh = predicted_wh - prediction_w * remaining_secs
    return er.Quarter(
        name="plug-synth",
        qh_start=_SYNTH_Q,
        actual_wh=actual_wh,
        decide_lag=8.0,
        initial_on=dict.fromkeys(er.POWERS, all_on),
        anchors=(
            fls.ForecastAnchor(
                source="plug-synth",
                quarter_start=_SYNTH_Q,
                data_point_at=_SYNTH_Q + timedelta(seconds=samples_used),
                remaining_secs=remaining_secs,
                samples_used=samples_used,
                prediction_values=30,
                raw_wh=raw_wh,
                prediction_w=prediction_w,
                predicted_wh=predicted_wh,
                actual_wh=actual_wh,
                wall_at=None,
                prior_rate=0.0,
            ),
        ),
        events=(),
    )


def test_policy_defaults_are_the_shipped_engine() -> None:
    """The default policy is production's, and the header says so."""
    engine = er.Policy().engine()
    assert engine.HYSTERESIS_WH == er.HYSTERESIS_WH
    assert engine.TURN_ON_MARGIN_WH == 0.0
    assert engine.TURN_OFF_MARGIN_WH == 0.0
    assert engine.lumpy_first is False
    assert er.Policy().label == (
        "turn-on margin 0 Wh, turn-off margin 0 Wh, lumpy-first off, "
        "defer on, defer cap 120 s, ramp-aware off"
    )


def test_turn_off_margin_withholds_a_coarse_shed() -> None:
    """A margin as wide as the load's quantum declines the shed outright.

    This is the overshoot mechanism in miniature: at R=600 s the 260 W plug
    is worth 43.3 Wh, so a 14 Wh deficit cannot be closed without booking
    more than three times what was asked. Booking ``14 - 50`` asks for
    nothing, and the cheap plug stays on — the alternative to overshooting is
    not acting, which is the trade the margin buys.
    """
    quarter = _plug_quarter(predicted_wh=5.0, remaining_secs=600, all_on=True)
    shipped = er.replay(quarter, er.rate_status_quo)
    withheld = er.replay(
        quarter, er.rate_status_quo, policy=er.Policy(turn_off_margin_wh=50)
    )
    assert shipped.actions == (("ecoflow", "turn_off"),)
    # The command lands one decision lag after the data point (t = 300 s
    # here), so the realized divergence is the 592 s the plug was actually
    # off — not the 43.3 Wh booked at decision time.
    assert shipped.landing_wh == pytest.approx(-260.0 * 592 / 3600.0)
    assert withheld.actions == ()
    assert withheld.landing_wh == pytest.approx(quarter.actual_wh)


def test_turn_on_margin_keeps_the_surplus_unspent() -> None:
    """The same knob on the surplus side: a withheld budget buys nothing."""
    quarter = _plug_quarter(predicted_wh=-80.0, remaining_secs=600, all_on=False)
    shipped = er.replay(quarter, er.rate_status_quo)
    withheld = er.replay(
        quarter, er.rate_status_quo, policy=er.Policy(turn_on_margin_wh=50)
    )
    assert shipped.action_count == 1
    assert withheld.actions == ()


def test_banked_energy_unlocks_the_lumpy_first_gate() -> None:
    """Lumpy-first is unreachable unless the bank reaches the gate.

    Production reads ``ctx.banked_wh`` (``qh1.raw_wh``) to learn the trailing
    rate the lumpy load must be implicated in; a replay that left it None
    made ``--lumpy-first`` a silent no-op on every quarter. With the bank
    supplied, the 4,857 W heater leads the shed instead of following the
    260 W plugs — the smalls cover only 86.7 Wh of this 200 Wh deficit.
    """
    quarter = _plug_quarter(
        predicted_wh=191.0, remaining_secs=600, all_on=True, prediction_w=-1.35
    )
    shipped = er.replay(quarter, er.rate_status_quo)
    lumpy = er.replay(quarter, er.rate_status_quo, policy=er.Policy(lumpy_first=True))
    assert shipped.actions == (
        ("ecoflow", "turn_off"),
        ("water heater", "turn_off"),
    )
    assert lumpy.actions == (("water heater", "turn_off"),)


def test_shipped_fit_declines_a_load_that_cannot_hold_the_window() -> None:
    """Whole-window fit (shipped): a 21 Wh surplus cannot hold a 260 W plug.

    At R=600 s the plug's held cost is 43.3 Wh, so the surplus stays in the
    meter and the plug never starts. The dispatch-quantum alternative (price
    only the first dispatch) was measured a variance trade and removed
    2026-10-18 (see ``AGENTS.md``).
    """
    quarter = _plug_quarter(predicted_wh=-30.0, remaining_secs=600, all_on=False)
    shipped = er.replay(quarter, er.rate_status_quo)
    assert shipped.actions == ()


def test_a_default_policy_is_the_shipped_replay(
    quarter_a: er.Quarter, quarter_b: er.Quarter
) -> None:
    """Flag-off parity: the pinned verdicts cannot move under the new axis."""
    for quarter in (quarter_a, quarter_b):
        assert er.replay(
            quarter, er.rate_status_quo, policy=er.Policy()
        ) == er.replay(quarter, er.rate_status_quo)


def test_replay_all_forwards_the_policy() -> None:
    """The aggregate path carries the knobs, not just the single quarter."""
    quarter = _plug_quarter(predicted_wh=5.0, remaining_secs=600, all_on=True)
    quarters = {quarter.qh_start: quarter}
    shipped = er.replay_all(quarters, er.rate_status_quo)
    withheld = er.replay_all(
        quarters, er.rate_status_quo, policy=er.Policy(turn_off_margin_wh=50)
    )
    assert shipped[quarter.qh_start].actions == 1
    assert withheld[quarter.qh_start].actions == 0


# ── the rate-window axis (trailing horizon, in quanta) ───────────────
#
# The shipped rate is the *latest* end-of-quantum reading held forward: the
# meter reports a power per quantum and the shipped window equals the quantum,
# so the trailing mean uses one quantum of history. A longer window is a
# bias-variance question — does averaging k quanta beat the single reading? —
# and the knob must move *only* the rate: the cumulative energy the engine is
# handed (``raw_wh``, the gate's banked side) and the decision cadence stay
# fixed.


def _window_quarter() -> er.Quarter:
    """Four 30 s windows with different rates, so the horizon is legible.

    Window energies 10/60/30/20 Wh over (0,30]/(30,60]/(60,90]/(90,120] s give
    window rates 0.333/2.0/1.0/0.667 Wh/s. At the last anchor the shipped
    1-quantum rate is 0.667, the 2-quantum mean is (30+20)/60 = 0.833, the
    3-quantum mean is (60+30+20)/90 = 1.222 and the whole-quarter mean is
    120/120 = 1.0.
    """
    rows = (
        (30, 10.0, 10.0 / 30.0),
        (60, 70.0, 60.0 / 30.0),
        (90, 100.0, 30.0 / 30.0),
        (120, 120.0, 20.0 / 30.0),
    )
    return er.Quarter(
        name="window-synth",
        qh_start=_SYNTH_Q,
        actual_wh=0.0,
        decide_lag=8.0,
        initial_on=dict.fromkeys(er.POWERS, False),
        anchors=tuple(
            fls.ForecastAnchor(
                source="window-synth",
                quarter_start=_SYNTH_Q,
                data_point_at=_SYNTH_Q + timedelta(seconds=used),
                remaining_secs=900 - used,
                samples_used=used,
                prediction_values=30,
                raw_wh=raw_wh,
                prediction_w=prediction_w,
                predicted_wh=raw_wh + prediction_w * (900 - used),
                actual_wh=0.0,
                wall_at=None,
                prior_rate=0.0,
            )
            for used, raw_wh, prediction_w in rows
        ),
        events=(),
    )


def test_rate_window_one_is_the_shipped_estimator() -> None:
    """k = 1 takes the shipped path: the anchor's own window mean."""
    seen: list[float] = []

    def spy(w_cf: float, _anchor: fls.ForecastAnchor) -> float:
        seen.append(w_cf)
        return w_cf

    quarter = _window_quarter()
    er.replay(quarter, spy)
    er.replay(quarter, spy, rate_window_quanta=1)
    assert seen[:4] == pytest.approx(seen[4:], abs=1e-9)


def test_rate_window_re_averages_the_counterfactual_meter() -> None:
    """k quanta means the last k windows; 'all' means the elapsed quarter."""
    seen: list[float] = []

    def spy(w_cf: float, _anchor: fls.ForecastAnchor) -> float:
        seen.append(w_cf)
        return w_cf

    quarter = _window_quarter()
    er.replay(quarter, spy)
    shipped = seen[-1]
    seen.clear()
    er.replay(quarter, spy, rate_window_quanta=2)
    assert seen[-1] == pytest.approx(50.0 / 60.0, abs=1e-6)  # (30+20)/60
    assert seen[-1] != pytest.approx(shipped)
    seen.clear()
    er.replay(quarter, spy, rate_window_quanta=3)
    assert seen[-1] == pytest.approx(110.0 / 90.0, abs=1e-6)  # (60+30+20)/90
    seen.clear()
    er.replay(quarter, spy, rate_window_quanta="all")
    assert seen[-1] == pytest.approx(120.0 / 120.0, abs=1e-6)


def test_rate_window_leaves_the_banked_energy_untouched() -> None:
    """Only the rate moves; the cumulative ``raw_wh`` is identical.

    The engine's ``banked_wh`` and the gate's cumulative side read ``raw_wh``,
    so this is what "the window is only the rate's business" means in the
    replay: the knob cannot leak into the gate.
    """
    quarter = _window_quarter()
    for anchor in quarter.anchors:
        shipped = er._counterfactual(quarter, anchor, (), ())
        wide = er._counterfactual(quarter, anchor, (), (), 60.0)
        assert shipped.raw_wh == pytest.approx(wide.raw_wh)


def test_rate_window_does_not_move_the_cadence() -> None:
    """The knob re-averages the rate only; every anchor still decides."""
    quarter = _window_quarter()
    base = er.replay(quarter, er.rate_status_quo)
    wide = er.replay(quarter, er.rate_status_quo, rate_window_quanta=4)
    assert len(base.steps) == len(wide.steps) == len(quarter.anchors)


def test_replay_all_forwards_the_rate_window() -> None:
    """The aggregate path carries the window, not just the single quarter."""
    seen: list[float] = []

    def spy(w_cf: float, _anchor: fls.ForecastAnchor) -> float:
        seen.append(w_cf)
        return w_cf

    quarter = _window_quarter()
    er.replay_all({quarter.qh_start: quarter}, spy, rate_window_quanta=2)
    assert seen[-1] == pytest.approx(50.0 / 60.0, abs=1e-6)


@pytest.mark.parametrize("bad", [0, -1])
def test_replay_rejects_a_degenerate_rate_window(bad: int) -> None:
    """Below one quantum is not a window; k = 1 is the shipped status-quo."""
    with pytest.raises(ValueError):
        er.replay(_window_quarter(), er.rate_status_quo, rate_window_quanta=bad)


# ── the reporting layer ──────────────────────────────────────────────


def test_summarize_variant_averages_absolute_miss() -> None:
    """|miss| drives the verdict; the signed mean is reported alongside."""
    runs = {
        _SYNTH_Q: er.QuarterRun(miss_wh=-20.0, actions=2, reversals=1),
        _SYNTH_Q + timedelta(minutes=15): er.QuarterRun(
            miss_wh=10.0, actions=4, reversals=1
        ),
    }
    summary = er.summarize_variant("x", runs)
    assert summary.quarters == 2
    assert summary.mean_abs_miss_wh == pytest.approx(15.0)
    assert summary.mean_miss_wh == pytest.approx(-5.0)
    assert summary.mean_actions == pytest.approx(3.0)
    assert summary.mean_reversals == pytest.approx(1.0)


def test_compare_variants_flags_an_improvement() -> None:
    """A smaller |miss| counts as better, with the delta signed."""
    base = {_SYNTH_Q: er.QuarterRun(miss_wh=10.0, actions=2, reversals=1)}
    other = {_SYNTH_Q: er.QuarterRun(miss_wh=-4.0, actions=1, reversals=0)}
    comparison = er.compare_variants(base, other)
    assert (comparison.better, comparison.worse, comparison.same) == (1, 0, 0)
    assert comparison.changed == 1
    assert comparison.mean_delta_wh == pytest.approx(-6.0)


def test_compare_variants_flags_a_regression() -> None:
    """The regime-change case: worse |miss|, worse churn."""
    base = {_SYNTH_Q: er.QuarterRun(miss_wh=2.0, actions=1, reversals=0)}
    other = {_SYNTH_Q: er.QuarterRun(miss_wh=9.0, actions=3, reversals=2)}
    comparison = er.compare_variants(base, other)
    assert (comparison.better, comparison.worse) == (0, 1)
    assert comparison.worst_delta_wh == pytest.approx(7.0)
    assert comparison.p90_delta_wh == pytest.approx(7.0)


def test_rank_regressions_puts_the_biggest_loss_first() -> None:
    """The regression table is ordered by |miss| damage, not absolute miss."""
    later = _SYNTH_Q + timedelta(minutes=15)
    base = {
        _SYNTH_Q: er.QuarterRun(miss_wh=1.0, actions=0, reversals=0),
        later: er.QuarterRun(miss_wh=50.0, actions=0, reversals=0),
    }
    other = {
        _SYNTH_Q: er.QuarterRun(miss_wh=30.0, actions=0, reversals=0),
        later: er.QuarterRun(miss_wh=2.0, actions=0, reversals=0),
    }
    rows = er.rank_regressions(base, other, limit=2)
    assert [row[0] for row in rows] == [_SYNTH_Q, later]
    assert format(rows[0][2].miss_wh) == "30.0"


def test_select_regime_separates_car_and_plug_only_quarters(
    quarter_a: er.Quarter,
) -> None:
    """The verdict has to be readable per metering regime.

    Adding a car to the meter changes what the corpus measures, so the two
    populations (the car was in play, or the plugs were the only actuators)
    must be separable — the extended numbers cannot be read either way
    otherwise.
    """
    car = _tesla_quarter(6.0, predicted_wh=11.0, remaining_secs=600)
    idle = _tesla_quarter(
        0.0, predicted_wh=11.0, remaining_secs=600, plugged_in=False,
        car_active=False,
    )
    quarters = {car.qh_start: car, quarter_a.qh_start: quarter_a}
    assert set(er.select_regime(quarters, "tesla")) == {car.qh_start}
    assert set(er.select_regime(quarters, "plug-only")) == {quarter_a.qh_start}
    assert set(er.select_regime(quarters, "all")) == set(quarters)
    assert er.select_regime({idle.qh_start: idle}, "plug-only") == {
        idle.qh_start: idle
    }
    with pytest.raises(ValueError, match="unknown regime"):
        er.select_regime(quarters, "nope")


def test_variants_carry_the_documented_names_and_fresh_state() -> None:
    """Trend-gated holds tracker state, so it must not be shared between runs."""
    first = er.variants()
    second = er.variants()
    assert list(first) == ["status-quo", "prior", "prior-blend@60", "trend-gated"]
    assert first["status-quo"] is er.rate_status_quo
    assert first["trend-gated"] is not second["trend-gated"]


def test_trend_gated_variant_runs_and_tracks_each_quarter() -> None:
    """With no trusted trend the gated variant falls back on the prior.

    A single anchor cannot establish a trend, so the gated estimator must
    return the prior rate — the fallback that keeps regime-change quarters
    honest is the same code path.
    """
    anchor = _build_quarter(
        "synthetic", "2026-10-14 23:15", 8.0, 5.0, ((60, 840, 30, 74.0, 1.2, -0.01),), ()
    ).anchors[0]
    assert er.rate_trend_gated()(0.5, anchor) == pytest.approx(-0.01)


# ── the CLI ──────────────────────────────────────────────────────────


def test_main_reports_the_variant_table(tmp_path: Path, capsys) -> None:
    """The tool runs end to end on a log file and prints the comparison."""
    log = tmp_path / "synth.log"
    log.write_text(_replayable_log())
    assert er.main([str(log), "--variant", "prior"]) == 0
    out = capsys.readouterr().out
    assert "1 scored, 1 replayable" in out
    assert "prior" in out
    assert "prior vs status-quo |miss|" in out


def test_main_rejects_an_unknown_variant(capsys) -> None:
    """A typo cannot silently analyse nothing."""
    assert er.main(["--variant", "nope"]) == 1
    assert "unknown variant" in capsys.readouterr().err


def test_main_fails_when_a_log_holds_no_replayable_quarter(
    tmp_path: Path, capsys
) -> None:
    """No quarters is an error, not an empty success."""
    log = tmp_path / "empty.log"
    log.write_text("nothing to see here\n")
    assert er.main([str(log)]) == 1
    assert "no replayable quarters" in capsys.readouterr().err


def test_main_reports_the_policy_it_ran(tmp_path: Path, capsys) -> None:
    """The decision knobs are part of the result's identity, so they show.

    A margin sweep is only readable if each run says which policy produced
    it; the flags compose with ``--regime`` and ``--variant`` rather than
    replacing them.
    """
    log = tmp_path / "synth.log"
    log.write_text(_replayable_log())
    assert er.main([str(log), "--turn-off-margin", "40"]) == 0
    assert (
        "policy: turn-on margin 0 Wh, turn-off margin 40 Wh, lumpy-first off, "
        "defer on, defer cap 120 s, ramp-aware off" in capsys.readouterr().out
    )
    assert er.main([str(log), "--lumpy-first"]) == 0
    assert (
        "policy: turn-on margin 0 Wh, turn-off margin 0 Wh, lumpy-first on, "
        "defer on, defer cap 120 s, ramp-aware off" in capsys.readouterr().out
    )


def test_main_restricts_to_one_date(tmp_path: Path, capsys) -> None:
    """``--date`` filters the quarter set after ingestion."""
    log = tmp_path / "synth.log"
    log.write_text(_replayable_log())
    assert er.main([str(log), "--variant", "prior", "--date", "2026-10-01"]) == 0
    assert "restricted to 2026-10-01" in capsys.readouterr().out
    assert er.main([str(log), "--date", "2026-10-02"]) == 1


def test_main_reports_the_regime_it_measured(tmp_path: Path, capsys) -> None:
    """``--regime`` filters after ingestion and names the population.

    The idle-car log is one quarter where the plugs were the only actuators,
    so a car-active request has nothing to measure and says so instead of
    printing zeros.
    """
    log = tmp_path / "synth.log"
    log.write_text(_replayable_log())
    assert er.main([str(log), "--regime", "tesla"]) == 1
    assert "no replayable quarters" in capsys.readouterr().err
    assert er.main([str(log), "--regime", "plug-only"]) == 0
    out = capsys.readouterr().out
    assert "regime: plug-only (1 quarter(s))" in out


def test_main_adds_window_variants(tmp_path: Path, capsys) -> None:
    """``--rate-window-quanta`` adds a re-averaged shipped-rate variant."""
    log = tmp_path / "synth.log"
    log.write_text(_replayable_log())
    assert er.main([str(log), "--rate-window-quanta", "2,all"]) == 0
    out = capsys.readouterr().out
    assert "rate windows: window@2q, window@all" in out
    assert "window@2q vs status-quo |miss|" in out
    assert "window@all vs status-quo |miss|" in out


def test_main_default_output_carries_no_window_rows(
    tmp_path: Path, capsys
) -> None:
    """Flag-off parity: without the flag the report is the shipped one."""
    log = tmp_path / "synth.log"
    log.write_text(_replayable_log())
    assert er.main([str(log)]) == 0
    out = capsys.readouterr().out
    assert "window@" not in out
    assert "rate windows:" not in out


def test_main_rejects_a_degenerate_rate_window(tmp_path: Path, capsys) -> None:
    """k = 1 is the shipped status-quo, so it is not an accepted value."""
    log = tmp_path / "synth.log"
    log.write_text(_replayable_log())
    assert er.main([str(log), "--rate-window-quanta", "1"]) == 1
    assert "rate window" in capsys.readouterr().err
    assert er.main([str(log), "--rate-window-quanta", "wide"]) == 1


# ── fixture drift (needs the logs; skipped without them) ─────────────


@_NEEDS_LOGS
def test_checked_in_fixtures_still_match_the_logs() -> None:
    """Re-parse the logs and fail if the checked-in fixtures went stale.

    Ingestion now supplies the anchors, actual and events (and derives its
    own lag from the fetch line), so this also pins that the tool's own
    loader still agrees with the hand-extracted fixtures.
    """
    for log, fixture in (
        (_LOG_A, (QUARTER_A_ACTUAL, QUARTER_A_ANCHORS, QUARTER_A_EVENTS)),
        (_LOG_B, (QUARTER_B_ACTUAL, QUARTER_B_ANCHORS, QUARTER_B_EVENTS)),
    ):
        name, label, _lag = log
        actual_wh, anchors, events = fixture
        path = _BUGS / name
        quarters, _ = er.ingest_log(path, path.read_text(errors="replace"))
        qh_start = datetime.strptime(label, "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone.utc
        )
        assert qh_start in quarters, f"{label} no longer ingests"
        parsed = quarters[qh_start]
        assert parsed.actual_wh == pytest.approx(actual_wh, abs=1e-6)
        assert len(parsed.anchors) == len(anchors)
        for got, want in zip(parsed.anchors, anchors):
            samples, remaining, values, raw, watts, prior = want
            assert (got.samples_used, got.remaining_secs, got.prediction_values) == (
                samples,
                remaining,
                values,
            )
            assert got.raw_wh == pytest.approx(raw, abs=1e-5)
            assert got.prediction_w == pytest.approx(watts, abs=1e-5)
            assert (got.prior_rate or 0.0) == pytest.approx(prior, abs=1e-8)
        assert len(parsed.events) == len(events)
        for got_event, want_event in zip(parsed.events, sorted(events)):
            assert got_event[1:] == want_event[1:]
            assert got_event[0] == pytest.approx(want_event[0], abs=1.0)


# ── the guard arm (Subtask 2) ────────────────────────────────────────
#
# Until now the replay modelled a machine production does not run: the
# ``DecideContext`` it built omitted ``gap_jitter_wh_per_s`` and
# ``gap_trend_wh_per_s``, so the turn-on jitter guard, its banked-cover
# override and the ramp-aware Tesla stop could never fire. These tests pin the
# fidelity wiring (one tracker per quarter, fed production's own order and
# inputs), the inert arm's bit-parity with the published numbers, and the
# decline/rescue split the report has to name.


def _churning_quarter(banked_wh: float = 20.0) -> er.Quarter:
    """A quarter whose gap oscillates, so the tracker measures churn.

    Four anchors 30 s apart with the gap alternating 40 / 5 Wh. Two slopes of
    opposite sign make churn ~2.33 Wh/s — an order above the guard's floor —
    and the projected swing at that horizon swamps either claim, so the
    guard fires on *both* cycles that reach the turn-on side (5 Wh is past
    the 3 Wh deadband, so the second one is not swallowed by hysteresis).
    The first two anchors decide nothing (the cheapest 260 W plug costs 43.3
    Wh over 600 s, more than either surplus), which is what keeps the two
    arms' gap series identical up to the first decline.

    ``banked_wh`` is the quarter's accumulated energy: +20 Wh sits above the
    bar the cover needs (no override, so the guard's fire is a decline),
    while a deep export grants the cover and the fire becomes a rescue.
    """
    anchors = []
    for index, gap in enumerate((40.0, 5.0, 40.0, 5.0)):
        remaining = 600 - 30 * index
        predicted = er.TARGET_WH - gap
        anchors.append(
            (900 - remaining, remaining, 30, banked_wh,
             (predicted - banked_wh) / remaining, None)
        )
    return _build_quarter(
        "churn",
        "2026-10-16 12:00",
        8.0,
        11.0,
        anchors,
        (),
        initial_on=dict.fromkeys(er.POWERS, False),
    )


def test_guard_arm_defaults_to_inert_and_is_bit_identical() -> None:
    """The shipped arm is the default, and it carries no guard activity."""
    quarter = _churning_quarter()
    default = er.replay(quarter, er.rate_status_quo)
    inert = er.replay(quarter, er.rate_status_quo, guards="inert")
    assert default == inert
    assert inert.guard == er.GuardActivity()


def test_live_guard_declines_a_churning_turn_on() -> None:
    """With the tracker fed, the guard withholds the turn-ons it protects."""
    quarter = _churning_quarter()
    inert = er.replay(quarter, er.rate_status_quo)
    live = er.replay(quarter, er.rate_status_quo, guards="live")
    assert inert.guard.declines == 0
    # Both cycles that claim a surplus are declined: the inert arm starts a
    # plug into the first one and sheds it again one cycle later, so the arm
    # suppresses two commands and lands back on the logged actual.
    assert live.guard.declines == 2
    assert inert.action_count == 2
    assert live.action_count == 0
    assert live.landing_wh != inert.landing_wh
    # An oscillating gap is not a ramp: the trend stays untrusted, so the
    # ramp rule had nothing to hasten here.
    assert live.guard.trusted_trend_cycles == 0


def test_live_guard_override_releases_a_covered_turn_on() -> None:
    """A bank deep enough to cover the load is a rescue, not a decline.

    The rescue happens at the first fire, so the rest of the quarter then
    unfolds exactly as it does in the inert arm — including the shed that
    follows, which the guard never touches (turning off is not guarded).
    """
    quarter = _churning_quarter(banked_wh=-49.0)
    inert = er.replay(quarter, er.rate_status_quo)
    live = er.replay(quarter, er.rate_status_quo, guards="live")
    assert live.guard.declines == 0
    assert live.guard.rescues == 1
    assert live.steps == inert.steps
    assert live.landing_wh == inert.landing_wh


def test_live_arm_feeds_the_tracker_productions_own_signals() -> None:
    """The ctx decide() reads carries the churn/trend the tracker measured."""
    seen: list[er.DecideContext] = []
    original = er.GapMinder.decide

    def spy(self, ctx, predicted_wh, target_wh):  # type: ignore[no-untyped-def]
        seen.append(ctx)
        return original(self, ctx, predicted_wh, target_wh)

    er.GapMinder.decide = spy  # type: ignore[method-assign]
    try:
        er.replay(_churning_quarter(), er.rate_status_quo, guards="live")
        live_seen = list(seen)
        seen.clear()
        er.replay(_churning_quarter(), er.rate_status_quo, guards="churn")
        churn_seen = list(seen)
        seen.clear()
        er.replay(_churning_quarter(), er.rate_status_quo, guards="inert")
        inert_seen = list(seen)
    finally:
        er.GapMinder.decide = original  # type: ignore[method-assign]
    # Two cycles of samples are not enough for churn; the third measures it.
    assert live_seen[0].gap_jitter_wh_per_s == 0.0
    assert live_seen[2].gap_jitter_wh_per_s > JITTER_FLOOR_WH_PER_S
    assert all(ctx.gap_trend_wh_per_s is None for ctx in live_seen)
    # The churn arm feeds the same jitter signal but not the ramp rule's
    # trend — that separation is the whole point of the control.
    assert [ctx.gap_jitter_wh_per_s for ctx in churn_seen] == [
        ctx.gap_jitter_wh_per_s for ctx in live_seen
    ]
    assert all(ctx.gap_trend_wh_per_s is None for ctx in churn_seen)
    # The inert arm leaves both dark, which is the old machine.
    assert all(ctx.gap_jitter_wh_per_s is None for ctx in inert_seen)
    assert all(ctx.gap_trend_wh_per_s is None for ctx in inert_seen)


def test_churn_arm_isolates_the_ramp_rule_signal() -> None:
    """With no car to command, ``churn`` and ``live`` are the same machine.

    That identity is what licenses reading their difference elsewhere as the
    ramp rule's own effect rather than the bundle's.
    """
    quarter = _churning_quarter()
    churn = er.replay(quarter, er.rate_status_quo, guards="churn")
    live = er.replay(quarter, er.rate_status_quo, guards="live")
    assert churn == live
    assert churn.guard.declines > 0  # the guard is live in both


def test_guard_arm_rejects_an_unknown_name() -> None:
    """A typo is an error, not a silent fallback to inert."""
    with pytest.raises(ValueError):
        er.replay(_churning_quarter(), er.rate_status_quo, guards="sometimes")


def test_replay_all_forwards_the_guard_arm() -> None:
    """The aggregate path carries the arm, not just the single quarter."""
    quarter = _churning_quarter()
    quarters = {quarter.qh_start: quarter}
    inert = er.replay_all(quarters, er.rate_status_quo)
    live = er.replay_all(quarters, er.rate_status_quo, guards="live")
    assert inert[quarter.qh_start].guard.declines == 0
    assert live[quarter.qh_start].guard.declines == 2


def test_format_guard_arm_names_the_mechanisms_and_the_control() -> None:
    """The verdict block says what fired, against which controls, where."""
    quarter = _churning_quarter()
    quarters = {quarter.qh_start: quarter}
    body = er.format_guard_arm(
        er.replay_all(quarters, er.rate_status_quo, guards="live"),
        churn=er.replay_all(quarters, er.rate_status_quo, guards="churn"),
        inert=er.replay_all(quarters, er.rate_status_quo),
        rate_name="status-quo",
        worst=1,
    )
    assert "guard arm: live" in body
    assert "status-quo" in body
    assert "jitter guard" in body
    assert "decline" in body
    assert "rescue" in body
    assert "trusted-trend" in body
    assert "live vs inert" in body
    # The ramp rule gets its own line, and the changed quarters are listed.
    assert "churn-only vs inert" in body
    assert "live vs churn-only" in body
    assert "quarters the arm moved" in body
    assert "--guards" in body


def test_main_prints_the_guard_arm_only_for_live(tmp_path: Path, capsys) -> None:
    """``--guards inert`` is the shipped report; live adds the verdict block."""
    log = tmp_path / "synth.log"
    log.write_text(_replayable_log())
    assert er.main([str(log)]) == 0
    default = capsys.readouterr().out
    assert "guard arm" not in default
    assert er.main([str(log), "--guards", "inert"]) == 0
    assert capsys.readouterr().out == default
    assert er.main([str(log), "--guards", "live"]) == 0
    live = capsys.readouterr().out
    assert "guard arm: live" in live
    assert "scanned 1 log(s)" in live


def test_main_rejects_an_unknown_guard_arm(tmp_path: Path) -> None:
    """argparse owns the arm vocabulary, so a typo never runs silently."""
    log = tmp_path / "synth.log"
    log.write_text(_replayable_log())
    with pytest.raises(SystemExit):
        er.main([str(log), "--guards", "sometimes"])
