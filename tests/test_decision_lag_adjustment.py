"""Execution-lag correction for the decide-time quarter remainder.

At c658 in bugs/2026-10-14-water-heater-overshoot.log the decision booked
plug savings for 835 s (wall-clock remaining at fetch start) but the plugs
were only off from ~831 s out: fetch 3.1 s + plug/Tesla sync ~0.7 s of
execution lag is unaccounted, overstating heater + ecoflow savings by
~5.7 Wh (~40% of the +14.09 miss). The data lag IS already handled
(sample R 840 -> wall-clock 835); only the post-fetch execution lag leaks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

from clock import FakeClock
from load_controllers import PlugController
from load_manager import LoadManager, LoadManagerConfig
from load_models import PlugConfig


def _make_mgr(now: datetime) -> LoadManager:
    """Build a LoadManager with one stub plug on a FakeClock."""
    plugs = {
        "heater": PlugConfig(
            name="heater", accessory_id="h1",
            power_watts=4857.0, priority=60,
        ),
    }
    return LoadManager(LoadManagerConfig(
        plug_ctrl=PlugController(plugs),
        tesla_ctrl=None,
        target_wh=-9,
        nbc_device="main_panel",
        enabled=True,
        dry_run=True,
        clock=FakeClock(now),
    ))


class TestEffectiveSecondsRemaining:
    """_effective_seconds_remaining() shrinks the fetch-start remainder."""

    def test_zero_lag_unchanged(self) -> None:
        """No elapsed time keeps the fetch-start remainder."""
        now = datetime(2026, 10, 14, 23, 16, 5, tzinfo=timezone.utc)
        mgr = _make_mgr(now)
        assert mgr._effective_seconds_remaining(835, 0.0) == 835

    def test_execution_lag_shrinks_remainder(self) -> None:
        """c658 scale: ~4 s of post-fetch lag removes ~4 s of bookable time."""
        now = datetime(2026, 10, 14, 23, 16, 5, tzinfo=timezone.utc)
        mgr = _make_mgr(now)
        assert mgr._effective_seconds_remaining(835, 4.0) == 831

    def test_negative_elapsed_never_grows(self) -> None:
        """Clock skew must not extend the remainder past fetch start."""
        now = datetime(2026, 10, 14, 23, 16, 5, tzinfo=timezone.utc)
        mgr = _make_mgr(now)
        assert mgr._effective_seconds_remaining(835, -2.0) == 835

    def test_clamps_at_zero(self) -> None:
        """Lag past the boundary floors at zero instead of going negative."""
        now = datetime(2026, 10, 14, 23, 16, 5, tzinfo=timezone.utc)
        mgr = _make_mgr(now)
        assert mgr._effective_seconds_remaining(5, 10.0) == 0


class TestAsyncPhaseUsesEffectiveRemaining:
    """_cycle_async_phase_body decides on the lag-adjusted remainder."""

    def test_decide_sees_postfetch_minus_lag(self) -> None:
        """4 s of sync lag between postfetch and decide shrinks 835 to 831."""
        now = datetime(2026, 10, 14, 23, 16, 5, tzinfo=timezone.utc)
        mgr = _make_mgr(now)
        captured: dict[str, object] = {}

        real_fetch = mgr._fetch_tesla_state_async

        async def advancing_fetch(*args, **kwargs):  # type: ignore[no-untyped-def]
            mgr._clock.advance(4)  # noqa: SLF001
            return await real_fetch(*args, **kwargs)

        def capture(*args, **_kwargs):  # type: ignore[no-untyped-def]
            captured["seconds_remaining"] = args[4]
            return []

        with (
            patch.object(
                mgr, "_fetch_tesla_state_async", side_effect=advancing_fetch,
            ),
            patch.object(mgr, "_decide_actions", side_effect=capture),
        ):
            asyncio.run(mgr._cycle_async_phase_body(
                -1171.0, 1162.0, now, 835, True,
                data_point_at=now,
            ))

        assert captured["seconds_remaining"] == 831
