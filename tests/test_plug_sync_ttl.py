"""TTL-gated pre-gate plug sync during fast (5s) retry cycles.

The pre-gate dashboard poll (_sync_plugs_for_dashboard) must not hit
the plug controllers on every 5s waiting cycle. It syncs at most
once per config_interval_secs. A skipped pre-sync leaves
plug_pre_sync_done=False so the Stage 5 async phase re-syncs plug
state before any decision (via _async_sync_and_check_sentinel).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from clock import FakeClock
from load_controllers import PlugController
from load_manager import LoadManager, LoadManagerConfig
from load_models import CycleContext, PlugConfig
from tests.helpers import _make_metrics_with_wh


def _make_mgr(now: datetime) -> LoadManager:
    """Build a LoadManager with a counting stub plug controller."""
    plugs = {
        "heater": PlugConfig(
            name="heater", accessory_id="h1",
            power_watts=2000.0, priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)
    original = plug_ctrl.get_state
    plug_ctrl.get_state = AsyncMock(side_effect=original)  # type: ignore[method-assign]
    mgr = LoadManager(LoadManagerConfig(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=True,
        config_interval_secs=30,
    ))
    mgr._clock = FakeClock(now)
    return mgr


def _ctx(now: datetime) -> CycleContext:
    """Build a minimal CycleContext for the pre-gate sync."""
    ctx = CycleContext(now=now)
    ctx.now_postfetch = now
    ctx.predicted_wh = -2000.0
    return ctx


def _get_state_calls(mgr: LoadManager) -> int:
    """Return how many times the plug controller was polled."""
    return mgr.plug_ctrl.get_state.call_count  # type: ignore[attr-defined]


def test_first_pre_sync_polls_controllers() -> None:
    """First cycle always syncs: no prior sync timestamp exists."""
    now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
    mgr = _make_mgr(now)
    ctx = _ctx(now)

    mgr._sync_plugs_for_dashboard(ctx)

    assert _get_state_calls(mgr) == 1
    assert ctx.plug_pre_sync_done is True


def test_pre_sync_skipped_within_ttl() -> None:
    """A second cycle 5s later (inside the 30s TTL) does not poll.

    plug_pre_sync_done stays False so Stage 5 re-syncs before a
    decision instead of reusing stale state.
    """
    now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
    mgr = _make_mgr(now)
    mgr._sync_plugs_for_dashboard(_ctx(now))
    assert _get_state_calls(mgr) == 1

    later = now + timedelta(seconds=5)
    mgr._clock = FakeClock(later)  # type: ignore[assignment]
    ctx = _ctx(later)
    mgr._sync_plugs_for_dashboard(ctx)

    assert _get_state_calls(mgr) == 1
    assert ctx.plug_pre_sync_done is False


def test_pre_sync_resumes_after_ttl_expires() -> None:
    """Once the TTL has elapsed, the pre-gate poll runs again."""
    now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
    mgr = _make_mgr(now)
    mgr._sync_plugs_for_dashboard(_ctx(now))
    assert _get_state_calls(mgr) == 1

    later = now + timedelta(seconds=31)
    mgr._clock = FakeClock(later)  # type: ignore[assignment]
    ctx = _ctx(later)
    mgr._sync_plugs_for_dashboard(ctx)

    assert _get_state_calls(mgr) == 2
    assert ctx.plug_pre_sync_done is True


def test_stage5_sync_ignores_ttl() -> None:
    """Decision-time sync (_async_sync_and_check_sentinel) always polls.

    The TTL gates only the pre-gate dashboard poll; _sync_plug_states
    itself must stay ungated so a decision cycle always sees fresh
    plug state even when the pre-sync was skipped.
    """
    now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
    mgr = _make_mgr(now)
    mgr._sync_plugs_for_dashboard(_ctx(now))
    assert _get_state_calls(mgr) == 1

    later = now + timedelta(seconds=5)
    mgr._clock = FakeClock(later)  # type: ignore[assignment]
    mgr._sync_plugs_for_dashboard(_ctx(later))
    assert _get_state_calls(mgr) == 1

    asyncio.run(mgr._async_sync_and_check_sentinel())
    assert _get_state_calls(mgr) == 2
