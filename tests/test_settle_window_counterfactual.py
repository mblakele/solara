"""Tests for the settle-window counterfactual tool.

The tool prices the *adaptive* prediction/settle window against a pinned one
on a log. It reaches decisions only through two consumers — the fresh-data
gate (``EffectStore.has_since``) and the post-amp-change suppression
(``_suppressed_by_settle``) — so these tests pin, on synthetic text and
constructed cycles: the parser, the two predicates, the arm comparison and
the report. No `corpus/` needed; the real logs stay a measurement input.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from load_models import PendingEffect
from load_nbc import EffectStore, TeslaSettleTracker
from tools.settle_window_counterfactual import (
    PINNED_DEFAULT_SECS,
    Cycle,
    _suppressed,
    compare,
    format_comparison,
    parse_cycles,
    replay,
)

UTC = "tzinfo=datetime.timezone.utc"
PDT = "tzinfo=<DstTzInfo 'America/Los_Angeles' PDT-1 day, 17:00:00 DST>"


def _dt(year: int, month: int, day: int, hour: int, minute: int, sec: int, tz: str) -> str:
    """A ``datetime.datetime(...)`` repr argument list."""
    return f"datetime.datetime({year}, {month}, {day}, {hour}, {minute}, {sec}, {tz})"


def _effect(
    device: str,
    action: str,
    stamp: str,
    point: str,
    power: float,
    *,
    target_amps: str = "None",
    direction: str = "None",
    suppress_action: str = "None",
    qh_name: str = "None",
) -> str:
    """One ``PendingEffect(...)`` repr for a synthetic log line."""
    return (
        f"PendingEffect(device_name='{device}', action='{action}', "
        f"timestamp={stamp}, data_point_at={point}, power_watts={power}, "
        f"target_amps={target_amps}, direction={direction}, "
        f"suppress_action={suppress_action}, qh_name={qh_name})"
    )


def _cycle_line(
    *,
    at: str,
    data_point: str,
    window: int,
    actions: tuple[str, ...] = (),
    status: str = "ok",
    gap: str = "-50.0",
    include_diagnostics: bool = True,
) -> str:
    """One synthetic ``Load management cycle result`` log line."""
    body = (
        f"CycleResult(status='{status}', qh='QH1', predicted_wh=-8.4, "
        f"adjusted_wh=-8.4, target_wh=-9, current_wh=None, estimated_wh=None, "
        f"actions=[{', '.join(actions)}]"
    )
    if include_diagnostics:
        body += (
            f", diagnostics=CycleDiagnostics(gap_wh={gap}, hysteresis_wh=3, "
            f"seconds_remaining=296, data_point_at={data_point}, reason='ok', "
            f"pending_effects_count=0, quantization_seconds=15, "
            f"settle_window_secs={window})"
        )
    body += ")"
    return f"[{at} +0000] [1] [DEBUG] app Load management cycle result: {body}"


def _plug_effect(
    *, at: datetime, point: datetime, device: str = "jackery"
) -> PendingEffect:
    """A plug turn-off effect with explicit UTC timestamps."""
    return PendingEffect(
        device_name=device,
        action="turn_off",
        timestamp=at,
        data_point_at=point,
        power_watts=-260.0,
    )


def _settle_effect(*, at: datetime, point: datetime, direction: str) -> PendingEffect:
    """A Tesla amp-change effect with a settle direction."""
    return PendingEffect(
        device_name="tesla",
        action="set_amps",
        timestamp=at,
        data_point_at=point,
        power_watts=240.0 if direction == "increase" else -240.0,
        target_amps=6,
        direction=direction,  # type: ignore[arg-type]
        suppress_action="turn_off" if direction == "increase" else "turn_on",
        qh_name="QH1",
    )


# ── parsing ──────────────────────────────────────────────────────────


def test_parse_cycles_reads_cycle_and_effect_fields() -> None:
    """Every field the counterfactual needs comes off the cycle-result repr."""
    line = _cycle_line(
        at="2026-10-12 19:01:00",
        data_point=_dt(2026, 10, 12, 12, 0, 31, PDT),
        window=15,
        gap="-50.0",
        actions=(
            _effect(
                "jackery",
                "turn_off",
                _dt(2026, 10, 12, 19, 0, 10, UTC),
                _dt(2026, 10, 12, 12, 0, 31, PDT),
                -260.0,
            ),
        ),
    )
    cycles = parse_cycles(line, "synthetic.log")
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.source == "synthetic.log"
    assert cycle.status == "ok"
    assert cycle.qh_name == "QH1"
    assert cycle.gap_wh == -50.0
    assert cycle.window == 15
    assert cycle.at == datetime(2026, 10, 12, 19, 1, tzinfo=timezone.utc)
    # The diagnostics data point is PDT-local; the tool stores it in UTC.
    assert cycle.data_point_at == datetime(2026, 10, 12, 19, 0, 31, tzinfo=timezone.utc)
    assert len(cycle.effects) == 1
    effect = cycle.effects[0]
    assert effect.device_name == "jackery"
    assert effect.action == "turn_off"
    assert effect.timestamp == datetime(2026, 10, 12, 19, 0, 10, tzinfo=timezone.utc)


def test_parse_cycles_keeps_tesla_settle_fields() -> None:
    """A set_amps effect's direction, suppress action and QH round-trip."""
    line = _cycle_line(
        at="2026-10-12 19:01:00",
        data_point=_dt(2026, 10, 12, 12, 0, 31, PDT),
        window=30,
        actions=(
            _effect(
                "tesla",
                "set_amps",
                _dt(2026, 10, 12, 19, 0, 40, UTC),
                _dt(2026, 10, 12, 12, 0, 16, PDT),
                240.0,
                target_amps="6",
                direction="'increase'",
                suppress_action="'turn_off'",
                qh_name="'QH1'",
            ),
        ),
    )
    effect = parse_cycles(line)[0].effects[0]
    assert effect.action == "set_amps"
    assert effect.target_amps == 6
    assert effect.direction == "increase"
    assert effect.suppress_action == "turn_off"
    assert effect.qh_name == "QH1"


def test_parse_cycles_skips_lines_without_diagnostics() -> None:
    """Only lines carrying the diagnostics segment are cycles."""
    line = _cycle_line(
        at="2026-10-12 19:01:00",
        data_point=_dt(2026, 10, 12, 12, 0, 31, PDT),
        window=30,
        include_diagnostics=False,
    )
    assert parse_cycles(line) == []


def test_parse_cycles_defaults_window_when_absent() -> None:
    """A diagnostics segment without a window falls back to the classic value."""
    line = (
        "[2026-10-12 19:01:00 +0000] [1] [DEBUG] app Load management cycle "
        "result: CycleResult(status='ok', qh='QH1', predicted_wh=-8.4, "
        "adjusted_wh=-8.4, target_wh=-9, current_wh=None, estimated_wh=None, "
        "actions=[], diagnostics=CycleDiagnostics(gap_wh=-50.0, hysteresis_wh=3, "
        f"seconds_remaining=296, data_point_at={_dt(2026, 10, 12, 12, 0, 31, PDT)}, "
        "reason='ok', pending_effects_count=0))"
    )
    cycles = parse_cycles(line)
    assert len(cycles) == 1
    assert cycles[0].window == PINNED_DEFAULT_SECS


# ── the fresh-data gate across the two arms ──────────────────────────


def _short_window_cycles() -> list[Cycle]:
    """A→B where a 15 s window lets B decide and a 30 s window defers B.

    Effect E is taken at 19:00:10 with a data point of 18:59:31. B's data
    point is 19:00:31: E is 21 s older than B's data point, so a 15 s window
    says "not waiting" and a 30 s window says "waiting".
    """
    effect = _plug_effect(
        at=datetime(2026, 10, 12, 19, 0, 10, tzinfo=timezone.utc),
        point=datetime(2026, 10, 12, 18, 59, 31, tzinfo=timezone.utc),
    )
    first = Cycle(
        source="s",
        at=datetime(2026, 10, 12, 19, 0, 12, tzinfo=timezone.utc),
        status="ok",
        qh_name="QH1",
        gap_wh=-50.0,
        data_point_at=datetime(2026, 10, 12, 18, 59, 31, tzinfo=timezone.utc),
        window=15,
        effects=(effect,),
    )
    second = Cycle(
        source="s",
        at=datetime(2026, 10, 12, 19, 1, 0, tzinfo=timezone.utc),
        status="ok",
        qh_name="QH1",
        gap_wh=-50.0,
        data_point_at=datetime(2026, 10, 12, 19, 0, 31, tzinfo=timezone.utc),
        window=15,
        effects=(),
    )
    return [first, second]


def test_short_window_skips_a_wait_the_pinned_window_takes() -> None:
    """The whole point: a committed 15 s window decides where 30 s defers."""
    cycles = _short_window_cycles()
    adaptive = replay(cycles, None)
    pinned = replay(cycles, PINNED_DEFAULT_SECS)
    assert adaptive[1].waits is False
    assert pinned[1].waits is True
    comparison = compare(cycles, PINNED_DEFAULT_SECS)
    assert comparison.wait_flips == 1
    assert comparison.suppress_flips == 0
    assert comparison.changed == (cycles[1],)
    assert comparison.quarters_changed == 1


def test_both_arms_agree_when_the_effect_is_far_behind() -> None:
    """An effect far older than either window is a wait under neither arm."""
    effect = _plug_effect(
        at=datetime(2026, 10, 12, 18, 58, 0, tzinfo=timezone.utc),
        point=datetime(2026, 10, 12, 18, 58, 0, tzinfo=timezone.utc),
    )
    seed = Cycle(
        source="s",
        at=datetime(2026, 10, 12, 18, 58, 5, tzinfo=timezone.utc),
        status="ok",
        qh_name="QH1",
        gap_wh=-50.0,
        data_point_at=datetime(2026, 10, 12, 18, 58, 0, tzinfo=timezone.utc),
        window=15,
        effects=(effect,),
    )
    later = Cycle(
        source="s",
        at=datetime(2026, 10, 12, 19, 1, 0, tzinfo=timezone.utc),
        status="ok",
        qh_name="QH1",
        gap_wh=-50.0,
        data_point_at=datetime(2026, 10, 12, 19, 0, 31, tzinfo=timezone.utc),
        window=15,
        effects=(),
    )
    assert replay([seed, later], None)[1].waits is False
    assert replay([seed, later], 30)[1].waits is False
    assert compare([seed, later], 30).wait_flips == 0


def test_windows_and_non_default_counted() -> None:
    """The comparison reports the committed window histogram."""
    cycles = _short_window_cycles() + [
        Cycle(
            source="s",
            at=datetime(2026, 10, 12, 19, 2, 0, tzinfo=timezone.utc),
            status="ok",
            qh_name="QH1",
            gap_wh=-50.0,
            data_point_at=datetime(2026, 10, 12, 19, 1, 31, tzinfo=timezone.utc),
            window=30,
            effects=(),
        )
    ]
    comparison = compare(cycles, 30)
    assert comparison.windows == {15: 2, 30: 1}
    assert comparison.non_default == 2


# ── the suppression predicate ────────────────────────────────────────


def test_settle_suppression_is_subsumed_by_the_fresh_data_gate() -> None:
    """An active settle always trips the gate, so suppression never decides.

    The predicate itself can fire (proven directly), but when it would,
    ``has_pending_effect_since`` is already true for the same cycle, and
    production exits on the gate before reaching the suppression.
    """
    effect = _settle_effect(
        at=datetime(2026, 10, 12, 19, 0, 40, tzinfo=timezone.utc),
        point=datetime(2026, 10, 12, 19, 0, 16, tzinfo=timezone.utc),
        direction="increase",
    )
    seed = Cycle(
        source="s",
        at=datetime(2026, 10, 12, 19, 0, 45, tzinfo=timezone.utc),
        status="ok",
        qh_name="QH1",
        gap_wh=-50.0,
        data_point_at=datetime(2026, 10, 12, 19, 0, 16, tzinfo=timezone.utc),
        window=30,
        effects=(effect,),
    )
    decide = Cycle(
        source="s",
        at=datetime(2026, 10, 12, 19, 1, 0, tzinfo=timezone.utc),
        status="ok",
        qh_name="QH1",
        gap_wh=-50.0,
        data_point_at=datetime(2026, 10, 12, 19, 0, 31, tzinfo=timezone.utc),
        window=30,
        effects=(),
    )
    # The predicate fires when evaluated on its own inputs ...
    store = EffectStore(window_secs=30)
    store.add(effect)
    tracker = TeslaSettleTracker(store, window_secs=30)
    assert _suppressed(tracker, decide) is True
    # ... but the fresh-data gate is already true for the same cycle, so the
    # suppression never decides alone (production exits on the gate first).
    verdict = replay([seed, decide], None)[1]
    assert verdict.waits is True
    assert verdict.suppressed is True
    assert not (verdict.suppressed and not verdict.waits)


def test_settle_suppression_excluded_when_the_gap_is_wide() -> None:
    """A genuine deficit over the suppress threshold is not suppressed."""
    effect = _settle_effect(
        at=datetime(2026, 10, 12, 19, 0, 40, tzinfo=timezone.utc),
        point=datetime(2026, 10, 12, 19, 0, 16, tzinfo=timezone.utc),
        direction="increase",
    )
    store = EffectStore(window_secs=30)
    store.add(effect)
    tracker = TeslaSettleTracker(store, window_secs=30)
    cycle = Cycle(
        source="s",
        at=datetime(2026, 10, 12, 19, 1, 0, tzinfo=timezone.utc),
        status="ok",
        qh_name="QH1",
        gap_wh=-500.0,
        data_point_at=datetime(2026, 10, 12, 19, 0, 31, tzinfo=timezone.utc),
        window=30,
        effects=(),
    )
    assert _suppressed(tracker, cycle) is False


# ── report + CLI ─────────────────────────────────────────────────────


def test_format_comparison_reports_totals_and_gate_counts() -> None:
    """The report names both arms' totals, not just the flips."""
    comparisons = [("synthetic.log", compare(_short_window_cycles(), 30))]
    out = format_comparison(comparisons, 30)
    assert "settle-window counterfactual — committed vs pinned 30 s" in out
    assert "synthetic.log" in out
    assert "total: 2 cycles, 2 non-default window, 1 changed decisions" in out
    assert "fresh-data gate fires: committed 0, pinned 1;" in out
    assert "settle suppression fires: committed 0, pinned 0" in out


def test_pinned_default_is_the_classic_window() -> None:
    """The default pinned arm is the pre-quantization 30 s window."""
    assert PINNED_DEFAULT_SECS == 30


def test_runs_as_a_standalone_script(tmp_path: Path) -> None:
    """`python tools/settle_window_counterfactual.py <log>` works from root."""
    log = tmp_path / "synthetic.log"
    log.write_text(
        _cycle_line(
            at="2026-10-12 19:01:00",
            data_point=_dt(2026, 10, 12, 12, 0, 31, PDT),
            window=15,
        )
    )
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "settle_window_counterfactual.py"),
            str(log),
        ],
        capture_output=True,
        text=True,
        cwd=root,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "settle-window counterfactual" in proc.stdout
