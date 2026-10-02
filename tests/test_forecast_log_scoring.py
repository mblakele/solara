"""Tests for the offline forecast replay parser (plan option 1, pass A).

The harness scores forecast candidates against completed-quarter actuals
recovered from production logs. These tests pin the *parser*: quarter
identity, actual resolution, and the derived error quantities.

Each fetch logs ``nbc_set len N`` immediately *before* the
``create_metrics result`` line reporting the same ``N``, so ``_fetch`` below
emits that order.

`bugs/` is gitignored, so tests that need real logs opt in via
`@pytest.mark.skipif`. Everything else runs on synthetic log text.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.forecast_log_scoring import (
    ForecastAnchor,
    iter_quarters,
    parse_log,
    scored_anchors,
)

_BUGS = Path(__file__).resolve().parents[1] / "bugs"
_OVERSHOOT = _BUGS / "2026-10-10-tesla-overshoot.log"
_NEEDS_REAL_LOG = pytest.mark.skipif(
    not _OVERSHOOT.exists(), reason="bugs/ is gitignored; log not present"
)

START = "2026-10-01 20:30:00"
COMPLETED_TAIL = (
    ", prediction_values=None, prediction_w=None, predicted_wh=None, "
    "remaining_seconds=None, samples_used=None)"
)


def _nbc(
    length: int,
    raw_wh: float,
    prediction_w: float,
    predicted_wh: float,
    remaining: int,
    used: int,
    complete: bool = False,
    complete_q2: float | None = None,
) -> str:
    """An ``nbc_set`` line for one fetch."""
    parts = [
        f"qh1=NBCQuarter(complete={complete}, raw_wh={raw_wh!r}, wh=0, "
        f"prediction_values=30, prediction_w={prediction_w!r}, "
        f"predicted_wh={predicted_wh!r}, remaining_seconds={remaining}, "
        f"samples_used={used})"
    ]
    for name, val in (("qh2", complete_q2), ("qh3", None)):
        raw = "None" if val is None else repr(val)
        parts.append(
            f"{name}=NBCQuarter(complete=True, raw_wh={raw}, wh=0.0"
            f"{COMPLETED_TAIL}"
        )
    return (
        f"[2026-10-01 20:40:34 +0000] [980472] [DEBUG] app nbc_set len {length} "
        f"(NBCQuarterSet({', '.join(parts)})))"
    )


def _fetch(
    total: int,
    raw_wh: float,
    prediction_w: float,
    predicted_wh: float,
    remaining: int,
    used: int,
    data_start: str = START,
    complete: bool = False,
    complete_q2: float | None = None,
) -> str:
    """One fetch: the nbc_set line, then its data_start line (real order)."""
    return (
        _nbc(total, raw_wh, prediction_w, predicted_wh, remaining, used, complete, complete_q2)
        + "\n"
        + f"[2026-10-01 20:40:34 +0000] [980472] [DEBUG] app create_metrics "
        f"result: devices=1, data_start={data_start}+00:00, "
        f"per_second_data_total={total}"
    )


def _parse(*lines: str) -> list[ForecastAnchor]:
    return parse_log("\n".join(lines), source="synthetic.log")


# ── quarter identity ─────────────────────────────────────────────────


def test_anchor_quarter_start_from_data_start() -> None:
    """Anchors within the first 900 samples belong to the data_start quarter."""
    got = _parse(_fetch(571, -100.238065, -0.0125, -104.350565, 329, 571))
    assert len(got) == 1
    a = got[0]
    assert a.quarter_start == datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)
    assert a.data_point_at == datetime(2026, 10, 1, 20, 39, 30, tzinfo=timezone.utc)
    assert a.remaining_secs == 329
    assert a.samples_used == 571


def test_anchor_quarter_start_rolls_past_ninety_minutes() -> None:
    """Once samples exceed 900, qh1 is the *next* quarter, not data_start."""
    got = _parse(_fetch(931, 18.775313333333333, 0.605, 544.5378, 869, 31))
    a = got[0]
    assert a.quarter_start == datetime(2026, 10, 1, 20, 45, tzinfo=timezone.utc)
    assert a.samples_used == 31
    assert a.remaining_secs == 869


def test_misaligned_data_start_is_skipped() -> None:
    """A non-quarter-hour data_start makes the offset arithmetic meaningless."""
    assert _parse(_fetch(571, -100.0, -0.0125, -104.0, 329, 571, data_start="2026-10-01 20:30:07")) == []


def test_nbc_line_never_followed_by_data_start_is_skipped() -> None:
    """Without the pairing line there is no way to place the quarter."""
    assert _parse(_nbc(571, -100.0, -0.0125, -104.0, 329, 571)) == []


def test_length_mismatch_between_pair_is_skipped() -> None:
    """nbc_set len N but per_second_data_total M cannot be the same fetch."""
    lines = (
        _nbc(571, -100.0, -0.0125, -104.0, 329, 571)
        + "\n"
        + "[2026-10-01 20:40:34 +0000] [980472] [DEBUG] app create_metrics result: "
        f"devices=1, data_start={START}+00:00, per_second_data_total=999"
    )
    assert _parse(lines) == []


def test_complete_qh1_is_not_an_anchor() -> None:
    """Complete quarters are ground truth, not forecast anchors."""
    assert _parse(_fetch(571, -100.0, -0.0125, -104.0, 329, 571, complete=True)) == []


# ── actual resolution ────────────────────────────────────────────────


def test_actual_resolved_from_later_qh2() -> None:
    """The quarter's real total arrives as qh2 on a subsequent fetch."""
    got = _parse(
        _fetch(571, -100.238065, -0.0125, -104.350565, 329, 571),
        _fetch(931, 18.775313333333333, 0.605, 544.5378, 869, 31, complete_q2=1.7394091666666602),
    )
    a = got[0]
    assert a.actual_wh == pytest.approx(1.7394091666666602)
    assert a.error == pytest.approx(-104.350565 - 1.7394091666666602)
    assert a.horizon_ratio == pytest.approx(329 / 30)


def test_actual_resolved_from_completed_qh1() -> None:
    """A rollover that marks qh1 complete also supplies the actual."""
    got = _parse(
        _fetch(571, -100.238065, -0.0125, -104.350565, 329, 571, complete=True),
        _fetch(571, -100.238065, -0.0125, -104.350565, 329, 571),
    )
    assert len(got) == 1
    assert got[0].actual_wh == pytest.approx(-100.238065)


def test_missing_actual_leaves_none() -> None:
    """An unresolved quarter is reported, not guessed."""
    a = _parse(_fetch(571, -100.238065, -0.0125, -104.350565, 329, 571))[0]
    assert a.actual_wh is None
    assert a.error is None
    assert a.true_forward_rate is None
    assert a.rate_shortfall is None
    assert scored_anchors([a]) == []


def test_actual_never_crosses_quarter_boundaries() -> None:
    """qh2 describes the quarter *before* qh1, not qh1's own quarter."""
    got = _parse(
        _fetch(571, -50.0, 0.1, -17.1, 329, 571, complete_q2=999.0),
    )
    assert got[0].actual_wh is None


def test_duplicate_fetches_are_kept_as_separate_anchors() -> None:
    """Refetching identical data is still one forecast observation each."""
    got = _parse(
        _fetch(571, -100.238065, -0.0125, -104.350565, 329, 571),
        _fetch(571, -100.238065, -0.0125, -104.350565, 329, 571),
    )
    assert len(got) == 2
    assert got[0] == got[1]


# ── derived quantities ───────────────────────────────────────────────


def test_true_forward_rate_and_shortfall() -> None:
    """Shortfall is how far the trailing mean under-stated the forward rate."""
    a = _parse(
        _fetch(601, -92.737065, 0.25, -17.977, 299, 601),
        _fetch(931, 18.775, 0.605, 544.5, 869, 31, complete_q2=1.7394091666666602),
    )[0]
    assert a.true_forward_rate == pytest.approx((1.7394091666666602 + 92.737065) / 299)
    assert a.rate_shortfall == pytest.approx(a.true_forward_rate - 0.25)


def test_zero_remaining_has_no_forward_rate() -> None:
    """remaining_seconds=0 would divide by zero."""
    a = _parse(
        _fetch(900, -16.0, 0.4, -16.0, 0, 900),
        _fetch(931, 18.775, 0.605, 544.5, 869, 31, complete_q2=-16.5),
    )[0]
    assert a.true_forward_rate is None
    assert a.rate_shortfall is None


def test_iter_quarters_deduplicates() -> None:
    """One entry per distinct quarter, in first-seen order."""
    got = _parse(
        _fetch(571, -1.0, 0.1, -60.0, 329, 571),
        _fetch(601, -2.0, 0.1, -30.0, 299, 601),
        _fetch(931, 3.0, 0.2, 100.0, 869, 31),
    )
    assert [q for _, q in iter_quarters(got)] == [
        datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc),
        datetime(2026, 10, 1, 20, 45, tzinfo=timezone.utc),
    ]


# ── pinned against production evidence ──────────────────────────────


@_NEEDS_REAL_LOG
def test_real_log_matches_hand_verified_values() -> None:
    """Pins the parser to the numbers recovered by hand from the 2026-10-10 log."""
    got = parse_log(_OVERSHOOT.read_text(errors="replace"), source=_OVERSHOOT.name)
    by_start: dict[datetime, list[ForecastAnchor]] = {}
    for a in got:
        by_start.setdefault(a.quarter_start, []).append(a)

    target = datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)
    assert target in by_start, "expected the 20:30 quarter in the overshoot log"
    quarter = sorted(by_start[target], key=lambda a: a.data_point_at)

    # First anchor: raw -100.238065, w -0.0125, predicted -104.350565, R 329.
    assert quarter[0].raw_wh == pytest.approx(-100.238065)
    assert quarter[0].prediction_w == pytest.approx(-0.0125)
    assert quarter[0].predicted_wh == pytest.approx(-104.350565)
    assert quarter[0].remaining_secs == 329

    # Every anchor resolves to the same completed total for that quarter.
    assert all(a.actual_wh == pytest.approx(1.7394091666666602) for a in quarter)
    assert len(quarter) == 15


@_NEEDS_REAL_LOG
def test_real_log_is_fully_scored() -> None:
    """The hand analysis recovered 10 anchors; the parser must find them all."""
    got = scored_anchors(parse_log(_OVERSHOOT.read_text(errors="replace"), source="x"))
    assert len(got) >= 10
    assert all(a.error is not None for a in got)

# ── pass B: reporting ────────────────────────────────────────────────

from tools.forecast_log_scoring import (  # noqa: E402
    BUCKET_EDGES,
    forecast_uncertainty_wh,
    format_report,
    percentile,
    stddev,
    summarize,
)


def _anchor(
    remaining: int,
    raw_wh: float,
    prediction_w: float,
    predicted_wh: float,
    actual_wh: float | None,
    quarter: datetime | None = None,
) -> ForecastAnchor:
    """Build an anchor directly, bypassing the parser."""
    start = quarter or datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)
    return ForecastAnchor(
        source="unit.log",
        quarter_start=start,
        data_point_at=start,
        remaining_secs=remaining,
        prediction_values=30,
        samples_used=900 - remaining,
        raw_wh=raw_wh,
        prediction_w=prediction_w,
        predicted_wh=predicted_wh,
        actual_wh=actual_wh,
    )


def test_percentile_nearest_rank() -> None:
    assert percentile([5.0], 0.8) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.8) == 4.0
    assert percentile([1.0, 2.0], 0.0) == 1.0


def test_summarize_counts_and_totals() -> None:
    anchors = [
        _anchor(300, -90.0, 0.10, -60.0, 0.0),
        _anchor(200, -50.0, 0.20, -10.0, 0.0),
        _anchor(100, -20.0, 0.30, 10.0, 0.0),
    ]
    report = summarize(anchors)
    assert report.anchor_count == 3
    assert report.quarter_count == 1
    assert report.mean_abs_error == pytest.approx((60.0 + 10.0 + 10.0) / 3)
    assert report.max_abs_error == 60.0


def test_summarize_reports_optimistic_fraction() -> None:
    """Negative error = forecast promised more export than arrived."""
    report = summarize([
        _anchor(300, -90.0, 0.10, -60.0, 0.0),
        _anchor(200, -50.0, 0.20, -5.0, 0.0),
        _anchor(100, -20.0, 0.30, 10.0, 0.0),
    ])
    assert report.optimistic_count == 2
    assert report.optimistic_fraction == pytest.approx(2 / 3)


def test_summarize_excludes_unscored_anchors() -> None:
    report = summarize([
        _anchor(300, -90.0, 0.10, -60.0, None),
        _anchor(200, -50.0, 0.20, -10.0, 0.0),
    ])
    assert report.anchor_count == 1
    assert report.dropped_count == 1


def test_summarize_empty_is_safe() -> None:
    report = summarize([])
    assert report.anchor_count == 0
    assert report.buckets == ()
    assert report.mean_abs_error == 0.0
    assert "no scored anchors" in format_report(report).lower()


def test_buckets_partition_by_remaining_secs() -> None:
    """Every bucket gets exactly one anchor, and nothing falls outside them.

    ``remaining_seconds`` 901+ is unreachable inside a quarter, so the top
    edge is exclusive; sampling each bucket's last valid value keeps the
    expectations honest.
    """
    edges = BUCKET_EDGES
    in_bucket = [edges[i + 1] - 1 for i in range(len(edges) - 1)]
    anchors = [_anchor(secs, -10.0, 0.1, -1.0, 0.0) for secs in in_bucket]
    report = summarize(anchors)
    assert [b.count for b in report.buckets] == [1] * (len(edges) - 1)
    assert sum(b.count for b in report.buckets) == report.anchor_count
    for bucket in report.buckets:
        assert bucket.low_secs < bucket.high_secs


def test_bucket_records_shortfall_direction() -> None:
    """shortfall > 0 means the trailing mean under-stated the forward rate."""
    def bucket_for(anchor: ForecastAnchor) -> object:
        report = summarize([anchor])
        return next(b for b in report.buckets if b.count)

    under = bucket_for(_anchor(90, -30.0, 0.20, -12.0, 0.0))
    assert under.mean_shortfall == pytest.approx(30.0 / 90 - 0.20)
    assert under.under_stated_count == 1

    over = bucket_for(_anchor(90, -30.0, 0.60, 24.0, 0.0))
    assert over.mean_shortfall == pytest.approx(30.0 / 90 - 0.60)
    assert over.under_stated_count == 0


def test_format_report_surfaces_plan_questions() -> None:
    """The report must answer the open questions in the plan's section 3."""
    text = format_report(summarize([
        _anchor(300, -90.0, 0.10, -60.0, 0.0),
        _anchor(100, -20.0, 0.30, 10.0, 0.0),
    ])).lower()
    for needed in ("anchor", "optimistic", "shortfall", "r bucket", "quarter"):
        assert needed in text, needed


# ── pass C: the uncertainty band the plan needs ──────────────────────


def test_stddev_is_population() -> None:
    assert stddev([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]) == pytest.approx(2.0)
    assert stddev([5.0]) == 0.0


def test_forecast_uncertainty_scales_with_horizon() -> None:
    """The band must be proportional to the extrapolation horizon."""
    assert forecast_uncertainty_wh(300.0, 0.1, 2.0) == pytest.approx(60.0)
    assert forecast_uncertainty_wh(300.0, 0.1, 1.0) == pytest.approx(30.0)
    assert forecast_uncertainty_wh(0.0, 0.1, 2.0) == 0.0


def test_error_is_exactly_horizon_times_shortfall() -> None:
    """error == -R * shortfall, identically. Not an approximation.

    This is what makes a proportional band the right shape: there is no
    separate R-independent error term to cover, so U(R) = k * sigma * R is
    the complete model rather than a convenient approximation.
    """
    a = _anchor(90, -30.0, 0.20, -12.0, 4.0)
    assert a.error == pytest.approx(-90.0 * a.rate_shortfall)


def test_report_exposes_sigma_rate_and_coverage() -> None:
    report = summarize([
        _anchor(100, -20.0, 0.10, -10.0, 0.0),   # shortfall +0.20, error -10
        _anchor(100, -20.0, 0.10, -10.0, 20.0),  # shortfall  0.00, error -30
    ])
    assert report.sigma_rate == pytest.approx(0.10)
    # Band is k * 0.10 Wh/s * 100 s: +/-10 at k=1, +/-30 at k=3.
    assert report.coverage(1.0) == 0.5
    assert report.coverage(3.0) == 1.0
    # At k=0.25 the band is +/-2.5 Wh and neither lands inside it.
    assert report.coverage(0.25) == 0.0


def test_coverage_is_monotonic_in_k() -> None:
    report = summarize([
        _anchor(100, -20.0, 0.10, -10.0, 0.0),
        _anchor(100, -20.0, 0.10, -10.0, 20.0),
        _anchor(400, -80.0, 0.20, 0.0, 10.0),
    ])
    widths = [report.coverage(k) for k in (0.0, 0.25, 0.5, 1.0, 2.0, 4.0)]
    assert widths == sorted(widths)
    assert widths[-1] == 1.0


def test_bucket_reports_sigma_rate() -> None:
    """sigma_rate is the spread of the trailing mean's rate error (Wh/s)."""
    report = summarize([
        _anchor(90, -30.0, 0.20, -12.0, 0.0),   # shortfall +0.1333
        _anchor(90, -30.0, 0.40, 6.0, 0.0),     # shortfall -0.0667
    ])
    bucket = next(b for b in report.buckets if b.count)
    assert bucket.sigma_rate == pytest.approx(0.10, abs=0.01)


def test_format_report_shows_sigma_rate() -> None:
    text = format_report(summarize([
        _anchor(100, -20.0, 0.10, -10.0, 0.0),
        _anchor(400, -80.0, 0.20, 0.0, 10.0),
    ])).lower()
    assert "sigma" in text
    assert "coverage" in text


# ── invocation as a script ──────────────────────────────────────────


def test_runs_as_a_standalone_script() -> None:
    """`python tools/forecast_log_scoring.py <log>` must work from the repo root.

    sys.path[0] becomes tools/ on direct invocation, so the root-relative
    `util` import needs a bootstrap. Verified by actually running it.
    """
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(root / "tools" / "forecast_log_scoring.py")],
        capture_output=True,
        text=True,
        cwd=root,
        timeout=60,
    )
    assert proc.returncode in (0, 1), proc.stderr
    assert "scored anchors" in proc.stdout or "no scored anchors" in proc.stdout


# ── pass D: churn band, guard/cycle parsing, target-miss ─────────────
#
# The scorer stays a *scorer*: it never replays decisions. It now also
# (a) measures the jitter signal the turn-on guard consumes — production's
# own GapTrendTracker fed from the anchor series, scored against realized
# error like the sigma band — and (b) reads what production logged about
# its own decisions (cycle-result reprs, guard INFO lines) to attribute a
# target and a guard-firing count to each quarter.

from constants import GAP_TREND_EWMA_ALPHA  # noqa: E402
from tools import forecast_log_scoring as fls  # noqa: E402

_LA_DP = (
    "datetime.datetime(2026, 10, 1, 13, 39, 31, "
    "tzinfo=<DstTzInfo 'America/Los_Angeles' PDT-1 day, 17:00:00 DST>)"
)
_LA_DP_US = (
    "datetime.datetime(2026, 10, 1, 13, 39, 31, 123456, "
    "tzinfo=<DstTzInfo 'America/Los_Angeles' PDT-1 day, 17:00:00 DST>)"
)
_Q = datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)


def _churn_anchor(
    offset_secs: int,
    predicted: float,
    actual: float,
    remaining: int = 300,
    quarter: datetime | None = None,
) -> ForecastAnchor:
    """An anchor with a distinct data point, so the tracker sees real slopes."""
    start = quarter or _Q
    return ForecastAnchor(
        source="unit.log",
        quarter_start=start,
        data_point_at=start + timedelta(seconds=offset_secs),
        remaining_secs=remaining,
        prediction_values=30,
        samples_used=offset_secs + 1,
        raw_wh=0.0,
        prediction_w=0.0,
        predicted_wh=predicted,
        actual_wh=actual,
    )


def _cycle_line(
    target: int | None = -9,
    reason: str | None = "excessive_jitter",
    jitter: float | None = 0.6249,
    data_point: str | None = _LA_DP_US,
    prefix: str = "[2026-10-01 20:39:35 +0000]",
) -> str:
    """A cycle-result DEBUG line, carrying parse traps on both sides.

    The action's ``data_point_at`` sits *before* ``diagnostics=`` and the
    candidate's ``reason`` *after* the diagnostics ``reason`` — a parser
    that searches the whole line picks up the wrong one.
    """
    target_s = "None" if target is None else str(target)
    jitter_s = "None" if jitter is None else str(jitter)
    dp_s = "None" if data_point is None else data_point
    reason_s = "None" if reason is None else f"'{reason}'"
    return (
        f"{prefix} [980472] [DEBUG] app Load management cycle result: "
        f"CycleResult(status='ok', qh='QH1', predicted_wh=-45.6, adjusted_wh=-45.6, "
        f"target_wh={target_s}, current_wh=None, estimated_wh=None, "
        f"actions=[PendingEffect(device_type='plug', device_name='jackery', "
        f"action='turn_on', timestamp=datetime.datetime(2026, 10, 1, 13, 0, 5, "
        f"tzinfo=<DstTzInfo 'America/Los_Angeles' PDT-1 day, 17:00:00 DST>), "
        f"data_point_at=datetime.datetime(2026, 10, 1, 13, 0, 5, "
        f"tzinfo=<DstTzInfo 'America/Los_Angeles' PDT-1 day, 17:00:00 DST>), "
        f"power_watts=260.0)], "
        f"diagnostics=CycleDiagnostics(gap_wh=7.7, hysteresis_wh=3, "
        f"seconds_remaining=269, data_point_at={dp_s}, reason={reason_s}, "
        f"pending_effects_count=0, tesla_configured=False, tesla_state=None, "
        f"tesla_error=None, tesla_login_url=None, plugs_configured=['jackery'], "
        f"candidates=[CandidateDetail(device_type='plug', name='jackery', "
        f"power_watts=260.0, capacity_wh=21.6, can_toggle=True, desired_state=True, "
        f"actual_state=True, state_available=None, is_charging=None, "
        f"current_amps=None, plugged_in=None, at_home=None, reason='not_eligible', "
        f"error=None)], sentinel_names=None, sentinel_on=False, "
        f"telemetry_registered=None, active_tesla_telemetry=None, "
        f"tesla_command_offline=None, quantization_seconds=None, "
        f"quantization_offset=None, quantization_confidence=None, "
        f"settle_window_secs=None, gap_trend_wh_per_s=None, "
        f"gap_jitter_wh_per_s={jitter_s}), sleep_hint=0.0, gap_wh=7.7, "
        f"cycle_id='abc', timings=None)"
    )


def _guard_line(
    gap: float = 7.7,
    jitter: float = 0.6249,
    swing: float = 68.7,
    r: int = 111,
    prefix: str = "[2026-10-01 20:43:12 +0000]",
) -> str:
    """A guard INFO line in logfmt's rendered shape (message + extra suffix)."""
    return (
        f"{prefix} [980472] [INFO] load_nbc gapminder_jitter_guard gap={gap} "
        f"jitter={jitter} swing={swing} R={r} "
        f"[event=gapminder_jitter_guard gap_wh={gap} jitter_wh_per_s={jitter} "
        f"swing_wh={swing} seconds_remaining={r}]"
    )


# ── churn band (option A) ────────────────────────────────────────────


def test_churn_by_anchor_seeds_on_third_sample() -> None:
    """Two slopes are needed before churn exists: |s2 - s1| seeds it."""
    got = fls.churn_by_anchor([
        _churn_anchor(0, 0.0, -50.0),
        _churn_anchor(30, 30.0, -50.0),
        _churn_anchor(60, 0.0, -50.0),
    ])
    # slopes: (30-0)/30 = +1.0, (0-30)/30 = -1.0 -> seed |(-1) - 1| = 2.0
    assert got == pytest.approx([0.0, 0.0, 2.0])


def test_churn_folds_new_pairs_via_ewma() -> None:
    """A fourth sample folds |s3 - s2| into the EWMA with alpha weighting."""
    got = fls.churn_by_anchor([
        _churn_anchor(0, 0.0, -50.0),
        _churn_anchor(30, 30.0, -50.0),
        _churn_anchor(60, 0.0, -50.0),
        _churn_anchor(90, 60.0, -50.0),
    ])
    # trailing window (30, 0, 60): slopes -1.0, +2.0 -> pair 3.0
    expected = GAP_TREND_EWMA_ALPHA * 3.0 + (1.0 - GAP_TREND_EWMA_ALPHA) * 2.0
    assert got[3] == pytest.approx(expected)


def test_churn_is_invariant_to_offset_and_sign() -> None:
    """Why raw predicted_wh may stand in for the production gap.

    Churn is EWMA of |Δslope|: a constant offset cancels in slopes and a
    sign flip cancels in absolute value, so target + adjustment missing
    offline cannot change the measure.
    """
    def churns(predictions: list[float]) -> list[float]:
        return fls.churn_by_anchor([
            _churn_anchor(i * 30, p, -50.0) for i, p in enumerate(predictions)
        ])

    baseline = churns([0.0, 30.0, 0.0])
    assert baseline == pytest.approx([0.0, 0.0, 2.0])
    assert churns([1000.0, 1030.0, 1000.0]) == pytest.approx(baseline)
    assert churns([0.0, -30.0, 0.0]) == pytest.approx(baseline)


def test_churn_resets_across_quarters() -> None:
    """A fresh quarter starts from no history (tracker QH rollover)."""
    got = fls.churn_by_anchor([
        _churn_anchor(0, 0.0, -50.0),
        _churn_anchor(30, 30.0, -50.0),
        _churn_anchor(60, 0.0, -50.0),
        _churn_anchor(0, 0.0, -50.0, quarter=datetime(2026, 10, 1, 20, 45,
                                                      tzinfo=timezone.utc)),
        _churn_anchor(30, 30.0, -50.0, quarter=datetime(2026, 10, 1, 20, 45,
                                                       tzinfo=timezone.utc)),
    ])
    assert got[:3] == pytest.approx([0.0, 0.0, 2.0])
    assert got[3:] == pytest.approx([0.0, 0.0])


def test_churn_coverage_uses_k_churn_r_band() -> None:
    """The guard's own uncertainty model, scored like the sigma band.

    Oscillating predictions give churn 2.0 Wh/s at the third anchor
    (band k*2.0*300); the first two anchors have no churn yet, so their
    band is zero and a nonzero error can never hide inside it.
    """
    report = fls.summarize([
        _churn_anchor(0, 0.0, -50.0),
        _churn_anchor(30, 30.0, -50.0),
        _churn_anchor(60, 0.0, -50.0),
    ])
    assert report.churns == pytest.approx([0.0, 0.0, 2.0])
    assert len(report.churns) == report.anchor_count
    assert report.measurable_churn_count == 1
    assert fls.churn_uncertainty_wh(300.0, 2.0, 1.0) == pytest.approx(600.0)
    # errors: 50, 80, 50 — only the third anchor's 600 Wh band admits one.
    assert report.churn_coverage(1.0) == pytest.approx(1 / 3)
    assert report.churn_coverage(0.05) == 0.0
    widths = [report.churn_coverage(k) for k in (0.0, 0.05, 0.1, 1.0, 3.0)]
    assert widths == sorted(widths)
    # sigma_rate is degenerate here (identical shortfalls), so the sigma
    # band has zero width — the churn band still carries information.
    assert report.sigma_rate == pytest.approx(0.0)
    assert report.coverage(3.0) == 0.0


def test_zero_churn_never_covers_nonzero_error() -> None:
    """A monotone quarter has no swing: the guard's band is exactly zero."""
    report = fls.summarize([
        _churn_anchor(0, 0.0, -50.0),
        _churn_anchor(30, 30.0, -50.0),
        _churn_anchor(60, 60.0, -50.0),
    ])
    assert report.churns == pytest.approx([0.0, 0.0, 0.0])
    assert report.measurable_churn_count == 0
    assert report.churn_coverage(3.0) == 0.0


def test_format_report_shows_churn_band_coverage() -> None:
    text = fls.format_report(fls.summarize([
        _churn_anchor(0, 0.0, -50.0),
        _churn_anchor(30, 30.0, -50.0),
        _churn_anchor(60, 0.0, -50.0),
    ]))
    assert "churn band coverage" in text
    assert "swing measurable" in text


# ── guard events and cycle records (option B) ────────────────────────


def test_parse_guard_event_fields_and_quarter() -> None:
    events = fls.parse_guard_events(_guard_line(), source="g.log")
    assert len(events) == 1
    ev = events[0]
    assert ev.source == "g.log"
    assert ev.at == datetime(2026, 10, 1, 20, 43, 12, tzinfo=timezone.utc)
    assert ev.gap_wh == pytest.approx(7.7)
    assert ev.jitter_wh_per_s == pytest.approx(0.6249)
    assert ev.swing_wh == pytest.approx(68.7)
    assert ev.seconds_remaining == 111
    assert ev.quarter_start == _Q


def test_guard_event_wall_time_decides_quarter() -> None:
    """Wall time *is* the data-point quarter: the pipeline's previous_qh
    check exits before decide(), so a guard event can only fire while
    data_point_at lies in the current wall-clock quarter."""
    events = fls.parse_guard_events(
        _guard_line(prefix="[2026-10-01 20:45:03 +0000]"), source="g.log"
    )
    assert events[0].quarter_start == datetime(2026, 10, 1, 20, 45,
                                               tzinfo=timezone.utc)


def test_parse_cycle_record_scopes_fields_to_diagnostics() -> None:
    """target/reason/jitter/data_point must come from the right segments."""
    records = fls.parse_cycles(_cycle_line(), source="c.log")
    assert len(records) == 1
    rec = records[0]
    # Quarter from the *diagnostics* data point (13:39:31 local), not the
    # pending action's older data point (13:00:05 local -> 20:00 quarter).
    assert rec.quarter_start == _Q
    # Diagnostics reason, not the candidate's 'not_eligible'.
    assert rec.reason == "excessive_jitter"
    assert rec.target_wh == -9
    assert rec.gap_jitter_wh_per_s == pytest.approx(0.6249)


def test_parse_cycle_record_handles_microseconds_and_naive_fields() -> None:
    """Real reprs may carry microseconds; missing data points keep stats."""
    records = fls.parse_cycles(_cycle_line(), source="c.log")
    assert records[0].quarter_start == _Q  # fixture dp has .123456

    bare = fls.parse_cycles(_cycle_line(data_point=None), source="c.log")
    assert bare[0].quarter_start is None
    assert bare[0].reason == "excessive_jitter"

    no_diag = fls.parse_cycles(
        "[2026-10-01 20:39:35 +0000] [980472] [DEBUG] app Load management "
        "cycle result: CycleResult(status='disabled', diagnostics=None)",
        source="c.log",
    )
    assert no_diag == []


def test_summarize_joins_target_miss_and_guard() -> None:
    """A quarter gets its target from cycle records and its guard count
    from guard events, both keyed the same way as the anchors."""
    report = fls.summarize(
        [
            _churn_anchor(0, 0.0, -50.0),
            _churn_anchor(30, 30.0, -50.0),
            _churn_anchor(60, 0.0, -50.0),
        ],
        cycles=[
            fls.CycleRecord(
                source="unit.log", quarter_start=_Q, target_wh=-9,
                reason="excessive_jitter", gap_jitter_wh_per_s=0.6249,
            ),
            # Target-less early exit must not overwrite the target.
            fls.CycleRecord(
                source="unit.log", quarter_start=_Q, target_wh=None,
                reason="waiting_for_fresh_data", gap_jitter_wh_per_s=None,
            ),
            # Another quarter's target must not leak into this one.
            fls.CycleRecord(
                source="unit.log", quarter_start=datetime(2026, 10, 1, 20, 45,
                                                         tzinfo=timezone.utc),
                target_wh=-20, reason="ok", gap_jitter_wh_per_s=None,
            ),
        ],
        guards=[
            fls.GuardEvent(
                source="unit.log",
                at=datetime(2026, 10, 1, 20, 43, 1, tzinfo=timezone.utc),
                gap_wh=7.7, jitter_wh_per_s=0.6249, swing_wh=68.7,
                seconds_remaining=111,
            ),
        ],
    )
    quarter = next(q for q in report.quarters if q.quarter_start == _Q)
    assert quarter.target_wh == pytest.approx(-9.0)
    assert quarter.miss_wh == pytest.approx(-50.0 - (-9.0))
    assert quarter.guard_firings == 1
    assert report.guard.firing_count == 1
    assert report.guard.excessive_jitter_cycles == 1
    assert report.guard.production_churn_mean == pytest.approx(0.6249)
    assert report.guard.production_churn_max == pytest.approx(0.6249)


def test_format_report_quarter_table_target_miss_guard() -> None:
    text = fls.format_report(fls.summarize(
        [_churn_anchor(0, 0.0, -50.0), _churn_anchor(30, 30.0, -50.0)],
        cycles=[
            fls.CycleRecord(
                source="unit.log", quarter_start=_Q, target_wh=-9,
                reason="ok", gap_jitter_wh_per_s=None,
            ),
        ],
        guards=[
            fls.GuardEvent(
                source="unit.log",
                at=datetime(2026, 10, 1, 20, 43, 1, tzinfo=timezone.utc),
                gap_wh=7.7, jitter_wh_per_s=0.6249, swing_wh=68.7,
                seconds_remaining=111,
            ),
        ],
    ))
    for header in ("target", "miss", "guard"):
        assert header in text, header
    assert "-9.0" in text
    assert "-41.00" in text  # miss = actual (-50) - target (-9)


def test_format_report_jitter_guard_line() -> None:
    text = fls.format_report(fls.summarize(
        [_churn_anchor(0, 0.0, -50.0)],
        cycles=[
            fls.CycleRecord(
                source="unit.log", quarter_start=_Q, target_wh=-9,
                reason="excessive_jitter", gap_jitter_wh_per_s=0.6249,
            ),
        ],
        guards=[
            fls.GuardEvent(
                source="unit.log",
                at=datetime(2026, 10, 1, 20, 43, 1, tzinfo=timezone.utc),
                gap_wh=7.7, jitter_wh_per_s=0.6249, swing_wh=68.7,
                seconds_remaining=111,
            ),
        ],
    ))
    assert "jitter guard:" in text
    assert "guard event(s)" in text
    assert "excessive_jitter" in text
    assert "production churn mean" in text


# ── pinned against production evidence ──────────────────────────────


@_NEEDS_REAL_LOG
def test_real_log_incident_quarter_target_and_miss() -> None:
    """The incident quarter scores target -9 Wh and a +10.74 Wh miss.

    This is the acceptance metric the replay gate used (|miss| < 10.7
    against target -9), now reported straight from the log's own
    cycle-result reprs.
    """
    text = _OVERSHOOT.read_text(errors="replace")
    report = fls.summarize(
        fls.parse_log(text, source=_OVERSHOOT.name),
        cycles=fls.parse_cycles(text, source=_OVERSHOOT.name),
    )
    quarter = next(
        q for q in report.quarters if q.quarter_start == _Q
    )
    assert quarter.target_wh == pytest.approx(-9.0)
    assert quarter.miss_wh == pytest.approx(1.7394091666666602 - (-9.0))


@_NEEDS_REAL_LOG
def test_real_log_has_no_guard_events() -> None:
    """The incident predates the guard: nothing to attribute, nothing invented."""
    assert fls.parse_guard_events(
        _OVERSHOOT.read_text(errors="replace"), source=_OVERSHOOT.name
    ) == []
