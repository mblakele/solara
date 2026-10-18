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
actual plus the integrated ON-time divergence over the quarter. Only
``rate_fn`` differs between runs, and it takes the counterfactual window rate
``w_cf`` so the variant's formula is identical to
``tools.forecast_log_scoring.estimate_prior_only`` / ``estimate_prior_blend``
(pinned by test, so the replay cannot evaluate a different variant than the
scorer scored).

Runs:
  * ``history`` — both sides see the logged events, so the landing is the
    logged actual by construction. The framing control.
  * ``status-quo`` — production's trailing-window rate through ``decide()``.
  * variant — the estimator's rate through the same ``decide()``.

Scope: the two plug-only quarters this model covers (no Tesla activity).
Nameplate watts stand in for measured draw, so absolute landings are model
values; the *deltas against the status-quo run* are the result, following the
``test_tesla_overshoot_replay`` convention.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import pytest

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
# The corpus replay (1,107 clean quarters of 13 logs, tools/estimator_replay.py)
# confirmed it at scale and killed the whole line: status-quo 23.56 mean |miss|
# against prior 28.25, prior-blend@60 26.64 and trend-gated 24.92, with churn
# roughly halved (2.79 -> 1.40 actions/quarter). Every smoother variant is
# worse closed-loop even though the shipped window is 2x worse open-loop
# (27.61 vs 13.98 mean |e|) — a 30 s loop pays for responsiveness, not
# smoothness.


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


def test_ingest_log_drops_a_quarter_where_the_tesla_may_draw() -> None:
    """The meter is plugs-only, so a charging quarter is unusable."""
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
    assert quarters == {}
    assert stats.tesla == 1


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


def test_main_restricts_to_one_date(tmp_path: Path, capsys) -> None:
    """``--date`` filters the quarter set after ingestion."""
    log = tmp_path / "synth.log"
    log.write_text(_replayable_log())
    assert er.main([str(log), "--variant", "prior", "--date", "2026-10-01"]) == 0
    assert "restricted to 2026-10-01" in capsys.readouterr().out
    assert er.main([str(log), "--date", "2026-10-02"]) == 1


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
