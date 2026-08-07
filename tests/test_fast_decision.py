"""Tests for LoadManager.run_decision_cycle() — the fast decision loop.

The fast decision loop re-evaluates cached metrics (frozen between
fetches) on a ~5s cadence so loads fit as the quarter-hour shrinks and
gates clear faster, while the slow loop remains the only fetcher. The
contract under test: ``run_decision_cycle`` NEVER triggers a metrics
fetch, holds when data is stale or pending effects are unreflected, and
otherwise produces the same actions as the full ``run_cycle`` path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch

import pytz

from clock import FakeClock
from energy_cache import EnergyCache
from load_controllers import PlugController
from load_manager import LoadManager, LoadManagerConfig
from load_models import DeviceState, PendingEffect, PlugConfig

FIXED_NOW = datetime(2026, 5, 7, 15, 20, 30, tzinfo=timezone.utc)


def _make_energy_cache_with_prediction(
    predicted_wh: float,
    now: datetime,
    data_lag_secs: float = 0.0,
    ttl_seconds: int = 30,
) -> EnergyCache:
    """Create an EnergyCache pre-populated with samples producing a prediction.

    With constant per-second samples the extrapolated NBC quarter
    prediction equals ``predicted_wh`` regardless of sample count, so the
    cache read is stable across the shrinking-QH window.

    Args:
        predicted_wh: Target Wh prediction for the incomplete quarter.
        now: Current time for sample timestamps.
        data_lag_secs: Simulated API lag in seconds.
        ttl_seconds: Cache TTL; defaults to 30 to match production.

    Returns:
        EnergyCache with samples covering the current incomplete QH.
    """
    sample_value = predicted_wh / 900_000.0

    # Align data_start to the start of the current QH window so the samples
    # cover an incomplete quarter (len % 900 != 0).
    qh_minute = (now.minute // 15) * 15
    data_start = now.replace(minute=qh_minute, second=0, microsecond=0)
    sample_count = max(1, int((now - data_start).total_seconds()))

    cache = EnergyCache(ttl_seconds=ttl_seconds)
    samples = [sample_value] * sample_count
    with cache._lock:
        cache.samples = samples
        cache.data_start = data_start
        cache.last_sample_at = now - timedelta(seconds=1)
        cache.sample_count = sample_count
        cache.last_fetch_at = now
        cache._set_data_field(data_lag_secs=data_lag_secs)
    return cache


def _make_plug_manager(
    now: datetime,
    predicted_wh: float,
    plugs: dict[str, PlugConfig] | None = None,
    clock: FakeClock | None = None,
    dry_run: bool = False,
    ttl_seconds: int = 30,
    data_lag_secs: float = 0.0,
    enabled: bool | tuple[time, time] = True,
) -> tuple[LoadManager, PlugController, list[int]]:
    """Create a LoadManager with a stub plug controller and populated cache.

    Args:
        now: Fixed time for the cache and clock.
        predicted_wh: Prediction for the incomplete quarter.
        plugs: Plug configs; defaults to a single 2400 W pool_pump.
        clock: FakeClock seeded at ``now``.
        dry_run: Whether decisions should execute against the stub.
        ttl_seconds: Cache TTL.
        data_lag_secs: Simulated API lag in seconds.
        enabled: Enabled value — True, False, or a (start, end) time range
            tuple like ``_parse_load_manage_enabled("06:00-21:00")``.

    Returns:
        Tuple of (LoadManager, PlugController, fetch_count list).
    """
    if plugs is None:
        plugs = {
            "pool_pump": PlugConfig(
                name="pool_pump",
                accessory_id="xyz789",
                power_watts=2400.0,
                priority=10,
            ),
        }
    plug_ctrl = PlugController(plugs)
    if clock is None:
        clock = FakeClock(now)

    energy_cache = _make_energy_cache_with_prediction(
        predicted_wh, now=now, ttl_seconds=ttl_seconds,
        data_lag_secs=data_lag_secs,
    )
    fetch_count: list[int] = []

    def metrics_fetch() -> dict | None:
        fetch_count.append(1)
        return None

    mgr = LoadManager(LoadManagerConfig(
        metrics_fetch=metrics_fetch,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=enabled,
        dry_run=dry_run,
        clock=clock,
    ))
    return mgr, plug_ctrl, fetch_count


def _assert_never_fetched(fetch_count: list[int]) -> None:
    """Assert the metrics_fetch callable was never invoked."""
    assert fetch_count == [], (
        "run_decision_cycle must never call metrics_fetch "
        f"(called {len(fetch_count)} times)"
    )


def test_fast_cycle_no_cache_returns_none():
    """Empty cache → None (hold), and metrics_fetch is never called."""
    now = FIXED_NOW
    clock = FakeClock(now)
    fetch_count: list[int] = []

    def metrics_fetch() -> dict | None:
        fetch_count.append(1)
        return None

    mgr = LoadManager(LoadManagerConfig(
        metrics_fetch=metrics_fetch,
        energy_cache=EnergyCache(ttl_seconds=30),
        plug_ctrl=PlugController({}),
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        clock=clock,
    ))

    result = mgr.run_decision_cycle(now=now)

    assert result is None
    _assert_never_fetched(fetch_count)


def test_fast_cycle_holds_when_data_stale():
    """Data point older than STALE_DATA_THRESHOLD_SECS → stale_data hold."""
    now = FIXED_NOW
    # data_lag 130s → data_point_at = now - 130s → age exceeds the 80s gate.
    mgr, _plug_ctrl, fetch_count = _make_plug_manager(
        now, predicted_wh=-2000.0, data_lag_secs=130.0,
    )

    result = mgr.run_decision_cycle(now=now)

    assert result is not None
    assert result.status == "stale_data"
    assert result.actions == []
    _assert_never_fetched(fetch_count)


def test_fast_cycle_holds_when_pending_effects():
    """Effect recorded after the data point → waiting_for_fresh_data hold."""
    now = FIXED_NOW
    mgr, _plug_ctrl, fetch_count = _make_plug_manager(now, predicted_wh=-2000.0)
    mgr.state.pending_effects.append(PendingEffect(
        device_name="pool_pump",
        action="turn_on",
        timestamp=now - timedelta(seconds=5),
        data_point_at=now - timedelta(seconds=1),
        power_watts=2400.0,
    ))

    result = mgr.run_decision_cycle(now=now)

    assert result is not None
    assert result.status == "waiting_for_fresh_data"
    assert result.actions == []
    _assert_never_fetched(fetch_count)


def test_fast_cycle_fits_within_shrinking_qh():
    """The core value: a load too large at t1 fits at t1+25s, no fetch.

    With a fixed cached prediction (-1056 Wh) and a 2400 W plug:
    edge_gap = 390 Wh; capacity = power * seconds_remaining / 3600.
    At t1 (600 s left) capacity is 400 Wh > 390 → too large, no action.
    At t2 (575 s left) capacity is ~383 Wh <= 390 → turn_on fires even
    though no fresh data arrived.
    """
    t1 = datetime(2026, 5, 7, 15, 20, 0, tzinfo=timezone.utc)
    clock = FakeClock(t1)
    mgr, _plug_ctrl, fetch_count = _make_plug_manager(
        t1, predicted_wh=-1056.0, clock=clock,
    )

    result1 = mgr.run_decision_cycle(now=t1)
    assert result1 is not None
    assert result1.actions == [], "plug too large at t1 — no action expected"
    assert result1.diagnostics.reason == "no_candidates"

    t2 = t1 + timedelta(seconds=25)
    clock.advance(25)
    result2 = mgr.run_decision_cycle(now=t2)
    assert result2 is not None
    assert any(
        a.device_name == "pool_pump" and a.action == "turn_on"
        for a in result2.actions
    ), "plug must fit at t2 — turn_on expected"
    _assert_never_fetched(fetch_count)


def test_fast_cycle_respects_debounce():
    """Turn-on debounce gates decisions; actions fire after MIN_TOGGLE_ON_SECS."""
    t1 = FIXED_NOW
    clock = FakeClock(t1)
    mgr, _plug_ctrl, fetch_count = _make_plug_manager(
        t1, predicted_wh=-1200.0, clock=clock, ttl_seconds=180,
    )
    # Plug toggled 10 s ago → still inside the 60 s turn-on debounce.
    mgr.state.set_device(
        "pool_pump",
        DeviceState(
            name="pool_pump",
            last_toggle=t1 - timedelta(seconds=10),
            desired_state=False,
        ),
    )

    result1 = mgr.run_decision_cycle(now=t1)
    assert result1 is not None
    assert result1.actions == [], "debounce still active — no action expected"

    # Advance past MIN_TOGGLE_ON_SECS (60 s); cache TTL 180 s keeps data valid.
    clock.advance(65)
    result2 = mgr.run_decision_cycle(now=t1 + timedelta(seconds=65))
    assert result2 is not None
    assert any(
        a.device_name == "pool_pump" and a.action == "turn_on"
        for a in result2.actions
    ), "debounce expired — turn_on expected"
    _assert_never_fetched(fetch_count)


def test_fast_cycle_never_fetches_across_cycles():
    """Several consecutive decision cycles never invoke metrics_fetch."""
    t1 = FIXED_NOW
    clock = FakeClock(t1)
    mgr, _plug_ctrl, fetch_count = _make_plug_manager(
        t1, predicted_wh=-2000.0, clock=clock, ttl_seconds=180,
    )

    for seconds in range(0, 60, 5):
        now = t1 + timedelta(seconds=seconds)
        clock.advance(5)
        # Result may be None (hold) or a CycleResult — but never a fetch.
        mgr.run_decision_cycle(now=now)

    _assert_never_fetched(fetch_count)


def test_fast_cycle_parity_with_run_cycle():
    """run_decision_cycle matches run_cycle actions on identical inputs.

    With a valid cache and no pending effects, both paths run the same
    decision stages on the same prediction; run_cycle(force=False) reads
    the cache via the same cache-hit branch (get_current_qh_cached).
    """
    now = FIXED_NOW
    mgr_a, _plug_ctrl, _fetch_a = _make_plug_manager(now, predicted_wh=-2000.0)
    mgr_b, _plug_ctrl, fetch_b = _make_plug_manager(now, predicted_wh=-2000.0)

    result_a = mgr_a.run_cycle(force=False)
    result_b = mgr_b.run_decision_cycle(now=now)

    assert result_a is not None
    assert result_b is not None
    assert result_a.status == result_b.status
    assert len(result_a.actions) == len(result_b.actions)
    assert [a.device_name for a in result_a.actions] == [
        a.device_name for a in result_b.actions
    ]
    assert [a.action for a in result_a.actions] == [
        a.action for a in result_b.actions
    ]
    _assert_never_fetched(fetch_b)


def test_stage_nbc_read_cache_populates_ctx():
    """_stage_nbc_read_cache populates ctx fields from the cache only."""
    now = FIXED_NOW
    mgr, _plug_ctrl, fetch_count = _make_plug_manager(now, predicted_wh=-2000.0)
    from load_models import CycleContext

    ctx = CycleContext(now=now, force=False)
    got_data = mgr._stage_nbc_read_cache(ctx)

    assert got_data is True
    assert ctx.qh_name == "QH1"
    assert ctx.predicted_wh == -2000.0
    assert ctx.seconds_remaining == 570
    assert ctx.data_point_at == now
    assert ctx.now_postfetch == now
    _assert_never_fetched(fetch_count)


def test_stage_nbc_read_cache_returns_false_on_empty_cache():
    """_stage_nbc_read_cache returns False when the cache holds no data."""
    now = FIXED_NOW
    fetch_count: list[int] = []

    def metrics_fetch() -> dict | None:
        fetch_count.append(1)
        return None

    mgr = LoadManager(LoadManagerConfig(
        metrics_fetch=metrics_fetch,
        energy_cache=EnergyCache(ttl_seconds=30),
        plug_ctrl=PlugController({}),
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        clock=FakeClock(now),
    ))
    from load_models import CycleContext

    ctx = CycleContext(now=now, force=False)
    got_data = mgr._stage_nbc_read_cache(ctx)

    assert got_data is False
    assert ctx.qh_name is None
    _assert_never_fetched(fetch_count)


# --- Time-range (LOAD_MANAGE_ENABLED=HH:MM-HH:MM) tests ---


def _la_time(hour: int, minute: int = 0) -> datetime:
    """Return a UTC datetime corresponding to the given America/Los_Angeles wall time."""
    tz = pytz.timezone("America/Los_Angeles")
    return tz.localize(datetime(2025, 6, 15, hour, minute)).astimezone(timezone.utc)


@patch("device_config.get_timezone", return_value="America/Los_Angeles")
def test_fast_cycle_disabled_outside_time_range(mock_tz):
    """run_decision_cycle holds (disabled) outside LOAD_MANAGE_ENABLED window.

    Mirrors ``LOAD_MANAGE_ENABLED=06:00-18:00``: at 03:00 device-local the
    fast loop must refuse to decide even with a valid, fresh cache — and
    it must never fetch to discover that.
    """
    outside = _la_time(3, 0)
    clock = FakeClock(outside)
    mgr, _plug_ctrl, fetch_count = _make_plug_manager(
        outside, predicted_wh=-2000.0, clock=clock,
        enabled=(time(6, 0), time(18, 0)),
    )

    result = mgr.run_decision_cycle(now=outside)

    assert result is not None
    assert result.status == "disabled"
    assert "outside_time_range" in result.diagnostics.reason
    _assert_never_fetched(fetch_count)


@patch("device_config.get_timezone", return_value="America/Los_Angeles")
def test_fast_cycle_proceeds_inside_time_range(mock_tz):
    """run_decision_cycle decides normally inside the window.

    At 12:05 device-local the fast loop proceeds past the enabled check
    and evaluates the cached prediction (here a no-candidate hold), never
    fetching metrics.
    """
    inside = _la_time(12, 5)
    clock = FakeClock(inside)
    mgr, _plug_ctrl, fetch_count = _make_plug_manager(
        inside, predicted_wh=-2000.0, clock=clock,
        enabled=(time(6, 0), time(18, 0)),
    )

    result = mgr.run_decision_cycle(now=inside)

    assert result is not None
    assert result.status != "disabled"
    _assert_never_fetched(fetch_count)


@patch("device_config.get_timezone", return_value="America/Los_Angeles")
def test_fast_cycle_parity_with_run_cycle_outside_range(mock_tz):
    """Both loops agree on the disabled decision outside the window.

    The fast decision cycle and the legacy run_cycle share the enabled
    check; both must report disabled with the same status when the
    configured time window is closed.
    """
    outside = _la_time(3, 0)
    mgr_fast, _plug_ctrl, fetch_count = _make_plug_manager(
        outside, predicted_wh=-2000.0,
        enabled=(time(6, 0), time(18, 0)),
    )

    fast_result = mgr_fast.run_decision_cycle(now=outside)
    slow_result = mgr_fast.run_cycle(force=False)

    assert fast_result is not None
    assert slow_result is not None
    assert fast_result.status == "disabled"
    assert fast_result.status == slow_result.status
    assert fast_result.diagnostics.reason == slow_result.diagnostics.reason
    _assert_never_fetched(fetch_count)


# --- Async-phase timeout (head-of-line blocking fix) ---


async def _hang_async_phase(*args, **kwargs):
    """Async phase that never completes — simulates a stuck controller.

    The real ``_cycle_async_phase`` can stall on plug/Tesla network calls;
    this stands in for that so tests can exercise the timeout bound without
    real hardware.
    """
    _ = (args, kwargs)  # accept any _cycle_async_phase call shape
    await asyncio.sleep(30)


def _make_stuck_plug_manager(now: datetime) -> tuple[LoadManager, PlugController, list[int]]:
    """Create a LoadManager whose async phase hangs indefinitely.

    Args:
        now: Fixed time for the cache and clock.

    Returns:
        Tuple of (LoadManager, PlugController, fetch_count list).
    """
    mgr, plug_ctrl, fetch_count = _make_plug_manager(now, predicted_wh=-2000.0)
    mgr._cycle_async_phase = _hang_async_phase
    return mgr, plug_ctrl, fetch_count


@patch("load_manager.ASYNC_PHASE_TIMEOUT_SECS", 0.1)
def test_fast_cycle_async_phase_timeout_returns_early_exit():
    """A stuck async phase returns async_phase_timeout instead of blocking.

    The head-of-line blocking fix bounds the lock hold: when controller
    calls hang, the decision cycle must return an early-exit status within
    the timeout instead of stalling the metrics loop's fetch.
    """
    import time as time_mod

    now = FIXED_NOW
    mgr, _plug_ctrl, fetch_count = _make_stuck_plug_manager(now)

    t0 = time_mod.perf_counter()
    result = mgr.run_decision_cycle(now=now)
    elapsed = time_mod.perf_counter() - t0

    assert result is not None
    assert result.status == "async_phase_timeout"
    assert result.actions == []
    assert elapsed < 2.0, f"async phase must be timeboxed, took {elapsed:.1f}s"
    _assert_never_fetched(fetch_count)


@patch("load_manager.ASYNC_PHASE_TIMEOUT_SECS", 0.1)
def test_async_phase_timeout_skips_commit():
    """A timed-out async phase records no pending effects.

    The outcome of a cancelled async phase is unknown, so the pipeline
    must not commit effects or mutate device state; the next cycle's
    plug-state sync converges from actual device state instead.
    """
    now = FIXED_NOW
    mgr, _plug_ctrl, fetch_count = _make_stuck_plug_manager(now)
    assert mgr.state.pending_effects == []

    result = mgr.run_decision_cycle(now=now)

    assert result is not None
    assert result.status == "async_phase_timeout"
    assert mgr.state.pending_effects == [], (
        "timeout must not commit effects for unknown outcomes"
    )
    _assert_never_fetched(fetch_count)


@patch("load_manager.ASYNC_PHASE_TIMEOUT_SECS", 0.1)
def test_run_cycle_async_phase_timeout_returns_early_exit():
    """Legacy run_cycle also returns async_phase_timeout on a stuck async phase.

    The slow loop falls back to run_cycle when LOAD_FAST_DECIDE_ENABLED is
    off; the same timeout bound must protect it.
    """
    import time as time_mod

    now = FIXED_NOW
    mgr, _plug_ctrl, fetch_count = _make_stuck_plug_manager(now)

    t0 = time_mod.perf_counter()
    result = mgr.run_cycle(force=False)
    elapsed = time_mod.perf_counter() - t0

    assert result is not None
    assert result.status == "async_phase_timeout"
    assert elapsed < 2.0, f"run_cycle async phase must be timeboxed, took {elapsed:.1f}s"
    _assert_never_fetched(fetch_count)


@patch("load_manager.ASYNC_PHASE_TIMEOUT_SECS", 0.2)
def test_fetch_cycle_not_stalled_by_stuck_decision():
    """fetch_cycle completes promptly while a decision cycle's async phase hangs.

    The decision loop holds the LoadManager lock through its async phase;
    the timebox bounds that hold so the metrics loop's fetch_cycle is not
    starved past the timeout.
    """
    import threading
    import time as time_mod

    now = FIXED_NOW
    mgr, _plug_ctrl, fetch_count = _make_plug_manager(now, predicted_wh=-2000.0)

    hang_started = threading.Event()

    async def hang_async_phase(*args, **kwargs):
        _ = (args, kwargs)  # accept any _cycle_async_phase call shape
        hang_started.set()
        await asyncio.sleep(30)

    mgr._cycle_async_phase = hang_async_phase

    results: dict[str, object] = {}

    def worker():
        results["decision"] = mgr.run_decision_cycle(now=now)

    thread = threading.Thread(target=worker)
    thread.start()
    assert hang_started.wait(timeout=2.0), "decision cycle never reached async phase"

    t0 = time_mod.perf_counter()
    fetch_result = mgr.fetch_cycle()
    elapsed = time_mod.perf_counter() - t0
    thread.join(timeout=5)

    assert not thread.is_alive(), "decision cycle must not outlive the timeout bound"
    assert elapsed < 2.0, (
        f"fetch_cycle stalled {elapsed:.1f}s behind a hung async phase"
    )
    assert fetch_result is None, "cache hit — fetch must succeed without stalling"
    assert results["decision"].status == "async_phase_timeout"
    _assert_never_fetched(fetch_count)


def test_fast_cycle_holds_on_insufficient_samples():
    """Cached QH with < MIN_SAMPLES_FOR_PREDICTION samples → hold.

    The fetch path (``_stage_nbc_fetch``) refuses to act on predictions
    built from fewer than ``MIN_SAMPLES_FOR_PREDICTION`` samples
    (reason="insufficient_samples"). The decision loop must apply the same
    gate to its cache-only read (``_stage_nbc_read_cache``) so a
    nearly-empty cache cannot fire actions on unreliable data: hold (None)
    and never fetch.
    """
    now = datetime(2026, 5, 7, 15, 2, 2, tzinfo=timezone.utc)
    clock = FakeClock(now)

    # Cache with only 2 samples in the incomplete QH (well below
    # MIN_SAMPLES_FOR_PREDICTION). _make_energy_cache_with_prediction
    # derives its sample count from the wall clock, so build the tiny
    # cache directly.
    qh_minute = (now.minute // 15) * 15
    data_start = now.replace(minute=qh_minute, second=0, microsecond=0)
    sample_value = -2000.0 / 900_000.0
    cache = EnergyCache(ttl_seconds=30)
    with cache._lock:
        cache.samples = [sample_value, sample_value]
        cache.data_start = data_start
        cache.last_sample_at = now
        cache.sample_count = 2
        cache.last_fetch_at = now

    fetch_count: list[int] = []

    def metrics_fetch() -> dict | None:
        fetch_count.append(1)
        return None

    mgr = LoadManager(LoadManagerConfig(
        metrics_fetch=metrics_fetch,
        energy_cache=cache,
        plug_ctrl=PlugController({
            "pool_pump": PlugConfig(
                name="pool_pump",
                accessory_id="xyz789",
                power_watts=2400.0,
                priority=10,
            ),
        }),
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
        clock=clock,
    ))

    result = mgr.run_decision_cycle(now=now)

    assert result is None, f"expected hold (None), got {result}"
    _assert_never_fetched(fetch_count)
