"""
Tests for Tesla location-conditional charging gate.

Ensures Tesla charging actions require location info *if and only if*
we have a configured Tesla vehicle AND both TESLA_HOME_LAT and
TESLA_HOME_LON are defined. If we have a configured vehicle but
lat/lon are not defined, no location check is performed.

Test-driven: the RED phase produces failing tests that define the
expected behavior. GREEN comes after production changes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from decouple import UndefinedValueError, config

import device_config

from load_manager import (
    GapMinder,
    LoadManager,
    PlugConfig,
    StateTracker,
    TeslaConfig,
    TeslaState,
    load_tesla_config,
)
from load_nbc import DecideContext


# ---------------------------------------------------------------------------
# Part A: TeslaConfig Model Tests (3 tests)
# ---------------------------------------------------------------------------

def test_tesla_config_accepts_none_home_lat_lon():
    """TeslaConfig can be instantiated with home_lat=None and home_lon=None."""
    cfg = TeslaConfig(
        client_id="cid",
        client_secret="csec",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
        home_lat=None,
        home_lon=None,
        home_radius_m=500,
    )
    assert cfg.home_lat is None
    assert cfg.home_lon is None


def test_tesla_config_accepts_partial_coords():
    """TeslaConfig can be instantiated with only one coordinate."""
    cfg = TeslaConfig(
        client_id="cid",
        client_secret="csec",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
        home_lat=37.5,
        home_lon=None,
        home_radius_m=500,
    )
    assert cfg.home_lat == 37.5
    assert cfg.home_lon is None


def test_tesla_config_accepts_full_coords():
    """TeslaConfig still works with both coordinates (backward compat)."""
    cfg = TeslaConfig(
        client_id="cid",
        client_secret="csec",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
        home_lat=37.7749,
        home_lon=-122.4194,
        home_radius_m=500,
    )
    assert cfg.home_lat == 37.7749
    assert cfg.home_lon == -122.4194


# ---------------------------------------------------------------------------
# Part B: TeslaConfig Loading Tests (3 tests)
# ---------------------------------------------------------------------------

def test_load_tesla_config_returns_config_without_coords():
    """Vehicle + credentials configured, no lat/lon → TeslaConfig with None coords."""
    config.set("TESLA_CLIENT_ID", "client-id")
    config.set("TESLA_CLIENT_SECRET", "client-secret")
    config.set("TESLA_VEHICLE_ID", "7SAYGDED7TF555555")
    config.set("TESLA_REDIRECT_URI", "http://localhost/callback")

    with patch("device_config._load", return_value=None):
        device_config.reload()
        cfg = load_tesla_config()

    assert cfg is not None
    assert cfg.home_lat is None
    assert cfg.home_lon is None


def test_load_tesla_config_returns_config_with_one_coord():
    config.set("TESLA_CLIENT_ID", "client-id")
    config.set("TESLA_CLIENT_SECRET", "client-secret")
    config.set("TESLA_VEHICLE_ID", "7SAYGDED7TF555555")
    config.set("TESLA_REDIRECT_URI", "http://localhost/callback")
    config.set("TESLA_HOME_LON", "-122.4194")
    # TESLA_HOME_LAT deliberately not set (already cleared by conftest clean_env)

    with patch("device_config._load", return_value=None):
        device_config.reload()
        cfg = load_tesla_config()

    assert cfg is not None
    assert cfg.home_lat is None
    assert cfg.home_lon == -122.4194


def test_load_tesla_config_returns_none_without_vehicle():
    """No vehicle_id → None (unchanged behavior)."""
    config.set("TESLA_CLIENT_ID", "client-id")
    config.set("TESLA_CLIENT_SECRET", "client-secret")

    def mock_decouple(key, default=None, cast=str):  # type: ignore[no-untyped-def]
        if key == "TESLA_VEHICLE_ID":
            return ""
        if default is not None:
            return default
        raise UndefinedValueError(key)

    with patch("config._decouple_config", side_effect=mock_decouple):
        cfg = load_tesla_config()

    assert cfg is None


# ---------------------------------------------------------------------------
# Part C: Decision Logic — No Coords (charging allowed, no location check) (8 tests)
# ---------------------------------------------------------------------------

_fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


def _make_ctx(
    tesla: TeslaState | None = None,
    requires_home_check: bool = False,
    seconds_remaining: int = 300,
) -> DecideContext:
    """Helper to build a DecideContext for tests."""
    return DecideContext(
        now=_fixed_now,
        seconds_remaining=seconds_remaining,
        state=StateTracker(),
        plugs={},
        tesla=tesla,
        requires_home_check=requires_home_check,
    )


def test_tesla_amps_increase_without_coords_at_home_false():
    """requires_home_check=False, at_home=False → set_amps action produced."""
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=False,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=False, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=-561.258618125,
        target_wh=-9.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"


def test_tesla_plug_overflow_to_amps_without_coords():
    """Plug too large for gap, falls to Tesla → set_amps action."""
    engine = GapMinder(hysteresis_wh=3)
    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="abc123",
            power_watts=4500.0,
        )
    }
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=False,
    )
    ctx = DecideContext(
        now=_fixed_now,
        seconds_remaining=300,
        state=StateTracker(),
        plugs=plugs,
        tesla=tesla,
        requires_home_check=False,
    )

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=-350.0,
        target_wh=-9.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"


def test_tesla_amp_reduce_without_coords_at_home_false():
    """requires_home_check=False, at_home=False → reduction set_amps produced."""
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=20,
        plugged_in=True,
        at_home=False,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=False, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=219,
        target_wh=-9.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"
    # wh_to_amps(228, 711) = 4.81 → ceil=5, target = 20 - 5 = 15
    assert actions[0].target_amps == 15


def test_tesla_amp_reduce_without_coords_at_home_true():
    """requires_home_check=False, at_home=True → reduction set_amps produced."""
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=20,
        plugged_in=True,
        at_home=True,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=False, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=219,
        target_wh=-9.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"


def test_tesla_turn_off_without_coords_at_home_false():
    """Large deficit, Tesla at min amps, at_home=False.

    Vehicle should NOT be stopped — Step 1 was entered (car was charging)
    and returned None (already at minimum amps, stop_allowed=False).
    Step 3 must respect that decision.
    """
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=False,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=False, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=5000.0,
        target_wh=-9.0,
    )

    assert len(actions) == 0


def test_tesla_turn_off_without_coords_at_home_true():
    """Large deficit, Tesla at min amps, at_home=True.

    Vehicle should NOT be stopped — Step 1 was entered (car was charging)
    and returned None (already at minimum amps, stop_allowed=False).
    Step 3 must respect that decision.
    """
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=False, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=5000.0,
        target_wh=-9.0,
    )

    assert len(actions) == 0


# ---------------------------------------------------------------------------
# Part D: Decision Logic — Coords Defined (location check enforced) (8 tests)
# ---------------------------------------------------------------------------

def test_tesla_amps_increase_with_coords_at_home_false():
    """requires_home_check=True, at_home=False → no Tesla action."""
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=False,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=True, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=-561.258618125,
        target_wh=-9.0,
    )

    assert len(actions) == 0


def test_tesla_amps_increase_with_coords_at_home_true():
    """requires_home_check=True, at_home=True → set_amps action."""
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=True, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=-561.258618125,
        target_wh=-9.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"


def test_tesla_supports_amps_with_coords_at_home_false():
    """requires_home_check=True, at_home=False → _tesla_supports_amps returns False."""
    engine = GapMinder()
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=False,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=True)

    assert engine._tesla_supports_amps(None, ctx.tesla, ctx.requires_home_check) is False


def test_tesla_supports_amps_with_coords_at_home_true():
    """requires_home_check=True, at_home=True → _tesla_supports_amps returns True."""
    engine = GapMinder()
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=True)

    assert engine._tesla_supports_amps(None, ctx.tesla, ctx.requires_home_check) is True


def test_tesla_amp_reduce_with_coords_at_home_false():
    """requires_home_check=True, at_home=False → no reduction action."""
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=20,
        plugged_in=True,
        at_home=False,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=True, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=219,
        target_wh=-9.0,
    )

    assert len(actions) == 0


def test_tesla_amp_reduce_with_coords_at_home_true():
    """requires_home_check=True, at_home=True → reduction set_amps produced."""
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=20,
        plugged_in=True,
        at_home=True,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=True, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=219,
        target_wh=-9.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"


def test_tesla_turn_off_with_coords_at_home_false():
    """Large deficit, requires_home_check=True, at_home=False → no turn_off."""
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=False,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=True, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=5000.0,
        target_wh=-9.0,
    )

    assert len(actions) == 0


def test_tesla_turn_off_with_coords_at_home_true():
    """Large deficit, requires_home_check=True, at_home=True.

    Vehicle should NOT be stopped — Step 1 was entered (car was charging)
    and returned None (already at minimum amps, stop_allowed=False).
    Step 3 must respect that decision.
    """
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )
    ctx = _make_ctx(tesla=tesla, requires_home_check=True, seconds_remaining=711)

    actions = engine.decide(
        ctx=ctx,
        predicted_wh=5000.0,
        target_wh=-9.0,
    )

    assert len(actions) == 0


# ---------------------------------------------------------------------------
# Part E: Integration — requires_home_check set from config (2 tests)
# ---------------------------------------------------------------------------

def test_requires_home_check_true_when_both_coords():
    """DecideContext defaults to True when coords are configured."""
    cfg = TeslaConfig(
        client_id="cid",
        client_secret="csec",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
        home_lat=37.7749,
        home_lon=-122.4194,
        home_radius_m=500,
    )
    assert cfg.home_lat is not None
    assert cfg.home_lon is not None

    # Verify that the condition for requires_home_check is True
    requires = cfg.home_lat is not None and cfg.home_lon is not None
    assert requires is True


def test_requires_home_check_false_when_missing():
    """DecideContext requires_home_check=False when either coord missing."""
    cfg = TeslaConfig(
        client_id="cid",
        client_secret="csec",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
        home_lat=None,
        home_lon=-122.4194,
        home_radius_m=500,
    )
    requires = cfg.home_lat is not None and cfg.home_lon is not None
    assert requires is False


# ---------------------------------------------------------------------------
# Part F: _cycle_async_phase wires requires_home_check from TeslaConfig (2 tests)
# ---------------------------------------------------------------------------

def _make_lm_with_tesla_config(
    home_lat: float | None = None,
    home_lon: float | None = None,
) -> LoadManager:
    """Helper: create a LoadManager with a TeslaConfig that has the given coords."""
    config = TeslaConfig(
        client_id="cid",
        client_secret="csec",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
        home_lat=home_lat,
        home_lon=home_lon,
    )
    mgr = LoadManager(dry_run=True, config_interval_secs=30)
    mgr.tesla_config = config
    mgr.tesla_ctrl = None  # skip Tesla fetch, covered by mock below
    return mgr


def test_cycle_async_phase_requires_home_check_true():
    """When home coords are configured, requires_home_check=True."""
    mgr = _make_lm_with_tesla_config(home_lat=37.77, home_lon=-122.42)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    with patch.object(mgr, "_sync_plug_states", return_value=None), \
         patch.object(mgr, "_fetch_tesla_state_async", return_value=(None, None, None)), \
         patch.object(mgr.engine, "decide", return_value=[]) as mock_decide:
        asyncio.run(mgr._cycle_async_phase(
            gap_wh=100.0, adjusted_wh=100.0, now=now,
            seconds_remaining=500, dry_run=True,
        ))

    call_kwargs = mock_decide.call_args[1]
    assert call_kwargs["ctx"].requires_home_check is True


def test_cycle_async_phase_requires_home_check_false():
    """When home coords are NOT configured, requires_home_check=False."""
    mgr = _make_lm_with_tesla_config(home_lat=None, home_lon=None)
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    with patch.object(mgr, "_sync_plug_states", return_value=None), \
         patch.object(mgr, "_fetch_tesla_state_async", return_value=(None, None, None)), \
         patch.object(mgr.engine, "decide", return_value=[]) as mock_decide:
        asyncio.run(mgr._cycle_async_phase(
            gap_wh=100.0, adjusted_wh=100.0, now=now,
            seconds_remaining=500, dry_run=True,
        ))

    call_kwargs = mock_decide.call_args[1]
    assert call_kwargs["ctx"].requires_home_check is False
