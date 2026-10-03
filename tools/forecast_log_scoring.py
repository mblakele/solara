"""Offline tool: score NBC forecast quality against production logs.

Extracts every forecast anchor from a production log — the incomplete QH1
``NBCQuarter`` emitted on each fetch — pairs it with the completed quarter's
real total, and reports the derived quantities needed to size a forecast
uncertainty band.

Despite "scoring", this does not re-execute any decision logic; it measures
the forecast only. (Contrast ``tests/test_tesla_ramp_replay.py``, which
replays a log's cycles back through the production classes and asserts the
decision outcome changed.) Name the log because logs are the input.

It also reads what production logged about its own decisions — the DEBUG
``Load management cycle result: CycleResult(...)`` reprs (per-quarter
target, reason, production churn) and the INFO ``gapminder_jitter_guard``
events — and reports them alongside a churn band (``k * churn * R``,
production's ``GapTrendTracker`` fed from the anchor series) as a second
uncertainty model next to the sigma band. Logged decisions are reported,
never re-executed. Offline churn uses raw ``predicted_wh`` (no
pending-effect adjustment), so quarters containing actions make it a
slight approximation of the value the guard consumed.

Everything here is read-only analysis over log text; no production module
imports this. The forecast itself (``util.compute_nbc_quarter``) is
deliberately left unchanged: see
``.opencode/plans/forecast-uncertainty-action-band.md`` §4 for the candidates
already evaluated and rejected.

Run from the repo root::

    uv run python tools/forecast_log_scoring.py bugs/*.log
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import pytz

# Solara is a flat-layout project: production modules sit beside this one,
# not inside a package. Running this file directly puts tools/ (not the repo
# root) on sys.path, so the root has to be added back before importing util.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Flat-layout script: both local imports must follow the _ROOT sys.path
# bootstrap above, so they deliberately sit below module-level code (flake8
# E402 and pylint wrong-import-position are the same complaint).
from gap_trend import GapTrendTracker  # noqa: E402 # pylint: disable=wrong-import-position
from util import QH_PERIOD_SECONDS, floor_to_qh # noqa: E402 # pylint: disable=wrong-import-position

# data_start is the alignment anchor; per-second samples are contiguous from
# it and every bucket is quarter-hour aligned (see the contiguity axiom in
# AGENTS.md), so ``data_start + k*900s`` walks the quarter boundaries.
_DATA_START_RE = re.compile(
    r"create_metrics result:.*?data_start=([\d]{4}-[\d]{2}-[\d]{2} [\d:]+)\+00:00"
)
_LEN_RE = re.compile(r"nbc_set len (\d+)")
_DATA_TOTAL_RE = re.compile(r"per_second_data_total=(\d+)")
_QUARTER_RE = re.compile(
    r"qh(\d)=NBCQuarter\(complete=(True|False), raw_wh=(-?[\d.eE+-]+|None),"
    r" wh=(-?[\d.eE+-]+|None),"
    r" prediction_values=(\d+|None), prediction_w=(-?[\d.eE+-]+|None),"
    r" predicted_wh=(-?[\d.eE+-]+|None), remaining_seconds=(\d+|None),"
    r" samples_used=(\d+|None)\)"
)

# Log line prefix: "[2026-10-01 20:40:01 +0000] [pid] [LEVEL] ..." — the
# wall-clock stamp that places an INFO event in its quarter.
_LINE_PREFIX_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ([+-]\d{4})\]")
# Message core of the guard INFO line; the logfmt extra suffix uses
# ``gap_wh=``/``jitter_wh_per_s=``/``swing_wh=``/``seconds_remaining=``
# keys, so this matches exactly once per line.
_GUARD_RE = re.compile(
    r"gapminder_jitter_guard gap=(-?[\d.]+) jitter=(-?[\d.]+) swing=(-?[\d.]+) R=(\d+)"
)
_CYCLE_DIAGS = "diagnostics=CycleDiagnostics("
_CYCLE_TARGET_RE = re.compile(r"target_wh=(-?\d+|None)")
# data_point_at=datetime.datetime(2026, 10, 1, 13, 39, 31[, 123456],
#   tzinfo=<DstTzInfo 'America/Los_Angeles' PDT-1 day, 17:00:00 DST>)
_CYCLE_DATA_POINT_RE = re.compile(
    r"data_point_at=datetime\.datetime\((\d{4}), (\d{1,2}), (\d{1,2}),"
    r" (\d{1,2}), (\d{1,2}), (\d{1,2})(?:, (\d{1,6}))?, tzinfo=<[^>]*?'([^']+)'"
)
_CYCLE_REASON_RE = re.compile(r"reason=(?:'([^']*)'|None)")
_CYCLE_JITTER_RE = re.compile(r"gap_jitter_wh_per_s=(-?[\d.eE+-]+|None)")

_QuarterKey = tuple[str, datetime]
"""``(source, quarter_start)`` — the key quarters are grouped by."""


@dataclass(frozen=True)
class ForecastAnchor:
    """One QH1 forecast, plus the completed quarter's real total when known.

    An anchor is a point where load management *could* have acted: the
    incomplete quarter's ``raw_wh`` plus ``remaining_seconds *
    prediction_w``. The realized error against that quarter's final total is
    the quantity the uncertainty band has to cover.
    """

    source: str
    quarter_start: datetime
    data_point_at: datetime
    remaining_secs: int
    samples_used: int
    prediction_values: int
    raw_wh: float
    prediction_w: float
    predicted_wh: float
    actual_wh: float | None

    @property
    def horizon_ratio(self) -> float:
        """Extrapolation horizon divided by the window it was measured over.

        ``prediction_w`` is a mean over ``prediction_values`` per-second
        samples but is extrapolated across ``remaining_secs``. At the
        quarter's start that is ~30x; near the end it approaches 1x.
        """
        return self.remaining_secs / self.prediction_values

    @property
    def error(self) -> float | None:
        """Signed forecast error: ``predicted_wh - actual`` (negative = optimistic)."""
        if self.actual_wh is None:
            return None
        return self.predicted_wh - self.actual_wh

    @property
    def true_forward_rate(self) -> float | None:
        """Rate actually realized over the forecast horizon (Wh/s)."""
        if self.actual_wh is None or self.remaining_secs <= 0:
            return None
        return (self.actual_wh - self.raw_wh) / self.remaining_secs

    @property
    def rate_shortfall(self) -> float | None:
        """How far the trailing mean under-stated the forward rate (Wh/s).

        This is the bias a slope-intercept fit would have to correct. On
        2026-10-01 it ran from -21% to -48% of the trailing mean while the
        rate stepped rather than ramped, which is why a linear fit did not
        reach it.
        """
        fwd = self.true_forward_rate
        if fwd is None:
            return None
        return fwd - self.prediction_w


@dataclass(frozen=True)
class _Quarter:
    index: int
    complete: bool
    raw_wh: float | None
    prediction_values: int | None
    prediction_w: float | None
    predicted_wh: float | None
    remaining_secs: int | None
    samples_used: int | None


def _parse_data_start(line: str) -> datetime | None:
    """Return the QH-aligned ``data_start`` on a create_metrics line."""
    match = _DATA_START_RE.search(line)
    if not match:
        return None
    stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    if stamp != floor_to_qh(stamp):
        # Misaligned window: floor(len/900)*900 no longer walks quarter
        # boundaries, so quarter identity cannot be trusted. Drop it.
        return None
    return stamp


def _parse_quarters(line: str) -> tuple[int | None, list[_Quarter]]:
    """Return ``(cache_len, quarters)`` for an nbc_set line."""
    len_match = _LEN_RE.search(line)
    if not len_match:
        return None, []
    quarters = [
        _Quarter(
            index=int(idx),
            complete=complete == "True",
            raw_wh=None if raw == "None" else float(raw),
            prediction_values=None if pv == "None" else int(pv),
            prediction_w=None if pw == "None" else float(pw),
            predicted_wh=None if pred == "None" else float(pred),
            remaining_secs=None if rem == "None" else int(rem),
            samples_used=None if used == "None" else int(used),
        )
        for idx, complete, raw, _wh, pv, pw, pred, rem, used in _QUARTER_RE.findall(line)
    ]
    return int(len_match.group(1)), quarters


def _quarter_start(data_start: datetime, cache_len: int, index: int) -> datetime:
    """Absolute start of quarter ``index`` (1-based; 1 is the incomplete one).

    The cache holds whole quarters from ``data_start`` onward, so the first
    ``cache_len`` samples span ``ceil(cache_len / 900)`` quarters. QH1 is the
    last of those; QH2/QH3/QH4 walk backwards from it.
    """
    quarters_spanned = (cache_len + QH_PERIOD_SECONDS - 1) // QH_PERIOD_SECONDS
    offset = (quarters_spanned - index) * QH_PERIOD_SECONDS
    return data_start + timedelta(seconds=offset)


def parse_log(text: str, source: str = "<log>") -> list[ForecastAnchor]:
    """Extract forecast anchors from one log's text.

    Completed quarters encountered anywhere in the log (as a completed QH1 or
    as QH2/QH3/QH4) supply ground truth; each is matched to anchors by the
    quarter start it belongs to.

    Args:
        text: Full log contents.
        source: Identifier recorded on each anchor (usually the filename).

    Returns:
        Anchors in log order. ``actual_wh`` is None when the log never showed
        that quarter completing — the caller decides whether to drop those.
    """
    anchors: list[ForecastAnchor] = []
    actuals: dict[datetime, float] = {}
    # Each fetch logs ``nbc_set len N`` immediately *before* the
    # ``create_metrics result`` line that reports the same ``N``. Holding one
    # record bridges that ordering; the length must agree or the pairing is
    # not trustworthy and both sides are dropped.
    pending: tuple[int, list[_Quarter]] | None = None

    for line in text.splitlines():
        if "create_metrics result:" in line:
            if pending is not None:
                data_start = _parse_data_start(line)
                total = _DATA_TOTAL_RE.search(line)
                held_len, held_quarters = pending
                if data_start is not None and total is not None:
                    if int(total.group(1)) == held_len:
                        _collect(
                            data_start, held_len, held_quarters,
                            source, anchors, actuals,
                        )
            pending = None
            continue
        if "nbc_set" not in line:
            continue
        parsed_len, parsed_quarters = _parse_quarters(line)
        if parsed_len is None:
            continue
        pending = (parsed_len, parsed_quarters)

    return [
        anchor if anchor.quarter_start not in actuals
        else ForecastAnchor(**{**anchor.__dict__, "actual_wh": actuals[anchor.quarter_start]})
        for anchor in anchors
    ]


def _to_anchor(
    quarter: _Quarter, start: datetime, source: str
) -> ForecastAnchor | None:
    """Build an anchor from a QH1 entry, or None if any field is missing."""
    if (
        quarter.raw_wh is None
        or quarter.prediction_w is None
        or quarter.predicted_wh is None
    ):
        return None
    if (
        quarter.remaining_secs is None
        or quarter.samples_used is None
        or quarter.prediction_values is None
    ):
        return None
    return ForecastAnchor(
        source=source,
        quarter_start=start,
        data_point_at=start + timedelta(seconds=quarter.samples_used - 1),
        remaining_secs=quarter.remaining_secs,
        prediction_values=quarter.prediction_values,
        samples_used=quarter.samples_used,
        raw_wh=quarter.raw_wh,
        prediction_w=quarter.prediction_w,
        predicted_wh=quarter.predicted_wh,
        actual_wh=None,
    )


def _collect(
    data_start: datetime,
    cache_len: int,
    quarters: list[_Quarter],
    source: str,
    anchors: list[ForecastAnchor],
    actuals: dict[datetime, float],
) -> None:
    """File one parsed ``nbc_set`` record under its resolved data_start."""
    for quarter in quarters:
        start = _quarter_start(data_start, cache_len, quarter.index)
        if quarter.complete:
            if quarter.raw_wh is not None:
                actuals[start] = quarter.raw_wh
            continue
        anchor = _to_anchor(quarter, start, source)
        if anchor is not None:
            anchors.append(anchor)


def scored_anchors(anchors: Iterable[ForecastAnchor]) -> list[ForecastAnchor]:
    """Keep only anchors whose quarter total is known."""
    return [a for a in anchors if a.actual_wh is not None]


def iter_quarters(anchors: Iterable[ForecastAnchor]) -> Iterator[tuple[str, datetime]]:
    """Yield ``(source, quarter_start)`` once per distinct quarter."""
    seen: set[tuple[str, datetime]] = set()
    for anchor in anchors:
        key = (anchor.source, anchor.quarter_start)
        if key not in seen:
            seen.add(key)
            yield key


# ── decision records (logged, never replayed) ────────────────────────


@dataclass(frozen=True)
class GuardEvent:
    """One ``gapminder_jitter_guard`` INFO line: a turn-on it declined."""

    source: str
    at: datetime
    gap_wh: float
    jitter_wh_per_s: float
    swing_wh: float
    seconds_remaining: int

    @property
    def quarter_start(self) -> datetime:
        """Quarter the decision belonged to.

        Wall time is exact here: the pipeline's ``previous_qh`` check
        exits before ``decide()``, so a guard event can only fire while
        ``data_point_at`` lies in the current wall-clock quarter.
        """
        return floor_to_qh(self.at)


@dataclass(frozen=True)
class CycleRecord:
    """The decision context one cycle-result repr reports.

    Attributes:
        source: Log the record came from (usually the filename).
        quarter_start: Quarter of the cycle's ``data_point_at``, or None
            when it is missing or its timezone cannot be resolved.
        target_wh: The cycle's ``target_wh``, or None for early exits.
        reason: The diagnostics reason (e.g. ``excessive_jitter``).
        gap_jitter_wh_per_s: Production's own churn value when logged.
    """

    source: str
    quarter_start: datetime | None
    target_wh: int | None
    reason: str | None
    gap_jitter_wh_per_s: float | None


def _line_timestamp(line: str) -> datetime | None:
    """Return the line's prefix timestamp in UTC, or None if absent."""
    match = _LINE_PREFIX_RE.match(line)
    if match is None:
        return None
    stamp = datetime.strptime(
        f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S %z"
    )
    return stamp.astimezone(timezone.utc)


def _cycle_quarter(segment: str) -> datetime | None:
    """Quarter of the diagnostics ``data_point_at`` inside ``segment``.

    Args:
        segment: Text starting at ``diagnostics=CycleDiagnostics(``.

    Returns:
        The QH-aligned UTC data point, or None when it cannot be parsed
        or the repr's timezone name is unknown.
    """
    match = _CYCLE_DATA_POINT_RE.search(segment)
    if match is None:
        return None
    year, month, day, hour, minute, sec, micro, zone = match.groups()
    try:
        tz = pytz.timezone(zone)
    except pytz.UnknownTimeZoneError:
        return None
    naive = datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(sec), int(micro or 0)
    )
    return floor_to_qh(tz.localize(naive).astimezone(timezone.utc))


def parse_guard_events(text: str, source: str = "<log>") -> list[GuardEvent]:
    """Extract ``gapminder_jitter_guard`` INFO events from one log's text.

    Args:
        text: Full log contents.
        source: Identifier recorded on each event (usually the filename).

    Returns:
        Events in log order; empty for logs written before the guard
        existed. Events without a parseable timestamp prefix are skipped
        rather than guessed into a quarter.
    """
    events: list[GuardEvent] = []
    for line in text.splitlines():
        if "gapminder_jitter_guard" not in line:
            continue
        match = _GUARD_RE.search(line)
        if match is None:
            continue
        stamp = _line_timestamp(line)
        if stamp is None:
            continue
        events.append(
            GuardEvent(
                source=source,
                at=stamp,
                gap_wh=float(match.group(1)),
                jitter_wh_per_s=float(match.group(2)),
                swing_wh=float(match.group(3)),
                seconds_remaining=int(match.group(4)),
            )
        )
    return events


def parse_cycles(text: str, source: str = "<log>") -> list[CycleRecord]:
    """Extract cycle-result records from one log's text.

    Parses the DEBUG ``Load management cycle result: CycleResult(...)``
    reprs. ``target_wh`` is taken from the record itself; reason, data
    point, and production churn are scoped to the
    ``diagnostics=CycleDiagnostics(...)`` segment so the pending effects'
    older ``data_point_at`` and the candidates' own ``reason`` never
    leak in.

    Args:
        text: Full log contents.
        source: Identifier recorded on each record (usually the filename).

    Returns:
        Records in log order. Lines without a diagnostics segment are
        skipped; a record whose data point is unparseable keeps its
        reason/churn values with ``quarter_start`` None.
    """
    records: list[CycleRecord] = []
    for line in text.splitlines():
        if "CycleResult(" not in line:
            continue
        diags = line.find(_CYCLE_DIAGS)
        if diags < 0:
            continue
        segment = line[diags:]
        target = _CYCLE_TARGET_RE.search(line)
        reason = _CYCLE_REASON_RE.search(segment)
        jitter = _CYCLE_JITTER_RE.search(segment)
        records.append(
            CycleRecord(
                source=source,
                quarter_start=_cycle_quarter(segment),
                target_wh=(
                    None
                    if target is None or target.group(1) == "None"
                    else int(target.group(1))
                ),
                reason=None if reason is None else reason.group(1),
                gap_jitter_wh_per_s=(
                    None
                    if jitter is None or jitter.group(1) == "None"
                    else float(jitter.group(1))
                ),
            )
        )
    return records


# ── reporting ────────────────────────────────────────────────────────

BUCKET_EDGES: tuple[int, ...] = (0, 60, 120, 180, 240, 360, 901)
"""``remaining_seconds`` bucket boundaries for the error report.

Zero ``remaining_seconds`` is the quarter boundary itself, where no decision
could be made; the buckets above it are the ranges the load manager actually
acts in.
"""


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of a non-empty sequence.

    Args:
        values: Samples; must not be empty.
        fraction: Rank in ``[0, 1]``.

    Returns:
        The smallest sample at or above the requested rank.
    """
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def stddev(values: Sequence[float]) -> float:
    """Population standard deviation.

    Args:
        values: Samples; must not be empty.

    Returns:
        The population sigma (0.0 for a single sample).
    """
    if not values:
        raise ValueError("stddev requires at least one value")
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def forecast_uncertainty_wh(remaining_secs: float, sigma_rate: float, k: float) -> float:
    """Band half-width for a forecast made ``remaining_secs`` before the boundary.

    The realized error is *identically* ``-remaining_secs * rate_shortfall``
    (see ``ForecastAnchor.error``), so a band covering the spread of the
    trailing mean's rate error only has to scale with the horizon. There is no
    separate horizon-independent error term to fit.

    Args:
        remaining_secs: Extrapolation horizon left in the quarter.
        sigma_rate: Spread of the trailing mean's rate error, in Wh/s.
        k: Multiple of sigma to cover.

    Returns:
        Half-width of the band, in Wh.
    """
    return k * sigma_rate * remaining_secs


def churn_by_anchor(anchors: Sequence[ForecastAnchor]) -> list[float]:
    """Production churn estimate for each anchor, in input order.

    Each quarter's anchors are fed to ``gap_trend.GapTrendTracker`` the
    way production feeds its cycles: one ``update(data_point_at, ...)``
    per anchor in log order (repeated data points are rejected by the
    tracker just as in production). Raw ``predicted_wh`` stands in for
    the production gap ``target - adjusted``: the unknown target is
    constant within the quarter, and both a constant offset and a sign
    flip leave slopes - and therefore the EWMA of ``|Δslope|`` -
    unchanged. ``noise_floor`` is left at 0 because churn is folded
    before the trust checks, so the floor cannot change it. The
    pending-effect adjustment is unavailable offline, so anchors
    recorded after an action make this a slight approximation of the
    value the guard consumed.

    Args:
        anchors: Anchors for the log; per-quarter input order (log
            order) is preserved.

    Returns:
        One churn value (Wh/s) per anchor, aligned by index. 0.0 until
        an anchor has two slopes behind it, after a quarter rollover,
        or after a data gap over the tracker's max span.
    """
    churns = [0.0] * len(anchors)
    trackers: dict[_QuarterKey, GapTrendTracker] = {}
    for index, anchor in enumerate(anchors):
        key = (anchor.source, anchor.quarter_start)
        tracker = trackers.get(key)
        if tracker is None:
            tracker = GapTrendTracker()
            trackers[key] = tracker
        tracker.update(anchor.data_point_at, anchor.predicted_wh)
        churns[index] = tracker.churn_wh_per_s
    return churns


def churn_uncertainty_wh(remaining_secs: float, churn_rate: float, k: float) -> float:
    """Churn band half-width for a forecast made ``remaining_secs`` out.

    The time-varying counterpart of ``forecast_uncertainty_wh``: the
    global ``sigma_rate`` is replaced by the churn observed right at the
    anchor. This is the uncertainty model the turn-on jitter guard
    consumes (``churn * R >= gap`` declines the action), so scoring it
    against realized error sizes the guard's fraction directly.

    Args:
        remaining_secs: Extrapolation horizon left in the quarter.
        churn_rate: EWMA swing of the gap slope at the anchor, in Wh/s.
        k: Multiple of churn to cover.

    Returns:
        Half-width of the band, in Wh.
    """
    return k * churn_rate * remaining_secs


@dataclass(frozen=True)
class ErrorBucket:
    """Forecast-error statistics for one ``remaining_seconds`` range."""

    low_secs: int
    high_secs: int
    count: int
    mean_abs_error: float
    p80_abs_error: float
    mean_signed_error: float
    mean_shortfall: float | None
    sigma_rate: float | None
    under_stated_count: int

    @property
    def label(self) -> str:
        """Human-readable range, e.g. ``120-179``."""
        return f"{self.low_secs}-{self.high_secs - 1}"


@dataclass(frozen=True)
class QuarterSummary:
    """Per-quarter roll-up, to expose quarters that behave differently."""

    source: str
    quarter_start: datetime
    anchor_count: int
    actual_wh: float
    max_abs_error: float
    mean_signed_error: float
    target_wh: float | None
    guard_firings: int

    @property
    def miss_wh(self) -> float | None:
        """``actual - target``: + means the quarter finished above target.

        Returns:
            The miss in Wh, or None when no cycle reported a target.
        """
        if self.target_wh is None:
            return None
        return self.actual_wh - self.target_wh


@dataclass(frozen=True)
class GuardSummary:
    """What production logged about its turn-on jitter guard.

    Gathered from ``gapminder_jitter_guard`` INFO events, the cycle
    results reporting reason ``excessive_jitter``, and the production
    churn values — all three only exist in logs written after the
    guard shipped, so historical logs summarize to zeros/None.
    """

    firing_count: int
    excessive_jitter_cycles: int
    production_churn_mean: float | None
    production_churn_max: float | None


@dataclass(frozen=True)
class ForecastReport:
    """Aggregate view of forecast quality across every scored anchor."""

    anchor_count: int
    quarter_count: int
    dropped_count: int
    mean_abs_error: float
    max_abs_error: float
    median_signed_error: float
    optimistic_count: int
    optimistic_fraction: float
    buckets: tuple[ErrorBucket, ...]
    quarters: tuple[QuarterSummary, ...]
    horizons: tuple[int, ...]
    errors: tuple[float, ...]
    sigma_rate: float
    churns: tuple[float, ...]
    guard: GuardSummary

    @property
    def measurable_churn_count(self) -> int:
        """Scored anchors whose churn band has nonzero width.

        Returns:
            Count of anchors with churn above zero (0.0 means the swing
            was not measurable yet or was flat).
        """
        return sum(1 for c in self.churns if c > 0.0)

    def coverage(self, k: float) -> float:
        """Fraction of scored anchors the band ``forecast_uncertainty_wh`` would admit.

        Args:
            k: Multiple of ``sigma_rate`` the band covers.

        Returns:
            Share of anchors whose realized error falls inside the band,
            or 0.0 when there is nothing to score.
        """
        if not self.errors:
            return 0.0
        inside = sum(
            1
            for horizon, error in zip(self.horizons, self.errors)
            if abs(error) <= forecast_uncertainty_wh(horizon, self.sigma_rate, k)
        )
        return inside / len(self.errors)

    def churn_coverage(self, k: float) -> float:
        """Fraction of scored anchors inside the churn band ``k * churn * R``.

        The counterpart of ``coverage`` for the jitter guard's
        uncertainty model. Anchors without measurable churn (0.0)
        contribute a zero-width band, so they only count when the error
        is also zero.

        Args:
            k: Multiple of each anchor's churn the band covers.

        Returns:
            Share of anchors whose realized error falls inside the band,
            or 0.0 when there is nothing to score.
        """
        if not self.errors:
            return 0.0
        inside = sum(
            1
            for churn, horizon, error in zip(self.churns, self.horizons, self.errors)
            if abs(error) <= churn_uncertainty_wh(horizon, churn, k)
        )
        return inside / len(self.errors)


def _bucket(anchors: Sequence[ForecastAnchor], low: int, high: int) -> ErrorBucket:
    """Summarize the anchors whose ``remaining_secs`` falls in ``[low, high)``."""
    errors = [a.error for a in anchors if a.error is not None]
    signed = [e for e in errors if e is not None]
    shortfalls = [a.rate_shortfall for a in anchors if a.rate_shortfall is not None]
    if not errors:
        return ErrorBucket(low, high, 0, 0.0, 0.0, 0.0, None, None, 0)
    return ErrorBucket(
        low_secs=low,
        high_secs=high,
        count=len(errors),
        mean_abs_error=sum(abs(e) for e in signed) / len(signed),
        p80_abs_error=percentile([abs(e) for e in signed], 0.8),
        mean_signed_error=sum(signed) / len(signed),
        mean_shortfall=sum(shortfalls) / len(shortfalls) if shortfalls else None,
        sigma_rate=stddev(shortfalls) if len(shortfalls) > 1 else None,
        under_stated_count=sum(1 for s in shortfalls if s > 0),
    )


def _quarter_summary(
    source: str,
    start: datetime,
    items: Sequence[ForecastAnchor],
    target_wh: float | None,
    guard_firings: int,
) -> QuarterSummary | None:
    """Roll one quarter's anchors up, or None if none carries a known total.

    Args:
        source: Log the anchors came from.
        start: Quarter start shared by ``items``.
        items: The quarter's scored anchors.
        target_wh: Last non-None target logged for this quarter, or None.
        guard_firings: Guard events attributed to this quarter.

    Returns:
        The roll-up, or None when no anchor has a known total.
    """
    actual = next((a.actual_wh for a in items if a.actual_wh is not None), None)
    errors = [e for e in (a.error for a in items) if e is not None]
    if actual is None or not errors:
        return None
    return QuarterSummary(
        source=source,
        quarter_start=start,
        anchor_count=len(errors),
        actual_wh=actual,
        max_abs_error=max(abs(e) for e in errors),
        mean_signed_error=sum(errors) / len(errors),
        target_wh=target_wh,
        guard_firings=guard_firings,
    )


def _jitter_context(
    cycles: Sequence[CycleRecord], guards: Sequence[GuardEvent]
) -> tuple[dict[_QuarterKey, float], dict[_QuarterKey, int], list[float], int]:
    """Join the logged decision records to quarters.

    Args:
        cycles: Parsed cycle records.
        guards: Parsed guard events.

    Returns:
        ``(targets, guard_counts, production_churns, excessive_count)``:
        the last non-None target seen per ``(source, quarter)`` (later
        cycles win; None targets never overwrite), guard firing counts
        per ``(source, quarter)``, every logged production churn value,
        and how many cycles reported reason ``excessive_jitter``.
    """
    targets: dict[_QuarterKey, float] = {}
    for record in cycles:
        if record.quarter_start is not None and record.target_wh is not None:
            targets[(record.source, record.quarter_start)] = float(record.target_wh)
    guard_counts: dict[_QuarterKey, int] = {}
    for event in guards:
        key = (event.source, event.quarter_start)
        guard_counts[key] = guard_counts.get(key, 0) + 1
    jitters = [
        record.gap_jitter_wh_per_s
        for record in cycles
        if record.gap_jitter_wh_per_s is not None
    ]
    excessive = sum(1 for record in cycles if record.reason == "excessive_jitter")
    return targets, guard_counts, jitters, excessive


def summarize(
    anchors: Iterable[ForecastAnchor],
    bucket_edges: Sequence[int] = BUCKET_EDGES,
    cycles: Sequence[CycleRecord] = (),
    guards: Sequence[GuardEvent] = (),
) -> ForecastReport:
    """Aggregate scored anchors into the report the plan's questions need.

    Args:
        anchors: Parsed anchors; those without a known quarter total are
            counted in ``dropped_count`` and excluded from statistics.
        bucket_edges: Ascending ``remaining_seconds`` boundaries.
        cycles: Parsed cycle records, supplying per-quarter targets and
            the production jitter counts (optional).
        guards: Parsed guard events, supplying per-quarter firing
            counts (optional).

    Returns:
        A report covering error magnitude, error sign, rate shortfall,
        the churn band, and per-quarter target/miss/guard context.
    """
    all_anchors = list(anchors)
    scored = [a for a in all_anchors if a.actual_wh is not None]
    churns_all = churn_by_anchor(all_anchors)
    churns = tuple(
        churn
        for churn, anchor in zip(churns_all, all_anchors)
        if anchor.actual_wh is not None
    )
    targets, guard_counts, jitters, excessive = _jitter_context(cycles, guards)
    guard_summary = GuardSummary(
        firing_count=len(guards),
        excessive_jitter_cycles=excessive,
        production_churn_mean=sum(jitters) / len(jitters) if jitters else None,
        production_churn_max=max(jitters) if jitters else None,
    )

    if not scored:
        return ForecastReport(
            anchor_count=0,
            quarter_count=0,
            dropped_count=len(all_anchors),
            mean_abs_error=0.0,
            max_abs_error=0.0,
            median_signed_error=0.0,
            optimistic_count=0,
            optimistic_fraction=0.0,
            buckets=(),
            quarters=(),
            horizons=(),
            errors=(),
            sigma_rate=0.0,
            churns=(),
            guard=guard_summary,
        )

    signed = [e for e in (a.error for a in scored) if e is not None]
    optimistic = sum(1 for e in signed if e < 0)
    shortfalls = [s for s in (a.rate_shortfall for a in scored) if s is not None]

    buckets = tuple(
        _bucket(
            [a for a in scored if low <= a.remaining_secs < high],
            low,
            high,
        )
        for low, high in zip(bucket_edges, bucket_edges[1:])
    )

    grouped: dict[tuple[str, datetime], list[ForecastAnchor]] = {}
    for anchor in scored:
        grouped.setdefault((anchor.source, anchor.quarter_start), []).append(anchor)
    quarters = tuple(
        summary
        for summary in (
            _quarter_summary(
                source,
                start,
                items,
                targets.get((source, start)),
                guard_counts.get((source, start), 0),
            )
            for (source, start), items in sorted(grouped.items(), key=lambda kv: kv[0][1])
        )
        if summary is not None
    )

    return ForecastReport(
        anchor_count=len(scored),
        quarter_count=len(grouped),
        dropped_count=len(all_anchors) - len(scored),
        mean_abs_error=sum(abs(e) for e in signed) / len(signed),
        max_abs_error=max(abs(e) for e in signed),
        median_signed_error=percentile(signed, 0.5),
        optimistic_count=optimistic,
        optimistic_fraction=optimistic / len(signed),
        buckets=buckets,
        quarters=quarters,
        horizons=tuple(a.remaining_secs for a in scored),
        errors=tuple(signed),
        sigma_rate=stddev(shortfalls) if len(shortfalls) > 1 else 0.0,
        churns=churns,
        guard=guard_summary,
    )


def format_report(report: ForecastReport) -> str:
    """Render a report as plain text tables."""
    if report.anchor_count == 0:
        return "no scored anchors: no quarter's completed total was found in the logs"

    lines = [
        f"scored anchors: {report.anchor_count} across {report.quarter_count} quarters"
        f"  (dropped {report.dropped_count} with no completed total)",
        f"error: mean |e| {report.mean_abs_error:.2f} Wh, "
        f"max |e| {report.max_abs_error:.2f} Wh, median signed {report.median_signed_error:+.2f} Wh",
        f"optimistic (promised more export than arrived): "
        f"{report.optimistic_count}/{report.anchor_count} "
        f"({report.optimistic_fraction:.0%})",
        f"rate-error sigma_rate: {report.sigma_rate:.4f} Wh/s"
        f"  ->  error == -R * rate_error, so a band U(R) = k * sigma * R is complete",
        "band coverage of realized errors: "
        + "  ".join(f"k={k:g} -> {report.coverage(float(k)):.0%}" for k in (1, 2, 3)),
        "churn band coverage (|e| <= k * churn * R): "
        + "  ".join(f"k={k:g} -> {report.churn_coverage(float(k)):.0%}" for k in (1, 2, 3))
        + f"  (swing measurable: {report.measurable_churn_count}/{report.anchor_count})",
        "jitter guard: "
        f"{report.guard.firing_count} guard event(s), "
        f"{report.guard.excessive_jitter_cycles} excessive_jitter cycle(s)"
        + (
            f"; production churn mean {report.guard.production_churn_mean:.2f}"
            f" (max {report.guard.production_churn_max:.2f}) Wh/s"
            if report.guard.production_churn_mean is not None
            else ""
        ),
        "",
        f"{'R bucket (s)':>14} {'n':>4} {'mean|e|':>9} {'p80|e|':>8} "
        f"{'mean signed':>12} {'mean shortfall':>15} {'sigma_rate':>11} {'under-stated':>13}",
    ]
    for bucket in report.buckets:
        if bucket.count == 0:
            continue
        shortfall = (
            f"{bucket.mean_shortfall:+.4f}" if bucket.mean_shortfall is not None else "n/a"
        )
        sigma = f"{bucket.sigma_rate:.4f}" if bucket.sigma_rate is not None else "n/a"
        lines.append(
            f"{bucket.label:>14} {bucket.count:>4} {bucket.mean_abs_error:>9.2f} "
            f"{bucket.p80_abs_error:>8.2f} {bucket.mean_signed_error:>+12.2f} "
            f"{shortfall:>15} {sigma:>11} {bucket.under_stated_count:>8}/{bucket.count:<4}"
        )

    lines += [
        "",
        f"{'quarter':<22} {'anchors':>8} {'actual':>9} {'target':>7} {'miss':>8} "
        f"{'max|e|':>8} {'mean signed':>12} {'guard':>6}",
    ]
    for quarter in report.quarters:
        target = f"{quarter.target_wh:.1f}" if quarter.target_wh is not None else "n/a"
        miss = f"{quarter.miss_wh:+.2f}" if quarter.miss_wh is not None else "n/a"
        lines.append(
            f"{quarter.quarter_start.strftime('%Y-%m-%d %H:%M'):<22} {quarter.anchor_count:>8} "
            f"{quarter.actual_wh:>9.2f} {target:>7} {miss:>8} {quarter.max_abs_error:>8.2f} "
            f"{quarter.mean_signed_error:>+12.2f} {quarter.guard_firings:>6}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the forecast-error report for the given log paths.

    Args:
        argv: Log file paths. Defaults to every ``*.log`` under ``bugs/``.

    Returns:
        Process exit status: 0 on success, 1 if a log cannot be read.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    paths = [Path(a) for a in args] if args else sorted((Path("bugs")).glob("*.log"))
    anchors: list[ForecastAnchor] = []
    cycles: list[CycleRecord] = []
    guards: list[GuardEvent] = []
    for path in paths:
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            print(f"skipping {path}: {exc}", file=sys.stderr)
            continue
        anchors.extend(parse_log(text, source=path.name))
        cycles.extend(parse_cycles(text, source=path.name))
        guards.extend(parse_guard_events(text, source=path.name))
    report = summarize(anchors, cycles=cycles, guards=guards)
    print(f"scanned {len(paths)} log(s)\n")
    print(format_report(report))
    return 0 if report.anchor_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
