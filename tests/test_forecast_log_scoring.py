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
from datetime import datetime, timezone
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
