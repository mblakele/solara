"""Tests for stub PlugController and TeslaController implementations."""

import asyncio

import pytest

from load_manager import (
    AbstractPlugController,
    AbstractTeslaController,
    PlugConfig,
    TeslaConfig,
    TeslaState,
    PlugController,
    TeslaController,
)


# --- Fixtures ---


@pytest.fixture
def stub_plugs():
    """Provide a standard plugs dict for PlugController stub tests."""
    return {
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


@pytest.fixture
def stub_tesla_config():
    """Provide a standard TeslaConfig for TeslaController stub tests."""
    return TeslaConfig(
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


# --- PlugController stub tests ---


def test_initial_state_off(stub_plugs):
    """All plugs start off."""
    ctrl = PlugController(stub_plugs)
    state = asyncio.run(ctrl.get_state("water_heater"))
    assert not state


def test_set_state_on(stub_plugs):
    """set_state turns plug on and updates internal state."""
    ctrl = PlugController(stub_plugs)
    result = asyncio.run(ctrl.set_state("water_heater", True))
    assert result
    state = asyncio.run(ctrl.get_state("water_heater"))
    assert state


def test_set_state_off(stub_plugs):
    """set_state turns plug off and updates internal state."""
    ctrl = PlugController(stub_plugs)
    asyncio.run(ctrl.set_state("pool_pump", True))
    result = asyncio.run(ctrl.set_state("pool_pump", False))
    assert result
    state = asyncio.run(ctrl.get_state("pool_pump"))
    assert not state


def test_unknown_plug_returns_none(stub_plugs):
    """get_state returns None for unknown plug name."""
    ctrl = PlugController(stub_plugs)
    state = asyncio.run(ctrl.get_state("nonexistent"))
    assert state is None


def test_set_unknown_plug_returns_false(stub_plugs):
    """set_state returns False for unknown plug name."""
    ctrl = PlugController(stub_plugs)
    result = asyncio.run(ctrl.set_state("nonexistent", True))
    assert not result


def test_action_log_tracks_calls(stub_plugs):
    """action_log records all set_state calls in order."""
    ctrl = PlugController(stub_plugs)
    asyncio.run(ctrl.set_state("water_heater", True))
    asyncio.run(ctrl.set_state("pool_pump", True))
    asyncio.run(ctrl.set_state("water_heater", False))

    assert len(ctrl.action_log) == 3
    assert ctrl.action_log[0].name == "water_heater"
    assert ctrl.action_log[0].on
    assert ctrl.action_log[1].name == "pool_pump"
    assert ctrl.action_log[1].on
    assert ctrl.action_log[2].name == "water_heater"
    assert not ctrl.action_log[2].on


def test_inherits_abstract_interface(stub_plugs):
    """PlugController is an instance of AbstractPlugController."""
    ctrl = PlugController(stub_plugs)
    assert isinstance(ctrl, AbstractPlugController)


# --- TeslaController stub tests ---


def test_default_state(stub_tesla_config):
    """Default state matches constructor defaults."""
    ctrl = TeslaController(stub_tesla_config)
    state = asyncio.run(ctrl.get_charging_state())
    assert state is not None
    assert not state.is_charging
    assert state.current_amps is None
    assert state.soc_percent == 50.0
    assert not state.plugged_in
    assert state.at_home
    assert not state.at_charge_limit


def test_set_mock_state(stub_tesla_config):
    """set_mock_state replaces internal state."""
    ctrl = TeslaController(stub_tesla_config)
    new_state = TeslaState(
        is_charging=True,
        current_amps=32,
        soc_percent=75.0,
        plugged_in=True,
        at_home=False,
        at_charge_limit=True,
    )
    ctrl.set_mock_state(new_state)

    state = asyncio.run(ctrl.get_charging_state())
    assert state.is_charging
    assert state.current_amps == 32
    assert not state.at_home
    assert state.at_charge_limit


def test_start_charging(stub_tesla_config):
    """start_charging sets is_charging and current_amps."""
    ctrl = TeslaController(stub_tesla_config)
    result = asyncio.run(ctrl.start_charging())
    assert result
    state = asyncio.run(ctrl.get_charging_state())
    assert state.is_charging
    assert state.current_amps == 5


def test_stop_charging(stub_tesla_config):
    """stop_charging clears is_charging and current_amps."""
    ctrl = TeslaController(stub_tesla_config)
    ctrl.set_mock_state(
        TeslaState(
            is_charging=True,
            current_amps=24,
            soc_percent=60.0,
            plugged_in=True,
            at_home=True,
            at_charge_limit=False,
        )
    )
    result = asyncio.run(ctrl.stop_charging())
    assert result
    state = asyncio.run(ctrl.get_charging_state())
    assert not state.is_charging
    assert state.current_amps is None


def test_inherits_abstract_interface_tesla(stub_tesla_config):
    """TeslaController is an instance of AbstractTeslaController."""
    ctrl = TeslaController(stub_tesla_config)
    assert isinstance(ctrl, AbstractTeslaController)


def test_set_charge_amps_clamps_to_range(stub_tesla_config):
    """set_charge_amps clamps to [min, max] range."""
    ctrl = TeslaController(stub_tesla_config)
    asyncio.run(ctrl.set_charge_amps(1))
    state = asyncio.run(ctrl.get_charging_state())
    assert state.current_amps == 5

    asyncio.run(ctrl.set_charge_amps(100))
    state = asyncio.run(ctrl.get_charging_state())
    assert state.current_amps == 48


def test_set_charge_amps_starts_charging(stub_tesla_config):
    """set_charge_amps also sets is_charging to True."""
    ctrl = TeslaController(stub_tesla_config)
    result = asyncio.run(ctrl.set_charge_amps(24))
    assert result
    state = asyncio.run(ctrl.get_charging_state())
    assert state.is_charging
    assert state.current_amps == 24


def test_is_at_home_returns_mock_value(stub_tesla_config):
    """is_at_home returns the mocked value."""
    ctrl = TeslaController(stub_tesla_config)
    result = asyncio.run(ctrl.is_at_home())
    assert result

    ctrl.set_mock_state(
        TeslaState(
            is_charging=False,
            current_amps=None,
            soc_percent=50.0,
            plugged_in=False,
            at_home=False,
            at_charge_limit=False,
        )
    )
    result = asyncio.run(ctrl.is_at_home())
    assert not result


def test_is_plugged_in_returns_mock_value(stub_tesla_config):
    """is_plugged_in returns the mocked value."""
    ctrl = TeslaController(stub_tesla_config)
    ctrl.set_mock_state(
        TeslaState(
            is_charging=False,
            current_amps=None,
            soc_percent=50.0,
            plugged_in=True,
            at_home=True,
            at_charge_limit=False,
        )
    )
    result = asyncio.run(ctrl.is_plugged_in())
    assert result


def test_is_at_charge_limit_returns_mock_value(stub_tesla_config):
    """is_at_charge_limit returns the mocked value."""
    ctrl = TeslaController(stub_tesla_config)
    ctrl.set_mock_state(
        TeslaState(
            is_charging=True,
            current_amps=48,
            soc_percent=90.0,
            plugged_in=True,
            at_home=True,
            at_charge_limit=True,
        )
    )
    result = asyncio.run(ctrl.is_at_charge_limit())
    assert result


def test_get_charge_limit_pct_returns_none_when_limited(stub_tesla_config):
    """get_charge_limit_pct returns None when at charge limit."""
    ctrl = TeslaController(stub_tesla_config)
    ctrl.set_mock_state(
        TeslaState(
            is_charging=True,
            current_amps=48,
            soc_percent=90.0,
            plugged_in=True,
            at_home=True,
            at_charge_limit=True,
        )
    )
    result = asyncio.run(ctrl.get_charge_limit_pct())
    assert result is None


def test_authenticate_noop(stub_tesla_config):
    """authenticate is a no-op that completes without error."""
    ctrl = TeslaController(stub_tesla_config)
    asyncio.run(ctrl.authenticate())


def test_get_charge_limit_pct_returns_soc_when_not_limited(stub_tesla_config):
    """get_charge_limit_pct returns SOC percentage when not at charge limit."""
    ctrl = TeslaController(stub_tesla_config)
    # Default state has soc_percent=50.0, at_charge_limit=False
    result = asyncio.run(ctrl.get_charge_limit_pct())
    assert result == 50.0

    ctrl.set_mock_state(
        TeslaState(
            is_charging=True,
            current_amps=32,
            soc_percent=75.0,
            plugged_in=True,
            at_home=True,
            at_charge_limit=False,
        )
    )
    result = asyncio.run(ctrl.get_charge_limit_pct())
    assert result == 75.0
