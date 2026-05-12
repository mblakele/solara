"""Tests for VOCOlinc plug controller and composite controller routing."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from decouple import config

from load_manager import (
    AbstractPlugController,
    CompositePlugController,
    DeviceState,
    PlugConfig,
    LoadManager,
    VocolincPlugController,
    load_vocolinc_plugs_from_file,
    load_vocolinc_credentials,
)
import device_config

from metrics import EnergyCache

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)
import pytest

from tests.helpers import _make_metrics_with_wh


@pytest.fixture
def vocolinc_ctrl_plugs():
    """Provide a standard plugs dict for VocolincPlugController tests."""
    return {
        "lamp": PlugConfig(
            name="lamp",
            accessory_id="living_room_lamp",
            power_watts=100.0,
            role="flexible",
            controller_type="vocolinc",
        ),
    }


# --- Vocolinc credentials tests ---

def test_returns_tuple_when_both_set():
    """Returns (username, password) when both env vars are set."""
    config.set("VOCOLINC_USERNAME", "testuser")
    config.set("VOCOLINC_PASSWORD", "testpass")
    result = load_vocolinc_credentials()
    assert result == ("testuser", "testpass")


def test_returns_none_when_username_missing():
    """Returns None when username is not set."""
    config.set("VOCOLINC_PASSWORD", "testpass")
    result = load_vocolinc_credentials()
    assert result is None


def test_returns_none_when_password_missing():
    """Returns None when password is not set."""
    config.set("VOCOLINC_USERNAME", "testuser")
    result = load_vocolinc_credentials()
    assert result is None


def test_returns_none_when_both_missing():
    """Returns None when neither is set."""
    result = load_vocolinc_credentials()
    assert result is None


def test_strips_whitespace():
    """Strips leading/trailing whitespace from credentials."""
    config.set("VOCOLINC_USERNAME", "  testuser  ")
    config.set("VOCOLINC_PASSWORD", "  testpass  ")
    result = load_vocolinc_credentials()
    assert result == ("testuser", "testpass")


# --- Vocolinc plug env loading tests ---

def test_loads_valid_plug():
    """Loads valid VOCOlinc plug config."""
    with patch("device_config._load", return_value={
        "plugs": {
            "vocolinc": [
                {"name": "water_heater", "device_name": "floor_lamp",
                 "power_watts": 4500, "role": "flexible", "priority": 10},
            ]
        }
    }):
        device_config.reload()
        plugs = load_vocolinc_plugs_from_file()

    assert len(plugs) == 1
    wh = plugs["water_heater"]
    assert wh.accessory_id == "floor_lamp"
    assert wh.power_watts == 4500.0
    assert wh.role == "flexible"
    assert wh.priority == 10
    assert wh.controller_type == "vocolinc"


def test_default_priority_zero():
    """Defaults priority to 0 when not specified."""
    with patch("device_config._load", return_value={
        "plugs": {
            "vocolinc": [
                {"name": "test", "device_name": "device",
                 "power_watts": 1000, "role": "flexible"},
            ]
        }
    }):
        device_config.reload()
        plugs = load_vocolinc_plugs_from_file()

    assert plugs["test"].priority == 0


def test_controller_type_is_vocolinc():
    """Every loaded plug has controller_type='vocolinc'."""
    with patch("device_config._load", return_value={
        "plugs": {
            "vocolinc": [
                {"name": "a", "device_name": "dev1",
                 "power_watts": 500, "role": "fixed"},
                {"name": "b", "device_name": "dev2",
                 "power_watts": 1000, "role": "flexible"},
            ]
        }
    }):
        device_config.reload()
        plugs = load_vocolinc_plugs_from_file()

    for plug in plugs.values():
        assert plug.controller_type == "vocolinc"


def test_invalid_role_is_skipped():
    """Plugs with invalid role are skipped."""
    with patch("device_config._load", return_value={
        "plugs": {
            "vocolinc": [
                {"name": "bad", "device_name": "dev",
                 "power_watts": 500, "role": "invalid"},
            ]
        }
    }):
        device_config.reload()
        plugs = load_vocolinc_plugs_from_file()

    assert len(plugs) == 0


def test_invalid_format_is_skipped():
    """Plugs with too few parts are skipped."""
    with patch("device_config._load", return_value={
        "plugs": {
            "vocolinc": [
                {"name": "bad"},  # missing device_name, power_watts, role
            ]
        }
    }):
        device_config.reload()
        with pytest.raises(KeyError):
            load_vocolinc_plugs_from_file()


def test_multiple_plugs_loaded():
    """Multiple VOCOlinc plugs are loaded correctly."""
    with patch("device_config._load", return_value={
        "plugs": {
            "vocolinc": [
                {"name": "heater", "device_name": "heater_dev",
                 "power_watts": 4500, "role": "fixed", "priority": 10},
                {"name": "pump", "device_name": "pump_dev",
                 "power_watts": 1500, "role": "flexible", "priority": 20},
            ]
        }
    }):
        device_config.reload()
        plugs = load_vocolinc_plugs_from_file()

    assert len(plugs) == 2
    assert "heater" in plugs
    assert "pump" in plugs


# --- Vocolinc plug controller tests ---

def test_unknown_plug_returns_none(vocolinc_ctrl_plugs):
    """get_state returns None for unknown plug."""
    ctrl = VocolincPlugController(vocolinc_ctrl_plugs)
    state = asyncio.run(ctrl.get_state("nonexistent"))
    assert state is None


def test_set_unknown_plug_returns_false(vocolinc_ctrl_plugs):
    """set_state returns False for unknown plug."""
    ctrl = VocolincPlugController(vocolinc_ctrl_plugs)
    result = asyncio.run(ctrl.set_state("nonexistent", True))
    assert not result


def test_initialization_fails_without_credentials(vocolinc_ctrl_plugs):
    """Operations fail gracefully when no credentials configured."""
    ctrl = VocolincPlugController(vocolinc_ctrl_plugs)
    state = asyncio.run(ctrl.get_state("lamp"))
    assert state is None
    result = asyncio.run(ctrl.set_state("lamp", True))
    assert not result


def test_inherits_abstract_interface(vocolinc_ctrl_plugs):
    """VocolincPlugController is an instance of AbstractPlugController."""
    ctrl = VocolincPlugController(vocolinc_ctrl_plugs)
    assert isinstance(ctrl, AbstractPlugController)


def test_get_state_with_mock_client(vocolinc_ctrl_plugs):
    """get_state calls client.get_plug via asyncio.to_thread."""
    config.set("VOCOLINC_USERNAME", "test")
    config.set("VOCOLINC_PASSWORD", "test")

    with patch("load_manager.VOCOlinc") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.get_plug.return_value = True
        mock_instance.devices = {}

        ctrl = VocolincPlugController(vocolinc_ctrl_plugs)
        state = asyncio.run(ctrl.get_state("lamp"))

    assert state is True
    mock_instance.get_plug.assert_called_once_with("living_room_lamp")


def test_set_state_with_mock_client(vocolinc_ctrl_plugs):
    """set_state calls client.set_plug via asyncio.to_thread."""
    config.set("VOCOLINC_USERNAME", "test")
    config.set("VOCOLINC_PASSWORD", "test")

    with patch("load_manager.VOCOlinc") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.set_plug.return_value = None
        mock_instance.devices = {}

        ctrl = VocolincPlugController(vocolinc_ctrl_plugs)
        result = asyncio.run(ctrl.set_state("lamp", True))

    assert result is True
    mock_instance.set_plug.assert_called_once_with("living_room_lamp", True)


# --- Composite integration tests ---

def test_composite_turns_on_both_types():
    """Excess solar turns on both HomeKit and VOCOlinc plugs."""
    config.set("VOCOLINC_USERNAME", "test")
    config.set("VOCOLINC_PASSWORD", "test")
    config.set("LOAD_PLUG_CONTROLLER", "stub")

    with patch("device_config._load", return_value={
        "plugs": {
            "homekit": [
                {"name": "hk_heater", "accessory_id": "hk123",
                 "power_watts": 4500, "role": "flexible", "priority": 10},
            ],
            "vocolinc": [
                {"name": "vc_pump", "device_name": "vc_pump",
                 "power_watts": 1500, "role": "flexible", "priority": 20},
            ]
        }
    }):
        device_config.reload()

        with patch("load_manager.VOCOlinc") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.get_plug.return_value = False
            mock_instance.set_plug.return_value = None
            mock_instance.devices = {}

            mgr = LoadManager(
                metrics_fetch=lambda: _make_metrics_with_wh(
                    "main_panel", "QH3", -8000.0
                ),
                energy_cache=EnergyCache(),
                target_wh=-500,
                nbc_device="main_panel",
                enabled=True,
                dry_run=False,
            )

            assert isinstance(mgr.plug_ctrl, CompositePlugController)

            result = mgr.run_cycle()

            assert result["status"] == "ok"
            action_names = [a["device"] for a in result["actions"]]
            assert "hk_heater" in action_names
            assert "vc_pump" in action_names


def test_composite_over_target_turns_off_vocolinc():
    """Over-target turns off VOCOlinc flexible plug but not fixed."""
    config.set("VOCOLINC_USERNAME", "test")
    config.set("VOCOLINC_PASSWORD", "test")

    with patch("device_config._load", return_value={
        "plugs": {
            "vocolinc": [
                {"name": "vc_flex", "device_name": "vc_flex",
                 "power_watts": 1500, "role": "flexible", "priority": 20},
                {"name": "vc_fixed", "device_name": "vc_fixed",
                 "power_watts": 4500, "role": "fixed", "priority": 10},
            ]
        }
    }):
        device_config.reload()

        with patch("load_manager.VOCOlinc") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.get_plug.return_value = True
            mock_instance.set_plug.return_value = None
            mock_instance.devices = {}

            mgr = LoadManager(
                metrics_fetch=lambda: _make_metrics_with_wh(
                    "main_panel", "QH2", 2000.0
                ),
                energy_cache=EnergyCache(),
                target_wh=-500,
                nbc_device="main_panel",
                enabled=True,
                dry_run=False,
            )

            # Set both devices as ON in state tracker — use fixed timestamps
            with patch("load_manager.datetime") as mock_dt:
                mock_dt.now.return_value = fixed_now
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                mgr.state.devices["vc_flex"] = DeviceState(
                    name="vc_flex",
                    last_toggle=fixed_now - timedelta(seconds=120),
                    desired_state=True,
                )
                mgr.state.devices["vc_fixed"] = DeviceState(
                    name="vc_fixed",
                    last_toggle=fixed_now - timedelta(seconds=120),
                    desired_state=True,
                )

            with patch("load_manager.datetime") as mock_dt:
                mock_dt.now.return_value = fixed_now
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                result = mgr.run_cycle()

            action_names = [a["device"] for a in result["actions"]]
            assert "vc_flex" in action_names
            assert "vc_fixed" not in action_names
