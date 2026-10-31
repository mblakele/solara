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
target, reason, historical churn) and the INFO ``gapminder_jitter_guard``
events in old logs — and reports them alongside a churn band (``k * churn
* R``) as a second uncertainty model next to the sigma band. Production
removed the churn EWMA on 2026-10-21, so the tool rebuilds it locally from
the anchor series. Logged decisions are reported, never re-executed.
Offline churn uses raw ``predicted_wh`` (no pending-effect adjustment), so
quarters containing actions make it a slight approximation of the value
the guard consumed.

Everything here is read-only analysis over log text; no production module
imports this. The forecast itself (``util.compute_nbc_quarter``) is
deliberately left unchanged: see
``.opencode/plans/forecast-uncertainty-action-band.md`` §4 for the candidates
already evaluated and rejected.

``--estimator`` additionally *scores* alternative forecast rates over the
same anchors — the shipped trailing window, the prior completed quarter's
rate, blends of the two, and a trend-gated mix — so an estimator change can
be judged from the corpus before any production edit
(``.opencode/plans/forecast-variance-reduction.md``). The default output is
unchanged.

``--turn-offs`` adds the turn-off path collection: every entry into the
unguarded ``GapMinder._decide_turn_off`` (horizon, gap, each shed it booked,
whether the gap was covered, whether a shed overshot it, and whether a plug
was turned back on in the same quarter) beside the quarter it produced.
That path has no minimum-horizon floor and no overshoot cap by design, so
the data is collected before any guard is considered. The same section ends
with a hedge attribution: every shed bucketed by its quarter's rate sigma,
its quarter's production churn, and its own pre-shed gap, with the overshoot
and reversal rate per bucket. The two quarter signals are retrospective (the
whole quarter's anchors), the gap is the only one a decision had at the
shed. Also opt-in; the default output is unchanged.

``--guards`` adds the historical suppression census: every mechanism that
only *declines* work is invisible to a closed loop that watches actions, so
this section counts what each one withheld in the OLD logs and how reachable
its trigger was — jitter-guard declines (and the loads that were on offer in
the same cycle), banked-cover rescues, stop deferrals with their horizon
distribution, the ramp rule's arithmetical reach, and the committed settle
window. The jitter guard and banked-cover override were removed from
production on 2026-10-21. A guarded quarter is reported beside an unguarded
one with the confound stated: the guard fired *in* the churning-forecast
quarters, so the split is a correlation, never the guard's effect.

Run from the repo root::

    uv run python tools/forecast_log_scoring.py bugs/*.log
    uv run python tools/forecast_log_scoring.py bugs/*.log --estimator
    uv run python tools/forecast_log_scoring.py bugs/*.log --turn-offs
    uv run python tools/forecast_log_scoring.py bugs/*.log --guards
"""

from __future__ import annotations

import math
import re
import statistics
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence, TypeVar

import pytz


# Solara is a flat-layout project: production modules sit beside this one,
# not inside a package. Running this file directly puts tools/ (not the repo
# root) on sys.path, so the root has to be added back before importing util.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Flat-layout script: every local import must follow the _ROOT sys.path
# bootstrap above, so they deliberately sit below module-level code (flake8
# E402 and pylint wrong-import-position are the same complaint).
from constants import (  # noqa: E402 # pylint: disable=wrong-import-position
    DEFAULT_HYSTERESIS_WH,
    GAP_TREND_EWMA_ALPHA,
    GAP_TREND_MAX_SPAN_SECS,
    GAP_TREND_MIN_SLOPES,
    GAP_TREND_WINDOW,
    TESLA_CHARGE_AMPS_MIN_DEFAULT,
    TESLA_NOMINAL_VOLTAGE,
)
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
    wall_at: datetime | None = None
    prior_rate: float | None = None
    """Median realized rate (Wh/s) of the completed quarters the same fetch
    reports beside this incomplete one; None when it reports none.

    This is information production already holds at decision time — the
    completed ``qh2..qh4`` entries come from the same payload as the
    incomplete QH1 — and it is the alternative to the trailing window the
    estimator sweep scores (``.opencode/plans/forecast-variance-reduction.md``).
    """

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

    def residual_rate(self, on_watts: float) -> float:
        """Trailing rate net of the controllable draw (Wh/s).

        Subtracting what load management could remove leaves the
        weather-plus-house rate the residual margin has to cover.
        """
        return self.prediction_w - on_watts / 3600.0

    def residual_predicted(self, on_watts: float) -> float:
        """Counterfactual quarter total with the ON loads shed."""
        return self.raw_wh + self.residual_rate(on_watts) * self.remaining_secs

    def residual_error(self, on_watts: float) -> float | None:
        """Signed residual error (None without a known quarter total)."""
        if self.actual_wh is None:
            return None
        return self.residual_predicted(on_watts) - self.actual_wh

    def residual_shortfall(self, on_watts: float) -> float | None:
        """How far the residual rate under-stated the forward rate."""
        fwd = self.true_forward_rate
        if fwd is None:
            return None
        return fwd - self.residual_rate(on_watts)


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
    pending: tuple[int, list[_Quarter], datetime | None] | None = None

    for line in text.splitlines():
        if "create_metrics result:" in line:
            if pending is not None:
                data_start = _parse_data_start(line)
                total = _DATA_TOTAL_RE.search(line)
                held_len, held_quarters, held_wall = pending
                if data_start is not None and total is not None:
                    if int(total.group(1)) == held_len:
                        _collect(
                            data_start, held_len, held_quarters,
                            source, anchors, actuals, wall_at=held_wall,
                        )
            pending = None
            continue
        if "nbc_set" not in line:
            continue
        parsed_len, parsed_quarters = _parse_quarters(line)
        if parsed_len is None:
            continue
        pending = (parsed_len, parsed_quarters, _line_timestamp(line))

    return [
        anchor if anchor.quarter_start not in actuals
        else ForecastAnchor(**{**anchor.__dict__, "actual_wh": actuals[anchor.quarter_start]})
        for anchor in anchors
    ]


def _to_anchor(
    quarter: _Quarter, start: datetime, source: str,
    wall_at: datetime | None = None,
    prior_rate: float | None = None,
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
        wall_at=wall_at,
        prior_rate=prior_rate,
    )


def _prior_rate(quarters: Sequence[_Quarter]) -> float | None:
    """Median realized rate of a record's completed neighbouring quarters.

    The incomplete QH1 is index 1 and is skipped: its total is the *current*
    quarter's outcome, not a prior one. Completed entries with no ``raw_wh``
    (the API sometimes reports the flag without the total) contribute
    nothing rather than a zero.

    Args:
        quarters: The quarters reported by one ``nbc_set`` record.

    Returns:
        The median rate in Wh/s, or None when no completed total is present.
    """
    rates = [
        quarter.raw_wh / QH_PERIOD_SECONDS
        for quarter in quarters
        if quarter.complete and quarter.index != 1 and quarter.raw_wh is not None
    ]
    return statistics.median(rates) if rates else None


def _collect(
    data_start: datetime,
    cache_len: int,
    quarters: list[_Quarter],
    source: str,
    anchors: list[ForecastAnchor],
    actuals: dict[datetime, float],
    wall_at: datetime | None = None,
) -> None:
    """File one parsed ``nbc_set`` record under its resolved data_start."""
    prior_rate = _prior_rate(quarters)
    for quarter in quarters:
        start = _quarter_start(data_start, cache_len, quarter.index)
        if quarter.complete:
            if quarter.raw_wh is not None:
                actuals[start] = quarter.raw_wh
            continue
        anchor = _to_anchor(
            quarter, start, source, wall_at=wall_at, prior_rate=prior_rate
        )
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


@dataclass(frozen=True)
class CycleStateSnapshot:
    """The dispatch-relevant state one cycle-result line reports.

    :func:`parse_cycles` reads a cycle-result repr only for its decision
    context. The replay needs the other half of the same repr: whether
    each plug candidate is *actually* running (the meter it integrates is
    physical, not desired), and whether the Tesla could be drawing (a
    quarter the car charged in is outside a plugs-only meter's scope).

    Attributes:
        source: Log the line came from.
        at: The line's prefix timestamp in UTC, or None when absent.
        quarter_start: Quarter of the diagnostics ``data_point_at``, or
            None when it cannot be parsed.
        plugs: ``(name, desired, actual)`` per plug candidate, in log
            order. Tesla candidates carry no plug state and are omitted.
        tesla_charging: Whether the cycle reported the Tesla charging, or
            None when the diagnostics logged no Tesla state at all (the
            cycle exited before the async phase — unknown, never "idle").
        tesla_amps: The Tesla's reported amps, or None when unknown.
        tesla_plugged_in: Whether the car was plugged in, or None when no
            state was logged. The replay gates every command on it, so a
            counterfactual that dropped it could charge an absent car.
        tesla_at_home: Whether the car was at home, or None when no state
            was logged. Production gates on it when a home location is
            configured.
    """

    source: str
    at: datetime | None
    quarter_start: datetime | None
    plugs: tuple[tuple[str, bool | None, bool | None], ...]
    tesla_charging: bool | None = None
    tesla_amps: float | None = None
    tesla_plugged_in: bool | None = None
    tesla_at_home: bool | None = None


def _flag(value: str) -> bool | None:
    """``"True"``/``"False"``/``"None"`` as a tri-state bool."""
    if value == "None":
        return None
    return value == "True"


def parse_cycle_states(text: str, source: str = "<log>") -> list[CycleStateSnapshot]:
    """Extract per-cycle plug and Tesla state from one log's text.

    Reads the same ``Load management cycle result: CycleResult(...)`` reprs
    as :func:`parse_cycles`, scoped to the
    ``diagnostics=CycleDiagnostics(...)`` segment so the pending effects'
    own ``data_point_at`` and the candidates' own Tesla fields cannot leak
    in. ``tesla_state=None`` (Tesla still configured, no state read — so
    the cycle either exited before the async phase or found the car
    unavailable) is reported as unknown rather than defaulted to idle.

    Args:
        text: Full log contents.
        source: Identifier recorded on each snapshot (usually the filename).

    Returns:
        Snapshots in log order. Lines without a diagnostics segment are
        skipped.
    """
    snapshots: list[CycleStateSnapshot] = []
    for line in text.splitlines():
        if "CycleResult(" not in line:
            continue
        diags = line.find(_CYCLE_DIAGS)
        if diags < 0:
            continue
        segment = line[diags:]
        tesla_chunk = ""
        marker = segment.find(_TESLA_STATE_MARKER)
        if marker >= 0:
            tesla_chunk = segment[marker:]
        charging = _TESLA_CHARGING_RE.search(tesla_chunk)
        amps = _TESLA_AMPS_RE.search(tesla_chunk)
        plugged = _TESLA_PLUGGED_RE.search(tesla_chunk)
        at_home = _TESLA_AT_HOME_RE.search(tesla_chunk)
        snapshots.append(
            CycleStateSnapshot(
                source=source,
                at=_line_timestamp(line),
                quarter_start=_cycle_quarter(segment),
                plugs=tuple(
                    (name, _flag(desired), _flag(actual))
                    for name, desired, actual in _CANDIDATE_STATE_RE.findall(segment)
                ),
                tesla_charging=(
                    None if charging is None else charging.group(1) == "True"
                ),
                tesla_amps=None if amps is None else float(amps.group(1)),
                tesla_plugged_in=_flag(plugged.group(1)) if plugged else None,
                tesla_at_home=_flag(at_home.group(1)) if at_home else None,
            )
        )
    return snapshots


# The same candidate entry, extended to the trailing ``actual_state``: the
# replay models the meter from what the controller reported, which differs
# from the wanted state while a command is unconfirmed. Tesla entries are
# dropped here — they carry no plug state.
_CANDIDATE_STATE_RE = re.compile(
    r"CandidateDetail\(device_type='plug', name='([^']*)', "
    r"power_watts=[\d.eE+-]+,.*?"
    r"desired_state=(True|False|None), actual_state=(True|False|None)"
)

# ``tesla_state={'is_charging': False, 'current_amps': 0, ...}``. The quoted
# keys are what separate the dict from a tesla CandidateDetail's own
# ``is_charging=None`` fields, so no dict-boundary parse is needed.
_TESLA_STATE_MARKER = "tesla_state={"
_TESLA_CHARGING_RE = re.compile(r"'is_charging':\s*(True|False)")
_TESLA_AMPS_RE = re.compile(r"'current_amps':\s*(-?\d+(?:\.\d+)?)")
_TESLA_PLUGGED_RE = re.compile(r"'plugged_in':\s*(True|False|None)")
_TESLA_AT_HOME_RE = re.compile(r"'at_home':\s*(True|False|None)")


# Matches one candidate entry in production field order, e.g.
# ``CandidateDetail(device_type='plug', name='jackery', power_watts=260.0,
# ... desired_state=True, ...)``. Tesla entries carry power_watts=None
# and are skipped by the caller, not the pattern.
_CANDIDATE_RE = re.compile(
    r"CandidateDetail\(device_type='(plug|tesla)', name='([^']*)', "
    r"power_watts=([\d.eE+-]+|None),.*?desired_state=(True|False|None)"
)


def _iter_candidate_entries(line: str) -> Iterator[tuple[str, str, str, str]]:
    """Yield ``(device_type, name, power, desired)`` per candidate entry.

    Shared by the ON-watts timeline and the plug-state parser so the two
    never disagree about field order or the repeated-list shape.
    """
    yield from _CANDIDATE_RE.findall(line)


def _iter_result_lines(text: str) -> Iterator[tuple[datetime, str]]:
    """Yield ``(wall_time, line)`` per timestamped cycle-result line.

    Shared scan preamble for every parser reading ``CycleResult`` reprs;
    lines without a parseable timestamp are skipped rather than guessed.
    """
    for line in text.splitlines():
        if "CycleResult(" not in line:
            continue
        stamp = _line_timestamp(line)
        if stamp is None:
            continue
        yield stamp, line


def parse_on_watts_timeline(
    text: str, source: str = "<log>"
) -> list[tuple[datetime, float]]:
    """Extract per-cycle controllable watts from cycle-result reprs.

    Each ``CycleResult`` line's candidates report every plug's rated watts
    and desired state; summing the wanted-on plugs gives the controllable
    draw production saw embedded in the trailing rate at that wall time.

    Args:
        text: Full log contents.
        source: Identifier (unused beyond symmetry with the other parsers).

    Returns:
        ``(wall_time, on_watts)`` in log order; lines without a parseable
        timestamp are skipped rather than guessed.
    """
    _ = source
    timeline: list[tuple[datetime, float]] = []
    for stamp, line in _iter_result_lines(text):
        # The repr carries the candidate list twice (top level and the
        # diagnostics segment); first occurrence wins so each plug counts
        # once no matter how many segments repeat it.
        seen: set[str] = set()
        watts = 0.0
        for kind, name, power, desired in _iter_candidate_entries(line):
            if kind != "plug" or power == "None" or name in seen:
                continue
            if desired != "True":
                continue
            seen.add(name)
            watts += float(power)
        timeline.append((stamp, watts))
    return timeline


_T = TypeVar("_T")
"""Value type for the shared prior-cycle join."""


def _prior_values(
    walls: Sequence[datetime | None],
    timeline: Sequence[tuple[datetime, _T]],
) -> list[_T | None]:
    """Latest timeline value at or before each wall time.

    Shared two-pointer walk behind the ON-watts and plug-state joins: a
    forecast only knows the cycle result already logged, never the one
    still computing. Walls predating every entry yield None (excluded
    downstream, never zero-filled).
    """
    ordered = sorted(timeline, key=lambda entry: entry[0])
    values: list[_T | None] = []
    cursor = 0
    current: _T | None = None
    for wall in walls:
        if wall is None:
            values.append(None)
            continue
        while cursor < len(ordered) and ordered[cursor][0] <= wall:
            current = ordered[cursor][1]
            cursor += 1
        values.append(current)
    return values


def assign_on_watts(
    anchors: Sequence[ForecastAnchor],
    timeline: Sequence[tuple[datetime, float]],
) -> list[float | None]:
    """Attribute each anchor the latest cycle ON-watts at or before it.

    A forecast can only be netted by what production knew when it was
    fresh; the cycle result lags its fetch by ~30 s (Telegram sync), so the
    prior cycle's ON set is the contemporaneous one, not a leak from the
    future.

    Args:
        anchors: Anchors in log order (their ``wall_at`` stamps).
        timeline: ``(wall_time, on_watts)`` in ascending order.

    Returns:
        One ON-watts value per anchor, aligned by index, or None for
        anchors predating every cycle line (excluded, never zero-filled).
    """
    return _prior_values(
        [anchor.wall_at for anchor in anchors], list(timeline)
    )


# ── reporting ────────────────────────────────────────────────────────

BUCKET_EDGES: tuple[int, ...] = (0, 60, 120, 180, 240, 360, 901)
"""``remaining_seconds`` bucket boundaries for the error report.

Zero ``remaining_seconds`` is the quarter boundary itself, where no decision
could be made; the buckets above it are the ranges the load manager actually
acts in.
"""

PLUG_ACTION_WATTS = 260.0
"""Rated draw of the small dispatchable plug the survival check is sized on.

A 260 W action moves the quarter at ``260/3600 Wh/s``; comparing that rate
against the residual sigma answers the band plan's killer question in the
residual currency. Both sides scale with R, so survival is R-independent.
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
    """Historical churn estimate for each anchor, in input order.

    Production removed the turn-on guard's churn EWMA on 2026-10-21, but
    this offline tool still scores the old logs. It therefore rebuilds the
    same series locally: one sample per anchor in log order (repeated data
    points are rejected), reset on quarter-hour rollover and on a data gap
    over the tracker's max span, then an EWMA of ``|Δslope|`` once two
    slopes exist. Raw ``predicted_wh`` stands in for the production gap
    ``target - adjusted``: the unknown target is constant within the
    quarter, and both a constant offset and a sign flip leave slopes - and
    therefore the EWMA of ``|Δslope|`` - unchanged. The pending-effect
    adjustment is unavailable offline, so anchors recorded after an action
    make this a slight approximation of the value the guard consumed.

    Args:
        anchors: Anchors for the log; per-quarter input order (log
            order) is preserved.

    Returns:
        One churn value (Wh/s) per anchor, aligned by index. 0.0 until
        an anchor has two slopes behind it, after a quarter rollover,
        or after a data gap over the tracker's max span.
    """
    churns = [0.0] * len(anchors)
    windows: dict[_QuarterKey, deque[tuple[datetime, float]]] = {}
    qh_starts: dict[_QuarterKey, datetime] = {}
    accumulators: dict[_QuarterKey, float | None] = {}
    for index, anchor in enumerate(anchors):
        key = (anchor.source, anchor.quarter_start)
        window = windows.setdefault(
            key, deque(maxlen=GAP_TREND_WINDOW)
        )
        qh_start = floor_to_qh(anchor.data_point_at)
        if key in qh_starts and qh_start != qh_starts[key]:
            window.clear()
            accumulators[key] = None
        qh_starts[key] = qh_start

        if window:
            prev_ts = window[-1][0]
            if anchor.data_point_at <= prev_ts:
                churns[index] = accumulators.get(key) or 0.0
                continue
            span = (anchor.data_point_at - prev_ts).total_seconds()
            if span > GAP_TREND_MAX_SPAN_SECS:
                window.clear()
                accumulators[key] = None
        window.append((anchor.data_point_at, anchor.predicted_wh))

        if len(window) < GAP_TREND_MIN_SLOPES + 1:
            churns[index] = accumulators.get(key) or 0.0
            continue
        recent = list(window)[-(GAP_TREND_MIN_SLOPES + 1):]
        slopes = [
            (cur_gap - prev_gap) / (cur_ts - prev_ts).total_seconds()
            for (prev_ts, prev_gap), (cur_ts, cur_gap) in zip(recent, recent[1:])
        ]
        pair_churn = abs(slopes[-1] - slopes[-2])
        prior = accumulators.get(key)
        next_churn = (
            pair_churn
            if prior is None
            else GAP_TREND_EWMA_ALPHA * pair_churn
            + (1.0 - GAP_TREND_EWMA_ALPHA) * prior
        )
        accumulators[key] = next_churn
        churns[index] = next_churn
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


# ── estimator variants ───────────────────────────────────────────────
#
# The forecast production ships (``util.compute_nbc_quarter``) is
# deliberately left unchanged here. These variants only *score*
# alternatives over the same anchors, so the question "is the trailing
# window the noise source?" can be answered from the corpus before any
# production change is considered
# (``.opencode/plans/forecast-variance-reduction.md``, subtask 1).
# Every variant predicts the same quantity as the shipped forecast —
# ``raw_wh + remaining_secs * rate`` — and differs only in the rate.

DEFAULT_PRIOR_WEIGHTS_SECS: tuple[int, ...] = (60, 120, 300)
"""Blend weights, in seconds of prior-quarter equivalent.

The blend is ``(n * window_rate + m * prior_rate) / (n + m)`` with
``m`` in these units: ``m = n`` (30) is an even split, ``m = 300`` leaves
the trailing window ~9% of the weight.
"""

TREND_GATE_NOISE_FLOOR_WH: float = float(DEFAULT_HYSTERESIS_WH)
"""Minimum gap slope (Wh) the trend gate trusts, matching production's method.

Production passes ``engine.HYSTERESIS_WH / seconds_remaining`` to
``GapTrendTracker.update`` (``load_manager._stage_compute_gap``), and
``engine.HYSTERESIS_WH`` is the deployment's ``|target_wh| / 3`` — 3 Wh
for this corpus, not the 20 Wh residential default. Override with
``--trend-floor`` when the corpus was run at another scale.
"""

ESTIMATOR_NAMES: tuple[str, ...] = (
    "status-quo",
    "prior",
) + tuple(
    f"prior-blend@{weight}" for weight in DEFAULT_PRIOR_WEIGHTS_SECS
) + (
    "trend-gated",
)
"""Every scorable variant, in report order."""


def estimate_prior_only(anchor: ForecastAnchor) -> float:
    """Predict the quarter total from the prior quarters' rate alone.

    Args:
        anchor: The forecast anchor.

    Returns:
        ``raw_wh + remaining_secs * prior_rate``, or the shipped forecast
        when the fetch reported no completed neighbour (so every variant
        scores an identical anchor set).
    """
    if anchor.prior_rate is None:
        return anchor.predicted_wh
    return anchor.raw_wh + anchor.remaining_secs * anchor.prior_rate


def estimate_prior_blend(anchor: ForecastAnchor, prior_weight_secs: float) -> float:
    """Predict the quarter total from a weighted window/prior rate.

    Args:
        anchor: The forecast anchor.
        prior_weight_secs: Weight given to the prior rate, in seconds of
            window-equivalent samples (``m`` in the formula).

    Returns:
        ``raw_wh + remaining_secs * rate`` with
        ``rate = (n * prediction_w + m * prior_rate) / (n + m)`` and
        ``n = prediction_values``. Falls back to the shipped forecast
        without a prior rate, or when the weights degenerate.
    """
    if anchor.prior_rate is None:
        return anchor.predicted_wh
    samples = float(anchor.prediction_values)
    weight = float(prior_weight_secs)
    if samples + weight <= 0.0:
        return estimate_prior_only(anchor)
    rate = (samples * anchor.prediction_w + weight * anchor.prior_rate) / (
        samples + weight
    )
    return anchor.raw_wh + anchor.remaining_secs * rate


def _blend_weight(name: str) -> float:
    """Parse the weight out of ``prior-blend@<secs>``."""
    return float(name.split("@", 1)[1])


def _trend_gated_series(
    anchors: Sequence[ForecastAnchor], noise_floor_wh: float
) -> list[float]:
    """Keep the trailing window only while ``GapTrendTracker`` trusts a trend.

    Mirrors ``_stage_compute_gap``: one ``update`` per anchor in log order,
    keyed by quarter, with ``predicted_wh`` standing in for the gap (a
    constant offset and a sign flip both leave slopes unchanged) and the
    trust floor ``noise_floor_wh / remaining_secs``. Where the trend is
    trusted the responsive window is the better estimator (a genuine
    regime change); everywhere else the prior quarter's rate is.

    Args:
        anchors: Anchors in log order.
        noise_floor_wh: Trust floor in Wh, divided by ``remaining_secs``.

    Returns:
        One predicted total per anchor, aligned by index.
    """
    values: list[float] = []
    trackers: dict[_QuarterKey, GapTrendTracker] = {}
    for anchor in anchors:
        key = (anchor.source, anchor.quarter_start)
        tracker = trackers.get(key)
        if tracker is None:
            tracker = GapTrendTracker()
            trackers[key] = tracker
        floor = (
            noise_floor_wh / anchor.remaining_secs if anchor.remaining_secs else 0.0
        )
        _, trusted = tracker.update(
            anchor.data_point_at, anchor.predicted_wh, noise_floor=floor
        )
        values.append(anchor.predicted_wh if trusted else estimate_prior_only(anchor))
    return values


def estimate_series(
    anchors: Sequence[ForecastAnchor],
    name: str,
    *,
    trend_floor_wh: float = TREND_GATE_NOISE_FLOOR_WH,
) -> list[float]:
    """Predict every anchor with one named variant.

    Args:
        anchors: Anchors in log order (scored or not; the trend-gated
            variant needs the full series to build its history).
        name: A variant from :data:`ESTIMATOR_NAMES`.
        trend_floor_wh: Trust floor for the trend gate.

    Returns:
        One predicted quarter total per anchor, aligned by index.

    Raises:
        ValueError: If ``name`` is not a registered variant.
    """
    if name == "status-quo":
        return [anchor.predicted_wh for anchor in anchors]
    if name == "prior":
        return [estimate_prior_only(anchor) for anchor in anchors]
    if name.startswith("prior-blend@"):
        weight = _blend_weight(name)
        return [estimate_prior_blend(anchor, weight) for anchor in anchors]
    if name == "trend-gated":
        return _trend_gated_series(anchors, trend_floor_wh)
    raise ValueError(f"unknown estimator variant: {name}")


@dataclass(frozen=True)
class EstimatorBucket:
    """One variant's rate-error spread inside a ``remaining_seconds`` range."""

    low_secs: int
    high_secs: int
    count: int
    sigma_rate: float | None

    @property
    def label(self) -> str:
        """Human-readable range, e.g. ``120-179``."""
        return f"{self.low_secs}-{self.high_secs - 1}"


@dataclass(frozen=True)
class EstimatorSummary:
    """One estimator variant scored over the anchors that have a total.

    ``sigma_rate`` is the spread of ``error / remaining_secs`` — the same
    statistic the main report uses, and the one a band ``k * sigma * R``
    consumes, so a variant's number is directly comparable with
    ``ForecastReport.sigma_rate``.
    """

    name: str
    count: int
    mean_abs_error: float
    p80_abs_error: float
    max_abs_error: float
    optimistic_count: int
    optimistic_fraction: float
    sigma_rate: float
    buckets: tuple[EstimatorBucket, ...]


def _estimator_bucket(
    errors: Sequence[float], horizons: Sequence[int], low: int, high: int
) -> EstimatorBucket:
    """Rate-error spread for the anchors whose horizon falls in ``[low, high)``."""
    rates = [
        error / horizon
        for error, horizon in zip(errors, horizons)
        if low <= horizon < high and horizon > 0
    ]
    return EstimatorBucket(
        low_secs=low,
        high_secs=high,
        count=sum(1 for horizon in horizons if low <= horizon < high),
        sigma_rate=stddev(rates) if len(rates) > 1 else None,
    )


def _estimator_summary(
    name: str,
    errors: Sequence[float],
    horizons: Sequence[int],
    bucket_edges: Sequence[int],
) -> EstimatorSummary:
    """Roll one variant's errors up into its report row."""
    if not errors:
        return EstimatorSummary(name, 0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, ())
    rate_errors = [
        error / horizon for error, horizon in zip(errors, horizons) if horizon > 0
    ]
    optimistic = sum(1 for error in errors if error < 0)
    return EstimatorSummary(
        name=name,
        count=len(errors),
        mean_abs_error=sum(abs(e) for e in errors) / len(errors),
        p80_abs_error=percentile([abs(e) for e in errors], 0.8),
        max_abs_error=max(abs(e) for e in errors),
        optimistic_count=optimistic,
        optimistic_fraction=optimistic / len(errors),
        sigma_rate=stddev(rate_errors) if len(rate_errors) > 1 else 0.0,
        buckets=tuple(
            _estimator_bucket(errors, horizons, low, high)
            for low, high in zip(bucket_edges, bucket_edges[1:])
        ),
    )


def score_estimators(
    anchors: Sequence[ForecastAnchor],
    names: Sequence[str] = ESTIMATOR_NAMES,
    trend_floor_wh: float = TREND_GATE_NOISE_FLOOR_WH,
    bucket_edges: Sequence[int] = BUCKET_EDGES,
) -> tuple[EstimatorSummary, ...]:
    """Score each named variant over the same set of scored anchors.

    Anchors without a known quarter total are excluded from every row, and
    variants missing a prior rate fall back to the shipped forecast, so
    the rows stay comparable anchor-for-anchor. The trend-gated variant
    still sees the full series (its tracker history must not lose the
    anchors at a log's tail).

    Args:
        anchors: Parsed anchors in log order.
        names: Variants to score.
        trend_floor_wh: Trust floor for the trend gate.
        bucket_edges: Ascending ``remaining_seconds`` boundaries.

    Returns:
        One summary per name, in the order given.
    """
    all_anchors = list(anchors)
    keep = [
        index
        for index, anchor in enumerate(all_anchors)
        if anchor.actual_wh is not None
    ]
    horizons = [all_anchors[index].remaining_secs for index in keep]
    summaries: list[EstimatorSummary] = []
    for name in names:
        series = estimate_series(all_anchors, name, trend_floor_wh=trend_floor_wh)
        errors: list[float] = []
        for index in keep:
            actual = all_anchors[index].actual_wh
            if actual is not None:
                errors.append(series[index] - actual)
        summaries.append(_estimator_summary(name, errors, horizons, bucket_edges))
    return tuple(summaries)


def format_estimators(
    summaries: Sequence[EstimatorSummary],
    trend_floor_wh: float = TREND_GATE_NOISE_FLOOR_WH,
) -> str:
    """Render the variant comparison as plain text tables.

    Args:
        summaries: Rows from :func:`score_estimators`.
        trend_floor_wh: Trust floor that produced the trend-gated row, so
            the number is reproducible from the output.

    Returns:
        The comparison table plus a per-``R``-bucket ``sigma_rate`` matrix,
        which is where the differences between variants show up.
    """
    if not summaries:
        return "no estimator variants requested"
    lines = [
        f"estimator variants ({summaries[0].count} scored anchors)",
        f"trend gate: trust floor {trend_floor_wh:.1f} Wh (--trend-floor)",
        f"{'variant':>18} {'n':>5} {'mean|e|':>9} {'p80|e|':>8} {'max|e|':>9} "
        f"{'optimistic':>11} {'sigma_rate':>11}",
    ]
    for summary in summaries:
        lines.append(
            f"{summary.name:>18} {summary.count:>5} {summary.mean_abs_error:>9.2f} "
            f"{summary.p80_abs_error:>8.2f} {summary.max_abs_error:>9.2f} "
            f"{summary.optimistic_fraction:>10.0%} {summary.sigma_rate:>11.4f}"
        )
    labels = [bucket.label for bucket in summaries[0].buckets]
    lines += [
        "",
        f"sigma_rate by R bucket (Wh/s): {' '.join(labels)}",
        f"{'variant':>18} " + " ".join(f"{label:>9}" for label in labels),
    ]
    for summary in summaries:
        cells = " ".join(
            f"{bucket.sigma_rate:>9.4f}" if bucket.sigma_rate is not None
            else f"{'n/a':>9}"
            for bucket in summary.buckets
        )
        lines.append(f"{summary.name:>18} {cells}")
    return "\n".join(lines)


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
class ResidualSummary:
    """Net-of-controllable error across the attributed anchors.

    Kept separate so the main report stays under the attribute budget;
    the residual margin question gets its own scope.
    """

    count: int = 0
    sigma_rate: float = 0.0
    errors: tuple[float, ...] = ()
    horizons: tuple[int, ...] = ()

    def coverage(self, k: float) -> float:
        """Fraction of residual anchors inside ``k * sigma * R``."""
        if not self.errors:
            return 0.0
        inside = sum(
            1
            for horizon, error in zip(self.horizons, self.errors)
            if abs(error) <= forecast_uncertainty_wh(horizon, self.sigma_rate, k)
        )
        return inside / len(self.errors)

    def plug_survival(self, k: float) -> float:
        """Share of residual anchors where a 260 W action beats the band.

        The counterpart of the band plan's killer question in the residual
        currency: the action survives when its quarter effect exceeds the
        residual band at the same horizon. Both sides scale with R, so the
        verdict is horizon-independent for a fixed sigma.
        """
        if not self.errors:
            return 0.0
        action_rate = PLUG_ACTION_WATTS / 3600.0
        survived = sum(
            1
            for horizon in self.horizons
            if action_rate * horizon > k * self.sigma_rate * horizon
        )
        return survived / len(self.errors)


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
    residual: ResidualSummary = field(default_factory=ResidualSummary)

    @property
    def residual_count(self) -> int:
        """Anchors carrying ON-watts attribution."""
        return self.residual.count

    @property
    def sigma_residual(self) -> float:
        """Spread of the residual rate error, in Wh/s."""
        return self.residual.sigma_rate

    def residual_coverage(self, k: float) -> float:
        """Fraction of residual anchors inside ``k * sigma_residual * R``.

        Args:
            k: Multiple of ``sigma_residual`` the band covers.

        Returns:
            Share inside the band, or 0.0 when there is nothing to score.
        """
        return self.residual.coverage(k)

    def plug_survival(self, k: float) -> float:
        """Share of residual anchors where a 260 W action beats the band.

        Args:
            k: Multiple of ``sigma_residual`` the band covers.

        Returns:
            Share surviving, or 0.0 when there is nothing to score.
        """
        return self.residual.plug_survival(k)

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


def _residual_summary(
    anchors: Sequence[ForecastAnchor],
    watts_by_anchor: Sequence[float | None],
) -> ResidualSummary:
    """Roll the attributed anchors up into the residual error series.

    Only scored anchors carrying ON-watts enter; the rest keep the base
    statistics untouched.

    Args:
        anchors: All parsed anchors in log order.
        watts_by_anchor: Controllable watts aligned with ``anchors``.

    Returns:
        The residual roll-up (empty when nothing is attributed).
    """
    pairs = [
        (anchor, watts)
        for anchor, watts in zip(anchors, watts_by_anchor)
        if anchor.actual_wh is not None and watts is not None
    ]
    errors = [
        e for e in (a.residual_error(w) for a, w in pairs) if e is not None
    ]
    shortfalls = [
        s for s in (a.residual_shortfall(w) for a, w in pairs) if s is not None
    ]
    return ResidualSummary(
        count=len(errors),
        sigma_rate=stddev(shortfalls) if len(shortfalls) > 1 else 0.0,
        errors=tuple(errors),
        horizons=tuple(a.remaining_secs for a, _ in pairs),
    )


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


def _quarter_tables(
    scored: list[ForecastAnchor],
    bucket_edges: Sequence[int],
    targets: dict[tuple[str, datetime], float],
    guard_counts: dict[tuple[str, datetime], int],
) -> tuple[tuple[ErrorBucket, ...], tuple[QuarterSummary, ...]]:
    """Bucket and quarter roll-ups for the scored anchors.

    Args:
        scored: Anchors with a known quarter total.
        bucket_edges: Ascending ``remaining_seconds`` boundaries.
        targets: Last logged target per ``(source, quarter)``.
        guard_counts: Guard firings per ``(source, quarter)``.

    Returns:
        ``(buckets, quarters)`` in report order.
    """
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
    return buckets, quarters


def summarize(
    anchors: Iterable[ForecastAnchor],
    bucket_edges: Sequence[int] = BUCKET_EDGES,
    cycles: Sequence[CycleRecord] = (),
    guards: Sequence[GuardEvent] = (),
    on_watts: Sequence[float | None] | None = None,
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
        on_watts: Per-anchor controllable watts from
            :func:`assign_on_watts` (aligned with ``anchors``); None
            entries are excluded from the residual series. Omit entirely
            for reports without ON-watts attribution.

    Returns:
        A report covering error magnitude, error sign, rate shortfall,
        the churn band, the residual series, and per-quarter
        target/miss/guard context.
    """
    all_anchors = list(anchors)
    if on_watts is not None and len(on_watts) != len(all_anchors):
        raise ValueError(
            f"on_watts has {len(on_watts)} entries for {len(all_anchors)} anchors"
        )
    watts_by_anchor = (
        list(on_watts) if on_watts is not None else [None] * len(all_anchors)
    )
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
    residual = _residual_summary(all_anchors, watts_by_anchor)
    buckets, quarters = _quarter_tables(scored, bucket_edges, targets, guard_counts)

    return ForecastReport(
        anchor_count=len(scored),
        quarter_count=len(quarters),
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
        residual=residual,
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
        (
            f"residual (net controllable): {report.residual_count} anchors, "
            f"sigma_residual={report.sigma_residual:.4f} Wh/s, coverage "
            + "  ".join(
                f"k={k:g} -> {report.residual_coverage(float(k)):.0%}" for k in (1, 2, 3)
            )
            + f", 260W survives k=1: {report.plug_survival(1.0):.0%}"
            if report.residual_count
            else "residual (net controllable): no ON-watts attribution"
        ),
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


# ── turn-off path collection ─────────────────────────────────────────
#
# The unguarded turn-off path (``GapMinder._decide_turn_off``) is a design
# decision, not an oversight: no minimum-horizon floor and no overshoot cap.
# Revisiting it needs data, and production already logs all of it — one
# entry line per decision (horizon + gap), one line per shed it books, and
# the plug turn-ons that later undo a shed. This section reads those lines.

TURN_OFF_SHORT_HORIZON_SECS: int = 60
"""A turn-off decided with less than this much quarter left counts as short.

Shedding that late books almost nothing (a 260 W plug is worth
``260 * R / 3600`` Wh, so 4.3 Wh at the floor) while still flipping
actuator state into the next quarter — the behavior a floor would
prevent, and the reason this number is collected.
"""

TURN_OFF_REVERSAL_WINDOW_SECS: int = 900
"""A turn-on within this long of a plug shed counts as undoing that shed.

One quarter-hour. Long enough to catch the boundary case — a shed with
four seconds left is undone by the next cycle, which is in the *following*
quarter — and short enough that a turn-on driven by genuinely new
conditions is not counted as churn.
"""

TURN_OFF_SIGMA_EDGES: tuple[float, ...] = (0.10, 0.20)
"""Rate-sigma edges (Wh/s) for the hedge-attribution buckets.

Two cuts, because the corpus's per-quarter sigma sits mostly below 0.20:
below 0.10 the trailing mean is steady, above 0.20 it is not.
"""

TURN_OFF_CHURN_EDGES: tuple[float, ...] = (0.10, 0.30)
"""Production-churn edges (Wh/s) for the hedge-attribution buckets."""

TURN_OFF_GAP_EDGES: tuple[float, ...] = (20.0, 60.0)
"""Pre-shed ``|gap|`` edges (Wh) for the hedge-attribution buckets.

A shed covers ``P * R / 3600`` Wh, so the corpus's median shed (~60 Wh)
is the top bucket: the path most often fires when the gap is large.
"""

_TURN_OFF_ENTRY_RE = re.compile(
    r"\[_decide_turn_off\] gap=(-?[\d.]+) Wh, seconds_remaining=(\d+)"
)
_TURN_OFF_PLUG_RE = re.compile(
    r"action=turn_off device=(.+?) savings=(-?[\d.]+) gap=(-?[\d.]+) priority=(\d+)"
)
_TURN_OFF_TESLA_AMPS_RE = re.compile(
    r"\[_decide_turn_off\] Tesla amps (\d+) → (\d+), savings=(-?[\d.]+) Wh, "
    r"remaining (-?[\d.]+) → (-?[\d.]+) Wh"
)
_TURN_OFF_TESLA_STOP_RE = re.compile(r"action=turn_off device=tesla reason=(\S+)")
_TURN_OFF_TESLA_STOP_REMAIN_RE = re.compile(r"reason=deficit remaining=(-?[\d.]+)")
_TURN_ON_PLUG_RE = re.compile(
    r"action=turn_on device=(.+?) capacity=(-?[\d.]+) gap=(-?[\d.]+) priority=(\d+)"
)


@dataclass(frozen=True)
class TurnOffAction:
    """One shed booked by the turn-off path, as production logged it.

    Attributes:
        device_name: Plug name, or ``"tesla"`` for either Tesla leg.
        kind: ``"plug"``, ``"tesla_amps"`` (partial amp reduction), or
            ``"tesla_stop"`` (charging stopped).
        savings_wh: Energy the shed booked for the rest of the quarter, or
            None for a stop (production logs no saving for one).
        remaining_gap_wh: The gap still to cover *before* this shed, or None
            when the line does not log it.
        current_amps: Amps before a Tesla amp reduction.
        target_amps: Amps after a Tesla amp reduction.
        priority: The plug's configured priority (shed order).
        reason: Why a Tesla stop fired (e.g. ``deficit``).
    """

    device_name: str
    kind: str
    savings_wh: float | None
    remaining_gap_wh: float | None
    current_amps: int | None = None
    target_amps: int | None = None
    priority: int | None = None
    reason: str | None = None

    @property
    def overshoot_wh(self) -> float | None:
        """Energy shed beyond what was still needed (Wh; >0 = overshoot).

        Returns:
            ``savings_wh - remaining_gap_wh``, or None when either is
            unlogged.
        """
        if self.savings_wh is None or self.remaining_gap_wh is None:
            return None
        return self.savings_wh - self.remaining_gap_wh


@dataclass(frozen=True)
class TurnOffDecision:
    """One entry into the turn-off path, plus every shed it booked."""

    source: str
    at: datetime
    gap_wh: float
    seconds_remaining: int
    actions: tuple[TurnOffAction, ...]

    @property
    def quarter_start(self) -> datetime:
        """Quarter the decision belonged to.

        Wall time is exact here, as for :class:`GuardEvent`: the pipeline's
        ``previous_qh`` check exits before ``decide()``, so a decision can
        only be taken while ``data_point_at`` lies in the current
        wall-clock quarter.
        """
        return floor_to_qh(self.at)

    @property
    def shed_wh(self) -> float:
        """Total energy the decision booked (Wh)."""
        return sum(a.savings_wh or 0.0 for a in self.actions)

    @property
    def residual_gap_wh(self) -> float:
        """Gap still uncovered after the decision (Wh; 0.0 = covered).

        Tesla stops log no saving, so a decision that stopped charging
        without an amp reduction reports the amp leg's remainder only.
        """
        return max(0.0, self.gap_wh - self.shed_wh)


@dataclass(frozen=True)
class TurnOnRecord:
    """One plug turn-on production decided, needed to spot a reversal."""

    at: datetime
    device_name: str
    capacity_wh: float


@dataclass(frozen=True)
class Reversal:
    """A plug shed that production turned back on soon afterwards."""

    source: str
    device_name: str
    quarter_start: datetime
    off_at: datetime
    on_at: datetime

    @property
    def delay_secs(self) -> float:
        """How long the plug stayed off before coming back (seconds)."""
        return (self.on_at - self.off_at).total_seconds()


@dataclass(frozen=True)
class TurnOffQuarter:
    """One quarter's turn-off activity beside the total it produced."""

    source: str
    quarter_start: datetime
    decision_count: int
    plug_off_count: int
    shed_wh: float
    min_remaining_secs: int
    reversal_count: int
    actual_wh: float | None
    target_wh: float | None

    @property
    def miss_wh(self) -> float | None:
        """``actual - target`` for the turn-off quarter, or None if unknown."""
        if self.actual_wh is None or self.target_wh is None:
            return None
        return self.actual_wh - self.target_wh


@dataclass(frozen=True)
class QuarterUncertainty:
    """The two forecast signals a turn-off decision had at its quarter.

    Attributes:
        sigma_rate: Population sigma of the quarter's scored rate
            shortfalls (Wh/s), or None when fewer than two anchors were
            scored.
        churn_wh_per_s: Median non-zero production churn in the quarter
            (Wh/s), or None when no anchor had two slopes behind it.
    """

    sigma_rate: float | None
    churn_wh_per_s: float | None


@dataclass(frozen=True)
class ShedBucket:
    """One hedge-attribution bucket: sheds sharing a signal range.

    The two denominators differ on purpose. ``saving_count`` is every shed
    that logged a saving (the plug and Tesla-amp legs), so it is the
    overshoot denominator; ``plug_count`` is the plug legs only, because a
    reversal needs a plug turn-on to undo the shed.
    """

    label: str
    shed_count: int
    saving_count: int
    overshoot_count: int
    plug_count: int
    reversal_count: int
    median_overshoot_wh: float | None

    @property
    def overshoot_fraction(self) -> float | None:
        """Share of the bucket's logged savings that overshot, if any."""
        if self.saving_count == 0:
            return None
        return self.overshoot_count / self.saving_count

    @property
    def reversal_fraction(self) -> float | None:
        """Share of the bucket's plug sheds that were undone, if any."""
        if self.plug_count == 0:
            return None
        return self.reversal_count / self.plug_count


@dataclass(frozen=True)
class HedgeAttribution:
    """Sheds bucketed by each uncertainty signal, for the B1 question.

    Every shed lands in exactly one gap bucket. A shed lands in a sigma or
    churn bucket only when its quarter carries that signal, so the three
    axes can total differently; ``unassigned_count`` says how many sheds
    had no quarter signal at all.
    """

    sigma: tuple[ShedBucket, ...]
    churn: tuple[ShedBucket, ...]
    gap: tuple[ShedBucket, ...]
    unassigned_count: int

    @property
    def shed_count(self) -> int:
        """Sheds bucketed on the gap axis (every shed the path booked)."""
        return sum(bucket.shed_count for bucket in self.gap)


@dataclass(frozen=True)
class TurnOffSummary:
    """What the corpus says about the unguarded turn-off path.

    The coverage pair is scoped to the decisions that could not cover their
    gap: ``uncovered_count`` counts those, and ``median_residual_wh`` is
    their median remainder (0.0 when every decision covered its gap), so a
    zero median reads as "the path kept up", not "the gap was tiny". The
    overshoot pair is scoped to the sheds that logged a saving — the
    plug/Tesla-amp legs, which the path books against the gap it still had.

    ``hedge`` is set only when the caller supplies the per-quarter
    uncertainty map; a summary built without one has no anchors to key the
    attribution on. See :class:`HedgeAttribution`.
    """

    decision_count: int
    quarter_count: int
    plug_off_count: int
    tesla_amp_count: int
    tesla_stop_count: int
    shed_total_wh: float
    median_shed_wh: float
    median_remaining_secs: float
    short_horizon_count: int
    short_horizon_secs: int
    uncovered_count: int
    median_residual_wh: float
    overshoot_count: int
    max_overshoot_wh: float
    reversals: tuple[Reversal, ...]
    quarters: tuple[TurnOffQuarter, ...]
    hedge: HedgeAttribution | None = None

    @property
    def action_count(self) -> int:
        """Sheds of every kind the path booked."""
        return self.plug_off_count + self.tesla_amp_count + self.tesla_stop_count

    @property
    def reversal_count(self) -> int:
        """Plug sheds a later same-quarter turn-on undid."""
        return len(self.reversals)

    @property
    def median_reversal_secs(self) -> float | None:
        """Median seconds between a shed and the turn-on that undid it."""
        if not self.reversals:
            return None
        return percentile([r.delay_secs for r in self.reversals], 0.5)


def _turn_off_action(line: str) -> TurnOffAction | None:
    """Parse one shed, or None when the line is not a turn-off action."""
    plug = _TURN_OFF_PLUG_RE.search(line)
    if plug is not None:
        return TurnOffAction(
            device_name=plug.group(1),
            kind="plug",
            savings_wh=float(plug.group(2)),
            remaining_gap_wh=float(plug.group(3)),
            priority=int(plug.group(4)),
        )
    amps = _TURN_OFF_TESLA_AMPS_RE.search(line)
    if amps is not None:
        # ``remaining before → after``; the before value is the budget the
        # shed drew on (the after value is before - savings, logged for
        # readers only).
        return TurnOffAction(
            device_name="tesla",
            kind="tesla_amps",
            savings_wh=float(amps.group(3)),
            remaining_gap_wh=float(amps.group(4)),
            current_amps=int(amps.group(1)),
            target_amps=int(amps.group(2)),
        )
    stop = _TURN_OFF_TESLA_STOP_RE.search(line)
    if stop is not None:
        remain = _TURN_OFF_TESLA_STOP_REMAIN_RE.search(line)
        return TurnOffAction(
            device_name="tesla",
            kind="tesla_stop",
            savings_wh=None,
            remaining_gap_wh=None if remain is None else float(remain.group(1)),
            reason=stop.group(1),
        )
    return None


def parse_turn_offs(text: str, source: str = "<log>") -> list[TurnOffDecision]:
    """Extract every entry into GapMinder's turn-off path.

    Production logs one ``[_decide_turn_off] gap=... seconds_remaining=...``
    line per decision, then one line per shed it books: an INFO
    ``action=turn_off device=<plug> savings=... gap=... priority=...``, a
    DEBUG Tesla ``amps A → B`` reduction, or an INFO Tesla stop. All of a
    cycle's lines share the line-prefix second, which is how the sheds are
    attached to their decision.

    Args:
        text: Full log contents.
        source: Identifier recorded on each decision.

    Returns:
        Decisions in log order. A shed whose decision line is missing
        (logs written before it shipped) is skipped rather than guessed
        into a quarter; a decision with no completed total still reports
        its own horizon and gap.
    """
    entries: list[tuple[datetime, float, int]] = []
    seen: set[datetime] = set()
    sheds: dict[datetime, list[TurnOffAction]] = {}
    for line in text.splitlines():
        stamp = _line_timestamp(line)
        if stamp is None:
            continue
        entry = _TURN_OFF_ENTRY_RE.search(line)
        if entry is not None:
            if stamp in seen:  # two cycles in one second: first sighting wins
                continue
            seen.add(stamp)
            entries.append((stamp, float(entry.group(1)), int(entry.group(2))))
            continue
        action = _turn_off_action(line)
        if action is not None:
            sheds.setdefault(stamp, []).append(action)
    return [
        TurnOffDecision(
            source=source,
            at=at,
            gap_wh=gap,
            seconds_remaining=remaining,
            actions=tuple(sheds.get(at, ())),
        )
        for at, gap, remaining in entries
    ]


def parse_turn_ons(text: str, source: str = "<log>") -> list[TurnOnRecord]:
    """Extract plug turn-ons, the signal that a shed was undone.

    Only decided turn-ons (``action=turn_on``) are collected: a plug that
    merely reports ON after a dashboard reconciliation is not production
    reversing its own shed.

    Args:
        text: Full log contents.
        source: Identifier (unused beyond symmetry with the other parsers).

    Returns:
        Turn-ons in log order; lines without a parseable timestamp are
        skipped rather than guessed.
    """
    _ = source
    records: list[TurnOnRecord] = []
    for line in text.splitlines():
        match = _TURN_ON_PLUG_RE.search(line)
        if match is None:
            continue
        stamp = _line_timestamp(line)
        if stamp is None:
            continue
        records.append(
            TurnOnRecord(
                at=stamp,
                device_name=match.group(1),
                capacity_wh=float(match.group(2)),
            )
        )
    return records


def find_reversals(
    decisions: Sequence[TurnOffDecision],
    turn_ons: Sequence[TurnOnRecord],
) -> list[Reversal]:
    """Pair each plug shed with the turn-on that undid it.

    A reversal is a *decided* turn-on of the same plug within
    :data:`TURN_OFF_REVERSAL_WINDOW_SECS` of the shed — production
    changing its mind about a shed it had just booked, which is what
    shedding on a noisy forecast costs when the Wh nets out anyway. The
    window is measured from the shed, not from the quarter, because the
    clearest case crosses the boundary: a shed with seconds left is undone
    by the next cycle, which lands in the following quarter.

    Args:
        decisions: Parsed turn-off decisions.
        turn_ons: Parsed turn-ons.

    Returns:
        Reversals in decision-then-time order.
    """
    ordered = sorted(turn_ons, key=lambda record: record.at)
    reversals: list[Reversal] = []
    for decision in decisions:
        quarter = decision.quarter_start
        for action in decision.actions:
            if action.kind != "plug":
                continue
            undo = next(
                (
                    record
                    for record in ordered
                    if record.device_name == action.device_name
                    and decision.at < record.at
                    and record.at - decision.at
                    <= timedelta(seconds=TURN_OFF_REVERSAL_WINDOW_SECS)
                ),
                None,
            )
            if undo is not None:
                reversals.append(
                    Reversal(
                        source=decision.source,
                        device_name=action.device_name,
                        quarter_start=quarter,
                        off_at=decision.at,
                        on_at=undo.at,
                    )
                )
    return reversals


def quarter_uncertainty(
    anchors: Sequence[ForecastAnchor],
) -> dict[_QuarterKey, QuarterUncertainty]:
    """Roll each quarter's forecast spread and production churn up.

    Both signals are the ones production already holds when it decides: the
    spread of the trailing mean's rate error over the quarter, and the
    churn the (now removed) turn-on guard used to consume. The churn is
    rebuilt offline by :func:`churn_by_anchor`.

    Args:
        anchors: Anchors in log order, as parsed for the same logs.

    Returns:
        ``(source, quarter_start)`` to signals. ``sigma_rate`` needs two
        scored anchors; ``churn_wh_per_s`` is the median of the non-zero
        rebuilt churn values, so the two anchors before the tracker warms
        up do not drag a quarter's signal to zero. Either is None when the
        quarter does not carry it.
    """
    churns = churn_by_anchor(anchors)
    shortfalls: dict[_QuarterKey, list[float]] = {}
    churn_samples: dict[_QuarterKey, list[float]] = {}
    for anchor, churn in zip(anchors, churns):
        key = (anchor.source, anchor.quarter_start)
        shortfall = anchor.rate_shortfall
        if shortfall is not None:
            shortfalls.setdefault(key, []).append(shortfall)
        if churn > 0.0:
            churn_samples.setdefault(key, []).append(churn)
    keys = set(shortfalls) | set(churn_samples) | {
        (anchor.source, anchor.quarter_start) for anchor in anchors
    }
    return {
        key: QuarterUncertainty(
            sigma_rate=(
                stddev(shortfalls[key]) if len(shortfalls.get(key, ())) > 1 else None
            ),
            churn_wh_per_s=(
                percentile(churn_samples[key], 0.5)
                if churn_samples.get(key)
                else None
            ),
        )
        for key in keys
    }


@dataclass
class _BucketAccumulator:
    """Mutable tally for one bucket while :func:`attribute_sheds` walks."""

    shed_count: int = 0
    saving_count: int = 0
    overshoot_count: int = 0
    plug_count: int = 0
    reversal_count: int = 0
    overshoots: list[float] = field(default_factory=list)


def _bucket_label(value: float, edges: Sequence[float], digits: int) -> str:
    """Label the bucket ``value`` falls in for an ascending ``edges`` tuple."""
    low: float | None = None
    for edge in edges:
        if value < edge:
            if low is None:
                return f"<{edge:.{digits}f}"
            return f"{low:.{digits}f}-{edge:.{digits}f}"
        low = edge
    return f">={edges[-1]:.{digits}f}"


def _new_buckets(
    edges: Sequence[float], digits: int
) -> dict[str, _BucketAccumulator]:
    """Empty accumulators keyed by every label the axis can produce.

    Building them all up front keeps the report's bucket order stable even
    when no shed lands in one of them.
    """
    labels = [f"<{edges[0]:.{digits}f}"]
    for low, high in zip(edges, edges[1:]):
        labels.append(f"{low:.{digits}f}-{high:.{digits}f}")
    labels.append(f">={edges[-1]:.{digits}f}")
    return {label: _BucketAccumulator() for label in labels}


def _freeze_bucket(label: str, acc: _BucketAccumulator) -> ShedBucket:
    """Turn one accumulator into its frozen report row."""
    return ShedBucket(
        label=label,
        shed_count=acc.shed_count,
        saving_count=acc.saving_count,
        overshoot_count=acc.overshoot_count,
        plug_count=acc.plug_count,
        reversal_count=acc.reversal_count,
        median_overshoot_wh=(
            percentile(acc.overshoots, 0.5) if acc.overshoots else None
        ),
    )


def attribute_sheds(
    decisions: Sequence[TurnOffDecision],
    turn_ons: Sequence[TurnOnRecord],
    uncertainty: Mapping[_QuarterKey, QuarterUncertainty],
) -> HedgeAttribution:
    """Bucket every shed by its quarter's signals and its own pre-shed gap.

    The B1 question is whether the overshoot and the reversal that undoes it
    concentrate in the quarters whose forecast is *measurably* uncertain. A
    constant hedge would then be paying in the calm quarters for the noisy
    ones; a signal-scaled one would not.

    Args:
        decisions: Parsed turn-off decisions.
        turn_ons: Parsed turn-ons, for the reversal join.
        uncertainty: Per-quarter signals from :func:`quarter_uncertainty`.

    Returns:
        One bucket row per axis and label, plus the count of sheds whose
        quarter carried no signal (those still appear on the gap axis).
    """
    undone = {
        (reversal.source, reversal.device_name, reversal.off_at)
        for reversal in find_reversals(decisions, turn_ons)
    }
    sigma_buckets = _new_buckets(TURN_OFF_SIGMA_EDGES, 2)
    churn_buckets = _new_buckets(TURN_OFF_CHURN_EDGES, 2)
    gap_buckets = _new_buckets(TURN_OFF_GAP_EDGES, 0)
    unassigned = 0
    for decision in decisions:
        signals = uncertainty.get((decision.source, decision.quarter_start))
        for action in decision.actions:
            gap = action.remaining_gap_wh
            if gap is None:
                gap = decision.gap_wh
            rows = [gap_buckets[_bucket_label(abs(gap), TURN_OFF_GAP_EDGES, 0)]]
            if signals is None or (
                signals.sigma_rate is None and signals.churn_wh_per_s is None
            ):
                unassigned += 1
            else:
                if signals.sigma_rate is not None:
                    rows.append(
                        sigma_buckets[
                            _bucket_label(
                                signals.sigma_rate, TURN_OFF_SIGMA_EDGES, 2
                            )
                        ]
                    )
                if signals.churn_wh_per_s is not None:
                    rows.append(
                        churn_buckets[
                            _bucket_label(
                                signals.churn_wh_per_s, TURN_OFF_CHURN_EDGES, 2
                            )
                        ]
                    )
            overshoot = action.overshoot_wh
            reversed_ = (
                decision.source,
                action.device_name,
                decision.at,
            ) in undone
            for acc in rows:
                acc.shed_count += 1
                if overshoot is not None:
                    acc.saving_count += 1
                    acc.overshoots.append(overshoot)
                    if overshoot > 1e-9:
                        acc.overshoot_count += 1
                if action.kind == "plug":
                    acc.plug_count += 1
                    if reversed_:
                        acc.reversal_count += 1
    return HedgeAttribution(
        sigma=tuple(
            _freeze_bucket(label, acc) for label, acc in sigma_buckets.items()
        ),
        churn=tuple(
            _freeze_bucket(label, acc) for label, acc in churn_buckets.items()
        ),
        gap=tuple(_freeze_bucket(label, acc) for label, acc in gap_buckets.items()),
        unassigned_count=unassigned,
    )


def summarize_turn_offs(
    decisions: Sequence[TurnOffDecision],
    turn_ons: Sequence[TurnOnRecord] = (),
    quarters: Sequence[QuarterSummary] = (),
    uncertainty: Mapping[_QuarterKey, QuarterUncertainty] | None = None,
) -> TurnOffSummary:
    """Roll the turn-off path's decisions into the collection report.

    Args:
        decisions: Parsed decisions, in any order.
        turn_ons: Parsed turn-ons, for the reversal join (optional).
        quarters: Per-quarter roll-ups from :func:`summarize`, joined by
            ``(source, quarter_start)`` for each turn-off quarter's
            completed total (optional).
        uncertainty: Per-quarter signals from :func:`quarter_uncertainty`.
            Supplying it adds the B1 hedge attribution; omitting it leaves
            ``hedge`` as None.

    Returns:
        The summary; all zeros for a log with no turn-off path lines.
    """
    reversals = find_reversals(decisions, turn_ons)
    actions = [action for decision in decisions for action in decision.actions]
    savings = [a.savings_wh for a in actions if a.savings_wh is not None]
    overshoots = [o for o in (a.overshoot_wh for a in actions) if o is not None]
    residuals = [d.residual_gap_wh for d in decisions]
    uncovered = [r for r in residuals if r > 1e-9]

    by_quarter: dict[_QuarterKey, list[TurnOffDecision]] = {}
    for decision in decisions:
        by_quarter.setdefault(
            (decision.source, decision.quarter_start), []
        ).append(decision)
    completed = {(q.source, q.quarter_start): q for q in quarters}
    reversal_counts: dict[_QuarterKey, int] = {}
    for reversal in reversals:
        key = (reversal.source, reversal.quarter_start)
        reversal_counts[key] = reversal_counts.get(key, 0) + 1

    rows: list[TurnOffQuarter] = []
    for key in sorted(by_quarter, key=lambda quarter_key: quarter_key[1]):
        items = by_quarter[key]
        row_actions = [a for d in items for a in d.actions]
        known = completed.get(key)
        rows.append(
            TurnOffQuarter(
                source=key[0],
                quarter_start=key[1],
                decision_count=len(items),
                plug_off_count=sum(1 for a in row_actions if a.kind == "plug"),
                shed_wh=sum(a.savings_wh or 0.0 for a in row_actions),
                min_remaining_secs=min(d.seconds_remaining for d in items),
                reversal_count=reversal_counts.get(key, 0),
                actual_wh=None if known is None else known.actual_wh,
                target_wh=None if known is None else known.target_wh,
            )
        )

    return TurnOffSummary(
        decision_count=len(decisions),
        quarter_count=len(by_quarter),
        plug_off_count=sum(1 for a in actions if a.kind == "plug"),
        tesla_amp_count=sum(1 for a in actions if a.kind == "tesla_amps"),
        tesla_stop_count=sum(1 for a in actions if a.kind == "tesla_stop"),
        shed_total_wh=sum(savings),
        median_shed_wh=percentile(savings, 0.5) if savings else 0.0,
        median_remaining_secs=(
            percentile([float(d.seconds_remaining) for d in decisions], 0.5)
            if decisions
            else 0.0
        ),
        short_horizon_count=sum(
            1 for d in decisions if d.seconds_remaining < TURN_OFF_SHORT_HORIZON_SECS
        ),
        short_horizon_secs=TURN_OFF_SHORT_HORIZON_SECS,
        uncovered_count=len(uncovered),
        median_residual_wh=percentile(uncovered, 0.5) if uncovered else 0.0,
        overshoot_count=sum(1 for o in overshoots if o > 1e-9),
        max_overshoot_wh=max([0.0, *overshoots]),
        reversals=tuple(reversals),
        quarters=tuple(rows),
        hedge=(
            None
            if uncertainty is None
            else attribute_sheds(decisions, turn_ons, uncertainty)
        ),
    )


def format_turn_offs(summary: TurnOffSummary) -> str:
    """Render the turn-off collection report as plain text tables."""
    if summary.decision_count == 0:
        return (
            "no turn-off decisions: no [_decide_turn_off] gap= line was found "
            "in the logs"
        )
    sheds = summary.plug_off_count + summary.tesla_amp_count
    median_delay = (
        f" (median {summary.median_reversal_secs:.0f} s later)"
        if summary.median_reversal_secs is not None
        else ""
    )
    lines = [
        f"turn-off decisions: {summary.decision_count} across "
        f"{summary.quarter_count} quarter(s)  (plug {summary.plug_off_count}, "
        f"Tesla amps {summary.tesla_amp_count}, Tesla stops "
        f"{summary.tesla_stop_count})",
        f"shed: total {summary.shed_total_wh:.2f} Wh, median "
        f"{summary.median_shed_wh:.2f} Wh per shed ({sheds} with a logged saving)",
        f"horizon: median R {summary.median_remaining_secs:.0f} s, "
        f"{summary.short_horizon_count}/{summary.decision_count} decisions under "
        f"{summary.short_horizon_secs} s",
        f"coverage: {summary.uncovered_count}/{summary.decision_count} decisions "
        f"left the gap uncovered (median residual "
        f"{summary.median_residual_wh:.2f} Wh among those)",
        f"overshoot: {summary.overshoot_count}/{sheds} sheds exceeded the "
        f"remaining gap (max {summary.max_overshoot_wh:.2f} Wh)",
        f"reversals: {summary.reversal_count}/{summary.plug_off_count} plug sheds "
        f"undone by a later turn-on within {TURN_OFF_REVERSAL_WINDOW_SECS} s"
        f"{median_delay}",
        "",
        f"{'quarter':<22} {'decisions':>9} {'plug-offs':>9} {'shed Wh':>8} "
        f"{'min R':>6} {'reversals':>9} {'actual':>8} {'target':>7} {'miss':>8}",
    ]
    for row in summary.quarters:
        actual = f"{row.actual_wh:.2f}" if row.actual_wh is not None else "n/a"
        target = f"{row.target_wh:.1f}" if row.target_wh is not None else "n/a"
        miss = f"{row.miss_wh:+.2f}" if row.miss_wh is not None else "n/a"
        lines.append(
            f"{row.quarter_start.strftime('%Y-%m-%d %H:%M'):<22} "
            f"{row.decision_count:>9} {row.plug_off_count:>9} {row.shed_wh:>8.2f} "
            f"{row.min_remaining_secs:>6} {row.reversal_count:>9} {actual:>8} "
            f"{target:>7} {miss:>8}"
        )
    if summary.hedge is not None:
        lines.extend(_format_hedge(summary.hedge))
    return "\n".join(lines)


def _fraction(count: int, denom: int) -> str:
    """Render ``count/denom`` with a whole-percent share, or ``-``."""
    if denom == 0:
        return "-"
    return f"{count}/{denom} ({100 * count / denom:.0f}%)"


def _trend(fractions: Sequence[float]) -> str:
    """Describe how a fraction moves across the filled buckets.

    Only the direction is reported: three buckets cannot support a
    correlation statistic, but they can say whether a constant hedge is
    subsidising a regime that never overshoots.
    """
    if len(fractions) < 2:
        return "n/a"
    steps = [high - low for low, high in zip(fractions, fractions[1:])]
    if all(step > 0 for step in steps):
        return "rises"
    if all(step < 0 for step in steps):
        return "falls"
    return "mixed"


def _format_hedge(hedge: HedgeAttribution) -> list[str]:
    """Render the B1 bucket table and its per-axis trend lines."""
    lines = [
        "",
        "hedge attribution — overshoot and reversal rate per bucket",
        f"  {'axis':<11} {'bucket':<11} {'sheds':>5} {'overshoot':>16} "
        f"{'reversal':>16} {'med OS Wh':>10}",
    ]
    axes = (
        ("sigma_rate", hedge.sigma),
        ("churn", hedge.churn),
        ("gap (Wh)", hedge.gap),
    )
    for name, buckets in axes:
        for bucket in buckets:
            if bucket.shed_count == 0:
                continue
            median = (
                f"{bucket.median_overshoot_wh:.2f}"
                if bucket.median_overshoot_wh is not None
                else "-"
            )
            lines.append(
                f"  {name:<11} {bucket.label:<11} {bucket.shed_count:>5} "
                f"{_fraction(bucket.overshoot_count, bucket.saving_count):>16} "
                f"{_fraction(bucket.reversal_count, bucket.plug_count):>16} "
                f"{median:>10}"
            )
    lines.append("")
    for name, buckets in axes:
        overshoot = [
            f for f in (b.overshoot_fraction for b in buckets) if f is not None
        ]
        reversal = [
            f for f in (b.reversal_fraction for b in buckets) if f is not None
        ]
        lines.append(
            f"  {name}: overshoot {_trend(overshoot)}, "
            f"reversal {_trend(reversal)} across buckets"
        )
    if hedge.unassigned_count:
        lines.append(
            f"  {hedge.unassigned_count}/{hedge.shed_count} sheds had no "
            "quarter signal (gap axis only)"
        )
    return lines


def _iter_log_texts(paths: Sequence[Path]) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, text)`` for each readable log, skipping the rest.

    Shared file loop for the offline CLIs so unreadable paths warn on
    stderr and never abort a multi-file run.
    """
    for path in paths:
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            print(f"skipping {path}: {exc}", file=sys.stderr)
            continue
        yield path, text


_ESTIMATOR_FLAG = "--estimator"
_TREND_FLOOR_FLAG = "--trend-floor"
_TURN_OFFS_FLAG = "--turn-offs"
_GUARDS_FLAG = "--guards"
# ── suppression census (--guards) ────────────────────────────────────
#
# A guard is only visible after the fact: it leaves no action behind, so
# every measurement that watches actions is blind to it. This section reads
# the log for each declining mechanism (the turn-on jitter guard, the banked
# cover that releases it, the stop deferral, the ramp rule that pre-empts it,
# the adaptive settle window) and for how reachable each trigger was.

# The ramp stop fires when ``t* = (P*R - gap) / (P + trend)`` fits inside one
# cycle, where P is the car's draw at the amp floor and ``trend`` is clamped
# to ``RAMP_TREND_CLAMP_FRACTION`` of P. The largest possible denominator
# (a fully clamped trend) bounds the reach: P*R - gap <= cycle*(1+frac)*P,
# so the rule can only ever bite below ``gap/P + cycle*(1+frac)`` seconds.
RAMP_BAND_CYCLE_SECS = 30
RAMP_BAND_DRAW_WH_PER_S = TESLA_CHARGE_AMPS_MIN_DEFAULT * TESLA_NOMINAL_VOLTAGE / 3600
RAMP_BAND_CLAMP_FRACTION = 0.5
"""Mirrors ``TeslaDecider.RAMP_TREND_CLAMP_FRACTION``; pinned by the tests
so the census can never drift from the rule it prices."""


def ramp_band_remaining_secs(gap_wh: float) -> float:
    """The largest horizon the ramp rule can reach, for a given gap.

    Args:
        gap_wh: Gap the stop would cover (Wh, positive).

    Returns:
        ``seconds_remaining`` above which ``t*`` cannot fit inside one
        cycle, so the ramp rule is arithmetically unable to fire.
    """
    return gap_wh / RAMP_BAND_DRAW_WH_PER_S + RAMP_BAND_CYCLE_SECS * (
        1 + RAMP_BAND_CLAMP_FRACTION
    )


@dataclass(frozen=True)
class GuardedDecline:
    """One cycle whose diagnostics reason was the jitter guard's own.

    The candidate list in the same repr is the only evidence of what the
    decline withheld: production logs the loads that were *available*, not
    the ones the guard turned away.
    """

    source: str
    at: datetime
    quarter_start: datetime | None
    available_candidates: int
    """Plug candidates that were off and toggleable in the same cycle."""


@dataclass(frozen=True)
class BankedOverride:
    """One ``gapminder_banked_override`` line: a guarded turn-on released."""

    source: str
    at: datetime
    gap_wh: float
    cover_wh: float


@dataclass(frozen=True)
class DeferEntry:
    """One stop the defer rule declined to take this cycle."""

    source: str
    at: datetime
    seconds_remaining: int
    gap_wh: float
    safe_defer_secs: int


@dataclass(frozen=True)
class RampStop:
    """One stop the ramp rule pre-empted the defer rule with."""

    source: str
    at: datetime


@dataclass(frozen=True)
class SettleWindowTrace:
    """One log's committed prediction window, cycle by cycle.

    Production never logs the commit itself, so a change between two
    consecutive cycles is the only visible form of one.
    """

    source: str
    windows: tuple[int, ...]

    @property
    def commits(self) -> tuple[tuple[int, int], ...]:
        """``(from, to)`` per window change, in log order."""
        return tuple(
            (before, after)
            for before, after in zip(self.windows, self.windows[1:])
            if before != after
        )


@dataclass(frozen=True)
class GuardCohort:
    """One side of the guarded-vs-unguarded quarter comparison.

    ``mean_abs_error_wh`` is the unweighted mean over the cohort's quarters
    of each quarter's own mean |anchor error|, so a quarter with many
    anchors does not dominate the column.
    """

    quarters: int = 0
    mean_abs_miss_wh: float | None = None
    mean_abs_error_wh: float | None = None


@dataclass(frozen=True)
class SuppressionCensus:
    """What every declining mechanism did across the scanned logs."""

    declines: int = 0
    declined_cycles: int = 0
    declined_with_candidate: int = 0
    rescues: int = 0
    deferrals: int = 0
    median_defer_secs: int | None = None
    p25_defer_secs: int | None = None
    p75_defer_secs: int | None = None
    max_defer_secs: int | None = None
    ramp_fires: int = 0
    ramp_eligible: int = 0
    settle_cycles: tuple[tuple[int, int], ...] = ()
    settle_commits: int = 0
    guarded: GuardCohort = GuardCohort()
    unguarded: GuardCohort = GuardCohort()


# The guard's own reason string, read only from the diagnostics segment: a
# candidate entry carries a ``reason`` field of its own, so an unscoped
# search could mistake a candidate's reason for the cycle's.
_DECLINE_REASON = "excessive_jitter"
_DECLINE_CANDIDATE_RE = re.compile(
    r"CandidateDetail\(device_type='plug', name='([^']*)', "
    r"power_watts=([\d.eE+-]+|None).*?can_toggle=(True|False), "
    r"desired_state=(True|False|None)"
)
_BANKED_RE = re.compile(
    r"gapminder_banked_override gap=([\d.eE+-]+) banked=([\d.eE+-]+) "
    r"cover=([\d.eE+-]+)"
)
_DEFER_RE = re.compile(
    r"\[_decide_tesla_reduce\] deferring stop: current_amps=\d+, "
    r"seconds_remaining=(\d+), safe_defer=(\d+)s, gap=([\d.eE+-]+) Wh"
)
_RAMP_RE = re.compile(r"\[_decide_tesla_reduce\] ramp stop: t\* within one cycle")
_SETTLE_RE = re.compile(r"settle_window_secs=(\d+)")


def parse_guarded_declines(
    text: str, source: str = "<log>"
) -> list[GuardedDecline]:
    """Extract cycles the turn-on jitter guard left inactive.

    Args:
        text: Full log contents.
        source: Identifier recorded on each decline (usually the filename).

    Returns:
        Declines in log order, one per cycle whose diagnostics reason is
        ``excessive_jitter``. Available loads are counted from the same
        repr, deduped by name because the repr repeats the candidate list.
    """
    declines: list[GuardedDecline] = []
    for stamp, line in _iter_result_lines(text):
        diags = line.find(_CYCLE_DIAGS)
        if diags < 0:
            continue
        segment = line[diags:]
        reason = _CYCLE_REASON_RE.search(segment)
        if reason is None or reason.group(1) != _DECLINE_REASON:
            continue
        available = {
            name
            for name, _power, toggleable, desired in _DECLINE_CANDIDATE_RE.findall(
                line
            )
            if toggleable == "True" and desired == "False"
        }
        declines.append(
            GuardedDecline(
                source=source,
                at=stamp,
                quarter_start=_cycle_quarter(segment),
                available_candidates=len(available),
            )
        )
    return declines


def parse_banked_overrides(
    text: str, source: str = "<log>"
) -> list[BankedOverride]:
    """Extract ``gapminder_banked_override`` INFO events from one log.

    Args:
        text: Full log contents.
        source: Identifier recorded on each event (usually the filename).

    Returns:
        Events in log order; empty for logs written before the override
        shipped. Events without a parseable timestamp prefix are skipped.
    """
    events: list[BankedOverride] = []
    for line in text.splitlines():
        if "gapminder_banked_override" not in line:
            continue
        match = _BANKED_RE.search(line)
        if match is None:
            continue
        stamp = _line_timestamp(line)
        if stamp is None:
            continue
        events.append(
            BankedOverride(
                source=source,
                at=stamp,
                gap_wh=float(match.group(1)),
                cover_wh=float(match.group(3)),
            )
        )
    return events


def parse_defer_entries(text: str, source: str = "<log>") -> list[DeferEntry]:
    """Extract stops the defer rule declined to take.

    A deferral means the quarter-hour still had more room than the car's
    own draw would eat, so the stop was left for a later cycle.

    Args:
        text: Full log contents.
        source: Identifier recorded on each entry (usually the filename).

    Returns:
        Entries in log order; empty for logs written before the defer rule
        shipped (or when it never declined a stop).
    """
    entries: list[DeferEntry] = []
    for line in text.splitlines():
        match = _DEFER_RE.search(line)
        if match is None:
            continue
        stamp = _line_timestamp(line)
        if stamp is None:
            continue
        entries.append(
            DeferEntry(
                source=source,
                at=stamp,
                seconds_remaining=int(match.group(1)),
                safe_defer_secs=int(match.group(2)),
                gap_wh=float(match.group(3)),
            )
        )
    return entries


def parse_ramp_stops(text: str, source: str = "<log>") -> list[RampStop]:
    """Extract stops the ramp rule pulled into the current cycle.

    This is the one declining path that *acts*, so a fire here is a
    deferral that was pre-empted rather than taken.

    Args:
        text: Full log contents.
        source: Identifier recorded on each stop (usually the filename).

    Returns:
        Stops in log order; empty when the rule never fired.
    """
    stops: list[RampStop] = []
    for line in text.splitlines():
        if _RAMP_RE.search(line) is None:
            continue
        stamp = _line_timestamp(line)
        if stamp is not None:
            stops.append(RampStop(source=source, at=stamp))
    return stops


def parse_settle_windows(text: str, source: str = "<log>") -> SettleWindowTrace:
    """Extract the committed prediction window per cycle-result line.

    Args:
        text: Full log contents.
        source: Identifier recorded on the trace (usually the filename).

    Returns:
        The windows in cycle order. The field only exists in logs written
        after the adaptive window shipped; earlier logs yield an empty
        trace.
    """
    windows: list[int] = []
    for _stamp, line in _iter_result_lines(text):
        match = _SETTLE_RE.search(line)
        if match is not None:
            windows.append(int(match.group(1)))
    return SettleWindowTrace(source=source, windows=tuple(windows))


def _separate_percentiles(
    values: Sequence[int],
) -> tuple[int | None, int | None, int | None, int | None]:
    """Return ``(p25, median, p75, max)``, Nones when too few to separate."""
    if not values:
        return None, None, None, None
    ordered = sorted(values)
    if len(ordered) == 1:
        return None, ordered[0], None, ordered[0]
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive")
    first, middle, third = (round(value) for value in quartiles)
    return first, middle, third, ordered[-1]


def _cohort(
    quarters: Sequence[QuarterSummary],
    errors: dict[tuple[str, datetime | None], list[float]],
) -> GuardCohort:
    """Summarize one side of the guarded/unguarded split."""
    if not quarters:
        return GuardCohort()
    misses = [abs(miss) for miss in (q.miss_wh for q in quarters) if miss is not None]
    per_quarter: list[float] = []
    for quarter in quarters:
        found = errors.get((quarter.source, quarter.quarter_start))
        if found:
            per_quarter.append(sum(abs(error) for error in found) / len(found))
    return GuardCohort(
        quarters=len(quarters),
        mean_abs_miss_wh=(
            sum(misses) / len(misses) if misses else None
        ),
        mean_abs_error_wh=(
            sum(per_quarter) / len(per_quarter) if per_quarter else None
        ),
    )


def _guard_cohorts(
    quarters: Sequence[QuarterSummary], anchors: Sequence[ForecastAnchor]
) -> tuple[GuardCohort, GuardCohort]:
    """Split quarters by whether the jitter guard fired in them.

    Errors are grouped by the anchor's own source and quarter, so one
    log's anchors only ever price that log's quarters.
    """
    errors: dict[tuple[str, datetime | None], list[float]] = {}
    for anchor in anchors:
        error = anchor.error
        if error is None:
            continue
        errors.setdefault((anchor.source, anchor.quarter_start), []).append(error)
    guarded = [q for q in quarters if q.guard_firings]
    unguarded = [q for q in quarters if not q.guard_firings]
    return _cohort(guarded, errors), _cohort(unguarded, errors)


def summarize_suppressions(
    *,
    guard_events: Sequence[GuardEvent] = (),
    declines: Sequence[GuardedDecline] = (),
    overrides: Sequence[BankedOverride] = (),
    defers: Sequence[DeferEntry] = (),
    ramp_fires: Sequence[RampStop] = (),
    settle_windows: Sequence[SettleWindowTrace] = (),
    quarters: Sequence[QuarterSummary] = (),
    anchors: Sequence[ForecastAnchor] = (),
) -> SuppressionCensus:
    """Roll the parsed suppression evidence into one census.

    Args:
        guard_events: Parsed ``gapminder_jitter_guard`` fires — one event
            per decline production *logged*, which is not the same count
            as the cycles it left inactive.
        declines: Parsed declined cycles, carrying the available loads.
        overrides: Parsed banked-cover rescues.
        defers: Parsed deferrals, whose horizons the ramp band is judged
            against.
        ramp_fires: Parsed ramp stops.
        settle_windows: One trace per log; commits are counted inside a
            trace, never across logs.
        quarters: Per-quarter roll-ups supplying the miss and the guard
            count of the guarded/unguarded split.
        anchors: Scored anchors, supplying the split's error column.

    Returns:
        The census, with zero counts and empty cohorts when nothing was
        parsed.
    """
    p25, median, p75, longest = _separate_percentiles(
        [entry.seconds_remaining for entry in defers]
    )
    cycles: dict[int, int] = {}
    for trace in settle_windows:
        for window in trace.windows:
            cycles[window] = cycles.get(window, 0) + 1
    guarded, unguarded = _guard_cohorts(quarters, anchors)
    return SuppressionCensus(
        declines=len(guard_events),
        declined_cycles=len(declines),
        declined_with_candidate=sum(
            1 for decline in declines if decline.available_candidates
        ),
        rescues=len(overrides),
        deferrals=len(defers),
        median_defer_secs=median,
        p25_defer_secs=p25,
        p75_defer_secs=p75,
        max_defer_secs=longest,
        ramp_fires=len(ramp_fires),
        ramp_eligible=sum(
            1
            for entry in defers
            if entry.seconds_remaining <= ramp_band_remaining_secs(entry.gap_wh)
        ),
        settle_cycles=tuple(sorted(cycles.items(), key=lambda row: (-row[1], row[0]))),
        settle_commits=sum(len(trace.commits) for trace in settle_windows),
        guarded=guarded,
        unguarded=unguarded,
    )


def _census_has_data(census: SuppressionCensus) -> bool:
    """Whether anything at all was parsed into the census."""
    return any(
        (
            census.declines,
            census.declined_cycles,
            census.rescues,
            census.deferrals,
            census.ramp_fires,
            census.settle_cycles,
            census.guarded.quarters,
            census.unguarded.quarters,
        )
    )


def _cohort_line(label: str, cohort: GuardCohort) -> str:
    """Render one cohort as a single line fragment."""
    if not cohort.quarters:
        return f"{label}: none"
    miss = (
        f"{cohort.mean_abs_miss_wh:.2f} Wh"
        if cohort.mean_abs_miss_wh is not None
        else "n/a"
    )
    error = (
        f"{cohort.mean_abs_error_wh:.2f} Wh"
        if cohort.mean_abs_error_wh is not None
        else "n/a"
    )
    return f"{label}: {cohort.quarters} quarter(s), mean|miss| {miss}, mean|e| {error}"


def _settle_line(census: SuppressionCensus) -> str:
    """Render the settle-window distribution."""
    if not census.settle_cycles:
        return "none seen"
    return ", ".join(
        f"{window} s x {count}" for window, count in census.settle_cycles
    )


def _availability_note(census: SuppressionCensus) -> str:
    """Say plainly whether the availability count can tell declines apart.

    Production lists *every* configured plug in a cycle's candidates, so
    whenever one is off and past its debounce the count is universal. That
    makes it a measure of what was on offer, never of what the decline
    changed — the question the closed-loop replay has to answer instead.
    """
    if census.declined_cycles and (
        census.declined_with_candidate == census.declined_cycles
    ):
        return (
            "universal, because every configured plug is listed, so this is "
            "what was available, not what the decline withheld"
        )
    return "some declined cycles had nothing to turn on"


def format_suppressions(census: SuppressionCensus) -> str:
    """Render the suppression census as plain text tables."""
    if not _census_has_data(census):
        return (
            "no suppression events: no guard, defer, ramp, or settle-window "
            "evidence was found in the logs"
        )
    horizon = (
        f"median R {census.median_defer_secs} s "
        f"(p25 {census.p25_defer_secs}, p75 {census.p75_defer_secs}, "
        f"max {census.max_defer_secs})"
        if census.median_defer_secs is not None
        else "no horizon logged"
    )
    return "\n".join(
        [
            "suppression census",
            f"  jitter guard: {census.declines} logged decline event(s) across "
            f"{census.declined_cycles} declined cycle(s)",
            f"  available turn-on: {census.declined_with_candidate} of "
            f"{census.declined_cycles} declined cycle(s) had an off, toggleable "
            f"plug candidate — {_availability_note(census)}",
            f"  rescue: {census.rescues} banked-cover turn-on(s) released again",
            "  " + _cohort_line("guarded quarters", census.guarded),
            "  " + _cohort_line("unguarded quarters", census.unguarded),
            "  confound: the guard fires in the churning-forecast quarters, so "
            "this split is correlation, not the guard's effect",
            f"  stop deferral: {census.deferrals} deferral(s), {horizon}",
            f"  ramp rule: {census.ramp_fires} fire(s); {census.ramp_eligible} "
            f"deferral(s) were eligible (R <= gap/P + "
            f"{RAMP_BAND_CYCLE_SECS * (1 + RAMP_BAND_CLAMP_FRACTION):.0f} s), "
            f"so only those could ever have fired",
            f"  settle window: {_settle_line(census)}, "
            f"{census.settle_commits} commit(s) visible as a change",
        ]
    )


@dataclass
class _CensusInputs:
    """Mutable accumulator for the opt-in suppression-census parsers.

    Kept as one object so ``main`` does not carry five parallel lists
    through its per-log loop (pylint's local-variable budget).
    """

    declines: list[GuardedDecline] = field(default_factory=list)
    overrides: list[BankedOverride] = field(default_factory=list)
    defers: list[DeferEntry] = field(default_factory=list)
    ramp_fires: list[RampStop] = field(default_factory=list)
    settle_windows: list[SettleWindowTrace] = field(default_factory=list)

    def ingest(self, text: str, source: str) -> None:
        """Parse one log's suppression evidence into the accumulator.

        Args:
            text: Full log contents.
            source: Identifier recorded on each parsed item.
        """
        self.declines.extend(parse_guarded_declines(text, source=source))
        self.overrides.extend(parse_banked_overrides(text, source=source))
        self.defers.extend(parse_defer_entries(text, source=source))
        self.ramp_fires.extend(parse_ramp_stops(text, source=source))
        self.settle_windows.append(parse_settle_windows(text, source=source))

    def summarize(
        self,
        guard_events: Sequence[GuardEvent],
        quarters: Sequence[QuarterSummary],
        anchors: Sequence[ForecastAnchor],
    ) -> SuppressionCensus:
        """Roll the accumulated evidence into the census.

        Args:
            guard_events: Jitter-guard fires parsed for every scan.
            quarters: Per-quarter roll-ups from the scored report.
            anchors: Scored anchors, for the cohort split's error column.

        Returns:
            The census over everything ingested so far.
        """
        return summarize_suppressions(
            guard_events=guard_events,
            declines=self.declines,
            overrides=self.overrides,
            defers=self.defers,
            ramp_fires=self.ramp_fires,
            settle_windows=self.settle_windows,
            quarters=quarters,
            anchors=anchors,
        )


_USAGE = (
    "usage: forecast_log_scoring.py [LOG ...] "
    "[--estimator[=NAME,...]] [--trend-floor=WH] [--turn-offs] [--guards]"
)


def _parse_cli(
    args: Sequence[str],
) -> tuple[list[Path], tuple[str, ...] | None, float, bool, bool]:
    """Split argv into log paths, requested variants, and the flags.

    Args:
        args: Raw argv entries — log paths and ``--flag[=value]`` pairs.
            With no paths, every ``*.log`` under ``bugs/`` is used.

    Returns:
        ``(paths, names, trend_floor_wh, turn_offs, guards)``. ``names`` is
        None when ``--estimator`` was not given (the shipped report only),
        else the variants to compare. ``turn_offs`` asks for the turn-off
        path collection report, ``guards`` for the suppression census.

    Raises:
        ValueError: On an unknown flag, an unknown variant name, or a
            non-numeric trend floor.
    """
    paths: list[Path] = []
    names: tuple[str, ...] | None = None
    trend_floor = TREND_GATE_NOISE_FLOOR_WH
    turn_offs = False
    guards = False
    for arg in args:
        if arg == _ESTIMATOR_FLAG:
            names = ESTIMATOR_NAMES
        elif arg.startswith(_ESTIMATOR_FLAG + "="):
            requested = tuple(
                name.strip()
                for name in arg.split("=", 1)[1].split(",")
                if name.strip()
            )
            unknown = [name for name in requested if name not in ESTIMATOR_NAMES]
            if unknown:
                raise ValueError(
                    f"unknown estimator variant(s): {', '.join(unknown)}"
                )
            names = requested
        elif arg.startswith(_TREND_FLOOR_FLAG + "="):
            trend_floor = float(arg.split("=", 1)[1])
        elif arg == _TURN_OFFS_FLAG:
            turn_offs = True
        elif arg == _GUARDS_FLAG:
            guards = True
        elif arg.startswith("-"):
            raise ValueError(f"unknown flag: {arg}")
        else:
            paths.append(Path(arg))
    if not paths:
        paths = sorted(Path("bugs").glob("*.log"))
    return paths, names, trend_floor, turn_offs, guards


def main(argv: Sequence[str] | None = None) -> int:
    """Print the forecast-error report for the given log paths.

    Args:
        argv: Log paths, plus optional ``--estimator[=NAME,...]`` (score
            the named forecast variants and print the comparison),
            ``--trend-floor=WH`` (trust floor for the trend-gated
            variant), ``--turn-offs`` (add the turn-off path
            collection report), and ``--guards`` (add the suppression
            census). Defaults to every ``*.log`` under ``bugs/``.

    Returns:
        Process exit status: 0 on success, 1 if a log cannot be read or an
        argument is rejected.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        paths, names, trend_floor, turn_offs, guards = _parse_cli(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 1
    anchors: list[ForecastAnchor] = []
    on_watts: list[float | None] = []
    cycles: list[CycleRecord] = []
    guard_events: list[GuardEvent] = []
    decisions: list[TurnOffDecision] = []
    turn_ons: list[TurnOnRecord] = []
    census = _CensusInputs()
    for path, text in _iter_log_texts(paths):
        path_anchors = parse_log(text, source=path.name)
        on_watts.extend(
            assign_on_watts(
                path_anchors, parse_on_watts_timeline(text, source=path.name)
            )
        )
        anchors.extend(path_anchors)
        cycles.extend(parse_cycles(text, source=path.name))
        guard_events.extend(parse_guard_events(text, source=path.name))
        if turn_offs:
            decisions.extend(parse_turn_offs(text, source=path.name))
            turn_ons.extend(parse_turn_ons(text, source=path.name))
        if guards:
            census.ingest(text, path.name)
    report = summarize(
        anchors, cycles=cycles, guards=guard_events, on_watts=on_watts
    )
    print(f"scanned {len(paths)} log(s)\n")
    print(format_report(report))
    if names is not None:
        print()
        print(
            format_estimators(
                score_estimators(
                    anchors, names=names, trend_floor_wh=trend_floor
                ),
                trend_floor_wh=trend_floor,
            )
        )
    if turn_offs:
        print()
        print(
            format_turn_offs(
                summarize_turn_offs(
                    decisions,
                    turn_ons,
                    report.quarters,
                    uncertainty=quarter_uncertainty(anchors),
                )
            )
        )
    if guards:
        print()
        print(
            format_suppressions(
                census.summarize(guard_events, report.quarters, anchors)
            )
        )
    return 0 if report.anchor_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
