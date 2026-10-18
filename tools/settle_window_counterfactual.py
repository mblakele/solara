"""Settle-window counterfactual: the committed adaptive window vs a pinned one.

The prediction/settle window is adaptive: ``EnergyCache`` quantization is
detected each fetch, ``_resolve_prediction_window`` filters it, and
``StateTracker.apply_prediction_window`` commits a change only after two
consecutive confirmations outside ``SETTLE_WINDOW_DEADBAND_SECS``. That one
window is the ``EffectStore``/``TeslaSettleTracker`` freshness window, so it
reaches decisions in exactly two places:

* the fresh-data gate ``has_pending_effect_since`` — an early exit that
  defers a decision while an effect taken after the newest data point has
  not been reflected in the API data yet; and
* the post-amp-change suppression ``_suppressed_by_settle``.

This tool prices the adaptation against a *pinned* window (default
``DEFAULT_PREDICTION_WINDOW_SECS``, the classic 30 s) on a log. It rebuilds
the pending-effect store from the logged ``actions=[PendingEffect(...)]``
reprs, replays those two consumers once per cycle under two arms — the window
the log committed at that cycle, and the pinned value — and reports every
cycle where the arms disagree. A disagreement is a decision the adaptation
changed; the price is the miss impact of the quarters those cycles fall in
(the scorer's target/miss table), which is zero when no cycle disagrees.

Fidelity: the store, the tracker and both predicates are production's own
(``EffectStore``/``TeslaSettleTracker``), driven with the window the log
committed. The effect set is rebuilt from the cycle-result ``actions=`` repr,
i.e. the effects production committed (the corpus is not dry-run —
``pending_effects_count`` is non-zero). One stated deviation: production
evaluates the fresh-data gate and the suppression at different points inside
a cycle; this tool evaluates both against the same pre-decision effect set,
which is the set production itself sees at the gate.

Corpus verdict: over ``corpus/`` the window differs from 30 s in 107 of
55,406 cycles (2026-10-12: 44 at 15 s; 2026-10-16: 59 at 15 s + 4 at 21 s),
the two arms disagree on **no** cycle, and the fresh-data gate fires the same
``wait`` count under both (2,628). The suppression predicate fires in no
cycle under either arm. It is in fact subsumed: whenever a Tesla settle
effect is active, ``has_pending_effect_since`` is also true (both compare the
effect's wall and data-point ages against the same window, and the settle's
``now`` is at or after ``data_point_at``), so the fresh-data gate exits
before the suppression is reached. The adaptation guards a failure mode this
corpus never exercises; nothing is simplified on the strength of it.

Run from the repo root::

    uv run python tools/settle_window_counterfactual.py corpus/*.log
    uv run python tools/settle_window_counterfactual.py corpus/*.log --pinned-secs 30
"""

# The ``_ROOT``/``_TOOLS`` bootstrap and the cycle-line scan are the established
# offline-tool patterns (``estimator_replay`` / ``forecast_log_scoring``); the
# parser deliberately re-walks the lines because it needs the committed window,
# the effect reprs and the exact data point, which the scorer's cycle record
# does not carry.
# pylint: disable=duplicate-code

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import pytz

_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = Path(__file__).resolve().parent
# Both roots go on the path: the repo root for the production modules, and
# the tools directory so ``forecast_log_scoring`` resolves to one module
# whether this file runs as a script or is imported as
# ``tools.settle_window_counterfactual``.
for _path in (str(_ROOT), str(_TOOLS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from forecast_log_scoring import (  # noqa: E402 # pylint: disable=wrong-import-position
    _CYCLE_DATA_POINT_RE,
    _CYCLE_DIAGS,
    _LINE_PREFIX_RE,
    _SETTLE_RE,
    _iter_log_texts,
)
from constants import (  # noqa: E402 # pylint: disable=wrong-import-position
    DEFAULT_PREDICTION_WINDOW_SECS,
)
from load_models import PendingEffect  # noqa: E402 # pylint: disable=wrong-import-position
from load_nbc import (  # noqa: E402 # pylint: disable=wrong-import-position
    EffectStore,
    GapMinder,
    TeslaSettleTracker,
)
from util import floor_to_qh  # noqa: E402 # pylint: disable=wrong-import-position

PINNED_DEFAULT_SECS = DEFAULT_PREDICTION_WINDOW_SECS
"""Pinned window when none is given: the classic pre-quantization value."""

_STATUS_RE = re.compile(r"CycleResult\(status='(\w+)'")
_QH_RE = re.compile(r"CycleResult\(status='\w+', qh=(?:'([^']*)'|None)")
_GAP_RE = re.compile(r"CycleDiagnostics\(gap_wh=(-?[\d.]+)")
# "datetime.datetime(2026, 10, 12, 9, 39, 32, tzinfo=<DstTzInfo
#  'America/Los_Angeles' PDT-1 day, 17:00:00 DST>)" or "... tzinfo=datetime.timezone.utc)".
_REPR_DATETIME_RE = re.compile(
    r"(\d{4}), (\d{1,2}), (\d{1,2}), (\d{1,2}), (\d{1,2}), (\d{1,2})"
    r"(?:, (\d{1,6}))?, tzinfo=(.+)"
)
# One PendingEffect repr. The timestamp/data_point args hold no parentheses
# (the tzinfo rendering is ``<DstTzInfo 'Zone' PDT-1 day, 17:00:00 DST>`` or
# ``datetime.timezone.utc``), so ``[^()]*`` spans each datetime exactly.
_EFFECT_RE = re.compile(
    r"device_name='([^']*)', action='([^']*)', "
    r"timestamp=datetime\.datetime\(([^()]*)\), "
    r"data_point_at=datetime\.datetime\(([^()]*)\), "
    r"power_watts=(-?[\d.]+), target_amps=(\d+|None), "
    r"direction=(?:'([^']*)'|None), "
    r"suppress_action=(?:'([^']*)'|None), "
    r"qh_name=(?:'([^']*)'|None)\)"
)


def _parse_repr_datetime(text: str) -> datetime | None:
    """Parse the arguments of one ``datetime.datetime(...)`` repr to UTC.

    Args:
        text: The text inside (and after) the datetime's parentheses.

    Returns:
        An aware UTC datetime, or None when the arguments cannot be read.
    """
    match = _REPR_DATETIME_RE.search(text)
    if match is None:
        return None
    year, month, day, hour, minute, second, micro, tzinfo = match.groups()
    naive = datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second),
        int(micro or 0),
    )
    zone = re.search(r"'([^']+)'", tzinfo)
    if zone is None:
        return naive.replace(tzinfo=timezone.utc)
    try:
        return pytz.timezone(zone.group(1)).localize(naive).astimezone(timezone.utc)
    except (pytz.UnknownTimeZoneError, ValueError):
        return None


def _cycle_data_point(segment: str) -> datetime | None:
    """Exact UTC data point of the diagnostics segment, or None.

    Args:
        segment: Text starting at ``diagnostics=CycleDiagnostics(``. The
            segment scopes the match so a pending effect's older
            ``data_point_at`` cannot stand in for the cycle's own.
    """
    match = _CYCLE_DATA_POINT_RE.search(segment)
    return None if match is None else _parse_repr_datetime(match.group(0))


def _parse_effects(line: str) -> tuple[PendingEffect, ...]:
    """Rebuild the ``actions=[PendingEffect(...)]`` list of one cycle result."""
    start = line.find("actions=[")
    if start < 0:
        return ()
    body = line[start + len("actions=["):]
    end = body.find("]")
    if end >= 0:
        body = body[:end]
    effects: list[PendingEffect] = []
    for match in _EFFECT_RE.finditer(body):
        stamp = _parse_repr_datetime(match.group(3))
        point = _parse_repr_datetime(match.group(4))
        if stamp is None or point is None:
            continue
        effects.append(
            PendingEffect(
                device_name=match.group(1),
                action=match.group(2),  # type: ignore[arg-type]
                timestamp=stamp,
                data_point_at=point,
                power_watts=float(match.group(5)),
                target_amps=(
                    None if match.group(6) == "None" else int(match.group(6))
                ),
                direction=match.group(7),  # type: ignore[arg-type]
                suppress_action=match.group(8),  # type: ignore[arg-type]
                qh_name=match.group(9),
            )
        )
    return tuple(effects)


@dataclass(frozen=True)
class Cycle:
    """One logged cycle: the window it committed and the effects it took."""

    source: str
    at: datetime
    status: str
    qh_name: str | None
    gap_wh: float | None
    data_point_at: datetime | None
    window: int
    effects: tuple[PendingEffect, ...]


@dataclass(frozen=True)
class Verdict:
    """The two window-dependent predicates for one cycle, one arm."""

    waits: bool
    suppressed: bool

    @property
    def deferred(self) -> bool:
        """True when either consumer keeps production from deciding.

        Production checks the fresh-data gate before ``decide()``, so a
        ``wait`` pre-empts the suppression; the effective outcome is the
        disjunction, which is what a change in window can move.
        """
        return self.waits or self.suppressed


@dataclass(frozen=True)
class Comparison:
    """Committed-window arm against a pinned arm over one log."""

    cycles: int
    windows: dict[int, int]
    waits: int
    pinned_waits: int
    suppressions: int
    pinned_suppressions: int
    wait_flips: int
    suppress_flips: int
    changed: tuple[Cycle, ...]

    @property
    def non_default(self) -> int:
        """Cycles whose committed window differs from the pinned default."""
        return sum(
            count
            for window, count in self.windows.items()
            if window != PINNED_DEFAULT_SECS
        )

    @property
    def quarters_changed(self) -> int:
        """Distinct quarters any changed cycle's data point falls in."""
        return len(
            {
                floor_to_qh(cycle.data_point_at)
                for cycle in self.changed
                if cycle.data_point_at is not None
            }
        )


def parse_cycles(text: str, source: str = "<log>") -> list[Cycle]:
    """Extract every cycle-result record from one log's text.

    Args:
        text: Full log contents.
        source: Identifier recorded on each cycle (usually the filename).

    Returns:
        Cycles in log order. Lines without a ``diagnostics=CycleDiagnostics``
        segment are skipped; a cycle whose data point is unparseable keeps its
        ``data_point_at`` None (the gate then never fires for it, as in
        production's early exits).
    """
    cycles: list[Cycle] = []
    for line in text.splitlines():
        if "CycleResult(" not in line:
            continue
        diags = line.find(_CYCLE_DIAGS)
        if diags < 0:
            continue
        segment = line[diags:]
        status = _STATUS_RE.search(line)
        at = _LINE_PREFIX_RE.match(line)
        if status is None or at is None:
            continue
        stamp = datetime.strptime(
            f"{at.group(1)} {at.group(2)}", "%Y-%m-%d %H:%M:%S %z"
        ).astimezone(timezone.utc)
        qh_name = _QH_RE.search(line)
        gap = _GAP_RE.search(segment)
        window = _SETTLE_RE.search(segment)
        cycles.append(
            Cycle(
                source=source,
                at=stamp,
                status=status.group(1),
                qh_name=qh_name.group(1) if qh_name else None,
                gap_wh=float(gap.group(1)) if gap else None,
                data_point_at=_cycle_data_point(segment),
                window=(
                    int(window.group(1))
                    if window
                    else DEFAULT_PREDICTION_WINDOW_SECS
                ),
                effects=_parse_effects(line),
            )
        )
    return cycles


def _suppressed(tracker: TeslaSettleTracker, cycle: Cycle) -> bool:
    """Production's ``_suppressed_by_settle`` over one cycle's logged inputs."""
    if cycle.data_point_at is None or cycle.gap_wh is None:
        return False
    gap = cycle.gap_wh
    suppress_wh = GapMinder.TESLA_SETTLE_SUPPRESS_WH
    increase = tracker.get_active(
        cycle.at, current_qh=cycle.qh_name,
        data_point_at=cycle.data_point_at, direction="increase",
    )
    if gap < 0 and abs(gap) < suppress_wh and increase is not None:
        return True
    decrease = tracker.get_active(
        cycle.at, current_qh=cycle.qh_name,
        data_point_at=cycle.data_point_at, direction="decrease",
    )
    if 0 < gap < suppress_wh and decrease is not None:
        return True
    return False


def replay(cycles: Sequence[Cycle], pinned: int | None) -> list[Verdict]:
    """Replay the two window consumers over a log, one arm per call.

    Args:
        cycles: The log's cycles in order.
        pinned: Window to pin for every cycle, or None to use the window the
            log committed at each cycle (the adaptive arm).

    Returns:
        One :class:`Verdict` per cycle, in order.
    """
    store = EffectStore()
    tracker = TeslaSettleTracker(store)
    verdicts: list[Verdict] = []
    for cycle in cycles:
        window = cycle.window if pinned is None else pinned
        store.window_secs = window
        tracker.window_secs = window
        waits = (
            cycle.data_point_at is not None
            and store.has_since(cycle.data_point_at)
        )
        verdicts.append(
            Verdict(waits=waits, suppressed=_suppressed(tracker, cycle))
        )
        if cycle.data_point_at is not None:
            store.prune(cycle.data_point_at, now=cycle.at)
        for effect in cycle.effects:
            store.add(effect)
    return verdicts


def compare(cycles: Sequence[Cycle], pinned: int) -> Comparison:
    """Compare the committed-window arm against the pinned arm."""
    adaptive = replay(cycles, None)
    fixed = replay(cycles, pinned)
    windows: dict[int, int] = {}
    for cycle in cycles:
        windows[cycle.window] = windows.get(cycle.window, 0) + 1
    changed = tuple(
        cycle
        for cycle, left, right in zip(cycles, adaptive, fixed)
        if left.deferred != right.deferred
    )
    return Comparison(
        cycles=len(cycles),
        windows=windows,
        waits=sum(v.waits for v in adaptive),
        pinned_waits=sum(v.waits for v in fixed),
        suppressions=sum(v.suppressed for v in adaptive),
        pinned_suppressions=sum(v.suppressed for v in fixed),
        wait_flips=sum(
            1 for left, right in zip(adaptive, fixed) if left.waits != right.waits
        ),
        suppress_flips=sum(
            1
            for left, right in zip(adaptive, fixed)
            if left.suppressed != right.suppressed
        ),
        changed=changed,
    )


def format_comparison(
    comparisons: Sequence[tuple[str, Comparison]], pinned: int
) -> str:
    """Render one counterfactual table plus its total row."""
    lines = [
        f"settle-window counterfactual — committed vs pinned {pinned} s",
        f"  {'log':<34} {'cycles':>7} {'nonstd':>7} {'15s':>5} "
        f"{'21s':>5} {'waitflip':>9} {'suppflip':>9}",
    ]
    total_cycles = 0
    total_nonstd = 0
    total_changed = 0
    total_quarters: set[datetime] = set()
    total_waits = 0
    total_pinned_waits = 0
    total_suppressions = 0
    total_pinned_suppressions = 0
    for label, item in comparisons:
        short = {
            window: count
            for window, count in item.windows.items()
            if window != pinned
        }
        lines.append(
            f"  {label:<34} {item.cycles:>7} {item.non_default:>7} "
            f"{short.get(15, 0):>5} {short.get(21, 0):>5} "
            f"{item.wait_flips:>9} {item.suppress_flips:>9}"
        )
        total_cycles += item.cycles
        total_nonstd += item.non_default
        total_changed += len(item.changed)
        total_quarters.update(
            floor_to_qh(cycle.data_point_at)
            for cycle in item.changed
            if cycle.data_point_at is not None
        )
        total_waits += item.waits
        total_pinned_waits += item.pinned_waits
        total_suppressions += item.suppressions
        total_pinned_suppressions += item.pinned_suppressions
    lines.append("")
    lines.append(
        f"  total: {total_cycles} cycles, {total_nonstd} non-default window, "
        f"{total_changed} changed decisions in {len(total_quarters)} quarter(s)"
    )
    lines.append(
        f"  fresh-data gate fires: committed {total_waits}, "
        f"pinned {total_pinned_waits}; settle suppression fires: "
        f"committed {total_suppressions}, pinned {total_pinned_suppressions}"
    )
    return "\n".join(lines)


__all__ = [
    "Comparison",
    "Cycle",
    "Verdict",
    "compare",
    "format_comparison",
    "parse_cycles",
    "replay",
]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", nargs="+", type=Path, help="log files to read")
    parser.add_argument(
        "--pinned-secs",
        type=int,
        default=PINNED_DEFAULT_SECS,
        help=f"window to pin (default {PINNED_DEFAULT_SECS})",
    )
    args = parser.parse_args(argv)
    comparisons: list[tuple[str, Comparison]] = []
    for path, text in _iter_log_texts(args.logs):
        comparisons.append(
            (path.name, compare(parse_cycles(text, path.name), args.pinned_secs))
        )
    print(format_comparison(comparisons, args.pinned_secs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
