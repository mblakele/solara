"""Parity tests: pins the current StateTracker behaviour before refactoring.

These tests read the unchanged ``StateTracker`` and must stay green through
Subtask 3 (the wiring). They exercise every sequence from the plan's table
and assert store propagation.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from load_nbc import NBCReader, StateTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tracker(
    prediction_window_seconds: int = 30,
) -> StateTracker:
    return StateTracker(prediction_window_seconds=prediction_window_seconds)


# ---------------------------------------------------------------------------
# Basic commit after two consecutive calls
# ---------------------------------------------------------------------------


class TestBasicCommit:

    def test_commits_after_two_consecutive_calls(self) -> None:
        """A candidate window commits only after two consecutive calls."""
        tracker = _make_tracker()
        assert tracker.effective_settle_secs == 30
        tracker.apply_prediction_window(120)
        assert tracker.effective_settle_secs == 30  # not yet committed
        tracker.apply_prediction_window(120)
        assert tracker.effective_settle_secs == 120

    def test_shrinks_back_to_30(self) -> None:
        """Committed 120 shrinks to 30 after two confirmations."""
        tracker = _make_tracker(prediction_window_seconds=120)
        assert tracker.effective_settle_secs == 120
        tracker.apply_prediction_window(30)
        assert tracker.effective_settle_secs == 120
        tracker.apply_prediction_window(30)
        assert tracker.effective_settle_secs == 30

    def test_does_not_commit_on_single_value(self) -> None:
        """One observation never commits."""
        tracker = _make_tracker()
        tracker.apply_prediction_window(60)
        assert tracker.effective_settle_secs == 30


# ---------------------------------------------------------------------------
# Alternating values never commit
# ---------------------------------------------------------------------------


class TestAlternatingValues:

    def test_120_60_120_60_stays_at_30(self) -> None:
        """Alternating observations never reach two consecutive same values."""
        tracker = _make_tracker()
        for _ in range(4):
            tracker.apply_prediction_window(120)
            assert tracker.effective_settle_secs == 30
            tracker.apply_prediction_window(60)
            assert tracker.effective_settle_secs == 30

    def test_120_then_60_then_120(self) -> None:
        """Three values with no repeat stays uncommitted."""
        tracker = _make_tracker()
        tracker.apply_prediction_window(120)
        assert tracker.effective_settle_secs == 30
        tracker.apply_prediction_window(60)
        assert tracker.effective_settle_secs == 30
        tracker.apply_prediction_window(120)
        assert tracker.effective_settle_secs == 30


# ---------------------------------------------------------------------------
# Deadband ignores small changes
# ---------------------------------------------------------------------------


class TestDeadband:

    def test_ignores_121_and_119(self) -> None:
        """Committed 120 ignores ±1 s."""
        tracker = _make_tracker(prediction_window_seconds=120)
        assert tracker.effective_settle_secs == 120
        tracker.apply_prediction_window(121)
        assert tracker.effective_settle_secs == 120
        tracker.apply_prediction_window(121)
        assert tracker.effective_settle_secs == 120
        tracker.apply_prediction_window(119)
        assert tracker.effective_settle_secs == 120

    def test_115_is_candidate(self) -> None:
        """Committed 120 accepts 115 (diff=5, exactly deadband) as candidate."""
        tracker = _make_tracker(prediction_window_seconds=120)
        assert tracker.effective_settle_secs == 120
        tracker.apply_prediction_window(115)
        assert tracker.effective_settle_secs == 120  # not yet committed
        tracker.apply_prediction_window(115)
        assert tracker.effective_settle_secs == 115


# ---------------------------------------------------------------------------
# Store propagation on commit
# ---------------------------------------------------------------------------


class TestStorePropagation:

    def test_effects_and_settle_sync_on_commit(self) -> None:
        """After a commit both stores equal the committed value."""
        tracker = _make_tracker()
        assert tracker._effects.window_secs == 30
        assert tracker.settle.window_secs == 30
        tracker.apply_prediction_window(120)
        assert tracker._effects.window_secs == 30  # not yet committed
        assert tracker.settle.window_secs == 30
        tracker.apply_prediction_window(120)
        assert tracker.effective_settle_secs == 120
        assert tracker._effects.window_secs == 120
        assert tracker.settle.window_secs == 120

    def test_store_propagates_shrink(self) -> None:
        """Shrinking also propagates."""
        tracker = _make_tracker(prediction_window_seconds=120)
        assert tracker._effects.window_secs == 120
        assert tracker.settle.window_secs == 120
        tracker.apply_prediction_window(30)
        tracker.apply_prediction_window(30)
        assert tracker._effects.window_secs == 30
        assert tracker.settle.window_secs == 30


# ---------------------------------------------------------------------------
# has_pending_effect_since changes at new window
# ---------------------------------------------------------------------------


class TestHasPendingEffectSince:

    def test_answer_changes_at_new_window(self) -> None:
        """has_pending_effect_since() uses the committed window."""
        from load_nbc import PendingEffect

        tracker = _make_tracker()
        base_time = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        effect = PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=base_time,
            data_point_at=base_time,
            power_watts=500.0,
        )
        tracker.pending_effects.append(effect)

        # Commit to 60 first so we can compare.
        tracker.apply_prediction_window(60)
        tracker.apply_prediction_window(60)
        assert tracker.effective_settle_secs == 60

        nbc_timestamp = datetime(2025, 6, 1, 12, 0, 35, tzinfo=timezone.utc)

        # With window=60: effect age = (nbc_timestamp - base_time) = 35 s < 60 → pending
        assert tracker.has_pending_effect_since(nbc_timestamp) is True

        # Commit back to 30.
        tracker.apply_prediction_window(30)
        tracker.apply_prediction_window(30)
        assert tracker.effective_settle_secs == 30

        # With window=30: same scenario → still pending (35 > 30, so NOT pending)
        # Actually 35 s is > 30 s window, so it should be False now.
        assert tracker.has_pending_effect_since(nbc_timestamp) is False


# ---------------------------------------------------------------------------
# Sub-floor values: StateTracker has no floor; _resolve_prediction_window does
# ---------------------------------------------------------------------------


class TestSubFloorValues:

    def test_low_value_commits_in_tracker(self) -> None:
        """StateTracker commits whatever it's given — the floor is in _resolve_prediction_window."""
        tracker = _make_tracker()
        tracker.apply_prediction_window(10)
        assert tracker.effective_settle_secs == 30
        tracker.apply_prediction_window(10)
        # StateTracker has no minimum window; it commits 10.
        assert tracker.effective_settle_secs == 10

    def test_very_low_value_commits(self) -> None:
        """Passing 5 twice commits in StateTracker."""
        tracker = _make_tracker()
        tracker.apply_prediction_window(5)
        assert tracker.effective_settle_secs == 30
        tracker.apply_prediction_window(5)
        # The floor is enforced by LoadManager._resolve_prediction_window(), not here.
        assert tracker.effective_settle_secs == 5


# ---------------------------------------------------------------------------
# Equality reset clears candidate
# ---------------------------------------------------------------------------


class TestEqualityReset:

    def test_same_value_clears_candidate(self) -> None:
        """observe(committed) resets the candidate."""
        tracker = _make_tracker()
        tracker.apply_prediction_window(120)  # CANDIDATE
        assert tracker.effective_settle_secs == 30
        tracker.apply_prediction_window(30)  # same as committed → clears
        assert tracker.effective_settle_secs == 30

    def test_same_value_from_120_clears_candidate(self) -> None:
        """From committed 120, observe(120) clears any candidate."""
        tracker = _make_tracker(prediction_window_seconds=120)
        tracker.apply_prediction_window(60)  # CANDIDATE (60)
        assert tracker.effective_settle_secs == 120
        tracker.apply_prediction_window(120)  # same → clears
        assert tracker.effective_settle_secs == 120
