"""Unit tests for the REST arbitration verdict state machine.

The machine owns the verdict and the poll clock. Callers pass ``now`` and
report events. These tests pin every row of the transition table, the
cooldown boundary, the invariants, and the change-only audit log.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

import pytest

from tesla_arbitration import ArbitrationState, RestArbitrationMachine

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)


def _machine(cooldown_secs: int = 300) -> RestArbitrationMachine:
    """Build a machine with a small, explicit cooldown."""
    return RestArbitrationMachine(cooldown_secs=cooldown_secs)


def _check_invariants(machine: RestArbitrationMachine) -> None:
    """Assert the state and clock invariants."""
    if machine.state == ArbitrationState.UNKNOWN:
        assert machine.last_poll_at is None
    if machine.state == ArbitrationState.CHARGING:
        assert machine.last_poll_at is not None


def test_initial_state_is_unknown() -> None:
    """A new machine has no verdict, no poll, and allows a poll."""
    machine = _machine()
    assert machine.state == ArbitrationState.UNKNOWN
    assert machine.last_poll_at is None
    assert machine.needs_poll(NOW) is True
    assert machine.sustains(NOW) is False
    _check_invariants(machine)


def test_charging_answer_latches_and_starts_cooldown() -> None:
    """A charging answer sets the verdict and the poll clock."""
    machine = _machine()
    machine.record_answer(NOW, charging=True)
    assert machine.state == ArbitrationState.CHARGING
    assert machine.last_poll_at == NOW
    assert machine.sustains(NOW) is True
    assert machine.needs_poll(NOW) is False
    _check_invariants(machine)


def test_cooldown_boundary_is_exact() -> None:
    """At exactly the cooldown, sustain stops and a poll is allowed."""
    machine = _machine()
    machine.record_answer(NOW, charging=True)
    inside = NOW + timedelta(seconds=299.999)
    boundary = NOW + timedelta(seconds=300)
    assert machine.sustains(inside) is True
    assert machine.sustains(boundary) is False
    assert machine.needs_poll(inside) is False
    assert machine.needs_poll(boundary) is True


def test_idle_answer_latches_and_cools_down() -> None:
    """A definitive idle answer also starts the anti-storm cooldown."""
    machine = _machine()
    machine.record_answer(NOW, charging=False)
    assert machine.state == ArbitrationState.IDLE
    assert machine.last_poll_at == NOW
    assert machine.sustains(NOW) is False
    assert machine.needs_poll(NOW) is False
    assert machine.needs_poll(NOW + timedelta(seconds=300)) is True
    _check_invariants(machine)


@pytest.mark.parametrize("amps", [0, -1, None])
def test_non_positive_amps_clear_charging_and_keep_clock(
    amps: int | None,
) -> None:
    """Absent or non-positive amps end the session but keep the poll clock."""
    machine = _machine()
    machine.record_answer(NOW, charging=True)
    poll_at = machine.last_poll_at
    machine.observe_amps(amps, now=NOW + timedelta(seconds=10))
    assert machine.state == ArbitrationState.IDLE
    assert machine.last_poll_at == poll_at
    assert machine.needs_poll(NOW + timedelta(seconds=10)) is False
    _check_invariants(machine)


def test_positive_amps_keep_charging() -> None:
    """Positive amps do not change a charging verdict."""
    machine = _machine()
    machine.record_answer(NOW, charging=True)
    machine.observe_amps(11, now=NOW)
    assert machine.state == ArbitrationState.CHARGING
    assert machine.last_poll_at == NOW


def test_corroborated_idle_clears_and_keeps_clock() -> None:
    """Authoritative idle telemetry ends the session."""
    machine = _machine()
    machine.record_answer(NOW, charging=True)
    machine.note_corroborated_idle(now=NOW + timedelta(seconds=10))
    assert machine.state == ArbitrationState.IDLE
    assert machine.last_poll_at == NOW
    _check_invariants(machine)


def test_commanded_change_clears_and_keeps_clock() -> None:
    """Our own command ends the session so a trim cannot restart the car."""
    machine = _machine()
    machine.record_answer(NOW, charging=True)
    machine.note_commanded_change(now=NOW + timedelta(seconds=10))
    assert machine.state == ArbitrationState.IDLE
    assert machine.last_poll_at == NOW
    _check_invariants(machine)


@pytest.mark.parametrize("seed", ["unknown", "charging", "idle"])
def test_transport_failure_is_a_noop(seed: str) -> None:
    """A failed poll is not an answer: state and clock stay put."""
    machine = _machine()
    if seed == "charging":
        machine.record_answer(NOW, charging=True)
    elif seed == "idle":
        machine.record_answer(NOW, charging=False)
    before_state = machine.state
    before_clock = machine.last_poll_at
    machine.record_transport_failure()
    assert machine.state == before_state
    assert machine.last_poll_at == before_clock
    _check_invariants(machine)


def test_cleared_verdict_still_blocks_a_poll() -> None:
    """Clearing the verdict does not reset the cooldown clock."""
    machine = _machine()
    machine.record_answer(NOW, charging=True)
    machine.observe_amps(0, now=NOW + timedelta(seconds=1))
    assert machine.state == ArbitrationState.IDLE
    assert machine.needs_poll(NOW + timedelta(seconds=299)) is False
    assert machine.needs_poll(NOW + timedelta(seconds=300)) is True


def test_enum_serializes_as_lowercase_strings() -> None:
    """StrEnum values serialize as their lowercase names."""
    assert ArbitrationState.UNKNOWN == "unknown"
    assert ArbitrationState.CHARGING.value == "charging"
    assert str(ArbitrationState.IDLE) == "idle"


def test_logs_one_line_per_verdict_change(caplog: pytest.LogCaptureFixture) -> None:
    """Only a verdict change emits a transition line."""
    machine = _machine()
    with caplog.at_level(logging.DEBUG, logger="tesla_arbitration"):
        machine.record_answer(NOW, charging=True)
        machine.record_answer(NOW + timedelta(seconds=1), charging=True)
        machine.observe_amps(0, now=NOW + timedelta(seconds=2))
        machine.observe_amps(0, now=NOW + timedelta(seconds=3))
    transitions = [
        record.getMessage()
        for record in caplog.records
        if record.name == "tesla_arbitration" and " -> " in record.getMessage()
    ]
    assert len(transitions) == 2
    assert "unknown -> charging" in transitions[0]
    assert "charging -> idle" in transitions[1]


def test_transport_failure_logs_without_changing_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed poll has a named audit line but no transition."""
    machine = _machine()
    with caplog.at_level(logging.DEBUG, logger="tesla_arbitration"):
        machine.record_transport_failure()
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "tesla_arbitration"
    ]
    assert any("transport_failure" in message for message in messages)
    assert machine.state == ArbitrationState.UNKNOWN
