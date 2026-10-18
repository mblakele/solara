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
_OVERSHOOT = _BUGS / "2026-10-01-tesla-overshoot.log"
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
    complete_q3: float | None = None,
) -> str:
    """An ``nbc_set`` line for one fetch.

    ``complete_q2``/``complete_q3`` set the raw totals of the completed
    quarters the same fetch reports alongside the incomplete QH1, which is
    where a prior-quarter rate comes from.
    """
    parts = [
        f"qh1=NBCQuarter(complete={complete}, raw_wh={raw_wh!r}, wh=0, "
        f"prediction_values=30, prediction_w={prediction_w!r}, "
        f"predicted_wh={predicted_wh!r}, remaining_seconds={remaining}, "
        f"samples_used={used})"
    ]
    for name, val in (("qh2", complete_q2), ("qh3", complete_q3)):
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
    complete_q3: float | None = None,
) -> str:
    """One fetch: the nbc_set line, then its data_start line (real order)."""
    return (
        _nbc(
            total, raw_wh, prediction_w, predicted_wh, remaining, used,
            complete, complete_q2, complete_q3,
        )
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
    wall_at: datetime | None = None,
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
        wall_at=wall_at,
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


# ── per-cycle dispatch state (plug desired/actual, Tesla flags) ─────


def _state_candidate(
    name: str,
    desired: bool | None,
    actual: bool | None,
    kind: str = "plug",
    power: str = "260.0",
) -> str:
    """One CandidateDetail entry with desired and actual set independently."""
    return (
        f"CandidateDetail(device_type='{kind}', name='{name}', "
        f"power_watts={power}, capacity_wh=21.6, can_toggle=True, "
        f"desired_state={desired}, actual_state={actual})"
    )


def _state_cycle_line(
    stamp: str,
    candidates: str,
    data_point: str | None = _LA_DP_US,
    tesla: str = "None",
) -> str:
    """A cycle-result line carrying plug candidates and a Tesla state dict."""
    dp_s = "None" if data_point is None else data_point
    return (
        f"[{stamp} +0000] [980472] [DEBUG] app Load management cycle result: "
        f"CycleResult(status='ok', qh='QH1', predicted_wh=-1.0, adjusted_wh=-1.0, "
        f"target_wh=-9, actions=[], diagnostics=CycleDiagnostics(gap_wh=7.7, "
        f"hysteresis_wh=3, seconds_remaining=269, data_point_at={dp_s}, "
        f"reason='ok', pending_effects_count=0, tesla_configured=True, "
        f"tesla_state={tesla}, candidates=[{candidates}])"
    )


def test_parse_cycle_states_reads_desired_and_actual_separately() -> None:
    """The two fields disagree while a command is still unconfirmed.

    The replay seeds its meter from the *actual* state, so a parser that
    collapsed the two would silently mis-model every pending quarter.
    """
    text = _state_cycle_line(
        "2026-10-14 23:16:40",
        ", ".join(
            [
                _state_candidate("water heater", False, True),
                _state_candidate("ecoflow", True, False),
                _state_candidate("tesla", None, None, kind="tesla", power="None"),
            ]
        ),
    )
    (snapshot,) = fls.parse_cycle_states(text, source="s.log")
    assert snapshot.plugs == (
        ("water heater", False, True),
        ("ecoflow", True, False),
    )
    assert snapshot.source == "s.log"


def test_parse_cycle_states_attributes_quarter_and_wall_time() -> None:
    """Quarter comes from the diagnostics data point, not the line prefix."""
    text = _state_cycle_line(
        "2026-10-01 20:16:41", _state_candidate("jackery", True, True)
    )
    (snapshot,) = fls.parse_cycle_states(text)
    assert snapshot.quarter_start == datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)
    assert snapshot.at == datetime(2026, 10, 1, 20, 16, 41, tzinfo=timezone.utc)


def test_parse_cycle_states_reports_tesla_charging_and_amps() -> None:
    """A Tesla state dict yields both flags, whatever the key order."""
    text = _state_cycle_line(
        "2026-10-14 23:16:40",
        _state_candidate("jackery", True, True),
        tesla="{'current_amps': 12, 'is_charging': True, 'plugged_in': True, "
        "'at_home': True}",
    )
    (snapshot,) = fls.parse_cycle_states(text)
    assert snapshot.tesla_charging is True
    assert snapshot.tesla_amps == pytest.approx(12.0)
    assert snapshot.tesla_plugged_in is True
    assert snapshot.tesla_at_home is True


def test_parse_cycle_states_reports_an_absent_car_as_unplugged() -> None:
    """``plugged_in`` is what stops the replay commanding a car that is away.

    Both directions of production's Tesla decision are gated on it, so a
    snapshot that dropped the flag would let the counterfactual meter charge
    a vehicle that is not there.
    """
    text = _state_cycle_line(
        "2026-10-14 23:16:40",
        _state_candidate("jackery", True, True),
        tesla="{'is_charging': False, 'current_amps': 0, 'plugged_in': False, "
        "'at_home': True}",
    )
    (snapshot,) = fls.parse_cycle_states(text)
    assert snapshot.tesla_plugged_in is False
    assert snapshot.tesla_at_home is True


def test_parse_cycle_states_tesla_absent_is_unknown_not_idle() -> None:
    """An early-exit cycle logs no Tesla state; that is not a quiet car.

    Tesla stays ``configured`` in these cycles, so ``None`` cannot be read
    as "not charging" — the replay drops the quarter instead.
    """
    text = _state_cycle_line(
        "2026-10-14 23:16:40", _state_candidate("jackery", True, True)
    )
    (snapshot,) = fls.parse_cycle_states(text)
    assert snapshot.tesla_charging is None
    assert snapshot.tesla_amps is None
    assert snapshot.tesla_plugged_in is None
    assert snapshot.tesla_at_home is None


def test_parse_cycle_states_skips_lines_without_a_diagnostics_segment() -> None:
    """Action lines and other reprs are not cycle states."""
    text = "\n".join(
        [
            "[2026-10-14 23:16:40 +0000] [1] [INFO] load_nbc action=turn_off "
            "device=jackery savings=60.0 gap=60.0 priority=100",
            _state_cycle_line(
                "2026-10-14 23:16:41", _state_candidate("jackery", True, True)
            ),
        ]
    )
    assert len(fls.parse_cycle_states(text)) == 1


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


# ── sheddable residual series ────────────────────────────────────────


def _resid_cycle_line(
    stamp: str, candidates: str, target: str = "-9", reason: str = "ok"
) -> str:
    """A CycleResult line carrying plug candidates for the ON-watts parse."""
    return (
        f"[{stamp} +0000] [628277] [DEBUG] app Load management cycle result: "
        f"CycleResult(status='ok', qh='QH1', predicted_wh=100.0, "
        f"adjusted_wh=100.0, target_wh={target}, actions=[], "
        f"diagnostics=CycleDiagnostics(gap_wh=-109.0, hysteresis_wh=3, "
        f"seconds_remaining=835, reason='{reason}'), candidates=[{candidates}])"
    )


def _candidate(
    name: str, power: float | None, desired: bool | None
) -> str:
    """One CandidateDetail entry in production field order."""
    return (
        f"CandidateDetail(device_type='plug', name='{name}', "
        f"power_watts={power}, capacity_wh=1.0, can_toggle=True, "
        f"desired_state={desired}, actual_state={desired})"
    )


def test_parse_on_watts_timeline_sums_wanted_on_plugs() -> None:
    """ON plugs add rated watts; off plugs and Tesla add nothing."""
    text = "\n".join([
        _resid_cycle_line("2026-10-14 23:16:40", ", ".join([
            _candidate("jackery", 260.0, True),
            _candidate("water heater", 4857.0, True),
            _candidate("ecoflow", 260.0, False),
            _candidate("tesla", None, None).replace(
                "device_type='plug'", "device_type='tesla'"
            ),
        ])),
        _resid_cycle_line("2026-10-14 23:18:03", ", ".join([
            _candidate("jackery", 260.0, True),
            _candidate("water heater", 4857.0, False),
            _candidate("ecoflow", 260.0, False),
        ])),
    ])
    timeline = fls.parse_on_watts_timeline(text, source="synthetic.log")
    assert [w for _, w in timeline] == pytest.approx([5117.0, 260.0])
    assert timeline[0][0] == datetime(2026, 10, 14, 23, 16, 40, tzinfo=timezone.utc)


def test_parse_on_watts_timeline_dedupes_repeated_candidate_lists() -> None:
    """The repr repeats candidates twice; each plug counts once."""
    entry = (
        "CandidateDetail(device_type='plug', name='water heater', "
        "power_watts=4857.0, capacity_wh=1.0, can_toggle=True, "
        "desired_state=True, actual_state=True)"
    )
    text = (
        "[2026-10-14 23:16:40 +0000] [1] [DEBUG] app Load management cycle result: "
        f"CycleResult(status='ok', candidates=[{entry}], "
        f"diagnostics=CycleDiagnostics(gap_wh=1.0, candidates=[{entry}]))"
    )
    ((_, watts),) = fls.parse_on_watts_timeline(text, source="synthetic.log")
    assert watts == pytest.approx(4857.0)


def test_anchor_residual_matches_hand_calc() -> None:
    """c658 full-net residual: netting 5377 W leaves a -97.6 Wh error."""
    anchor = _anchor(840, 74.3072625, 1.295, 1162.1072625, 5.09)
    assert anchor.residual_rate(5377.0) == pytest.approx(1.295 - 5377.0 / 3600.0)
    assert anchor.residual_predicted(5377.0) == pytest.approx(-92.5, abs=0.1)
    assert anchor.residual_error(5377.0) == pytest.approx(-97.6, abs=0.1)


def test_residual_error_is_exactly_horizon_times_shortfall() -> None:
    """The error identity holds for the residual series too."""
    anchor = _anchor(870, 37.907825, 1.2635941666, 1137.23475, -28.65)
    shortfall = anchor.residual_shortfall(5377.0)
    assert anchor.residual_error(5377.0) == pytest.approx(
        -870 * shortfall, abs=1e-6
    )


def test_assign_on_watts_uses_prior_cycle() -> None:
    """Each anchor takes the latest cycle at or before its wall time."""
    base = datetime(2026, 10, 14, 23, 15, 0, tzinfo=timezone.utc)
    anchors = [
        _anchor(840, 74.0, 1.0, 900.0, 5.0, wall_at=base + timedelta(seconds=65)),
        _anchor(810, 70.0, 0.5, 400.0, 5.0, wall_at=base + timedelta(seconds=95)),
    ]
    timeline = [
        (base + timedelta(seconds=40), 5117.0),
        (base + timedelta(seconds=80), 260.0),
    ]
    assert fls.assign_on_watts(anchors, timeline) == [5117.0, 260.0]


def test_assign_on_watts_none_before_first_cycle() -> None:
    """Anchors predating every cycle line are excluded, not zero-filled."""
    base = datetime(2026, 10, 14, 23, 15, 0, tzinfo=timezone.utc)
    anchors = [
        _anchor(840, 74.0, 1.0, 900.0, 5.0, wall_at=base + timedelta(seconds=10))
    ]
    timeline = [(base + timedelta(seconds=40), 5117.0)]
    assert fls.assign_on_watts(anchors, timeline) == [None]


def test_residual_report_exposes_sigma_and_plug_survival() -> None:
    """Two hand-computed residual shortfalls size sigma; 260 W survives k=1."""
    anchors = [
        _anchor(840, 74.3072625, 1.295, 1162.1072625, 5.09),
        _anchor(870, 37.907825, 1.2635941666, 1137.23475, -28.65),
    ]
    report = fls.summarize(anchors, on_watts=[5377.0, 5377.0])
    assert report.residual_count == 2
    assert report.sigma_residual == pytest.approx(0.0187, abs=0.001)
    assert report.residual_coverage(1.0) == pytest.approx(0.0)
    assert report.residual_coverage(10.0) == pytest.approx(1.0)
    assert report.plug_survival(1.0) == pytest.approx(1.0)


# ── pass E: estimator variants ───────────────────────────────────────
#
# Production's forecast is left alone. These variants exist so the corpus
# can be re-scored offline against alternatives, which is the only way to
# size the "is the trailing window the noise source?" question (see
# .opencode/plans/forecast-variance-reduction.md, subtask 1). The prior
# rate is the median realized rate of the completed quarters the *same
# fetch* reports next to the incomplete QH1 — information production
# already holds when it decides.

from tools.forecast_log_scoring import (  # noqa: E402
    DEFAULT_PRIOR_WEIGHTS_SECS,
    ESTIMATOR_NAMES,
    TREND_GATE_NOISE_FLOOR_WH,
    estimate_prior_blend,
    estimate_prior_only,
    estimate_series,
    format_estimators,
    score_estimators,
)


def _est_anchor(
    offset: int,
    remaining: int,
    raw_wh: float,
    prediction_w: float,
    predicted_wh: float,
    actual_wh: float | None,
    prior_rate: float | None = None,
    quarter: datetime | None = None,
) -> ForecastAnchor:
    """An anchor with an explicit data point and prior-quarter rate."""
    start = quarter or _Q
    return ForecastAnchor(
        source="unit.log",
        quarter_start=start,
        data_point_at=start + timedelta(seconds=offset),
        remaining_secs=remaining,
        prediction_values=30,
        samples_used=offset + 1,
        raw_wh=raw_wh,
        prediction_w=prediction_w,
        predicted_wh=predicted_wh,
        actual_wh=actual_wh,
        wall_at=start,
        prior_rate=prior_rate,
    )


def test_prior_rate_from_completed_qh2() -> None:
    """The completed neighbours in the same fetch supply the prior rate."""
    got = _parse(_fetch(500, -50.0, 0.1, -10.0, 400, 500, complete_q2=-9.0))
    assert got[0].prior_rate == pytest.approx(-9.0 / 900.0)


def test_prior_rate_is_median_of_completed_quarters() -> None:
    """Two completed neighbours collapse to their median rate."""
    got = _parse(
        _fetch(500, -50.0, 0.1, -10.0, 400, 500, complete_q2=-9.0, complete_q3=-27.0)
    )
    assert got[0].prior_rate == pytest.approx(-18.0 / 900.0)


def test_prior_rate_none_without_completed_quarter() -> None:
    """A fetch that reports no completed neighbour has no prior."""
    got = _parse(_fetch(500, -50.0, 0.1, -10.0, 400, 500))
    assert got[0].prior_rate is None


def test_prior_rate_ignores_unknown_completed_totals() -> None:
    """A completed quarter with no raw_wh cannot contribute a rate.

    The API sometimes sets the complete flag without a total; treating
    that as a zero-rate quarter would drag the median toward zero.
    """
    got = _parse(_fetch(500, -50.0, 0.1, -10.0, 400, 500, complete_q3=-27.0))
    assert got[0].prior_rate == pytest.approx(-27.0 / 900.0)


def test_estimate_prior_only_extrapolates_the_prior_rate() -> None:
    """prior = raw_wh + R * prior_rate, with no window term at all."""
    anchor = _est_anchor(400, 400, -50.0, 0.1, -10.0, -9.0, prior_rate=-0.01)
    assert estimate_prior_only(anchor) == pytest.approx(-50.0 + 400 * -0.01)


def test_estimate_prior_only_falls_back_to_the_window() -> None:
    """No prior available -> the shipped forecast stands in, so n is shared."""
    anchor = _est_anchor(400, 400, -50.0, 0.1, -10.0, -9.0, prior_rate=None)
    assert estimate_prior_only(anchor) == pytest.approx(anchor.predicted_wh)


def test_estimate_prior_blend_weights_follow_sample_counts() -> None:
    """blend rate = (n * window + m * prior) / (n + m) with n = 30 samples."""
    anchor = _est_anchor(400, 400, -50.0, 0.10, -10.0, -9.0, prior_rate=-0.01)
    expected_rate = (30 * 0.10 + 120 * -0.01) / 150
    assert estimate_prior_blend(anchor, 120) == pytest.approx(
        -50.0 + 400 * expected_rate
    )


def test_estimate_prior_blend_falls_back_to_the_window() -> None:
    """Without a prior the blend is the shipped forecast, not a shifted one."""
    anchor = _est_anchor(400, 400, -50.0, 0.10, -10.0, -9.0, prior_rate=None)
    assert estimate_prior_blend(anchor, 300) == pytest.approx(anchor.predicted_wh)


def test_estimator_names_cover_the_planned_sweep() -> None:
    """Every planned variant is registered, blends included."""
    assert "status-quo" in ESTIMATOR_NAMES
    assert "prior" in ESTIMATOR_NAMES
    assert "trend-gated" in ESTIMATOR_NAMES
    for weight in DEFAULT_PRIOR_WEIGHTS_SECS:
        assert f"prior-blend@{weight}" in ESTIMATOR_NAMES


def test_estimate_series_is_aligned_and_finite() -> None:
    """Every variant predicts one finite value per anchor, in order."""
    anchors = [
        _est_anchor(30, 870, -5.0, 0.1, 82.0, -9.0, prior_rate=-0.01),
        _est_anchor(60, 840, -20.0, -0.2, -188.0, -9.0, prior_rate=-0.01),
        _est_anchor(90, 810, -30.0, 0.05, 10.5, -9.0, prior_rate=None),
    ]
    for name in ESTIMATOR_NAMES:
        series = estimate_series(anchors, name)
        assert len(series) == len(anchors)
        assert all(isinstance(v, float) for v in series)
        assert all(v == v and abs(v) < 1e9 for v in series)


def test_estimate_series_rejects_unknown_variant() -> None:
    with pytest.raises(ValueError):
        estimate_series([], "no-such-estimator")


def test_trend_gated_prefers_the_window_once_the_trend_is_trusted() -> None:
    """A trusted ramp keeps the responsive window; before that, the prior."""
    anchors = [
        _est_anchor(30, 870, 10.0, 0.1, 10.0, -9.0, prior_rate=-0.01),
        _est_anchor(60, 840, 20.0, 0.1, 20.0, -9.0, prior_rate=-0.01),
        _est_anchor(90, 810, 40.0, 0.1, 40.0, -9.0, prior_rate=-0.01),
    ]
    series = estimate_series(anchors, "trend-gated", trend_floor_wh=0.0)
    assert series[0] == pytest.approx(10.0 + 870 * -0.01)
    assert series[1] == pytest.approx(20.0 + 840 * -0.01)
    assert series[2] == pytest.approx(40.0)  # slope confirmed -> window


def test_trend_gated_falls_back_when_nothing_is_trusted() -> None:
    """An unreachable noise floor leaves the prior rate in charge throughout."""
    anchors = [
        _est_anchor(30, 870, 10.0, 0.1, 10.0, -9.0, prior_rate=-0.01),
        _est_anchor(60, 840, 20.0, 0.1, 20.0, -9.0, prior_rate=-0.01),
        _est_anchor(90, 810, 40.0, 0.1, 40.0, -9.0, prior_rate=-0.01),
    ]
    series = estimate_series(anchors, "trend-gated", trend_floor_wh=1e9)
    assert series == pytest.approx([10.0 + 870 * -0.01, 20.0 + 840 * -0.01,
                                    40.0 + 810 * -0.01])


def test_trend_gated_does_not_leak_across_quarters() -> None:
    """Each quarter's tracker starts empty, so no ramp carries over."""
    other = _Q + timedelta(minutes=15)
    anchors = [
        _est_anchor(30, 870, 10.0, 0.1, 10.0, -9.0, prior_rate=-0.01),
        _est_anchor(60, 840, 20.0, 0.1, 20.0, -9.0, prior_rate=-0.01),
        _est_anchor(90, 810, 40.0, 0.1, 40.0, -9.0, prior_rate=-0.01),
        _est_anchor(30, 870, 100.0, 0.1, 100.0, -9.0, prior_rate=-0.01, quarter=other),
    ]
    series = estimate_series(anchors, "trend-gated", trend_floor_wh=0.0)
    assert series[3] == pytest.approx(100.0 + 870 * -0.01)


def test_score_estimators_uses_one_shared_anchor_set() -> None:
    """Rows are comparable: same scored anchors, same count, per variant."""
    anchors = [
        _est_anchor(400, 400, -50.0, 0.1, -10.0, -9.0, prior_rate=-0.01),
        _est_anchor(300, 500, -50.0, 0.1, 0.0, -9.0, prior_rate=-0.01),
        _est_anchor(200, 600, -50.0, 0.1, 10.0, None, prior_rate=-0.01),
    ]
    summaries = score_estimators(anchors)
    assert [s.name for s in summaries] == list(ESTIMATOR_NAMES)
    assert all(s.count == 2 for s in summaries)
    status, prior = summaries[0], summaries[1]
    assert status.mean_abs_error == pytest.approx((1.0 + 9.0) / 2)
    assert prior.mean_abs_error == pytest.approx((45.0 + 46.0) / 2)
    assert prior.max_abs_error == pytest.approx(46.0)
    assert status.sigma_rate == pytest.approx(stddev([-1.0 / 400, 9.0 / 500]))


def test_score_estimators_buckets_rate_sigma_by_horizon() -> None:
    """Per-R sigma_rate exposes where a variant wins (the opening buckets)."""
    anchors = [
        _est_anchor(30, 870, 10.0, 0.1, 10.0, -9.0, prior_rate=-0.01),
        _est_anchor(60, 840, 20.0, 0.1, 20.0, 5.0, prior_rate=-0.01),
    ]
    prior = score_estimators(anchors, names=("prior",))[0]
    buckets = {b.label: b for b in prior.buckets if b.count}
    assert list(buckets) == ["360-900"]
    assert buckets["360-900"].count == 2
    assert buckets["360-900"].sigma_rate == pytest.approx(
        stddev([(10.0 + 870 * -0.01 + 9.0) / 870,
                (20.0 + 840 * -0.01 - 5.0) / 840])
    )


def test_format_estimators_lists_every_variant_and_metric() -> None:
    """The table has to name the variants and their error scale."""
    anchors = [
        _est_anchor(400, 400, -50.0, 0.1, -10.0, -9.0, prior_rate=-0.01),
        _est_anchor(300, 500, -50.0, 0.1, 0.0, -9.0, prior_rate=-0.01),
    ]
    text = format_estimators(
        score_estimators(anchors), trend_floor_wh=TREND_GATE_NOISE_FLOOR_WH
    )
    for name in ESTIMATOR_NAMES:
        assert name in text
    assert "mean|e|" in text
    assert "by R bucket" in text
    assert f"{TREND_GATE_NOISE_FLOOR_WH:.1f}" in text


def test_cli_estimator_variants_do_not_change_default_output(tmp_path, capsys) -> None:
    """The shipped report is untouched: variants only appear on request."""
    log = tmp_path / "estimator.log"
    log.write_text(
        _fetch(500, -50.0, 0.1, -10.0, 400, 500, complete_q2=-9.0)
        + "\n"
        + _fetch(1800, -100.0, 0.05, -55.0, 500, 1300, complete_q2=-9.0)
    )
    assert fls.main([str(log)]) == 0
    default_out = capsys.readouterr().out
    assert "estimator" not in default_out.lower()

    assert fls.main([str(log), "--estimator"]) == 0
    variant_out = capsys.readouterr().out
    assert "status-quo" in variant_out
    assert "prior-blend@120" in variant_out
    assert default_out.strip() in variant_out


def test_cli_estimator_flag_accepts_a_variant_list(tmp_path, capsys) -> None:
    """``--estimator=prior`` restricts the table to the named variants."""
    log = tmp_path / "estimator.log"
    log.write_text(
        _fetch(500, -50.0, 0.1, -10.0, 400, 500, complete_q2=-9.0)
        + "\n"
        + _fetch(1800, -100.0, 0.05, -55.0, 500, 1300, complete_q2=-9.0)
    )
    assert fls.main([str(log), "--estimator=prior", "--trend-floor=3"]) == 0
    out = capsys.readouterr().out
    assert "prior" in out
    assert "trend-gated" not in out
    assert "3.0" in out


def test_cli_rejects_an_unknown_flag(tmp_path, capsys) -> None:
    """A mistyped flag is an error, not a silently skipped path."""
    log = tmp_path / "estimator.log"
    log.write_text(
        _fetch(500, -50.0, 0.1, -10.0, 400, 500, complete_q2=-9.0)
        + "\n"
        + _fetch(1800, -100.0, 0.05, -55.0, 500, 1300, complete_q2=-9.0)
    )
    assert fls.main([str(log), "--bogus"]) == 1
    captured = capsys.readouterr()
    assert "bogus" in captured.err
    assert "scored anchors" not in captured.out


@_NEEDS_REAL_LOG
def test_real_log_estimator_table_reports_every_variant() -> None:
    """On a real log every variant scores the same anchors."""
    anchors = parse_log(_OVERSHOOT.read_text(errors="replace"), source="x")
    summaries = score_estimators(anchors)
    counts = {s.count for s in summaries}
    assert len(counts) == 1
    assert counts.pop() >= 10
    assert "status-quo" in format_estimators(summaries)


# ── turn-off path data collection ────────────────────────────────────
#
# Production already logs every entry into the unguarded turn-off path:
# the decision (horizon + gap), each shed it books, and the plug turn-ons
# that later undo it. These helpers emit the same shapes so the parsers
# are pinned without a real log present.

_LOGDATE = "2026-10-17"


def _off_entry(stamp: str, gap: float, remaining: int) -> str:
    """A ``[_decide_turn_off]`` entry line: one decision's horizon and gap."""
    return (
        f"[{_LOGDATE} {stamp} +0000] [3224] [DEBUG] load_nbc "
        f"[_decide_turn_off] gap={gap:.1f} Wh, seconds_remaining={remaining}"
    )


def _plug_off(
    stamp: str, device: str, savings: float, gap: float, priority: int = 50
) -> str:
    """A plug shed INFO line, with its logfmt extra suffix."""
    return (
        f"[{_LOGDATE} {stamp} +0000] [3224] [INFO] load_nbc "
        f"action=turn_off device={device} savings={savings:.1f} gap={gap:.1f} "
        f"priority={priority} [action_type=turn_off device={device} event=action "
        f"priority={priority} remaining_gap_wh={gap} savings_wh={savings}]"
    )


def _tesla_amps(
    stamp: str, current: int, target: int, savings: float, before: float, after: float
) -> str:
    """The DEBUG line for a partial Tesla amps-only reduction."""
    return (
        f"[{_LOGDATE} {stamp} +0000] [3224] [DEBUG] load_nbc "
        f"[_decide_turn_off] Tesla amps {current} → {target}, savings={savings:.1f} Wh, "
        f"remaining {before:.1f} → {after:.1f} Wh"
    )


def _tesla_stop(stamp: str, reason: str, remaining: float | None = None) -> str:
    """A Tesla stop INFO line; only the deficit variant logs a remainder."""
    tail = "" if remaining is None else f" remaining={remaining:.1f}"
    return (
        f"[{_LOGDATE} {stamp} +0000] [3224] [INFO] load_nbc "
        f"action=turn_off device=tesla reason={reason}{tail}"
    )


def _plug_on(
    stamp: str, device: str, capacity: float, gap: float, priority: int = 100
) -> str:
    """A plug turn-on INFO line, the signal a shed was undone."""
    return (
        f"[{_LOGDATE} {stamp} +0000] [3224] [INFO] load_nbc "
        f"action=turn_on device={device} capacity={capacity:.1f} gap={gap:.1f} "
        f"priority={priority} [action_type=turn_on capacity_wh={capacity} "
        f"device={device} event=action priority={priority} remaining_gap_wh={gap}]"
    )


def test_parse_turn_offs_attaches_same_second_actions_to_their_decision() -> None:
    """The entry line owns every shed logged in its own second."""
    text = "\n".join(
        [
            _off_entry("21:44:58", 5.8, 4),
            _tesla_amps("21:44:58", 10, 5, 1.3, 5.8, 4.5),
            _plug_off("21:44:58", "ecoflow", 0.3, 4.5, priority=50),
            _plug_off("21:44:58", "jackery", 0.3, 4.2, priority=100),
        ]
    )
    decisions = fls.parse_turn_offs(text, source="x.log")
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.gap_wh == pytest.approx(5.8)
    assert decision.seconds_remaining == 4
    assert [a.kind for a in decision.actions] == ["tesla_amps", "plug", "plug"]
    assert [a.device_name for a in decision.actions] == ["tesla", "ecoflow", "jackery"]
    assert [a.priority for a in decision.actions] == [None, 50, 100]


def test_parse_turn_offs_skips_actions_without_a_decision_line() -> None:
    """A shed with no entry line is skipped, never guessed into a quarter."""
    text = _plug_off("21:44:58", "ecoflow", 0.3, 4.5)
    assert fls.parse_turn_offs(text, source="x.log") == []


def test_parse_turn_offs_handles_a_device_name_with_spaces() -> None:
    """``water heater`` is a plug name, so ``device=`` cannot be ``\\S+``."""
    text = "\n".join(
        [
            _off_entry("21:44:58", 22.9, 536),
            _plug_off("21:44:58", "water heater", 35.7, 22.9, priority=60),
        ]
    )
    decision = fls.parse_turn_offs(text, source="x.log")[0]
    assert [a.device_name for a in decision.actions] == ["water heater"]
    assert decision.shed_wh == pytest.approx(35.7)


def test_parse_turn_ons_handles_a_device_name_with_spaces() -> None:
    """The reversal join needs the same tolerance as the shed side."""
    records = fls.parse_turn_ons(
        _plug_on("21:45:59", "water heater", 61.0, 103.8, priority=60), source="x"
    )
    assert [(r.device_name, r.capacity_wh) for r in records] == [
        ("water heater", pytest.approx(61.0))
    ]


def test_parse_turn_offs_does_not_attach_other_seconds() -> None:
    """Only the entry's own second is attached; a later second is not."""
    text = "\n".join(
        [_off_entry("21:44:58", 5.8, 4), _plug_off("21:45:59", "jackery", 61.0, 51.7)]
    )
    decisions = fls.parse_turn_offs(text, source="x.log")
    assert len(decisions) == 1
    assert decisions[0].actions == ()
    assert fls.parse_turn_ons(text, source="x.log") == []


def test_turn_off_decision_quarter_is_its_wall_quarter() -> None:
    """Wall time is exact: decide() only runs inside the current quarter."""
    decisions = fls.parse_turn_offs(_off_entry("21:44:58", 5.8, 4), source="x.log")
    assert decisions[0].quarter_start == datetime(
        2026, 10, 17, 21, 30, tzinfo=timezone.utc
    )


def test_turn_off_decision_rolls_up_shed_residual_and_overshoot() -> None:
    """Shed sums, the uncovered remainder, and per-action overshoot."""
    text = "\n".join(
        [
            _off_entry("21:44:58", 5.8, 4),
            _tesla_amps("21:44:58", 10, 5, 1.3, 5.8, 4.5),
            _plug_off("21:44:58", "ecoflow", 0.3, 4.5),
            _plug_off("21:44:58", "jackery", 0.3, 4.2),
        ]
    )
    decision = fls.parse_turn_offs(text, source="x.log")[0]
    assert decision.shed_wh == pytest.approx(1.9)
    assert decision.residual_gap_wh == pytest.approx(3.9)
    assert [a.overshoot_wh for a in decision.actions] == pytest.approx(
        [-4.5, -4.2, -3.9]
    )


def test_turn_off_decision_reports_overshoot_when_a_shed_exceeds_the_gap() -> None:
    """A shed larger than what was still needed is a positive overshoot."""
    text = "\n".join(
        [_off_entry("22:15:59", 51.7, 872), _plug_off("22:15:59", "ecoflow", 61.0, 51.7)]
    )
    decision = fls.parse_turn_offs(text, source="x.log")[0]
    assert decision.residual_gap_wh == 0.0
    assert decision.actions[0].overshoot_wh == pytest.approx(9.3)


def test_tesla_stop_action_carries_its_reason_and_no_savings() -> None:
    """Every Tesla stop variant is collected; none logs a booked saving."""
    text = "\n".join(
        [
            _off_entry("22:58:03", 49.7, 300),
            _tesla_stop("22:58:03", "amps_min_reached"),
            _tesla_stop("22:58:03", "deficit", 49.7),
        ]
    )
    decision = fls.parse_turn_offs(text, source="x.log")[0]
    assert [a.kind for a in decision.actions] == ["tesla_stop", "tesla_stop"]
    assert [a.reason for a in decision.actions] == ["amps_min_reached", "deficit"]
    assert all(a.savings_wh is None for a in decision.actions)
    assert all(a.overshoot_wh is None for a in decision.actions)


def test_parse_turn_ons_reads_each_device_and_time() -> None:
    """Turn-ons are parsed for the reversal join only."""
    text = "\n".join(
        [
            _plug_on("21:45:59", "jackery", 61.0, 103.8),
            _plug_on("22:20:33", "ecoflow", 41.1, 45.8),
        ]
    )
    records = fls.parse_turn_ons(text, source="x.log")
    assert [(r.device_name, r.capacity_wh) for r in records] == [
        ("jackery", pytest.approx(61.0)),
        ("ecoflow", pytest.approx(41.1)),
    ]
    assert records[0].at == datetime(2026, 10, 17, 21, 45, 59, tzinfo=timezone.utc)


def test_find_reversals_pairs_a_shed_with_its_later_turn_on() -> None:
    """A shed undone by a turn-on a minute later, across the QH boundary."""
    off = fls.parse_turn_offs(
        "\n".join(
            [_off_entry("21:44:58", 0.6, 4), _plug_off("21:44:58", "jackery", 0.3, 0.6)]
        ),
        source="x.log",
    )
    ons = fls.parse_turn_ons(_plug_on("21:45:59", "jackery", 61.0, 103.8), source="x")
    reversals = fls.find_reversals(off, ons)
    assert len(reversals) == 1
    assert reversals[0].device_name == "jackery"
    assert reversals[0].delay_secs == pytest.approx(61.0)
    # Counted on the shed's own quarter, even though the turn-on is in the next.
    assert reversals[0].quarter_start == datetime(
        2026, 10, 17, 21, 30, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    ("stamp", "device"),
    [
        ("21:30:00", "jackery"),  # before the shed
        ("22:01:00", "jackery"),  # same plug, outside the reversal window
        ("21:45:59", "ecoflow"),  # right time, other plug
    ],
)
def test_find_reversals_ignores_non_matching_turn_ons(stamp: str, device: str) -> None:
    """Only a later, same-device turn-on inside the window undoes a shed."""
    off = fls.parse_turn_offs(
        "\n".join(
            [_off_entry("21:44:58", 0.6, 4), _plug_off("21:44:58", "jackery", 0.3, 0.6)]
        ),
        source="x.log",
    )
    ons = fls.parse_turn_ons(_plug_on(stamp, device, 61.0, 10.0), source="x")
    assert fls.find_reversals(off, ons) == []


def test_summarize_turn_offs_counts_medians_and_rollups() -> None:
    """The summary's counts, medians, and short-horizon/uncovered splits."""
    text = "\n".join(
        [
            _off_entry("21:36:07", 22.9, 536),
            _tesla_amps("21:36:07", 6, 5, 35.7, 22.9, -12.8),
            _off_entry("21:44:58", 5.8, 4),
            _plug_off("21:44:58", "ecoflow", 0.3, 4.5),
            _plug_off("21:44:58", "jackery", 0.3, 4.2),
            _off_entry("22:15:59", 51.7, 872),
            _plug_off("22:15:59", "ecoflow", 61.0, 51.7),
        ]
    )
    summary = fls.summarize_turn_offs(fls.parse_turn_offs(text, source="x.log"))
    assert summary.decision_count == 3
    assert summary.quarter_count == 2
    assert summary.plug_off_count == 3
    assert summary.tesla_amp_count == 1
    assert summary.tesla_stop_count == 0
    assert summary.action_count == 4
    assert summary.shed_total_wh == pytest.approx(97.3)
    assert summary.median_shed_wh == pytest.approx(0.3)
    assert summary.median_remaining_secs == pytest.approx(536.0)
    assert summary.short_horizon_count == 1
    assert summary.short_horizon_secs == fls.TURN_OFF_SHORT_HORIZON_SECS
    assert summary.uncovered_count == 1
    assert summary.median_residual_wh == pytest.approx(5.2)
    assert summary.overshoot_count == 2
    assert summary.max_overshoot_wh == pytest.approx(12.8)


def test_summarize_turn_offs_joins_the_quarter_miss() -> None:
    """A turn-off quarter carries the completed quarter's actual and miss."""
    decisions = fls.parse_turn_offs(
        "\n".join([_off_entry("21:44:58", 5.8, 4), _plug_off("21:44:58", "jackery", 0.3, 4.5)]),
        source="x.log",
    )
    quarters = [
        fls.QuarterSummary(
            source="x.log",
            quarter_start=datetime(2026, 10, 17, 21, 30, tzinfo=timezone.utc),
            anchor_count=3,
            actual_wh=-2.84,
            max_abs_error=4.0,
            mean_signed_error=0.5,
            target_wh=-9.0,
            guard_firings=0,
        )
    ]
    summary = fls.summarize_turn_offs(decisions, quarters=quarters)
    row = summary.quarters[0]
    assert row.decision_count == 1
    assert row.plug_off_count == 1
    assert row.shed_wh == pytest.approx(0.3)
    assert row.actual_wh == pytest.approx(-2.84)
    assert row.target_wh == pytest.approx(-9.0)
    assert row.miss_wh == pytest.approx(6.16)
    assert row.min_remaining_secs == 4


def test_summarize_turn_offs_counts_reversals_per_quarter() -> None:
    """Reversals are counted on the quarter whose shed they undid."""
    text = "\n".join(
        [
            _off_entry("21:44:58", 5.8, 4),
            _plug_off("21:44:58", "jackery", 0.3, 4.5),
            _plug_on("21:45:59", "jackery", 61.0, 103.8),
        ]
    )
    summary = fls.summarize_turn_offs(
        fls.parse_turn_offs(text, source="x.log"),
        fls.parse_turn_ons(text, source="x.log"),
    )
    assert summary.reversal_count == 1
    assert summary.median_reversal_secs == pytest.approx(61.0)
    assert summary.quarters[0].reversal_count == 1


def test_summarize_turn_offs_empty_is_all_zeros() -> None:
    """A log with no turn-off path lines summarizes to zeros, not a crash."""
    summary = fls.summarize_turn_offs([])
    assert summary.decision_count == 0
    assert summary.quarter_count == 0
    assert summary.shed_total_wh == 0.0
    assert summary.median_shed_wh == 0.0
    assert summary.median_residual_wh == 0.0
    assert summary.reversals == ()
    assert summary.median_reversal_secs is None


def test_format_turn_offs_reports_every_section() -> None:
    """The text report names shed, horizon, coverage, overshoot, reversals."""
    text = "\n".join(
        [
            _off_entry("21:44:58", 5.8, 4),
            _plug_off("21:44:58", "jackery", 0.3, 5.8),
            _plug_on("21:45:59", "jackery", 61.0, 103.8),
        ]
    )
    body = fls.format_turn_offs(
        fls.summarize_turn_offs(
            fls.parse_turn_offs(text, source="x.log"),
            fls.parse_turn_ons(text, source="x.log"),
        )
    )
    assert "turn-off decisions: 1 across 1 quarter" in body
    assert "plug 1, Tesla amps 0, Tesla stops 0" in body
    assert "shed:" in body
    assert "horizon:" in body
    assert "uncovered" in body
    assert "overshoot" in body
    assert "reversal" in body
    assert "2026-10-17 21:30" in body
    assert "n/a" in body  # no completed quarter joined


def test_format_turn_offs_without_decisions_says_so() -> None:
    """An empty summary prints a reason, not an empty table."""
    body = fls.format_turn_offs(fls.summarize_turn_offs([]))
    assert "no turn-off decisions" in body
    assert "quarter" not in body


def test_cli_turn_offs_flag_adds_a_section_and_default_omits_it(
    tmp_path, capsys
) -> None:
    """``--turn-offs`` is opt-in; the shipped report is unchanged."""
    log = tmp_path / "turnoffs.log"
    log.write_text(
        _fetch(500, -50.0, 0.1, -10.0, 400, 500, complete_q2=-9.0)
        + "\n"
        + _fetch(1800, -100.0, 0.05, -55.0, 500, 1300, complete_q2=-9.0)
        + "\n"
        + "\n".join(
            [_off_entry("21:44:58", 5.8, 4), _plug_off("21:44:58", "jackery", 0.3, 5.8)]
        )
    )
    assert fls.main([str(log)]) == 0
    default_out = capsys.readouterr().out
    assert "turn-off decisions" not in default_out

    assert fls.main([str(log), "--turn-offs"]) == 0
    turn_off_out = capsys.readouterr().out
    assert "turn-off decisions: 1 across 1 quarter" in turn_off_out
    assert default_out.strip() in turn_off_out


def test_cli_turn_offs_section_prints_without_scored_anchors(
    tmp_path, capsys
) -> None:
    """Turn-off data is still collected on a log with no completed total."""
    log = tmp_path / "turnoffs-only.log"
    log.write_text(
        "\n".join([_off_entry("21:44:58", 5.8, 4), _plug_off("21:44:58", "jackery", 0.3, 5.8)])
    )
    assert fls.main([str(log), "--turn-offs"]) == 1
    out = capsys.readouterr().out
    assert "no scored anchors" in out
    assert "turn-off decisions: 1 across 1 quarter" in out


@_NEEDS_REAL_LOG
def test_real_log_turn_off_decisions_parse() -> None:
    """A real log's turn-off decisions parse with quarters attached."""
    decisions = fls.parse_turn_offs(
        _OVERSHOOT.read_text(errors="replace"), source="x.log"
    )
    assert decisions
    assert all(d.quarter_start.minute % 15 == 0 for d in decisions)
    summary = fls.summarize_turn_offs(decisions)
    assert summary.decision_count == len(decisions)
    assert summary.quarter_count >= 1
    assert "turn-off decisions:" in fls.format_turn_offs(summary)


# ── suppression census (--guards) ────────────────────────────────────
#
# Every mechanism that *declines* to act is invisible to a closed loop that
# only observes actions. These tests pin the log-side census: what each guard
# suppressed, how reachable its trigger was, and the confound that makes the
# guarded-vs-unguarded comparison non-causal.


def _plug_candidate(
    name: str, desired: bool, toggleable: bool = True, power: float = 260.0
) -> str:
    """One ``CandidateDetail`` entry, in production field order."""
    return (
        f"CandidateDetail(device_type='plug', name='{name}', power_watts={power}, "
        f"capacity_wh={power * 0.125}, can_toggle={toggleable}, "
        f"desired_state={desired}, actual_state={desired}, state_available=None, "
        f"is_charging=None, current_amps=None, plugged_in=None, at_home=None, "
        f"reason=None, error=None)"
    )


def _cycle_result(
    stamp: str,
    *,
    reason: str | None = None,
    candidates: tuple[str, ...] = (),
    settle: int = 30,
    quant: int = 30,
    gap: float = 13.5,
    repeat_candidates: bool = False,
) -> str:
    """A cycle-result DEBUG line carrying the fields the census reads.

    ``repeat_candidates`` reproduces production's shape, where the repr
    carries the candidate list in two segments.
    """
    cands = ", ".join(candidates)
    top = f"candidates=[{cands}], " if repeat_candidates else ""
    return (
        f"[{_LOGDATE} {stamp} +0000] [3224] [DEBUG] app "
        f"Load management cycle result: CycleResult({top}status='ok', qh='QH1', "
        "predicted_wh=-22.5, adjusted_wh=-22.5, target_wh=-9, current_wh=None, "
        f"estimated_wh=None, actions=[], diagnostics=CycleDiagnostics(gap_wh={gap}, "
        "hysteresis_wh=3, seconds_remaining=449, "
        "data_point_at=datetime.datetime(2026, 10, 10, 8, 22, 31, "
        "tzinfo=<DstTzInfo 'America/Los_Angeles' PDT-1 day, 17:00:00 DST>), "
        f"reason={reason!r}, pending_effects_count=0, "
        f"plugs_configured=['jackery', 'ecoflow'], candidates=[{cands}], "
        f"quantization_seconds={quant}, settle_window_secs={settle}, "
        "quantization_offset=1, quantization_confidence=0.99))"
    )


def _defer(stamp: str, remaining: int, gap: float, safe: int) -> str:
    """The DEBUG line for a stop the defer rule declined to take."""
    return (
        f"[{_LOGDATE} {stamp} +0000] [3224] [DEBUG] load_nbc "
        f"[_decide_tesla_reduce] deferring stop: current_amps=5, "
        f"seconds_remaining={remaining}, safe_defer={safe}s, gap={gap:.1f} Wh"
    )


def _ramp_stop(stamp: str, remaining: int, gap: float, trend: float) -> str:
    """The DEBUG line for a stop the ramp rule pulled into this cycle."""
    return (
        f"[{_LOGDATE} {stamp} +0000] [3224] [DEBUG] load_nbc "
        f"[_decide_tesla_reduce] ramp stop: t* within one cycle "
        f"(seconds_remaining={remaining}, safe_defer=120s, gap={gap:.1f} Wh, "
        f"trend={trend:.3f} Wh/s)"
    )


def _override(stamp: str, gap: float, banked: float, cover: float, R: int) -> str:
    """The INFO line for a banked-cover rescue of a guarded turn-on."""
    return (
        f"[{_LOGDATE} {stamp} +0000] [3224] [INFO] load_nbc "
        f"gapminder_banked_override gap={gap:.1f} banked={banked:.1f} "
        f"cover={cover:.1f} jitter=0.5275 swing=79.1 R={R} "
        f"[banked_wh={banked} cover_wh={cover} event=gapminder_banked_override "
        f"gap_wh={gap} jitter_wh_per_s=0.5275498 seconds_remaining={R} "
        "swing_wh=79.13248]"
    )


def test_parse_guarded_declines_counts_available_turn_ons() -> None:
    """A declined cycle reports how many turn-ons were actually on offer."""
    text = "\n".join(
        [
            _cycle_result(
                "21:44:58",
                reason="excessive_jitter",
                candidates=(
                    _plug_candidate("jackery", True),
                    _plug_candidate("ecoflow", False),
                    _plug_candidate("sentinel", False, toggleable=False),
                ),
            ),
            _cycle_result(
                "21:45:28", reason="no_eligible", candidates=(_plug_candidate("ecoflow", False),)
            ),
        ]
    )
    declines = fls.parse_guarded_declines(text, source="x.log")
    assert len(declines) == 1
    # jackery is already ON and sentinel is not toggleable: only ecoflow counts.
    assert declines[0].available_candidates == 1
    assert declines[0].quarter_start is not None
    assert declines[0].quarter_start.minute % 15 == 0


def test_parse_guarded_declines_dedupes_a_repeated_candidate_list() -> None:
    """The repr repeats the candidate list; each plug counts once."""
    text = _cycle_result(
        "21:44:58",
        reason="excessive_jitter",
        candidates=(_plug_candidate("ecoflow", False), _plug_candidate("water heater", False, power=4857.0)),
        repeat_candidates=True,
    )
    declines = fls.parse_guarded_declines(text, source="x.log")
    assert declines[0].available_candidates == 2


def test_parse_guarded_declines_ignores_other_reasons() -> None:
    """Only the guard's own reason is a decline; a cycle with none is not."""
    text = "\n".join(
        [
            _cycle_result("21:44:58", reason=None),
            _cycle_result("21:45:28", reason="loads_too_large"),
        ]
    )
    assert fls.parse_guarded_declines(text, source="x.log") == []


def test_parse_defer_entries_and_ramp_stops_split_the_same_branch() -> None:
    """A deferral is a declined stop; a ramp stop is one that was taken."""
    text = "\n".join(
        [
            _defer("21:44:58", 790, 22.0, 66),
            _defer("21:45:28", 756, 18.0, 53),
            _ramp_stop("21:46:00", 149, 45.1, 0.250),
        ]
    )
    defers = fls.parse_defer_entries(text, source="x.log")
    assert [(d.seconds_remaining, d.gap_wh, d.safe_defer_secs) for d in defers] == [
        (790, 22.0, 66),
        (756, 18.0, 53),
    ]
    assert len(fls.parse_ramp_stops(text, source="x.log")) == 1


def test_parse_banked_overrides_and_settle_window_trace() -> None:
    """Rescues are counted, and the settle window keeps its cycle order."""
    text = "\n".join(
        [
            _override("21:44:58", 13.5, -19.2, 13.2, 449),
            _override("21:45:28", 12.4, -19.3, 13.3, 419),
            _cycle_result("21:46:00", settle=30),
            _cycle_result("21:46:30", settle=15),
            _cycle_result("21:47:00", settle=15),
        ]
    )
    assert len(fls.parse_banked_overrides(text, source="x.log")) == 2
    trace = fls.parse_settle_windows(text, source="x.log")
    assert trace.windows == (30, 15, 15)
    assert trace.commits == ((30, 15),)


def test_ramp_band_is_the_window_the_rule_can_fire_in() -> None:
    """The band is the arithmetical reach of ``t* <= cycle_secs``."""
    # t* = (P*R - gap) / (P + trend), trend clamped at P/2, so firing needs
    # P*R - gap <= cycle * 1.5*P, i.e. R <= gap/P + 45 s.
    assert fls.ramp_band_remaining_secs(30.0) == pytest.approx(135.0)
    assert fls.ramp_band_remaining_secs(0.0) == pytest.approx(45.0)


def test_summarize_suppressions_rolls_up_every_counter() -> None:
    """One summary carries the guard, the deferral, the ramp rule and settle."""
    text = "\n".join(
        [
            _cycle_result(
                "21:44:58",
                reason="excessive_jitter",
                candidates=(_plug_candidate("ecoflow", False),),
            ),
            _cycle_result("21:45:28", reason="excessive_jitter"),
            _override("21:45:58", 13.5, -19.2, 13.2, 449),
            _defer("21:46:00", 100, 30.0, 90),
            _defer("21:47:00", 700, 20.0, 60),
            _ramp_stop("21:48:00", 149, 45.1, 0.250),
            _cycle_result("21:48:30", settle=30),
            _cycle_result("21:49:00", settle=15),
        ]
    )
    census = fls.summarize_suppressions(
        declines=fls.parse_guarded_declines(text, source="x.log"),
        overrides=fls.parse_banked_overrides(text, source="x.log"),
        defers=fls.parse_defer_entries(text, source="x.log"),
        ramp_fires=fls.parse_ramp_stops(text, source="x.log"),
        settle_windows=[fls.parse_settle_windows(text, source="x.log")],
    )
    assert census.declined_cycles == 2
    assert census.declined_with_candidate == 1
    assert census.rescues == 1
    assert census.deferrals == 2
    assert census.median_defer_secs == 400
    assert census.max_defer_secs == 700
    assert census.ramp_fires == 1
    # R=100 lies inside 3*30+45=135 s; R=700 lies far outside 3*20+45=105 s.
    assert census.ramp_eligible == 1
    assert census.settle_cycles == ((30, 3), (15, 1))
    assert census.settle_commits == 1


def test_summarize_suppressions_counts_declines_from_guard_events() -> None:
    """Fires and declined cycles are different counts and are kept apart."""
    text = "\n".join(
        [
            _cycle_result("21:44:58", reason="excessive_jitter"),
            _cycle_result("21:45:28", reason="excessive_jitter"),
            _cycle_result("21:45:58", reason=None),
        ]
    )
    census = fls.summarize_suppressions(
        guard_events=fls.parse_guard_events(text, source="x.log"),
        declines=fls.parse_guarded_declines(text, source="x.log"),
    )
    assert census.declines == 0
    assert census.declined_cycles == 2


def test_summarize_suppressions_splits_guarded_and_unguarded_quarters() -> None:
    """The comparison reports both sides, so the confound is visible."""
    guilty = datetime(2026, 10, 17, 21, 30, tzinfo=timezone.utc)
    calm = datetime(2026, 10, 17, 22, 0, tzinfo=timezone.utc)
    quarters = [
        fls.QuarterSummary(
            source="x.log",
            quarter_start=guilty,
            anchor_count=2,
            actual_wh=11.0,
            max_abs_error=20.0,
            mean_signed_error=-10.0,
            target_wh=-9.0,
            guard_firings=3,
        ),
        fls.QuarterSummary(
            source="x.log",
            quarter_start=calm,
            anchor_count=2,
            actual_wh=-8.0,
            max_abs_error=2.0,
            mean_signed_error=1.0,
            target_wh=-9.0,
            guard_firings=0,
        ),
    ]
    anchors = [
        ForecastAnchor(
            source="x.log",
            quarter_start=guilty,
            data_point_at=guilty,
            remaining_secs=300,
            samples_used=10,
            prediction_values=10,
            raw_wh=0.0,
            prediction_w=1.0,
            predicted_wh=25.0,
            actual_wh=11.0,
        ),
        ForecastAnchor(
            source="x.log",
            quarter_start=guilty,
            data_point_at=guilty,
            remaining_secs=200,
            samples_used=10,
            prediction_values=10,
            raw_wh=0.0,
            prediction_w=1.0,
            predicted_wh=21.0,
            actual_wh=11.0,
        ),
        ForecastAnchor(
            source="x.log",
            quarter_start=calm,
            data_point_at=calm,
            remaining_secs=300,
            samples_used=10,
            prediction_values=10,
            raw_wh=0.0,
            prediction_w=1.0,
            predicted_wh=-7.0,
            actual_wh=-8.0,
        ),
    ]
    census = fls.summarize_suppressions(quarters=quarters, anchors=anchors)
    assert (census.guarded.quarters, census.unguarded.quarters) == (1, 1)
    assert census.guarded.mean_abs_miss_wh == pytest.approx(20.0)
    assert census.unguarded.mean_abs_miss_wh == pytest.approx(1.0)
    assert census.guarded.mean_abs_error_wh == pytest.approx(12.0)
    assert census.unguarded.mean_abs_error_wh == pytest.approx(1.0)


def test_format_suppressions_reports_every_section() -> None:
    """Every mechanism gets a line, and the confound is stated, not implied."""
    text = "\n".join(
        [
            _cycle_result(
                "21:44:58", reason="excessive_jitter", candidates=(_plug_candidate("ecoflow", False),)
            ),
            _override("21:45:00", 13.5, -19.2, 13.2, 449),
            _defer("21:46:00", 100, 30.0, 90),
            _ramp_stop("21:47:00", 149, 45.1, 0.250),
            _cycle_result("21:48:00", settle=30),
            _cycle_result("21:49:00", settle=15),
        ]
    )
    body = fls.format_suppressions(
        fls.summarize_suppressions(
            declines=fls.parse_guarded_declines(text, source="x.log"),
            overrides=fls.parse_banked_overrides(text, source="x.log"),
            defers=fls.parse_defer_entries(text, source="x.log"),
            ramp_fires=fls.parse_ramp_stops(text, source="x.log"),
            settle_windows=[fls.parse_settle_windows(text, source="x.log")],
        )
    )
    assert "jitter guard" in body
    assert "declined cycle" in body
    assert "rescue" in body
    assert "deferral" in body
    assert "ramp rule" in body
    assert "eligible" in body
    assert "settle window" in body
    assert "confound" in body
    # Every declined cycle here offered a load, so the census says outright
    # that availability is universal and cannot convict the guard.
    assert "not what the decline withheld" in body


def test_availability_note_flags_a_decline_with_nothing_to_turn_on() -> None:
    """A decline with no candidate at all is called out, not generalized."""
    text = _cycle_result("21:44:58", reason="excessive_jitter")
    body = fls.format_suppressions(
        fls.summarize_suppressions(
            declines=fls.parse_guarded_declines(text, source="x.log")
        )
    )
    assert "some declined cycles had nothing to turn on" in body


def test_format_suppressions_without_data_says_so() -> None:
    """An empty census prints a reason, not an empty table."""
    body = fls.format_suppressions(fls.summarize_suppressions())
    assert "no suppression" in body
    assert "quarter" not in body


def test_cli_guards_flag_adds_a_section_and_default_omits_it(tmp_path, capsys) -> None:
    """``--guards`` is opt-in; the shipped report is unchanged."""
    log = tmp_path / "guards.log"
    log.write_text(
        _fetch(500, -50.0, 0.1, -10.0, 400, 500, complete_q2=-9.0)
        + "\n"
        + _fetch(1800, -100.0, 0.05, -55.0, 500, 1300, complete_q2=-9.0)
        + "\n"
        + _cycle_result(
            "21:44:58", reason="excessive_jitter", candidates=(_plug_candidate("ecoflow", False),)
        )
    )
    assert fls.main([str(log)]) == 0
    default_out = capsys.readouterr().out
    assert "suppression census" not in default_out

    assert fls.main([str(log), "--guards"]) == 0
    guards_out = capsys.readouterr().out
    assert "suppression census" in guards_out
    assert default_out.strip() in guards_out


def test_ramp_band_constants_track_production(monkeypatch) -> None:
    """The band arithmetic mirrors load_nbc's own constants, never a copy."""
    from load_nbc import TeslaDecider

    from load_manager import LoadManagerConfig

    monkeypatch.setenv("TESLA_NOMINAL_VOLTAGE", "240")
    assert fls.RAMP_BAND_CLAMP_FRACTION == TeslaDecider.RAMP_TREND_CLAMP_FRACTION
    assert fls.RAMP_BAND_DRAW_WH_PER_S == pytest.approx(
        TeslaDecider().car_power_watts_5a / 3600
    )
    assert fls.RAMP_BAND_CYCLE_SECS == LoadManagerConfig().config_interval_secs


@_NEEDS_REAL_LOG
def test_real_log_suppression_census_is_self_consistent() -> None:
    """A real jitter log parses into a coherent census."""
    text = _OVERSHOOT.read_text(errors="replace")
    census = fls.summarize_suppressions(
        guard_events=fls.parse_guard_events(text, source="x.log"),
        declines=fls.parse_guarded_declines(text, source="x.log"),
        overrides=fls.parse_banked_overrides(text, source="x.log"),
        defers=fls.parse_defer_entries(text, source="x.log"),
        ramp_fires=fls.parse_ramp_stops(text, source="x.log"),
        settle_windows=[fls.parse_settle_windows(text, source="x.log")],
    )
    assert census.declined_with_candidate <= census.declined_cycles
    assert census.ramp_eligible <= census.deferrals
    assert sum(count for _, count in census.settle_cycles) == len(
        fls.parse_settle_windows(text, source="x.log").windows
    )
    assert "suppression census" in fls.format_suppressions(census)
