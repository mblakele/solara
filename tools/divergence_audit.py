"""Decision-divergence audit: status-quo vs residual small-plug budgets.

Offline-only analysis: no production module imports anything here. Compares,
per scored turn_off-side anchor, the watts each policy would shed given the
logged ON set:

* ``status_quo`` — raw gap, candidate-order binpack, no fit check (what
  production does today: turn_off sheds until the budget is covered, even
  for negligible savings).
* ``residual`` — lumpy-first when a large load is implicated in the rate
  *and* the deficit exceeds the smalls' combined capacity, then smalls on
  the residual budget (raw prediction minus shed-lumpy savings).

Sentinel is excluded by name (reserve, as production); Tesla is out of
scope (its own controller path, idle in the incident quarters). Turn-on
anchors decide nothing in either policy here and are skipped.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from forecast_log_scoring import (  # noqa: E402 # pylint: disable=wrong-import-position
    _iter_candidate_entries,
    _iter_log_texts,
    _iter_result_lines,
    _prior_values,
    parse_cycles,
    parse_log,
)

HYSTERESIS_WH = 3.0
"""Turn_off deficit threshold, matching the production logs under audit."""

LUMPY_MIN_WATTS = 1000.0
"""Minimum rating for lumpy-first candidacy; smalls can never qualify."""

SENTINEL_NAME = "sentinel"
"""Reserve plug, never dispatched by either policy."""


@dataclass(frozen=True)
class PlugState:
    """One candidate plug: rated draw and wanted state."""

    name: str
    power_watts: float
    desired: bool


def parse_plug_states(
    text: str, source: str = "<log>"
) -> list[tuple[datetime, list[PlugState]]]:
    """Extract per-cycle plug candidates from cycle-result reprs.

    Args:
        text: Full log contents.
        source: Identifier (unused beyond symmetry with the other parsers).

    Returns:
        ``(wall_time, candidates)`` in log order; Tesla entries are
        dropped (separate controller path) and entries without a parseable
        timestamp are skipped rather than guessed.
    """
    _ = source
    timeline: list[tuple[datetime, list[PlugState]]] = []
    for stamp, line in _iter_result_lines(text):
        # The repr carries the candidate list twice (top level and the
        # diagnostics segment); first occurrence wins so each plug counts
        # once no matter how many segments repeat it.
        seen: set[str] = set()
        states: list[PlugState] = []
        for kind, name, power, desired in _iter_candidate_entries(line):
            if kind != "plug" or power == "None" or name in seen:
                continue
            if desired not in ("True", "False"):
                continue
            seen.add(name)
            states.append(
                PlugState(
                    name=name, power_watts=float(power), desired=desired == "True"
                )
            )
        timeline.append((stamp, states))
    return timeline


def parse_priorities(text: str) -> dict[str, int]:
    """Recover plug priorities from logged action lines.

    Priorities live in config, not in candidates, but every plug action
    logs its priority (``... device=ecoflow savings=.. priority=50``);
    the first sighting per device wins. Devices that never act keep the
    production default 0.

    Args:
        text: Full log contents.

    Returns:
        Priority by plug name.
    """
    priorities: dict[str, int] = {}
    pattern = re.compile(
        r"action=(?:turn_off|turn_on) device=(.+?) (?:savings|capacity)=\S+"
        r".*?priority=(\d+)"
    )
    for line in text.splitlines():
        match = pattern.search(line)
        if match is None:
            continue
        name, priority = match.group(1), int(match.group(2))
        priorities.setdefault(name, priority)
    return priorities


def assign_plug_states(
    walls: list[datetime | None],
    timeline: list[tuple[datetime, list[PlugState]]],
) -> list[list[PlugState] | None]:
    """Attribute each wall time the latest cycle states at or before it.

    Same prior-cycle join as the ON-watts attribution: a forecast only
    knows the cycle result already logged, never the one still computing.

    Args:
        walls: Anchor wall times in log order (None stays unassigned).
        timeline: ``(wall_time, states)`` in ascending order.

    Returns:
        One state list per wall time, aligned by index, or None for
        walls predating every cycle line.
    """
    return _prior_values(list(walls), list(timeline))


def _binpack_shed(
    on_watts: list[float], budget_wh: float, remaining_secs: int
) -> float:
    """Watts shed binpacking candidate order until the budget is covered.

    Mirrors the production turn_off loop: each load books
    ``watts * R / 3600`` and there is no fit check, so coverage can
    overshoot by up to one load.
    """
    shed = 0.0
    for watts in on_watts:
        if budget_wh <= 0:
            break
        shed += watts
        budget_wh -= watts * remaining_secs / 3600.0
    return shed


def audit_anchors(
    predicted_wh: float,
    prediction_w: float,
    target_wh: float,
    remaining_secs: int,
    plugs: list[PlugState],
    priorities: dict[str, int] | None = None,
) -> tuple[float, float]:
    """Shed watts under each policy for one anchor's decision state.

    Args:
        predicted_wh: Raw quarter prediction.
        prediction_w: Trailing rate behind it.
        target_wh: Quarter target (negative = export).
        remaining_secs: Horizon for savings math.
        plugs: Candidate plugs in production order (desired = ON now).
        priorities: Priority by plug name (production sorts eligible
            lowest-first); unknown names take the production default 0.

    Returns:
        ``(status_quo_watts, residual_watts)``.
    """
    raw_gap = target_wh - predicted_wh
    if raw_gap >= -HYSTERESIS_WH:
        return (0.0, 0.0)
    order = priorities or {}
    # Production sorts eligible lowest-priority-first (stable: ties keep
    # config order, which is also the candidate line order). No name
    # tiebreak: it would reorder equal-priority plugs away from config.
    eligible = sorted(
        (p for p in plugs if p.desired and p.name != SENTINEL_NAME),
        key=lambda p: order.get(p.name, 0),
    )
    smalls = [p for p in eligible if p.power_watts < LUMPY_MIN_WATTS]
    lumpies = sorted(
        (
            p for p in eligible
            if p.power_watts >= LUMPY_MIN_WATTS
            and abs(prediction_w) >= 0.5 * p.power_watts / 3600.0
        ),
        key=lambda p: p.power_watts,
        reverse=True,
    )
    status_quo = _binpack_shed(
        [p.power_watts for p in eligible],
        -raw_gap,
        remaining_secs,
    )
    small_cap_wh = sum(p.power_watts for p in smalls) * remaining_secs / 3600.0
    if lumpies and -raw_gap > small_cap_wh:
        lumpy_watts = sum(p.power_watts for p in lumpies)
        small_gap = target_wh - (
            predicted_wh - lumpy_watts * remaining_secs / 3600.0
        )
        residual_small = _binpack_shed(
            [p.power_watts for p in smalls], -small_gap, remaining_secs,
        ) if small_gap < -HYSTERESIS_WH else 0.0
        return (status_quo, lumpy_watts + residual_small)
    return (status_quo, status_quo)


def main(argv: list[str] | None = None) -> int:
    """Audit status-quo vs residual shed watts across log paths.

    Args:
        argv: Log file paths. Defaults to every ``*.log`` under ``bugs/``.

    Returns:
        Process exit status: 0 unless nothing auditable was found.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    paths = [Path(a) for a in args] if args else sorted((Path("bugs")).glob("*.log"))
    rows: list[tuple[str, float, float]] = []
    for path, text in _iter_log_texts(paths):
        rows.extend(_audit_rows(text, path.name))
    return _report(rows)


def _audit_rows(text: str, source: str) -> list[tuple[str, float, float]]:
    """Compare policies anchor by anchor for one log.

    Args:
        text: Full log contents.
        source: Identifier for quarter keys (usually the filename).

    Returns:
        ``(quarter_key, status_quo_watts, residual_watts)`` per scored
        anchor carrying plug states and a logged target, in log order.
    """
    anchors = parse_log(text, source=source)
    states = assign_plug_states(
        [a.wall_at for a in anchors], parse_plug_states(text, source=source)
    )
    priorities = parse_priorities(text)
    targets: dict[str, float] = {}
    for record in parse_cycles(text, source=source):
        if record.quarter_start is not None and record.target_wh is not None:
            targets[str(record.quarter_start)] = float(record.target_wh)
    rows: list[tuple[str, float, float]] = []
    for anchor, plug_states in zip(anchors, states):
        if anchor.actual_wh is None or plug_states is None:
            continue
        target = targets.get(str(anchor.quarter_start))
        if target is None:
            continue
        status_quo, residual = audit_anchors(
            anchor.predicted_wh, anchor.prediction_w, target,
            anchor.remaining_secs, plug_states, priorities,
        )
        rows.append((f"{anchor.source}|{anchor.quarter_start}", status_quo, residual))
    return rows


def _report(rows: list[tuple[str, float, float]]) -> int:
    """Print the divergence summary; 0 unless nothing was audited."""
    quarters = {key for key, _, _ in rows}
    diverged = {key for key, status_quo, residual in rows if status_quo != residual}
    opening: dict[str, float] = {}
    for key, status_quo, residual in rows:
        opening.setdefault(key, abs(status_quo - residual))
    less = sum(1 for _, status_quo, residual in rows if residual < status_quo)
    more = sum(1 for _, status_quo, residual in rows if residual > status_quo)
    deltas = [abs(status_quo - residual) for _, status_quo, residual in rows]
    print(f"audited {len(quarters)} quarter(s), {len(rows)} anchors")
    print(f"diverged quarters: {len(diverged)} ({len(deltas) - deltas.count(0.0)} anchors)")
    print(f"direction: residual sheds less x{less}, more x{more}")
    print(f"total shed-watts delta: {sum(deltas):.0f} W, "
          f"max single-anchor: {max(deltas, default=0.0):.0f} W")
    print(f"opening-anchor delta sum: {sum(opening.values()):.0f} W")
    return 0 if quarters else 1


if __name__ == "__main__":
    raise SystemExit(main())
