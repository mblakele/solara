"""Integration tests for LoadManager run_cycle scenarios."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from load_manager import (
    DeviceState,
    LoadManager,
    NBCCache,
    PendingEffect,
    PlugConfig,
    PlugController,
    StateTracker,
    TeslaConfig,
    TeslaController,
    TeslaState,
    TetrisEngine,
)
from tests.helpers import _make_metrics_with_wh


# --- Excess solar helpers ---


def _make_excess_manager(
    predicted_wh: float = -2000.0, incomplete_qh: str = "QH3"
) -> tuple[LoadManager, PlugController, TeslaController]:
    """Create LoadManager with stub controllers and mock metrics."""
    plugs = {
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc123",
            power_watts=4500.0,
            role="flexible",
            priority=20,
        ),
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="xyz789",
            power_watts=1500.0,
            role="fixed",
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

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
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
            role="flexible",
            priority=10,
        ),
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc123",
            power_watts=4500.0,
            role="fixed",
            priority=20,
        ),
    }
    plug_ctrl = PlugController(plugs)

    asyncio.run(plug_ctrl.set_state("pool_pump", True))
    asyncio.run(plug_ctrl.set_state("water_heater", True))

    metrics_data = _make_metrics_with_wh("main_panel", "QH2", predicted_wh)

    def metrics_fetch():
        return metrics_data

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
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

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
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
    """Load shedding: turns off plugs in priority order."""
    # have 2000 deficit
    # water heater p20 4500w
    # pool pump p10 1500w
    # desired: shed water before pool pump
    mgr, plug_ctrl = _make_overn_target_manager(predicted_wh=2000.0)

    result = mgr.run_cycle()

    assert result["status"] == "ok"
    wh_state = asyncio.run(plug_ctrl.get_state("water_heater"))
    pp_state = asyncio.run(plug_ctrl.get_state("pool_pump"))
    assert wh_state
    assert pp_state is False


def test_plug_states_updated():
    """Excess solar: plug controller states are updated."""
    mgr, plug_ctrl, _ = _make_excess_manager(predicted_wh=-6000.0)

    mgr.run_cycle()

    wh_state = asyncio.run(plug_ctrl.get_state("water_heater"))
    pp_state = asyncio.run(plug_ctrl.get_state("pool_pump"))
    assert wh_state
    assert pp_state


# --- Over-target tests ---


def test_turns_off_flexible_only():
    """Over-target: turns off flexible plugs only, not fixed."""
    mgr, _ = _make_overn_target_manager(predicted_wh=2000.0)

    result = mgr.run_cycle()

    action_names = [a["device"] for a in result["actions"]]
    assert "pool_pump" in action_names
    assert "water_heater" not in action_names


def test_fixed_plug_remains_on():
    """Over-target: fixed plug state is unchanged."""
    mgr, plug_ctrl = _make_overn_target_manager(predicted_wh=2000.0)

    mgr.run_cycle()

    wh_state = asyncio.run(plug_ctrl.get_state("water_heater"))
    assert wh_state


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

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
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
            timestamp=now, power_delta_wh=500.0,
        )
    )

    result = mgr.run_cycle()

    assert result["status"] == "stale_data"


def test_stale_no_pending_effects_proceeds():
    """NBC data >120s old with zero pending effects should NOT skip.

    Regression test: previously, stale data alone (with no unreflected
    actions) caused permanent lockout since last_nbc_fetch was never
    updated while stuck in the stale path. With zero pending effects,
    there are no unreflected actions, so it's safe to proceed.
    """
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=130)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at

    def metrics_fetch():
        return metrics_data

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    assert len(mgr.state.pending_effects) == 0

    result = mgr.run_cycle()

    assert result["status"] != "stale_data"


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

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    now = datetime.now(timezone.utc)
    mgr.state.last_nbc_fetch = fetched_at
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=now,
            power_delta_wh=500.0,
        )
    )

    result = mgr.run_cycle()

    assert result["status"] == "waiting_for_fresh_data"


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

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
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
            timestamp=now, power_delta_wh=500.0,
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

    now = datetime.now(timezone.utc)
    fetched_at = now - timedelta(seconds=10)
    data_point_at = fetched_at - timedelta(seconds=50)  # 60s ago
    effect_time = fetched_at - timedelta(seconds=30)     # 40s ago

    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at
    metrics_data["_data_lag_secs"] = 50.0

    def metrics_fetch():
        return metrics_data

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    mgr.state.last_nbc_fetch = fetched_at
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=effect_time,
            power_delta_wh=500.0,
        )
    )

    result = mgr.run_cycle()

    assert result["status"] == "waiting_for_fresh_data"


def test_full_lifecycle_action_wait_resolve():
    """Full lifecycle: action taken -> wait for fresh data -> new NBC fetch
    arrives with updated timestamp -> pending effects pruned -> new decision."""
    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            role="flexible",
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    metrics_data_1 = _make_metrics_with_wh("main_panel", "QH3", -6000.0)
    metrics_data_2 = _make_metrics_with_wh("main_panel", "QH3", -4500.0)

    fetch_sequence = [metrics_data_1, metrics_data_2]
    fetch_index = [0]

    def metrics_fetch():
        data = fetch_sequence[fetch_index[0]]
        fetch_index[0] += 1
        return data

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
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
            power_delta_wh=100.0,
        ),
        PendingEffect(
            device_name="b", action="turn_on",
            timestamp=base_time + timedelta(seconds=30),
            power_delta_wh=200.0,
        ),
        PendingEffect(
            device_name="c", action="turn_off",
            timestamp=base_time + timedelta(seconds=60),
            power_delta_wh=-150.0,
        ),
    ])

    assert tracker.pending_since_count(base_time) == 2
    assert tracker.pending_since_count(base_time - timedelta(seconds=120)) == 3
    assert tracker.pending_since_count(base_time + timedelta(seconds=120)) == 0


def test_prune_old_effects():
    """prune_old_effects removes effects before cutoff."""
    tracker = StateTracker()
    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    tracker.pending_effects.extend([
        PendingEffect(
            device_name="old1", action="turn_on",
            timestamp=base_time - timedelta(seconds=60),
            power_delta_wh=100.0,
        ),
        PendingEffect(
            device_name="old2", action="turn_off",
            timestamp=base_time - timedelta(seconds=30),
            power_delta_wh=-50.0,
        ),
        PendingEffect(
            device_name="recent", action="turn_on",
            timestamp=base_time + timedelta(seconds=10),
            power_delta_wh=200.0,
        ),
    ])

    pruned = tracker.prune_old_effects(base_time)
    assert pruned == 2
    assert len(tracker.pending_effects) == 1
    assert tracker.pending_effects[0].device_name == "recent"


# --- Pending effect lifecycle tests ---


def test_adjusted_wh_in_response():
    """Response includes both raw predicted_wh and adjusted_wh."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -500.0)

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        nbc_cache=nbc_cache,
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

    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=180)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        nbc_cache=nbc_cache,
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
            power_delta_wh=500.0,
        ),
        PendingEffect(
            device_name="plug2", action="turn_off",
            timestamp=now - timedelta(seconds=30),
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
            role="flexible",
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=180)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        nbc_cache=nbc_cache,
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
            device_name="heater", action="turn_on",
            timestamp=now - timedelta(seconds=200),
            power_delta_wh=500.0,
        ),
        PendingEffect(
            device_name="heater", action="turn_off",
            timestamp=now - timedelta(seconds=160),
            power_delta_wh=-200.0,
        ),
    ])

    result = mgr.run_cycle()

    assert result["status"] == "stale_data"
    assert len(mgr.state.pending_effects) == 1
    assert mgr.state.pending_effects[0].device_name == "heater"
    assert mgr.state.pending_effects[0].action == "turn_off"


def test_stale_data_includes_candidates():
    """Stale data response includes candidates in diagnostics."""
    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            role="flexible",
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=180)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        nbc_cache=nbc_cache,
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
            timestamp=now, power_delta_wh=500.0,
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
    assert heater_candidate["role"] == "flexible"
    assert heater_candidate["power_watts"] == 2000.0


def test_waiting_for_fresh_data_includes_count():
    """Waiting response includes pending_effects_count."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        nbc_cache=nbc_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    now = datetime.now(timezone.utc)
    mgr.state.last_nbc_fetch = fetched_at
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="plug", action="turn_on",
            timestamp=now,
            power_delta_wh=500.0,
        )
    )

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
            role="flexible",
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)
    metrics_data["_fetched_at"] = fetched_at

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        nbc_cache=nbc_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    now = datetime.now(timezone.utc)
    mgr.state.last_nbc_fetch = fetched_at
    mgr.state.pending_effects.append(
        PendingEffect(
            device_name="heater", action="turn_on",
            timestamp=now,
            power_delta_wh=500.0,
        )
    )

    result = mgr.run_cycle()

    assert result["status"] == "waiting_for_fresh_data"
    candidates = result["diagnostics"]["candidates"]
    assert candidates is not None
    heater_candidate = next(
        (c for c in candidates if c["name"] == "heater"), None
    )
    assert heater_candidate is not None
    assert heater_candidate["role"] == "flexible"


# --- Cache tests ---


def test_cache_hits_within_ttl():
    """Second call within TTL + same QH uses cache."""
    fetch_count = [0]

    def metrics_fetch():
        fetch_count[0] += 1
        return _make_metrics_with_wh("main_panel", "QH3", -2000.0)

    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)
    nbc_cache = NBCCache(ttl_seconds=60)

    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    mgr.run_cycle()
    first_count = fetch_count[0]
    mgr.run_cycle()

    assert fetch_count[0] == first_count


def test_disabled_returns_early():
    """Disabled manager returns disabled status immediately."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -2000.0)

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        nbc_cache=nbc_cache,
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
            role="flexible",
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -6000.0)

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        nbc_cache=nbc_cache,
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
            role="flexible",
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -6000.0)

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        nbc_cache=nbc_cache,
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
            role="flexible",
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)

    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -6000.0)

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=lambda: metrics_data,
        nbc_cache=nbc_cache,
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
            role="fixed",
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

    metrics_data = _make_metrics_with_wh("main_panel", "QH3", -3000.0)

    def metrics_fetch():
        return metrics_data

    nbc_cache = NBCCache(ttl_seconds=60)
    mgr = LoadManager(
        metrics_fetch=metrics_fetch,
        nbc_cache=nbc_cache,
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
    engine = TetrisEngine(hysteresis_wh=3)
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
            role="flexible",
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
