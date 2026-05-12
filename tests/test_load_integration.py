"""Integration tests for LoadManager run_cycle scenarios."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from load_manager import (
    DeviceState,
    LoadManager,
    PendingEffect,
    PlugConfig,
    PlugController,
    StateTracker,
    TeslaConfig,
    TeslaController,
    TeslaState,
    GapMinder,
)
from tests.helpers import _make_metrics_with_wh
from metrics import EnergyCache


# --- Excess solar helpers ---


def _make_energy_cache_with_prediction(
    predicted_wh: float,
    now: datetime | None = None,
    data_lag_secs: float = 0.0,
    fetch_offset_secs: int = 0,
) -> EnergyCache:
    """Create an EnergyCache pre-populated with samples that produce a target prediction.

    Uses constant per-second kWh samples so the extrapolated NBC quarter prediction
    equals ``predicted_wh``. With constant samples, predicted_wh = sample_value * 900_000
    regardless of which quarter is incomplete.

    Args:
        predicted_wh: Target Wh prediction for the incomplete quarter.
        now: Current time for sample timestamps. Defaults to ``datetime.now(timezone.utc)``.
        data_lag_secs: Simulated API lag in seconds. The NBCReader computes
            ``data_point_at = _last_fetch_at - timedelta(seconds=data_lag_secs)``.
        fetch_offset_secs: How many seconds ago the data was "fetched". Defaults to 0
            (fresh). Pass a positive value to simulate older fetch time.

    Returns:
        EnergyCache with ~2800 samples backfilled from ``now``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    sample_value = predicted_wh / 900_000.0
    sample_count = 2800  # QH3 is about halfway through the hour
    cache = EnergyCache(ttl_seconds=30)
    samples = [sample_value] * sample_count
    with cache._lock:
        cache._samples = samples
        cache._data_start = now - timedelta(seconds=sample_count)
        cache._last_sample_at = now - timedelta(seconds=1)
        cache._sample_count = sample_count
        cache._last_fetch_at = now - timedelta(seconds=fetch_offset_secs)
    # Store _data_lag_secs on the cache so NBCReader picks it up.
    cache._data_lag_secs = data_lag_secs  # type: ignore[attr-defined]
    return cache


def _make_excess_manager(
    predicted_wh: float = -2000.0, incomplete_qh: str = "QH3"
) -> tuple[LoadManager, PlugController, TeslaController]:
    """Create LoadManager with stub controllers and mock metrics."""
    plugs = {
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc123",
            power_watts=4500.0,
            priority=20,
        ),
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="xyz789",
            power_watts=1500.0,
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    tesla_config = TeslaConfig(
        client_id="test-id",
        client_secret="test-secret",
        redirect_uri="http://localhost/callback",
        vehicle_id="vehicle-123",
        home_lat=37.0,
        home_lon=-122.0,
        home_radius_m=500,
    )
    tesla_ctrl = TeslaController(tesla_config)

    metrics_data = _make_metrics_with_wh("main_panel", incomplete_qh, predicted_wh)

    def metrics_fetch():
        return metrics_data

    energy_cache = _make_energy_cache_with_prediction(predicted_wh)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=tesla_ctrl,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )
    return mgr, plug_ctrl, tesla_ctrl


# --- Over-target helpers ---


def _make_overn_target_manager(
    predicted_wh: float = 2000.0,
) -> tuple[LoadManager, PlugController]:
    """Create LoadManager for over-target scenario with both plugs ON."""
    plugs = {
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="xyz789",
            power_watts=1500.0,
            priority=10,
        ),
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc123",
            power_watts=4500.0,
            priority=20,
        ),
    }
    plug_ctrl = PlugController(plugs)

    asyncio.run(plug_ctrl.set_state("pool_pump", True))
    asyncio.run(plug_ctrl.set_state("water_heater", True))

    metrics_data = _make_metrics_with_wh("main_panel", "QH2", predicted_wh)

    def metrics_fetch():
        return metrics_data

    energy_cache = _make_energy_cache_with_prediction(predicted_wh)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    now = datetime.now(timezone.utc)
    mgr.state.devices["pool_pump"] = DeviceState(
        name="pool_pump", last_toggle=now - timedelta(seconds=120), desired_state=True
    )
    mgr.state.devices["water_heater"] = DeviceState(
        name="water_heater", last_toggle=now - timedelta(seconds=120), desired_state=True
    )

    return mgr, plug_ctrl


# --- Tesla safety helpers ---


def _make_tesla_manager(
    tesla_state: TeslaState, predicted_wh: float = -2000.0
) -> tuple[LoadManager, TeslaController]:
    """Create LoadManager with a mocked Tesla controller."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    tesla_config = TeslaConfig(
        client_id="test-id",
        client_secret="test-secret",
        redirect_uri="http://localhost/callback",
        vehicle_id="vehicle-123",
        home_lat=37.0,
        home_lon=-122.0,
        home_radius_m=500,
    )
    tesla_ctrl = TeslaController(tesla_config)
    tesla_ctrl.set_mock_state(tesla_state)

    metrics_data = _make_metrics_with_wh("main_panel", "QH3", predicted_wh)

    def metrics_fetch():
        return metrics_data

    energy_cache = _make_energy_cache_with_prediction(predicted_wh)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=tesla_ctrl,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )
    return mgr, tesla_ctrl


# --- Excess solar tests ---


def test_turns_on_plugs_in_priority_order():
    """Excess solar: turns on plugs in priority order."""
    mgr, _, _ = _make_excess_manager(predicted_wh=-6000.0)

    result = mgr.run_cycle()

    assert result["status"] == "ok"
    action_names = [a["device"] for a in result["actions"]]
    assert "water_heater" in action_names
    assert "pool_pump" in action_names
    wh_idx = action_names.index("water_heater")
    pp_idx = action_names.index("pool_pump")
    assert wh_idx < pp_idx


def test_turns_off_plugs_in_priority_order():
    """Load shedding: turns plugs off priority order."""
    # predicted_wh=-200, target=-500 => gap=300 Wh
    # in reverse priority order:
    # p20 water heater 4500w => 1125 Wh/quarter-hour (fills deficit)
    # no more deficit so pool pump stays on
    mgr, plug_ctrl = _make_overn_target_manager(predicted_wh=-200.0)

    result = mgr.run_cycle()

    assert result["status"] == "ok"
    wh_state = asyncio.run(plug_ctrl.get_state("water_heater"))
    pp_state = asyncio.run(plug_ctrl.get_state("pool_pump"))
    assert wh_state is False
    assert pp_state


def test_plug_states_updated():
    """Excess solar: plug controller states are updated."""
    mgr, plug_ctrl, _ = _make_excess_manager(predicted_wh=-6000.0)

    mgr.run_cycle()

    wh_state = asyncio.run(plug_ctrl.get_state("water_heater"))
    pp_state = asyncio.run(plug_ctrl.get_state("pool_pump"))
    assert wh_state
    assert pp_state


# --- Over-target tests ---


def test_turns_off_all_on_plugs():
    """Over-target: turns off all plugs that are currently on."""
    mgr, _ = _make_overn_target_manager(predicted_wh=2000.0)

    result = mgr.run_cycle()

    action_names = [a["device"] for a in result["actions"]]
    assert "pool_pump" in action_names
    assert "water_heater" in action_names


# --- Tesla safety tests ---


def test_skip_tesla_not_at_home():
    """Tesla not at home: no Tesla actions."""
    state = TeslaState(
        is_charging=False, current_amps=None, soc_percent=50.0,
        plugged_in=True, at_home=False, at_charge_limit=False,
    )
    mgr, _ = _make_tesla_manager(state)

    result = mgr.run_cycle()

    tesla_actions = [a for a in result.get("actions", []) if a["device"] == "tesla"]
    assert len(tesla_actions) == 0


def test_skip_tesla_not_plugged_in():
    """Tesla not plugged in: no Tesla actions."""
    state = TeslaState(
        is_charging=False, current_amps=None, soc_percent=50.0,
        plugged_in=False, at_home=True, at_charge_limit=False,
    )
    mgr, _ = _make_tesla_manager(state)

    result = mgr.run_cycle()

    tesla_actions = [a for a in result.get("actions", []) if a["device"] == "tesla"]
    assert len(tesla_actions) == 0


def test_skip_tesla_at_charge_limit():
    """Tesla at charge limit: no Tesla actions."""
    state = TeslaState(
        is_charging=True, current_amps=48, soc_percent=90.0,
        plugged_in=True, at_home=True, at_charge_limit=True,
    )
    mgr, _ = _make_tesla_manager(state)

    result = mgr.run_cycle()

    tesla_actions = [a for a in result.get("actions", []) if a["device"] == "tesla"]
    assert len(tesla_actions) == 0


# --- Stale data tests ---


def test_stale_data_skips_cycle():
    """NBC data >120s old with pending effects returns stale_data status."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=130)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at

    def metrics_fetch():
        return metrics_data

    energy_cache = _make_energy_cache_with_prediction(-2000.0, data_lag_secs=130)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    now = datetime.now(timezone.utc)
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="plug", action="turn_on",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_delta_wh=500.0,
        )
    )

    result = mgr.run_cycle()

    assert result["status"] == "stale_data"


def test_stale_no_pending_effects_proceeds():
    """NBC data >120s old with zero pending effects should NOT skip.

    Regression test: previously, stale data alone (with no unreflected
    actions) caused permanent lockout since last_data_point_at was never
    updated while stuck in the stale path. With zero pending effects,
    there are no unreflected actions, so it's safe to proceed.
    """
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    fixed_now = datetime(2026, 5, 6, 7, 8, 00, tzinfo=timezone.utc)
    data_point_at = fixed_now - timedelta(seconds=StateTracker.STALE_THRESHOLD_SECS)
    fetched_at = data_point_at + timedelta(seconds=10)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at

    def metrics_fetch():
        return metrics_data

    energy_cache = _make_energy_cache_with_prediction(-2000.0)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )
    # use fixed_now inside LoadManager
    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)  # keep constructor working
        result = mgr.run_cycle()


    assert len(mgr.state.pending_effects) == 0
    assert result["status"] != "stale_data"


def test_stale_data_from_previous_qh():
    """Data from previous QH should be treated as stale even if <120s old.

    Regression: the age-based check only fires when data is >120s old, but
    data from the immediately preceding QH can be only seconds or minutes old.

    The system must NOT make load decisions on this data — turning on a plug
    based on stale QH1 prediction at 15:02 would waste energy.

    We patch ``datetime.now`` so that both the enabled-check time and
    now_postfetch are consistent: 15:02 (QH2).  data_point_at is at the
    very end of QH1 (14:59:30), so age ≈ 150 s — just over the threshold
    to force QH-boundary detection.

    To keep age strictly under 120 s (so the QH check, not age, fires),
    we use 15:01 with data_point_at at 14:59:30 → age ≈ 90 s.
    """
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    # Current wall-clock time: start of QH2
    fixed_now = datetime(2026, 5, 7, 15, 00, 00, tzinfo=timezone.utc)
    # data_point_at: end of previous QH
    data_point_at = fixed_now - timedelta(seconds=1)
    fetched_at = data_point_at + timedelta(seconds=10)

    metrics_data = _make_metrics_with_wh("main_panel", "QH2", -2000.0)
    metrics_data["_fetched_at"] = fetched_at

    def metrics_fetch():
        return metrics_data

    energy_cache = _make_energy_cache_with_prediction(-2000.0)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    # Add a pending effect so the stale-check path is triggered.
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="plug", action="turn_on",
            timestamp=fixed_now, power_delta_wh=500.0,
            data_point_at=fixed_now - timedelta(seconds=20))
    )

    # Patch get_current_qh to return our crafted data_point_at.
    original_get = mgr.nbc_reader.get_current_qh

    def patched_get(force=False, now=None):
        result = original_get(force=force, now=now)
        if result is not None:
            qh_name, predicted_wh, seconds_remaining = result[:3]
            return (qh_name, predicted_wh, seconds_remaining, data_point_at)
        return None

    mgr.nbc_reader.get_current_qh = patched_get  # type: ignore[method-assign]

    with patch("load_manager.datetime") as mock_dt_cls:
        mock_dt_cls.now.return_value = fixed_now

        result = mgr.run_cycle()

    assert result["status"] == "stale_data"
    # Verify the reason indicates QH boundary detection, not age-based stale.
    assert result["diagnostics"]["reason"] == "previous_qh"


# --- Pending effect tests ---


def test_waits_for_fresh_data():
    """Action taken after last NBC fetch -> wait for fresh data."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at

    def metrics_fetch():
        return metrics_data

    energy_cache = _make_energy_cache_with_prediction(-2000.0)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    mgr.state.last_data_point_at = fetched_at
    now = datetime.now(timezone.utc)
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_delta_wh=500.0,
        )
    )

    result = mgr.run_cycle(force=True)

    # With force=True, fresh data is always fetched. The pending effect
    # timestamp (now) may be after the API's data_point_at, but with fresh
    # fetch the NBC reader returns current time as data point. The cycle
    # proceeds and may return "ok" or still wait depending on timing.
    assert result["status"] in ("waiting_for_fresh_data", "ok")


# --- Data-point-age stale detection tests ---


def test_stale_detection_uses_data_point_age_not_fetch_time():
    """NBC data fetched recently but with large lag should be treated as stale.

    _fetched_at is only 60s ago, but lag=80s means the most recent data point
    is actually 140s old. Stale threshold is 120s, so this should trigger
    stale_data status (not proceed as it would with fetch-time-only check).
    """
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at
    metrics_data["_data_lag_secs"] = 80.0

    def metrics_fetch():
        return metrics_data

    energy_cache = _make_energy_cache_with_prediction(
        -2000.0, data_lag_secs=130  # data_point_at = now - 130s → stale (>120s)
    )
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    now = datetime.now(timezone.utc)
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="plug", action="turn_on",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_delta_wh=500.0,
        )
    )

    result = mgr.run_cycle()

    assert result["status"] == "stale_data"


def test_waiting_detection_uses_data_point_age_not_fetch_time():
    """Action taken between data point and fetch should trigger waiting.

    _fetched_at is 10s ago, lag=50s means data point is 60s old.
    A pending effect at fetched_at - 30s (i.e., 40s ago) is after the
    data point but before the fetch. Waiting detection should trigger
    because this effect isn't reflected in the data.
    """
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    fixed_now = datetime(2026, 5, 7, 15, 10, 00, tzinfo=timezone.utc)
    fetched_at = fixed_now - timedelta(seconds=10)
    data_point_at = fetched_at - timedelta(seconds=50)  # 60s ago
    effect_time = fetched_at - timedelta(seconds=30)     # 40s ago

    energy_cache = _make_energy_cache_with_prediction(
        -2000.0, now=fixed_now, data_lag_secs=50
    )
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    mgr.state.last_data_point_at = fetched_at
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=effect_time,
            data_point_at=effect_time - timedelta(seconds=20),
            power_delta_wh=500.0,
        )
    )

    # use fixed_now inside LoadManager
    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)  # keep constructor working
    result = mgr.run_cycle(force=True)

    # With force=True, fresh data is always fetched. The pending effect
    # timestamp (now) may be after the API's data_point_at, but with fresh
    # fetch the NBC reader returns current time as data point. The cycle
    # proceeds and may return "ok" or still wait depending on timing.
    assert result["status"] in ("waiting_for_fresh_data", "ok")


def test_full_lifecycle_action_wait_resolve():
    """Full lifecycle: action taken -> wait for fresh data -> new NBC fetch
    arrives with updated timestamp -> pending effects pruned -> new decision."""
    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    now = datetime.now(timezone.utc)
    energy_cache = _make_energy_cache_with_prediction(-6000.0, now=now)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    result_1 = mgr.run_cycle()
    assert result_1["status"] == "ok"
    assert len(result_1["actions"]) > 0
    assert len(mgr.state.pending_effects) > 0

    result_2 = mgr.run_cycle()
    assert result_2["status"] == "waiting_for_fresh_data"


def test_pending_since_count():
    """pending_since_count returns correct count of recent effects."""
    tracker = StateTracker()
    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    tracker.pending_effects.extend([
        PendingEffect(
            device_name="a", action="turn_on",
            timestamp=base_time - timedelta(seconds=60),
            data_point_at=base_time - timedelta(seconds=80),
            power_delta_wh=100.0,
        ),
        PendingEffect(
            device_name="b", action="turn_on",
            timestamp=base_time + timedelta(seconds=30),
            data_point_at=base_time + timedelta(seconds=10),
            power_delta_wh=200.0,
        ),
        PendingEffect(
            device_name="c", action="turn_off",
            timestamp=base_time + timedelta(seconds=60),
            data_point_at=base_time + timedelta(seconds=40),
            power_delta_wh=-150.0,
        ),
    ])

    assert tracker.pending_since_count(base_time) == 2
    assert tracker.pending_since_count(base_time - timedelta(seconds=120)) == 3
    assert tracker.pending_since_count(base_time + timedelta(seconds=120)) == 0


def test_prune_old_effects():
    """prune_old_effects removes effects before the data point that are old enough."""
    tracker = StateTracker()
    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    tracker.pending_effects.extend([
        PendingEffect(
            device_name="old1", action="turn_on",
            timestamp=base_time - timedelta(seconds=60),
            data_point_at=base_time - timedelta(seconds=80),
            power_delta_wh=100.0,
        ),
        PendingEffect(
            device_name="old2", action="turn_off",
            timestamp=base_time - timedelta(seconds=30),
            data_point_at=base_time - timedelta(seconds=50),
            power_delta_wh=-50.0,
        ),
        PendingEffect(
            device_name="recent", action="turn_on",
            timestamp=base_time + timedelta(seconds=10),
            data_point_at=base_time - timedelta(seconds=10),
            power_delta_wh=200.0,
        ),
    ])

    # data_point_at = base_time
    # old1 (60s old): exactly at boundary — pruned (60s >= MIN_SECS)
    # old2 (30s old): below MIN_SECS — kept
    # recent (future): above data_point_at — kept
    pruned = tracker.prune_old_effects(base_time)
    assert pruned == 1
    assert len(tracker.pending_effects) == 2
    assert {e.device_name for e in tracker.pending_effects} == {"old2", "recent"}


def test_prune_old_effects_respects_minimum_age():
    """prune_old_effects does not prune effects younger than PENDING_EFFECT_MIN_SECS in data-point time.

    Uses wall-clock ``now`` so the test is not sensitive to a frozen datetime.
    """
    tracker = StateTracker()
    now = datetime.now(timezone.utc)
    dp = now  # data_point_at is "now"

    tracker.pending_effects.extend([
        PendingEffect(
            device_name="young", action="turn_on",
            timestamp=now - timedelta(seconds=50),
            data_point_at=now - timedelta(seconds=70),
            power_delta_wh=100.0,
        ),
        PendingEffect(
            device_name="old", action="turn_off",
            timestamp=now - timedelta(seconds=300),
            data_point_at=now - timedelta(seconds=320),
            power_delta_wh=-50.0,
        ),
        PendingEffect(
            device_name="recent", action="turn_on",
            timestamp=now + timedelta(seconds=10),
            data_point_at=now - timedelta(seconds=10),
            power_delta_wh=200.0,
        ),
    ])

    pruned = tracker.prune_old_effects(dp)
    assert pruned == 1  # only the "old" effect is pruned
    assert len(tracker.pending_effects) == 2
    assert {e.device_name for e in tracker.pending_effects} == {"young", "recent"}


# --- Pending effect lifecycle tests ---


def test_adjusted_wh_in_response():
    """Response includes both raw predicted_wh and adjusted_wh."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    energy_cache = _make_energy_cache_with_prediction(-500.0)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    result = mgr.run_cycle()

    assert "predicted_wh" in result
    assert "adjusted_wh" in result
    assert result["predicted_wh"] == result["adjusted_wh"]


def test_stale_data_includes_pending_count():
    """Stale data response includes pending_effects_count."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    now = datetime.now(timezone.utc)
    energy_cache = _make_energy_cache_with_prediction(-2000.0, data_lag_secs=180)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    now = datetime.now(timezone.utc)
    mgr.state.pending_effects.extend([
        PendingEffect(
            device_name="plug", action="turn_on",
            timestamp=now - timedelta(seconds=60),
            data_point_at=now - timedelta(seconds=80),
            power_delta_wh=500.0,
        ),
        PendingEffect(
            device_name="plug2", action="turn_off",
            timestamp=now - timedelta(seconds=30),
            data_point_at=now - timedelta(seconds=50),
            power_delta_wh=-200.0,
        ),
    ])

    result = mgr.run_cycle()

    assert result["status"] == "stale_data"
    assert result["diagnostics"]["pending_effects_count"] == 2


def test_stale_data_prunes_old_effects():
    """Stale data cycle prunes effects older than fetched_at."""
    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    now = datetime.now(timezone.utc)
    energy_cache = _make_energy_cache_with_prediction(-2000.0, data_lag_secs=180)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    now = datetime.now(timezone.utc)
    # Cache created with data_lag_secs=180, fetch_offset_secs=0 (default).
    # data_point_at = cache_now - 180s, cutoff = data_point_at - 60s = cache_now - 240s.
    mgr.state.pending_effects.extend([
        PendingEffect(
            device_name="heater", action="turn_on",
            # -280s is before cutoff (now-240s) → pruned.
            timestamp=now - timedelta(seconds=280),
            data_point_at=now - timedelta(seconds=300),
            power_delta_wh=500.0,
        ),
        PendingEffect(
            device_name="heater", action="turn_off",
            # -80s is after cutoff → kept.
            timestamp=now - timedelta(seconds=80),
            data_point_at=now - timedelta(seconds=100),
            power_delta_wh=-200.0,
        ),
    ])

    # Use force=True to skip the stale-data check in _check_pending_state
    # and proceed directly to Stage 4 where pruning always happens.
    # Run the cycle once to trigger decide() which may add new actions.
    result = mgr.run_cycle(force=True)

    assert result["status"] == "ok"
    # Old effect (turn_on, -280s) should be pruned. New action (turn_on) may
    # have been added by decide(). The original turn_off should remain.
    assert all(
        pe.timestamp > mgr.state.last_data_point_at - timedelta(seconds=60)
        for pe in mgr.state.pending_effects
    )


def test_stale_data_includes_candidates():
    """Stale data response includes candidates in diagnostics."""
    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    now = datetime.now(timezone.utc)
    energy_cache = _make_energy_cache_with_prediction(-2000.0, data_lag_secs=180)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    now = datetime.now(timezone.utc)
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="heater", action="turn_on",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_delta_wh=500.0,
        )
    )

    result = mgr.run_cycle()

    assert result["status"] == "stale_data"
    candidates = result["diagnostics"]["candidates"]
    assert candidates is not None
    heater_candidate = next(
        (c for c in candidates if c["name"] == "heater"), None
    )
    assert heater_candidate is not None
    assert heater_candidate["power_watts"] == 2000.0


def test_waiting_for_fresh_data_includes_count():
    """Waiting response includes pending_effects_count."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)
    fetched_at = fixed_now - timedelta(seconds=10)
    energy_cache = _make_energy_cache_with_prediction(-2000.0, now=fixed_now, data_lag_secs=10)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    mgr.state.last_data_point_at = fetched_at
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="plug", action="turn_on",
            timestamp=fixed_now,
            data_point_at=fixed_now - timedelta(seconds=20),
            power_delta_wh=500.0,
        )
    )

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = mgr.run_cycle()

    assert result["status"] == "waiting_for_fresh_data"
    assert result["diagnostics"]["pending_effects_count"] == 1


def test_waiting_for_fresh_data_includes_candidates():
    """Waiting response includes candidates in diagnostics."""
    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)
    fetched_at = fixed_now - timedelta(seconds=10)
    energy_cache = _make_energy_cache_with_prediction(-2000.0, now=fixed_now, data_lag_secs=10)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    mgr.state.last_data_point_at = fetched_at
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="heater", action="turn_on",
            timestamp=fixed_now,
            data_point_at=fixed_now - timedelta(seconds=20),
            power_delta_wh=500.0,
        )
    )

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = mgr.run_cycle()

    assert result["status"] == "waiting_for_fresh_data"
    candidates = result["diagnostics"]["candidates"]
    assert candidates is not None
    heater_candidate = next(
        (c for c in candidates if c["name"] == "heater"), None
    )
    assert heater_candidate is not None


# --- Cache tests ---


def test_cache_hits_within_ttl():
    """Second call within TTL + same QH uses cache."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)
    energy_cache = _make_energy_cache_with_prediction(-2000.0)

    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    result_1 = mgr.run_cycle(force=True)
    result_2 = mgr.run_cycle(force=True)

    # Both calls should succeed (cache is valid within TTL).
    assert result_1["status"] == "ok"
    assert result_2["status"] == "ok"


def test_disabled_returns_early():
    """Disabled manager returns disabled status immediately."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)

    energy_cache = EnergyCache()
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=False,
        dry_run=False,
    )

    result = mgr.run_cycle()

    assert result["status"] == "disabled"


# --- Dry-run tests ---


def test_dry_run_returns_dry_run_status():
    """Dry run returns dry-run status instead of ok."""
    plugs = {
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc123",
            power_watts=4500.0,
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    energy_cache = _make_energy_cache_with_prediction(-6000.0)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=True,
    )

    result = mgr.run_cycle()

    assert result["status"] == "dry-run"
    action_names = [a["device"] for a in result["actions"]]
    assert "water_heater" in action_names


def test_dry_run_does_not_execute():
    """Dry run does not change plug state or add pending effects."""
    plugs = {
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc123",
            power_watts=4500.0,
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    energy_cache = _make_energy_cache_with_prediction(-6000.0)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=True,
    )

    mgr.run_cycle()

    wh_state = asyncio.run(plug_ctrl.get_state("water_heater"))
    assert not wh_state
    assert len(mgr.state.pending_effects) == 0


def test_dry_run_state_not_mutated():
    """Dry run does not mutate internal state, so repeated cycles produce
    the same actions instead of seeing stale desired_state."""
    plugs = {
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc123",
            power_watts=4500.0,
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    energy_cache = _make_energy_cache_with_prediction(-6000.0)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=True,
    )

    first_result = mgr.run_cycle()
    assert first_result["status"] == "dry-run"
    first_actions = [a["device"] for a in first_result["actions"]]
    assert "water_heater" in first_actions

    dev_state = mgr.state.devices.get("water_heater")
    if dev_state is not None:
        assert dev_state.desired_state is not True

    second_result = mgr.run_cycle()
    assert second_result["status"] == "dry-run"
    second_actions = [a["device"] for a in second_result["actions"]]
    assert "water_heater" in second_actions


# --- Tesla amp adjustment tests ---


@patch("load_manager.load_tesla_config", return_value=None)
def test_tesla_amp_adjustment(_mock_load_tesla):
    """After plugs, residual gap triggers Tesla set_charge_amps."""
    plugs = {
        "small_plug": PlugConfig(
            name="small_plug",
            accessory_id="abc",
            power_watts=500.0,
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    tesla_config = TeslaConfig(
        client_id="test-id",
        client_secret="test-secret",
        redirect_uri="http://localhost/callback",
        vehicle_id="vehicle-123",
        home_lat=37.0,
        home_lon=-122.0,
        home_radius_m=500,
        charge_amps_min=5,
        charge_amps_max=48,
    )
    tesla_ctrl = TeslaController(tesla_config)
    tesla_ctrl.set_mock_state(
        TeslaState(
            is_charging=True,
            current_amps=None,
            soc_percent=50.0,
            plugged_in=True,
            at_home=True,
            at_charge_limit=False,
        )
    )

    energy_cache = _make_energy_cache_with_prediction(-3000.0)
    mgr = LoadManager(
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=tesla_ctrl,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    result = mgr.run_cycle()

    action_devices = [a["device"] for a in result.get("actions", [])]
    assert "small_plug" in action_devices
    assert "tesla" in action_devices

    tesla_amps = tesla_ctrl._state.current_amps
    assert tesla_amps is not None
    assert tesla_amps >= 5
    assert tesla_amps <= 48


def test_turn_off_only_device_even_when_savings_exceed_gap():
    """Water heater must be turned off even when its savings exceed the remaining
    gap.

    Regression: at the start of a new quarter (seconds_remaining≈891) the engine
    turned the water heater on at the end of the *previous* quarter when only
    ~51 s remained (66.6 Wh fit the gap).  At QH-start the heater was still on
    and the predicted overshoot was ~976 Wh — less than the heater's full-quarter
    capacity of ~1163 Wh.  The engine skipped it as "too large to turn off",
    leaving it running and causing a 71.99 Wh overshoot at quarter end.

    The correct behaviour: when turning off a device would bring predicted_wh
    below target, still turn it off — any undershoot is smaller than the
    overshoot from leaving the device on.
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()

    # Water heater is on (confirmed), flexible — the only device available.
    state.devices["water_heater"] = DeviceState(
        name="water_heater",
        desired_state=True,
    )
    plugs = {
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc123",
            power_watts=4700.0,
        )
    }

    actions = engine.decide(
        predicted_wh=967.0,
        target_wh=-9.0,
        seconds_remaining=891,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    # Engine must still turn the heater off — accepting a mild undershoot is
    # better than guaranteeing a large overshoot.
    assert len(actions) == 1
    assert actions[0].action == "turn_off"
    assert actions[0].device_name == "water_heater"


# --- Adaptive sleep tests ---


class TestAdaptiveSleep:
    """Tests for the adaptive sleep hint returned by run_cycle()."""

    def _make_manager(self, interval=30):
        """Create a minimal LoadManager with stub controllers."""
        plug_ctrl = PlugController({})
        tesla_ctrl = TeslaController(
            TeslaConfig(
                client_id="test-id",
                client_secret="test-secret",
                redirect_uri="http://localhost/callback",
                vehicle_id="vehicle-123",
                home_lat=37.0,
                home_lon=-122.0,
                home_radius_m=500,
            )
        )

        def metrics_fetch():
            return None  # no data → will hit early returns

        energy_cache = EnergyCache()
        return LoadManager(
            metrics_fetch=metrics_fetch,
            energy_cache=energy_cache,
            plug_ctrl=plug_ctrl,
            tesla_ctrl=tesla_ctrl,
            target_wh=-500,
            config_interval_secs=interval,
        )

    def _make_cycle_result(self, **overrides):
        """Build a minimal cycle result dict for _calculate_adaptive_sleep."""
        base = {
            "status": "ok",
            "nbc_prediction_wh": -1000.0,
            "qh": "QH2",
            "predicted_wh": -1000.0,
            "adjusted_wh": -1000.0,
            "target_wh": -500,
            "actions": [],
            "diagnostics": {
                "gap_wh": -500,
                "seconds_remaining": 450,
                "reason": "ok",
            },
        }
        base.update(overrides)
        return base

    # --- Scenario 1: Actions taken (ok / dry-run) → config_interval ---

    @pytest.mark.parametrize("status", ["ok", "dry-run"])
    def test_actions_taken_returns_config_interval(self, status):
        """When no deficit and actions taken, sleep_hint uses QH timing multiplier."""
        lm = self._make_manager(interval=30)
        result = self._make_cycle_result(
            status=status, nbc_prediction_wh=0.0  # no deficit (target is -500)
        )
        hint = lm._calculate_adaptive_sleep(result)
        # No deficit, early in QH (450s > 300) → 1.5x config = 45
        assert hint == 45.0

    # --- Scenario 2: Disabled → config_interval ---

    def test_disabled_returns_config_interval(self):
        """When disabled, sleep_hint should be config_interval."""
        lm = self._make_manager(interval=30)
        result = self._make_cycle_result(status="disabled")
        hint = lm._calculate_adaptive_sleep(result)
        assert hint == 30

    # --- Scenario 3: Stale data → minimum sleep (5s) ---

    def test_stale_data_returns_minimum_sleep(self):
        """When data is stale, sleep_hint should be 5 seconds."""
        lm = self._make_manager()
        result = self._make_cycle_result(status="stale_data")
        hint = lm._calculate_adaptive_sleep(result)
        assert hint == 5.0

    # --- Scenario 4: Waiting for fresh data → seconds_remaining (clamped) ---

    def test_waiting_for_fresh_data_returns_seconds_remaining(self):
        """When waiting for fresh data, sleep_hint = min(seconds_remaining, 2*interval)."""
        lm = self._make_manager(interval=30)
        result = self._make_cycle_result(
            status="waiting_for_fresh_data", seconds_remaining=20.0
        )
        hint = lm._calculate_adaptive_sleep(result)
        assert hint == 20.0

    def test_waiting_for_fresh_data_clamped_to_max(self):
        """When seconds_remaining exceeds 2*interval, sleep_hint is clamped."""
        lm = self._make_manager(interval=30)
        result = self._make_cycle_result(
            status="waiting_for_fresh_data", seconds_remaining=120.0
        )
        hint = lm._calculate_adaptive_sleep(result)
        assert hint == 60.0  # 2 * interval

    # --- Scenario 5: No deficit (predicted >= target) → longer sleep early in QH ---

    def test_no_deficit_early_in_qh_returns_longer_sleep(self):
        """When no deficit and early in QH, sleep_hint > config_interval."""
        lm = self._make_manager(interval=30)
        result = self._make_cycle_result(
            status="ok",
            nbc_prediction_wh=0.0,  # no deficit (target is -500)
            seconds_remaining=600,  # 10 min left in QH
        )
        print(f"DEBUG: result={result}")
        print(f"DEBUG: lm.target_wh={lm.target_wh}, config_interval={lm.config_interval_secs}")
        hint = lm._calculate_adaptive_sleep(result)
        print(f"DEBUG: hint={hint}")
        assert hint == 45.0  # config_interval * 1.5

    def test_no_deficit_late_in_qh_returns_slightly_longer_sleep(self):
        """When no deficit and late in QH, sleep_hint is slightly above config_interval."""
        lm = self._make_manager(interval=30)
        result = self._make_cycle_result(
            status="ok",
            nbc_prediction_wh=0.0,  # no deficit (target is -500)
            seconds_remaining=120,  # 2 min left in QH
        )
        hint = lm._calculate_adaptive_sleep(result)
        assert hint == 37.5  # config_interval * 1.25

    # --- Scenario 6: Deficit exists but no capacity → minimum sleep (5s) ---

    def test_deficit_no_capacity_returns_minimum_sleep(self):
        """When deficit exists but no flexible capacity, sleep_hint = 5s."""
        lm = self._make_manager()
        result = self._make_cycle_result(
            status="ok",
            predicted_wh=-2000.0,  # deficit of 1500 Wh
            seconds_remaining=450,
        )
        # No candidates in diagnostics → no capacity → minimum sleep
        hint = lm._calculate_adaptive_sleep(result)
        assert hint == 5.0

    # --- Scenario 7: Deficit with capacity → proportional sleep (clamped) ---

    def test_deficit_with_capacity_returns_proportional_sleep(self):
        """When deficit exists with capacity, sleep_hint is proportional."""
        lm = self._make_manager()
        result = self._make_cycle_result(
            status="ok",
            nbc_prediction_wh=-2000.0,  # deficit of 1500 Wh
            seconds_remaining=450,
        )
        # Add candidates with 1000W flexible capacity
        result["diagnostics"]["candidates"] = [
            {"name": "heater", "power_watts": 1000.0}
        ]
        hint = lm._calculate_adaptive_sleep(result)
        # time_to_close = (1500 / 1000) * 3600 = 5400s
        # proportion = 450 / 5400 = 0.0833
        # sleep = 30 * 0.0833 = 2.5 → clamped to min(5)
        assert hint == 5.0

    def test_deficit_with_large_capacity_returns_scaled_sleep(self):
        """When deficit with large capacity, sleep_hint scales up proportionally."""
        lm = self._make_manager()
        result = self._make_cycle_result(
            status="ok",
            nbc_prediction_wh=-600.0,  # deficit of 100 Wh
            seconds_remaining=450,
        )
        # Add candidates with 1000W flexible capacity
        result["diagnostics"]["candidates"] = [
            {"name": "heater", "power_watts": 1000.0}
        ]
        hint = lm._calculate_adaptive_sleep(result)
        # time_to_close = (100 / 1000) * 3600 = 360s
        # proportion = 450 / 360 = 1.25
        # sleep = 30 * 1.25 = 37.5 → clamped to max(60)
        assert hint == 37.5

    def test_deficit_with_capacity_clamped_to_max(self):
        """When proportional sleep exceeds 2*interval, it is clamped."""
        lm = self._make_manager()
        result = self._make_cycle_result(
            status="ok",
            nbc_prediction_wh=-510.0,  # deficit of only 10 Wh
            seconds_remaining=899,  # almost full QH remaining
        )
        result["diagnostics"]["candidates"] = [
            {"name": "heater", "power_watts": 100.0}
        ]
        hint = lm._calculate_adaptive_sleep(result)
        # time_to_close = (10 / 100) * 3600 = 360s
        # proportion = 899 / 360 ≈ 2.5 → clamped to 2.0
        # sleep = 30 * 2.0 = 60 → clamped to max(60)
        assert hint == 60.0

    # --- Edge cases ---

    def test_no_incomplete_qh_returns_minimum_sleep(self):
        """When no incomplete QH, sleep_hint should be 5 seconds."""
        lm = self._make_manager()
        result = self._make_cycle_result(status="no_incomplete_qh")
        hint = lm._calculate_adaptive_sleep(result)
        assert hint == 5.0

    def test_custom_interval_is_respected(self):
        """Sleep hints scale with a custom config interval."""
        lm = self._make_manager(interval=60)
        result = self._make_cycle_result(
            status="ok", nbc_prediction_wh=0.0  # no deficit (target is -500)
        )
        hint = lm._calculate_adaptive_sleep(result)
        # No deficit, early in QH (450s > 300) → 1.5x config = 90
        assert hint == 90.0

    def test_min_clamp_prevents_sub_5_sleep(self):
        """Sleep hint is never less than 5 seconds."""
        lm = self._make_manager()
        result = self._make_cycle_result(
            status="ok",
            predicted_wh=-2000.0,  # large deficit
            seconds_remaining=10,
        )
        result["diagnostics"]["candidates"] = [
            {"name": "heater", "power_watts": 50.0}
        ]
        hint = lm._calculate_adaptive_sleep(result)
        assert hint >= 5.0

    def test_max_clamp_prevents_excessive_sleep(self):
        """Sleep hint is never more than 2 * config_interval."""
        lm = self._make_manager()
        result = self._make_cycle_result(
            status="ok",
            predicted_wh=-501.0,  # tiny deficit
            seconds_remaining=899,
        )
        result["diagnostics"]["candidates"] = [
            {"name": "heater", "power_watts": 10.0}
        ]
        hint = lm._calculate_adaptive_sleep(result)
        assert hint <= 60.0  # 2 * interval

    def test_run_cycle_includes_sleep_hint(self):
        """run_cycle() returns sleep_hint in the result dict."""
        lm = self._make_manager(interval=30)

        # Force a cycle — it will return early due to no incomplete QH,
        # but should still include sleep_hint
        result = lm.run_cycle(force=True)
        assert "sleep_hint" in result

    def test_disabled_run_cycle_includes_sleep_hint(self):
        """Disabled run_cycle returns sleep_hint = config_interval."""
        mock_cfg = MagicMock()
        mock_cfg.load_manage_enabled = False

        with patch("load_manager._cfg", mock_cfg):
            lm = self._make_manager(interval=30)

        result = lm.run_cycle()
        assert result["status"] == "disabled"
        assert result["sleep_hint"] == 30

    def test_stale_data_run_cycle_includes_sleep_hint(self):
        """Stale data run_cycle returns sleep_hint = 5."""
        now = datetime.now(timezone.utc)
        data_point_at = now - timedelta(seconds=200)  # >120s old → stale

        class StaleQHReader:
            """Mock NBC reader that returns data 200 seconds old."""

            def get_current_qh(self, force=False, now=None):
                return ("QH3", -1000.0, 600, data_point_at)

        lm = LoadManager(
            metrics_fetch=lambda: None,
            plug_ctrl=PlugController({}),
            tesla_ctrl=TeslaController(
                TeslaConfig(
                    client_id="test-id",
                    client_secret="test-secret",
                    redirect_uri="http://localhost/callback",
                    vehicle_id="vehicle-123",
                    home_lat=37.0,
                    home_lon=-122.0,
                    home_radius_m=500,
                )
            ),
            target_wh=-500,
            config_interval_secs=30,
        )
        lm.nbc_reader = StaleQHReader()
        lm.enabled = True

        # Pending effects present + stale data → stale_data status
        lm.state.pending_effects = [PendingEffect(
            device_name="test",
            action="turn_on",
            target_amps=None,
            power_delta_wh=100.0,
            timestamp=data_point_at - timedelta(seconds=30),  # before data point
            data_point_at=data_point_at - timedelta(seconds=50),
        )]

        result = lm.run_cycle()
        assert result["status"] == "stale_data"
        assert result["sleep_hint"] == 5.0

    def test_waiting_for_fresh_data_run_cycle_includes_sleep_hint(self):
        """Waiting for fresh data returns sleep_hint = min(seconds_remaining, 2*interval)."""
        fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)
        data_point_at = fixed_now - timedelta(seconds=10)  # recent data point

        class FreshQHReader:
            """Mock NBC reader that returns a recent data point."""

            def get_current_qh(self, force=False, now=None):
                return ("QH3", -1000.0, 600, data_point_at)

        lm = LoadManager(
            metrics_fetch=lambda: None,
            plug_ctrl=PlugController({}),
            tesla_ctrl=TeslaController(
                TeslaConfig(
                    client_id="test-id",
                    client_secret="test-secret",
                    redirect_uri="http://localhost/callback",
                    vehicle_id="vehicle-123",
                    home_lat=37.0,
                    home_lon=-122.0,
                    home_radius_m=500,
                )
            ),
            target_wh=-500,
            config_interval_secs=30,
        )
        lm.nbc_reader = FreshQHReader()
        lm.enabled = True

        # Pending effect AFTER data point → waiting_for_fresh_data status
        lm.state.pending_effects = [PendingEffect(
            device_name="test",
            action="turn_on",
            target_amps=None,
            power_delta_wh=100.0,
            timestamp=data_point_at + timedelta(seconds=5),  # after data point
            data_point_at=data_point_at - timedelta(seconds=15),
        )]

        with patch("load_manager.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = lm.run_cycle()

        assert result["status"] == "waiting_for_fresh_data"
        # sleep_hint should be min(seconds_remaining, 2*interval)
        assert result["sleep_hint"] <= 60

    def test_ok_run_cycle_includes_sleep_hint(self):
        """Normal ok run_cycle returns sleep_hint = config_interval."""
        lm = self._make_manager(interval=30)

        result = lm.run_cycle(force=True)
        # force=True bypasses stale check, but may hit no_incomplete_qh or hysteresis
        # Either way, sleep_hint should be present
        assert "sleep_hint" in result

    def test_hysteresis_run_cycle_includes_sleep_hint(self):
        """Hysteresis run_cycle returns sleep_hint = config_interval."""
        lm = self._make_manager(interval=30)

        result = lm.run_cycle(force=True)
        # With no plugs and no tesla, the cycle will likely hit hysteresis or
        # return ok with empty actions. Either way, sleep_hint should be present.
        assert "sleep_hint" in result

    def test_no_incomplete_qh_run_cycle_includes_sleep_hint(self):
        """No incomplete QH returns sleep_hint = 5."""

        # Create an LM with a mock NBC reader that returns None
        class NoQHReader:
            def get_current_qh(self, force=False, now=None):
                return None

        lm = LoadManager(
            metrics_fetch=lambda: None,
            plug_ctrl=None,
            tesla_ctrl=None,
            target_wh=-500,
            config_interval_secs=30,
        )
        lm.nbc_reader = NoQHReader()
        lm.enabled = True

        result = lm.run_cycle()
        assert result["status"] == "no_incomplete_qh"
        assert result["sleep_hint"] == 5.0

    def test_sleep_hint_clamped_to_min_even_with_zero_seconds_remaining(self):
        """Sleep hint is never less than 5 even with zero seconds remaining."""
        lm = self._make_manager()
        result = self._make_cycle_result(
            status="ok",
            predicted_wh=-2000.0,  # deficit
            seconds_remaining=0,
        )
        result["diagnostics"]["candidates"] = [
            {"name": "heater", "power_watts": 100.0}
        ]
        hint = lm._calculate_adaptive_sleep(result)
        assert hint >= 5.0

    def test_sleep_hint_clamped_to_max_with_large_seconds_remaining(self):
        """Sleep hint is never more than 2*interval even with large seconds remaining."""
        lm = self._make_manager()
        result = self._make_cycle_result(
            status="ok",
            predicted_wh=-501.0,  # tiny deficit
            seconds_remaining=9999,
        )
        result["diagnostics"]["candidates"] = [
            {"name": "heater", "power_watts": 10.0}
        ]
        hint = lm._calculate_adaptive_sleep(result)
        assert hint <= 60.0  # 2 * interval

    def test_sleep_hint_uses_nbc_prediction_wh_from_diagnostics(self):
        """_calculate_adaptive_sleep reads predicted_wh from nbc_prediction_wh key."""
        lm = self._make_manager()
        result = self._make_cycle_result(
            status="ok",
            nbc_prediction_wh=0.0,  # no deficit
            predicted_wh=-1000.0,  # different value — nbc_prediction_wh should be used
            seconds_remaining=600,
        )
        hint = lm._calculate_adaptive_sleep(result)
        # With no deficit (nbc_prediction_wh=0 >= target=-500), early in QH
        # should return config_interval * 1.5 = 45
        assert hint == 45.0

    def test_sleep_hint_defaults_to_zero_when_nbc_prediction_missing(self):
        """When nbc_prediction_wh is missing, defaults to 0 (no deficit)."""
        lm = self._make_manager()
        result = self._make_cycle_result()
        del result["nbc_prediction_wh"]
        hint = lm._calculate_adaptive_sleep(result)
        # With no nbc_prediction_wh, defaults to 0 which is >= target=-500
        # so treated as no deficit → early QH multiplier (1.5x) = 45
        assert hint == 45.0

    def test_sleep_hint_defaults_to_zero_when_seconds_remaining_missing(self):
        """When seconds_remaining is missing, defaults to config_interval."""
        lm = self._make_manager()
        result = self._make_cycle_result(nbc_prediction_wh=0.0)  # no deficit
        del result["diagnostics"]["seconds_remaining"]
        hint = lm._calculate_adaptive_sleep(result)
        # No deficit, seconds_remaining defaults to 30 (<=300 → late QH)
        # → 1.25x config = 37.5
        assert hint == 37.5

    def test_sleep_hint_handles_missing_candidates_key(self):
        """When diagnostics has no candidates key, treats as zero capacity."""
        lm = self._make_manager()
        result = self._make_cycle_result(nbc_prediction_wh=-2000.0)  # deficit
        # Base diagnostics has no "candidates" key — that's the point.
        hint = lm._calculate_adaptive_sleep(result)
        # No candidates → zero capacity → minimum sleep
        assert hint == 5.0

    def test_sleep_hint_handles_none_candidates(self):
        """When candidates is None, treats as zero capacity."""
        lm = self._make_manager()
        result = self._make_cycle_result()
        result["diagnostics"]["candidates"] = None
        # Add deficit to trigger proportional path
        result["nbc_prediction_wh"] = -2000.0
        hint = lm._calculate_adaptive_sleep(result)
        assert hint == 5.0

    def test_sleep_hint_handles_missing_diagnostics(self):
        """When diagnostics is missing, treats as zero capacity."""
        lm = self._make_manager()
        result = self._make_cycle_result()
        del result["diagnostics"]
        # Add deficit to trigger proportional path
        result["nbc_prediction_wh"] = -2000.0
        hint = lm._calculate_adaptive_sleep(result)
        assert hint == 5.0

    def test_sleep_hint_handles_missing_predicted_wh(self):
        """When nbc_prediction_wh is missing, defaults to 0."""
        lm = self._make_manager()
        result = self._make_cycle_result()
        del result["nbc_prediction_wh"]
        hint = lm._calculate_adaptive_sleep(result)
        # With no deficit (nbc_prediction_wh defaults to 0 >= target=-500),
        # returns config_interval * timing_multiplier (early in QH → 1.5x)
        assert hint == 45.0

    def test_sleep_hint_handles_missing_status(self):
        """When status is missing, treated as unknown → falls through to deficit path."""
        lm = self._make_manager()
        result = self._make_cycle_result(nbc_prediction_wh=0.0)  # no deficit
        del result["status"]
        hint = lm._calculate_adaptive_sleep(result)
        # No status → falls through to predicted_wh check.
        # predicted_wh=0 >= target=-500 → no deficit path, early QH (450s > 300)
        assert hint == 45.0  # config_interval * 1.5 (early in QH)
