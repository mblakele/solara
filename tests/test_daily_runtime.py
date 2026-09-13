"""Daily ON-time runtime tracking for Telegram turn_off alerts.

Red-phase tests: StateTracker.note_desired_transition() and
runtime_today_for() do not exist yet, so every test here must fail until
the DeviceState runtime fields + helpers are implemented.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config import Config
from load_nbc import StateTracker


@pytest.fixture()
def tracker() -> StateTracker:
    Config().set("TIMEZONE", "America/Los_Angeles")
    return StateTracker()


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestOnOffCredit:
    def test_on_off_credits_session(self, tracker: StateTracker) -> None:
        """ON -> OFF credits the session length (06:12 = 372 s)."""
        t_on = _utc(2026, 6, 15, 18, 0, 0)  # 11:00 PDT
        t_off = _utc(2026, 6, 15, 18, 6, 12)  # 11:06:12 PDT
        tracker.note_desired_transition("heater", True, t_on)
        tracker.note_desired_transition("heater", False, t_off)
        assert tracker.runtime_today_for("heater", t_off) == pytest.approx(372.0)

    def test_multiple_sessions_sum(self, tracker: StateTracker) -> None:
        """Two sessions on the same local day accumulate."""
        tracker.note_desired_transition("heater", True, _utc(2026, 6, 15, 18, 0, 0))
        tracker.note_desired_transition("heater", False, _utc(2026, 6, 15, 18, 2, 0))
        tracker.note_desired_transition("heater", True, _utc(2026, 6, 15, 19, 0, 0))
        tracker.note_desired_transition("heater", False, _utc(2026, 6, 15, 19, 5, 0))
        assert tracker.runtime_today_for(
            "heater", _utc(2026, 6, 15, 19, 5, 0)
        ) == pytest.approx(120.0 + 300.0)

    def test_open_session_included_in_read(self, tracker: StateTracker) -> None:
        """While ON, the read includes the still-open session."""
        t_on = _utc(2026, 6, 15, 18, 0, 0)
        tracker.note_desired_transition("heater", True, t_on)
        assert tracker.runtime_today_for(
            "heater", _utc(2026, 6, 15, 18, 1, 30)
        ) == pytest.approx(90.0)


class TestDayBoundary:
    def test_midnight_spanning_session_clips_to_new_day(
        self, tracker: StateTracker
    ) -> None:
        """ON 23:50 -> OFF 00:10 (local) credits only 10 min today."""
        # 2026-06-16 00:00 PDT == 2026-06-16 07:00 UTC.
        tracker.note_desired_transition("heater", True, _utc(2026, 6, 16, 6, 50, 0))
        t_off = _utc(2026, 6, 16, 7, 10, 0)  # 00:10 PDT
        tracker.note_desired_transition("heater", False, t_off)
        assert tracker.runtime_today_for("heater", t_off) == pytest.approx(600.0)

    def test_yesterday_total_does_not_leak(self, tracker: StateTracker) -> None:
        """A new local day starts from zero."""
        tracker.note_desired_transition("heater", True, _utc(2026, 6, 15, 18, 0, 0))
        tracker.note_desired_transition("heater", False, _utc(2026, 6, 15, 18, 6, 12))
        next_day = _utc(2026, 6, 16, 18, 0, 0)  # 11:00 PDT next day
        assert tracker.runtime_today_for("heater", next_day) == pytest.approx(0.0)


class TestEdgeCases:
    def test_unchanged_desired_is_noop(self, tracker: StateTracker) -> None:
        """Repeating OFF (or ON) does not double-credit."""
        tracker.note_desired_transition("heater", True, _utc(2026, 6, 15, 18, 0, 0))
        tracker.note_desired_transition("heater", False, _utc(2026, 6, 15, 18, 2, 0))
        tracker.note_desired_transition("heater", False, _utc(2026, 6, 15, 18, 9, 0))
        assert tracker.runtime_today_for(
            "heater", _utc(2026, 6, 15, 18, 9, 0)
        ) == pytest.approx(120.0)

    def test_first_sight_on_starts_session_without_backfill(
        self, tracker: StateTracker
    ) -> None:
        """First observation ON sets the session start; no invented history."""
        t_on = _utc(2026, 6, 15, 18, 0, 0)
        tracker.note_desired_transition("heater", True, t_on)
        assert tracker.runtime_today_for(
            "heater", _utc(2026, 6, 15, 18, 1, 0)
        ) == pytest.approx(60.0)

    def test_first_sight_off_starts_at_zero(self, tracker: StateTracker) -> None:
        """First observation OFF starts from zero (unknown prior ON-time)."""
        t = _utc(2026, 6, 15, 18, 0, 0)
        tracker.note_desired_transition("heater", False, t)
        assert tracker.runtime_today_for("heater", t) == pytest.approx(0.0)

    def test_unknown_device_reads_zero(self, tracker: StateTracker) -> None:
        """Reading a never-seen device returns 0."""
        assert tracker.runtime_today_for(
            "ghost", _utc(2026, 6, 15, 18, 0, 0)
        ) == pytest.approx(0.0)
