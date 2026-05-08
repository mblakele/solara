"""Tests for load_controllers.py — standalone functions, stub controllers, and composite controller.

Covers:
  - _haversine_distance (lines ~158-184)
  - Tesla token persistence functions (lines ~187-250)
  - PlugController stub expansion (lines ~46-73)
  - TeslaController stub expansion (lines ~76-156)
  - CompositePlugController routing and merging (lines ~1148-1219)
  - RealTeslaController.reset_session (lines ~719-730)
  - RealPlugController._load_pairing_data / _save_pairing_data (lines ~817-839)
  - VOCOlincPlugController._ensure_initialized (lines ~1050-1087)
  - VOCOlincPlugController error paths (lines ~1089-1145)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Import directly from load_controllers (not via load_manager)
from load_controllers import (
    CompositePlugController,
    PlugController,
    RealPlugController,
    TeslaController,
    VocolincPlugController,
    _haversine_distance,
    load_tesla_tokens,
    remove_tesla_tokens,
    save_tesla_tokens,
)

# Import shared models for fixtures and assertions
from load_models import PlugAction, PlugConfig, TeslaConfig


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def plug_config():
    """Standard PlugConfig for tests."""
    return {
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc123",
            power_watts=4500.0,
            role="flexible",
            priority=20,
        ),
    }


@pytest.fixture()
def tesla_config():
    """Standard TeslaConfig for tests."""
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


# =============================================================================
# 1. _haversine_distance (lines ~158-184)
# =============================================================================


class TestHaversineDistance:

    def test_same_point_returns_zero(self):
        """Identical coordinates should yield 0 m distance."""
        assert _haversine_distance(37.0, -122.0, 37.0, -122.0) == pytest.approx(0, abs=0.01)

    def test_known_distance_nyc_to_la(self):
        """NYC to LA is approximately 3940 km — verify within +/-1%."""
        # NYC: (40.7128, -74.0060), LA: (34.0522, -118.2437)
        distance_m = _haversine_distance(40.7128, -74.0060, 34.0522, -118.2437)
        expected_m = 3_940_000.0
        assert distance_m == pytest.approx(expected_m, rel=0.01)

    def test_same_lat_different_lon(self):
        """Moving purely along longitude at equator should give known distance."""
        # At the equator, 1 degree of longitude ~ 111.3 km
        distance_m = _haversine_distance(0.0, 0.0, 0.0, 1.0)
        # Roughly 111 km; allow generous tolerance for spherical approximation
        assert distance_m == pytest.approx(111_000, rel=0.05)


# =============================================================================
# 2. Tesla token persistence functions (lines ~187-249)
# =============================================================================


class TestTeslaTokenPersistence:

    def test_load_tesla_tokens_no_file(self, tmp_path):
        """Loading from a non-existent path returns None."""
        result = load_tesla_tokens(tmp_path / "nonexistent.json")
        assert result is None

    def test_load_tesla_tokens_valid(self, tmp_path):
        """Valid token file returns dict with all three required keys."""
        tokens_file = tmp_path / "tokens.json"
        tokens_data = {
            "refresh_token": "rt_abc",
            "access_token": "at_xyz",
            "expires": 9999,
        }
        tokens_file.write_text(json.dumps(tokens_data))

        result = load_tesla_tokens(tokens_file)
        assert result == tokens_data

    def test_load_tesla_tokens_missing_keys(self, tmp_path):
        """File missing required keys returns None and logs a warning."""
        tokens_file = tmp_path / "tokens.json"
        # Only has refresh_token, missing access_token and expires
        tokens_file.write_text(json.dumps({"refresh_token": "rt_abc"}))

        result = load_tesla_tokens(tokens_file)
        assert result is None

    def test_load_tesla_tokens_invalid_json(self, tmp_path):
        """Corrupt JSON file returns None and logs an error."""
        tokens_file = tmp_path / "tokens.json"
        tokens_file.write_text("not valid json {{{")

        result = load_tesla_tokens(tokens_file)
        assert result is None

    def test_save_and_load_roundtrip(self, tmp_path):
        """Saving then loading returns the same data."""
        tokens_file = tmp_path / "tokens.json"

        save_tesla_tokens(
            refresh_token="rt_123",
            access_token="at_456",
            expires=7890,
            tokens_path=tokens_file,
        )

        result = load_tesla_tokens(tokens_file)
        assert result == {
            "refresh_token": "rt_123",
            "access_token": "at_456",
            "expires": 7890,
        }

    def test_remove_tesla_tokens_existing(self, tmp_path):
        """Removing an existing file deletes it."""
        tokens_file = tmp_path / "tokens.json"
        tokens_file.write_text(json.dumps({"refresh_token": "rt"}))

        remove_tesla_tokens(tokens_file)
        assert not tokens_file.exists()

    def test_remove_tesla_tokens_nonexistent(self, tmp_path):
        """Removing a non-existent file does not raise."""
        tokens_file = tmp_path / "tokens.json"
        # Should not raise even if file doesn't exist
        remove_tesla_tokens(tokens_file)


# =============================================================================
# 3. PlugController stub expansion (lines ~46-72)
# =============================================================================


class TestPlugControllerExpansion:

    def test_initial_state_all_off(self, plug_config):
        """All plugs start in the off state."""
        ctrl = PlugController(plug_config)
        assert asyncio.run(ctrl.get_state("water_heater")) is False

    def test_toggles_multiple_times(self, plug_config):
        """State flips back and forth; action_log tracks all calls."""
        ctrl = PlugController(plug_config)

        asyncio.run(ctrl.set_state("water_heater", True))
        assert asyncio.run(ctrl.get_state("water_heater")) is True

        asyncio.run(ctrl.set_state("water_heater", False))
        assert asyncio.run(ctrl.get_state("water_heater")) is False

        asyncio.run(ctrl.set_state("water_heater", True))
        assert len(ctrl.action_log) == 3

    def test_unknown_get_logs_warning(self, plug_config, caplog):
        """get_state for unknown plug returns None and logs a warning."""
        ctrl = PlugController(plug_config)

        with caplog.at_level("WARNING"):
            result = asyncio.run(ctrl.get_state("nonexistent"))

        assert result is None
        assert "Unknown plug nonexistent" in caplog.text

    def test_unknown_set_logs_warning(self, plug_config, caplog):
        """set_state for unknown plug returns False and logs a warning."""
        ctrl = PlugController(plug_config)

        with caplog.at_level("WARNING"):
            result = asyncio.run(ctrl.set_state("nonexistent", True))

        assert result is False
        assert "Unknown plug nonexistent" in caplog.text

    def test_action_log_types(self, plug_config):
        """Each action log entry is a PlugAction with correct name/on fields."""
        ctrl = PlugController(plug_config)
        asyncio.run(ctrl.set_state("water_heater", True))

        entry = ctrl.action_log[0]
        assert isinstance(entry, PlugAction)
        assert entry.name == "water_heater"
        assert entry.on is True


# =============================================================================
# 4. TeslaController stub expansion (lines ~76-155)
# =============================================================================


class TestTeslaControllerExpansion:

    def test_default_is_not_at_charge_limit(self, tesla_config):
        """Default charge limit returns 50.0 (not None) when not limited."""
        ctrl = TeslaController(tesla_config)
        result = asyncio.run(ctrl.get_charge_limit_pct())
        assert result == 50.0

    def test_set_amps_exact_value(self, tesla_config):
        """Exact value within range is stored without clamping."""
        ctrl = TeslaController(tesla_config)
        asyncio.run(ctrl.set_charge_amps(24))
        state = asyncio.run(ctrl.get_charging_state())
        assert state.current_amps == 24

    def test_get_charging_state_returns_copy(self, tesla_config):
        """Two calls return different objects (copy semantics)."""
        ctrl = TeslaController(tesla_config)

        state1 = asyncio.run(ctrl.get_charging_state())
        state2 = asyncio.run(ctrl.get_charging_state())

        assert state1 is not state2
        # But they should have the same values
        assert state1.is_charging == state2.is_charging


# =============================================================================
# 5. CompositePlugController (lines ~1148-1219)
# =============================================================================


class TestCompositePlugController:

    @pytest.fixture()
    def mock_homekit(self):
        """MagicMock for init-time dict merging; get_state/set_state patched per-test."""
        ctrl = MagicMock()
        # .keys must return a real iterator so dict.update works in CompositePlugController.__init__
        ctrl.keys.return_value = iter([])  # type: ignore[attr-defined]
        return ctrl

    @pytest.fixture()
    def mock_vocolinc(self):
        """MagicMock for init-time dict merging; get_state/set_state patched per-test."""
        ctrl = MagicMock()
        ctrl.keys.return_value = iter([])  # type: ignore[attr-defined]
        return ctrl

    @pytest.fixture()
    def homekit_plug(self):
        """PlugConfig for a HomeKit plug."""
        return {
            "hk_plug": PlugConfig(
                name="hk_plug",
                accessory_id="192.168.1.10",
                power_watts=500.0,
                role="flexible",
                priority=10,
            ),
        }

    @pytest.fixture()
    def vocolinc_plug(self):
        """PlugConfig for a VOCOlinc plug."""
        return {
            "vc_plug": PlugConfig(
                name="vc_plug",
                accessory_id="vocolinc-device-1",
                power_watts=300.0,
                role="flexible",
                priority=15,
                controller_type="vocolinc",
            ),
        }

    def test_get_state_homekit(self, homekit_plug):
        """Plug with controller_type='homekit' routes to _homekit_ctrl.get_state()."""
        hk_ctrl = MagicMock(keys=iter([]))  # type: ignore[arg-type]
        hk_ctrl.get_state = AsyncMock(return_value=False)
        hk_ctrl.plugs = homekit_plug

        composite = CompositePlugController(hk_ctrl, MagicMock(keys=iter([])))  # type: ignore[arg-type]

        result = asyncio.run(composite.get_state("hk_plug"))
        hk_ctrl.get_state.assert_awaited_once_with("hk_plug")
        assert result is False

    def test_get_state_vocolinc(self, vocolinc_plug):
        """Plug with controller_type='vocolinc' routes to _vocolinc_ctrl.get_state()."""
        vc_ctrl = MagicMock(keys=iter([]))  # type: ignore[arg-type]
        vc_ctrl.get_state = AsyncMock(return_value=True)
        vc_ctrl.plugs = vocolinc_plug

        composite = CompositePlugController(MagicMock(keys=iter([])), vc_ctrl)  # type: ignore[arg-type]

        result = asyncio.run(composite.get_state("vc_plug"))
        assert result is True
        vc_ctrl.get_state.assert_awaited_once_with("vc_plug")

    def test_set_state_homekit(self, homekit_plug):
        """set_state routes correctly for HomeKit plug."""
        hk_ctrl = MagicMock(keys=iter([]))  # type: ignore[arg-type]
        hk_ctrl.set_state = AsyncMock(return_value=True)
        hk_ctrl.plugs = homekit_plug

        composite = CompositePlugController(hk_ctrl, MagicMock(keys=iter([])))  # type: ignore[arg-type]

        result = asyncio.run(composite.set_state("hk_plug", True))
        hk_ctrl.set_state.assert_awaited_once_with("hk_plug", True)
        assert result is True

    def test_set_state_vocolinc(self, vocolinc_plug):
        """set_state routes correctly for VOCOlinc plug."""
        vc_ctrl = MagicMock(keys=iter([]))  # type: ignore[arg-type]
        vc_ctrl.set_state = AsyncMock(return_value=False)
        vc_ctrl.plugs = vocolinc_plug

        composite = CompositePlugController(MagicMock(keys=iter([])), vc_ctrl)  # type: ignore[arg-type]

        result = asyncio.run(composite.set_state("vc_plug", False))
        vc_ctrl.set_state.assert_awaited_once_with("vc_plug", False)
        assert result is False

    def test_unknown_plug_returns_none(self, caplog):
        """get_state for unknown plug returns None and logs a warning."""

        composite = CompositePlugController(MagicMock(keys=iter([])), MagicMock(keys=iter([])))  # type: ignore[arg-type]

        with caplog.at_level("WARNING"):
            result = asyncio.run(composite.get_state("nonexistent"))

        assert result is None
        assert "Unknown plug nonexistent" in caplog.text

    def test_unknown_plug_set_returns_false(self, caplog):
        """set_state for unknown plug returns False and logs a warning."""

        composite = CompositePlugController(MagicMock(keys=iter([])), MagicMock(keys=iter([])))  # type: ignore[arg-type]

        with caplog.at_level("WARNING"):
            result = asyncio.run(composite.set_state("nonexistent", True))

        assert result is False
        assert "Unknown plug nonexistent" in caplog.text

    def test_plugs_merged(self):
        """plugs dict contains entries from both backends."""

        hk_plug = {
            "hk1": PlugConfig(
                name="hk1", accessory_id="192.168.1.10",
                power_watts=500, role="flexible", priority=10,
            ),
        }

        vc_plug = {
            "vc1": PlugConfig(
                name="vc1", accessory_id="vocolinc-2",
                power_watts=400, role="flexible", priority=15,
            ),
        }

        hk_mock = MagicMock(keys=iter([]))  # type: ignore[arg-type]
        hk_mock.plugs = hk_plug

        vc_mock = MagicMock(keys=iter([]))  # type: ignore[arg-type]
        vc_mock.plugs = vc_plug

        composite = CompositePlugController(hk_mock, vc_mock)
        assert len(composite.plugs) == 2
        assert "hk1" in composite.plugs
        assert "vc1" in composite.plugs


# =============================================================================
# 6. RealTeslaController.reset_session (lines ~719-730)
# =============================================================================


class TestRealTeslaControllerResetSession:

    def test_reset_session_clears_state(self):
        """After reset, _session, _api, and last_error are all None."""
        from load_controllers import RealTeslaController

        config = TeslaConfig(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="v123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            charge_amps_min=5,
            charge_amps_max=48,
        )

        ctrl = RealTeslaController(config)
        # Pretend we have state set up (without real API objects)
        ctrl._session = MagicMock()  # type: ignore[assignment]
        ctrl._api = MagicMock()     # type: ignore[assignment]
        ctrl.last_error = "some error"

        assert ctrl._session is not None  # type: ignore[unreachable]
        assert ctrl._api is not None      # type: ignore[unreachable]

        ctrl.reset_session()
        assert ctrl._session is None      # type: ignore[unreachable]
        assert ctrl._api is None          # type: ignore[unreachable]
        assert ctrl.last_error is None


# =============================================================================
# 7. RealPlugController._load_pairing_data and _save_pairing_data (lines ~817-839)
# =============================================================================


class TestRealPlugControllerPairingIO:

    def test_load_pairing_no_file(self, tmp_path):
        """Missing pairing file returns None without error."""
        ctrl = RealPlugController(
            plugs={"plug1": PlugConfig(name="p", accessory_id="x", power_watts=500, role="flexible", priority=1)},
            pairings_path=tmp_path / "pairings.json",
        )

        assert ctrl._load_pairing_data() is None

    def test_load_pairing_valid_json(self, tmp_path):
        """Valid JSON file returns parsed data dict."""
        ctrl = RealPlugController(
            plugs={"plug1": PlugConfig(name="p", accessory_id="x", power_watts=500, role="flexible", priority=1)},
            pairings_path=tmp_path / "pairings.json",
        )

        pairing_file = tmp_path / "pairings.json"
        data = {"192.168.1.10": {"data": "pairing_info"}}
        pairing_file.write_text(json.dumps(data))

        assert ctrl._load_pairing_data() == data

    def test_load_pairing_invalid_json(self, tmp_path):
        """Corrupt JSON returns None and logs an error."""
        ctrl = RealPlugController(
            plugs={"plug1": PlugConfig(name="p", accessory_id="x", power_watts=500, role="flexible", priority=1)},
            pairings_path=tmp_path / "pairings.json",
        )

        pairing_file = tmp_path / "pairings.json"
        pairing_file.write_text("{invalid json")

        assert ctrl._load_pairing_data() is None

    def test_save_pairing_writes_file(self, tmp_path):
        """Saving writes correct JSON to the file."""
        ctrl = RealPlugController(
            plugs={"plug1": PlugConfig(name="p", accessory_id="x", power_watts=500, role="flexible", priority=1)},
            pairings_path=tmp_path / "pairings.json",
        )

        data = {"192.168.1.10": {"data": "pairing_info"}}
        ctrl._save_pairing_data(data)

        assert tmp_path.joinpath("pairings.json").exists()
        loaded = json.loads(tmp_path.joinpath("pairings.json").read_text())

        assert loaded == data


# =============================================================================
# 8. VOCOlincPlugController._ensure_initialized (lines ~1050-1087)
# =============================================================================


class TestVocolincEnsureInit:

    def test_no_credentials_raises_runtime_error(self, monkeypatch):
        """Missing username/password raises RuntimeError."""
        monkeypatch.setenv("VOCOLINC_USERNAME", "")
        monkeypatch.setenv("VOCOLINC_PASSWORD", "")

        ctrl = VocolincPlugController(
            plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, role="flexible", priority=1)},
        )

        with pytest.raises(RuntimeError, match="VOCOlinc credentials not configured"):
            ctrl._ensure_initialized()

    def test_lazy_init_once(self, monkeypatch):
        """_initialized flag prevents re-initializing the client."""
        monkeypatch.setenv("VOCOLINC_USERNAME", "user")
        monkeypatch.setenv("VOCOLINC_PASSWORD", "pass")

        mock_client = MagicMock()
        # Patch vocolinc module so the import succeeds with a mock
        sys.modules["vocolinc"] = MagicMock(VOCOlinc=MagicMock(return_value=mock_client))

        try:
            ctrl = VocolincPlugController(
                plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, role="flexible", priority=1)},
            )

            ctrl._ensure_initialized()
            assert mock_client.login.call_count == 1

            # Second call should skip login since _initialized is True
            ctrl._ensure_initialized()
            assert mock_client.login.call_count == 1

        finally:
            del sys.modules["vocolinc"]


# =============================================================================
# 9. VOCOlincPlugController error paths (lines ~1089-1145)
# =============================================================================


class TestVocolincErrorPaths:

    def test_get_state_unknown_plug(self, monkeypatch, caplog):
        """Plug not in config dict returns None and logs warning."""
        monkeypatch.setenv("VOCOLINC_USERNAME", "user")
        monkeypatch.setenv("VOCOLINC_PASSWORD", "pass")

        ctrl = VocolincPlugController(
            plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, role="flexible", priority=1)},
        )

        with caplog.at_level("WARNING"):
            result = asyncio.run(ctrl.get_state("nonexistent"))

        assert result is None
        assert "Unknown plug nonexistent" in caplog.text

    def test_set_state_unknown_plug(self, monkeypatch, caplog):
        """Plug not in config dict returns False and logs warning."""
        monkeypatch.setenv("VOCOLINC_USERNAME", "user")
        monkeypatch.setenv("VOCOLINC_PASSWORD", "pass")

        ctrl = VocolincPlugController(
            plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, role="flexible", priority=1)},
        )

        with caplog.at_level("WARNING"):
            result = asyncio.run(ctrl.set_state("nonexistent", True))

        assert result is False
        assert "Unknown plug nonexistent" in caplog.text

    def test_get_state_init_failure_returns_none(self, monkeypatch):
        """RuntimeError from _ensure_initialized propagates as None return."""
        # Clear credentials so _ensure_initialized raises RuntimeError

        monkeypatch.setenv("VOCOLINC_USERNAME", "")
        ctrl = VocolincPlugController(
            plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, role="flexible", priority=1)},
        )

        result = asyncio.run(ctrl.get_state("p1"))
        assert result is None

    def test_set_state_init_failure_returns_false(self, monkeypatch):
        """RuntimeError from _ensure_initialized propagates as False return."""
        # Clear credentials so _ensure_initialized raises RuntimeError

        monkeypatch.setenv("VOCOLINC_USERNAME", "")
        ctrl = VocolincPlugController(
            plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, role="flexible", priority=1)},
        )

        result = asyncio.run(ctrl.set_state("p1", True))
        assert result is False

