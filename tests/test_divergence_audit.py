"""Decision-divergence audit: status-quo vs residual small-plug budgets.

Compares, per scored turn_off-side anchor, the watts each policy would
shed: (a) status quo (raw gap, candidate-order binpack, no fit check) vs
(b) lumpy-first when implicated and commensurate + residual small-plug
budget. Sentinel is excluded by name (reserve, as production); Tesla is
out of scope (handled separately, idle in the incident quarters).

Watts, not names, are compared: the 260 W smalls are interchangeable,
so ordering unknowns between them cannot move the verdict. The only
order-sensitive case is heater-vs-small, which is exactly the divergence
class under test.
"""

from __future__ import annotations

import pytest

from tools.divergence_audit import (
    PlugState,
    audit_anchors,
    parse_plug_states,
    parse_priorities,
)


def _plugs(*states: tuple[str, float, bool]) -> list[PlugState]:
    """Plug states in production candidate order."""
    return [
        PlugState(name=name, power_watts=power, desired=desired)
        for name, power, desired in states
    ]


def test_parse_plug_states_collects_desired_per_cycle() -> None:
    """Each cycle line yields its candidates in line order."""
    line = (
        "[2026-10-14 23:16:40 +0000] [1] [DEBUG] app Load management cycle result: "
        "CycleResult(status='ok', candidates=["
        "CandidateDetail(device_type='plug', name='ecoflow', power_watts=260.0, "
        "capacity_wh=1.0, can_toggle=True, desired_state=False, actual_state=True), "
        "CandidateDetail(device_type='plug', name='water heater', power_watts=4857.0, "
        "capacity_wh=1.0, can_toggle=True, desired_state=True, actual_state=True), "
        "CandidateDetail(device_type='tesla', name='tesla', power_watts=None, "
        "capacity_wh=0.0, can_toggle=False, desired_state=None, actual_state=None)])"
    )
    ((wall, states),) = parse_plug_states(line, source="s.log")
    assert [(s.name, s.power_watts, s.desired) for s in states] == [
        ("ecoflow", 260.0, False),
        ("water heater", 4857.0, True),
    ]


def test_audit_agrees_when_no_lumpy_implicated() -> None:
    """Small deficit, heater idle (w far below heater scale): one small plug."""
    assert audit_anchors(
        predicted_wh=30.0, prediction_w=0.05, target_wh=-9.0,
        remaining_secs=800,
        plugs=_plugs(("ecoflow", 260.0, True), ("water heater", 4857.0, True)),
    ) == (260.0, 260.0)


def test_audit_finds_lumpy_first_divergence() -> None:
    """c817 shape: status quo takes ecoflow+heater, residual takes heater."""
    assert audit_anchors(
        predicted_wh=1137.0, prediction_w=1.26, target_wh=-9.0,
        remaining_secs=859,
        plugs=_plugs(
            ("ecoflow", 260.0, True),
            ("water heater", 4857.0, True),
            ("jackery", 260.0, True),
        ),
    ) == (5117.0, 4857.0)


def test_audit_agrees_when_heater_covers_fully() -> None:
    """c658 shape with margin-less residual: both take ecoflow+heater."""
    assert audit_anchors(
        predicted_wh=1162.0, prediction_w=1.295, target_wh=-9.0,
        remaining_secs=835,
        plugs=_plugs(
            ("ecoflow", 260.0, True),
            ("water heater", 4857.0, True),
            ("jackery", 260.0, True),
        ),
    ) == (5117.0, 5117.0)


def test_audit_sentinel_never_sheds() -> None:
    """The 10 W reserve is excluded from both policies."""
    assert audit_anchors(
        predicted_wh=50.0, prediction_w=0.2, target_wh=-9.0,
        remaining_secs=800,
        plugs=_plugs(("sentinel", 10.0, True)),
    ) == (0.0, 0.0)


def test_audit_skips_turn_on_side() -> None:
    """Export-side anchors decide nothing in either policy here."""
    assert audit_anchors(
        predicted_wh=-30.0, prediction_w=-0.1, target_wh=-9.0,
        remaining_secs=750,
        plugs=_plugs(("ecoflow", 260.0, False), ("jackery", 260.0, True)),
    ) == (0.0, 0.0)


def test_parse_priorities_reads_action_lines() -> None:
    """Action lines carry each device's config priority."""
    text = "\n".join([
        "[2026-10-14 23:16:08 +0000] [1] [INFO] load_nbc action=turn_off "
        "device=ecoflow savings=60.3 gap=1171.1 priority=50 [event=action]",
        "[2026-10-14 23:16:08 +0000] [1] [INFO] load_nbc action=turn_off "
        "device=water heater savings=1126.6 gap=1110.8 priority=60 [event=action]",
        "[2026-10-14 23:16:08 +0000] [1] [INFO] load_nbc action=turn_off "
        "device=tesla reason=deficit remaining=10.0",
    ])
    assert parse_priorities(text) == {"ecoflow": 50, "water heater": 60}


def test_audit_respects_priority_order_over_line_order() -> None:
    """Binpack follows config priority even when the line lists heater first."""
    assert audit_anchors(
        predicted_wh=100.0, prediction_w=0.05, target_wh=-9.0,
        remaining_secs=800,
        plugs=_plugs(("water heater", 4857.0, True), ("ecoflow", 260.0, True)),
        priorities={"ecoflow": 50, "water heater": 60},
    ) == (5117.0, 5117.0)


def test_parse_plug_states_dedupes_repeated_candidate_lists() -> None:
    """CycleResult reprs carry candidates twice; one state per plug wins."""
    entry = (
        "CandidateDetail(device_type='plug', name='water heater', "
        "power_watts=4857.0, capacity_wh=1.0, can_toggle=True, "
        "desired_state=True, actual_state=True)"
    )
    line = (
        "[2026-10-14 23:16:40 +0000] [1] [DEBUG] app Load management cycle result: "
        f"CycleResult(status='ok', candidates=[{entry}], "
        f"diagnostics=CycleDiagnostics(gap_wh=1.0, candidates=[{entry}]))"
    )
    ((_, states),) = parse_plug_states(line, source="s.log")
    assert [(s.name, s.power_watts, s.desired) for s in states] == [
        ("water heater", 4857.0, True)
    ]
