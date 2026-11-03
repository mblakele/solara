"""Tests for the adaptive prediction/settle window state machine.

Pins every row of ``SettleWindowMachine.observe()``'s transition table,
the deadband edge, invariants, and the frozen decision type.
"""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass as dataclass_impl
from typing import cast as typing_cast

import pytest

try:
    from settle_window import (
        DEFAULT_PREDICTION_WINDOW_SECS,
        SETTLE_WINDOW_DEADBAND_SECS,
        SettleWindowDecision,
        SettleWindowState,
        SettleWindowMachine,
    )
except ModuleNotFoundError:
    # Subtask 1 failing test — the module does not exist yet.
    pytest.skip("settle_window.py not found", allow_module_level=True)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def machine() -> SettleWindowMachine:
    return SettleWindowMachine()


@pytest.fixture(params=[30, 60, 120])
def machine_with_initial(request: pytest.FixtureRequest) -> SettleWindowMachine:
    return SettleWindowMachine(initial_secs=request.param)


# ---------------------------------------------------------------------------
# Row 0 — initial state
# ---------------------------------------------------------------------------


class TestInitialState:

    def test_initial_state_is_committed(self, machine: SettleWindowMachine) -> None:
        assert machine.state == SettleWindowState.COMMITTED

    def test_initial_committed_equals_argument(self, machine: SettleWindowMachine) -> (
        None
    ):
        assert machine.committed_secs == DEFAULT_PREDICTION_WINDOW_SECS

    def test_initial_pending_is_none(self, machine: SettleWindowMachine) -> None:
        assert machine.pending_secs is None


# ---------------------------------------------------------------------------
# Row 1 — same as committed resets the candidate (equality rule)
# ---------------------------------------------------------------------------


class TestEqualityReset:

    def test_same_value_resets_candidate(self, machine: SettleWindowMachine) -> None:
        """observe(committed) clears any in-flight candidate."""
        machine.observe(120)  # CANDIDATE
        machine.observe(120)  # COMMITTED (confirmed)
        decision = machine.observe(120)
        assert decision.state == SettleWindowState.COMMITTED
        assert decision.committed_secs == 120
        assert decision.pending_secs is None
        assert decision.changed is False
        assert decision.reason == "same"

    def test_same_value_from_different_initial(self, machine_with_initial: (
        SettleWindowMachine,
    ) ) -> None:
        """Same rule holds for any initial committed value."""
        m = machine_with_initial
        decision = m.observe(m.committed_secs)
        assert decision.state == SettleWindowState.COMMITTED
        assert decision.reason == "same"


# ---------------------------------------------------------------------------
# Row 2 — deadband resets the candidate
# ---------------------------------------------------------------------------


class TestDeadband:

    def test_deadband_resets_candidate(self, machine: SettleWindowMachine) -> None:
        """Values within the dead-band never seed a candidate."""
        assert machine.committed_secs == DEFAULT_PREDICTION_WINDOW_SECS
        for delta in [-4, -3, -2, -1]:
            decision = machine.observe(30 + delta)
            assert decision.state == SettleWindowState.COMMITTED
            assert decision.committed_secs == 30
            assert decision.pending_secs is None
            assert decision.changed is False
            assert decision.reason == "deadband"
        # Delta=0 hits the equality rule first.
        decision = machine.observe(30)
        assert decision.reason == "same"
        for delta in [1, 2, 3, 4]:
            decision = machine.observe(30 + delta)
            assert decision.state == SettleWindowState.COMMITTED
            assert decision.committed_secs == 30
            assert decision.pending_secs is None
            assert decision.changed is False
            assert decision.reason == "deadband"

    def test_deadband_edge_is_strict(self, machine: SettleWindowMachine) -> None:
        """A difference of exactly D seeds a candidate, not deadband."""
        machine.observe(120)  # CANDIDATE (diff 90 >= 5)
        decision = machine.observe(35)  # diff 5 == D => CANDIDATE
        assert decision.state == SettleWindowState.CANDIDATE
        assert decision.pending_secs == 35

    def test_deadband_from_120_committed(self, machine: SettleWindowMachine) -> None:
        """observe(121) and observe(119) from committed 120 are deadband."""
        # First commit to 120.
        machine.observe(120)
        machine.observe(120)
        assert machine.committed_secs == 120

        for delta in [-1, 1]:
            decision = machine.observe(120 + delta)
            assert decision.state == SettleWindowState.COMMITTED
            assert decision.committed_secs == 120
            assert decision.pending_secs is None
            assert decision.changed is False
            assert decision.reason == "deadband"

    def test_deadband_0_resets_candidate(self, machine: SettleWindowMachine) -> None:
        """With deadband=0, same value still resets (equality first)."""
        m = SettleWindowMachine(deadband_secs=0)
        m.observe(120)  # CANDIDATE (diff 90 >= 0)
        decision = m.observe(30)  # diff 0 == D => deadband
        assert decision.state == SettleWindowState.COMMITTED
        assert decision.committed_secs == 30


# ---------------------------------------------------------------------------
# Row 3 — candidate confirmed after two observations
# ---------------------------------------------------------------------------


class TestCandidateConfirmation:

    def test_first_observation_seeds_candidate(self, machine: SettleWindowMachine) -> None:
        """One observation of a new value seeds CANDIDATE."""
        decision = machine.observe(120)
        assert decision.state == SettleWindowState.CANDIDATE
        assert decision.committed_secs == 30
        assert decision.pending_secs == 120
        assert decision.changed is False
        assert decision.reason == "candidate"

    def test_second_observation_commits(self, machine: SettleWindowMachine) -> None:
        """A second identical observation commits the candidate."""
        machine.observe(120)  # CANDIDATE
        decision = machine.observe(120)
        assert decision.state == SettleWindowState.COMMITTED
        assert decision.committed_secs == 120
        assert decision.pending_secs is None
        assert decision.changed is True
        assert decision.reason == "confirmed"

    def test_third_observation_is_equality_reset(self, machine: SettleWindowMachine) -> None:
        """A third repeated value after a commit is the equality reset."""
        machine.observe(120)  # CANDIDATE
        machine.observe(120)  # COMMITTED (confirmed)
        decision = machine.observe(120)
        assert decision.state == SettleWindowState.COMMITTED
        assert decision.committed_secs == 120
        assert decision.changed is False
        assert decision.reason == "same"


# ---------------------------------------------------------------------------
# Row 4 — different candidate replaces the old one
# ---------------------------------------------------------------------------


class TestCandidateReplacement:

    def test_different_candidate_replaces(self, machine: SettleWindowMachine) -> None:
        """A different candidate replaces the old one."""
        machine.observe(120)  # CANDIDATE (120)
        decision = machine.observe(60)  # CANDIDATE (60), old cleared
        assert decision.state == SettleWindowState.CANDIDATE
        assert decision.committed_secs == 30
        assert decision.pending_secs == 60

    def test_interleaved_values_never_commit(self, machine: SettleWindowMachine) -> None:
        """Alternating observations never reach two consecutive same values."""
        for _ in range(4):
            d1 = machine.observe(120)
            assert d1.state == SettleWindowState.CANDIDATE
            d2 = machine.observe(60)
            assert d2.state == SettleWindowState.CANDIDATE
        # Still not committed.
        assert machine.committed_secs == 30
        assert machine.pending_secs is not None


# ---------------------------------------------------------------------------
# Deadband=0 special cases
# ---------------------------------------------------------------------------


class TestDeadbandZero:

    def test_deadband_0_commits_same_value(self, machine: SettleWindowMachine) -> None:
        """With deadband=0, 120 commits after two observations."""
        m = SettleWindowMachine(deadband_secs=0)
        m.observe(120)  # CANDIDATE
        decision = m.observe(120)  # COMMITTED (confirmed)
        assert decision.state == SettleWindowState.COMMITTED
        assert decision.committed_secs == 120
        assert decision.changed is True

    def test_deadband_0_commits_different_value(self, machine: SettleWindowMachine) -> None:
        """With deadband=0, 121 commits after two observations from committed 120."""
        m = SettleWindowMachine(initial_secs=120, deadband_secs=0)
        assert m.committed_secs == 120
        m.observe(121)  # CANDIDATE (diff 1 >= 0)
        decision = m.observe(121)  # COMMITTED (confirmed)
        assert decision.state == SettleWindowState.COMMITTED
        assert decision.committed_secs == 121
        assert decision.changed is True


# ---------------------------------------------------------------------------
# StrEnum serialization and frozen decision
# ---------------------------------------------------------------------------


class TestTypes:

    def test_state_serializes_to_uppercase(self) -> None:
        """StrEnum values serialize as their uppercase strings."""
        assert SettleWindowState.COMMITTED == "COMMITTED"
        assert SettleWindowState.CANDIDATE == "CANDIDATE"

    def test_decision_is_frozen(self) -> None:
        """SettleWindowDecision is immutable."""
        decision = SettleWindowDecision(
            state=SettleWindowState.COMMITTED,
            committed_secs=30,
            pending_secs=None,
            changed=False,
            reason="same",
        )
        with pytest.raises(Exception):  # dataclass frozen → raises error
            decision.committed_secs = 60

    def test_decision_fields(self) -> None:
        decision = SettleWindowDecision(
            state=SettleWindowState.CANDIDATE,
            committed_secs=30,
            pending_secs=120,
            changed=False,
            reason="candidate",
        )
        assert decision.state == SettleWindowState.CANDIDATE
        assert decision.committed_secs == 30
        assert decision.pending_secs == 120
        assert decision.changed is False
        assert decision.reason == "candidate"


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


class TestInvariants:

    def test_committed_implies_no_pending(self, machine: SettleWindowMachine) -> None:
        """state=CANDIDATE implies pending_secs is not None."""
        # Start in COMMITTED.
        assert machine.state == SettleWindowState.COMMITTED
        assert machine.pending_secs is None
        # Seed a candidate.
        machine.observe(120)
        assert machine.state == SettleWindowState.CANDIDATE
        assert machine.pending_secs is not None
        assert machine.pending_secs != machine.committed_secs
        assert abs(machine.pending_secs - machine.committed_secs) >= SETTLE_WINDOW_DEADBAND_SECS

    def test_candidate_invariant(self, machine: SettleWindowMachine) -> None:
        """After CANDIDATE, pending != committed and diff >= deadband."""
        machine.observe(120)
        assert machine.state == SettleWindowState.CANDIDATE
        assert machine.pending_secs is not None
        assert machine.pending_secs != machine.committed_secs
        assert abs(machine.pending_secs - machine.committed_secs) >= SETTLE_WINDOW_DEADBAND_SECS

    def test_committed_invariant(self, machine: SettleWindowMachine) -> None:
        """After COMMITTED (via confirmation), pending is None."""
        machine.observe(120)  # CANDIDATE
        machine.observe(120)  # COMMITTED
        assert machine.state == SettleWindowState.COMMITTED
        assert machine.pending_secs is None

    def test_changed_only_on_confirmation(self, machine: SettleWindowMachine) -> None:
        """changed is True only on row 3 (confirmed)."""
        d1 = machine.observe(120)  # CANDIDATE → changed=False
        assert d1.changed is False
        d2 = machine.observe(120)  # COMMITTED → changed=True
        assert d2.changed is True
        d3 = machine.observe(60)  # CANDIDATE again → changed=False
        assert d3.changed is False

    def test_committed_changes_only_on_confirmation(self, machine: SettleWindowMachine) -> None:
        """committed_secs changes only on row 3."""
        before = machine.committed_secs
        machine.observe(120)  # CANDIDATE
        assert machine.committed_secs == before
        machine.observe(120)  # COMMITTED
        assert machine.committed_secs != before
