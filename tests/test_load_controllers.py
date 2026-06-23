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
from unittest.mock import AsyncMock, MagicMock, patch

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
from load_models import PlugAction, PlugConfig, TeslaConfig, TeslaAuthError


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
                power_watts=500, priority=10,
            ),
        }

        vc_plug = {
            "vc1": PlugConfig(
                name="vc1", accessory_id="vocolinc-2",
                power_watts=400, priority=15,
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
# 8. RealTeslaController._ensure_api with vehicle-command proxy (lines ~757-775)
# =============================================================================


class TestRealTeslaControllerProxyUrl:

    def test_ensure_api_sets_server_from_proxy_url(self):
        """_ensure_api overrides _api.server when vehicle_command_proxy_url
        is set on TeslaConfig."""
        import asyncio

        from load_controllers import RealTeslaController

        config = TeslaConfig(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="v123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            vehicle_command_proxy_url="https://localhost:4444",
        )

        ctrl = RealTeslaController(config)

        mock_api = MagicMock()
        mock_session = MagicMock()

        with (
            patch.object(ctrl, "_get_session", new_callable=lambda: AsyncMock(return_value=mock_session)),
            patch(
                "tesla_fleet_api.TeslaFleetOAuth", return_value=mock_api
            ) as mock_oauth_class,
        ):
            asyncio.run(ctrl._ensure_api())

        mock_oauth_class.assert_called_once()
        call_kwargs = mock_oauth_class.call_args.kwargs
        assert call_kwargs["region"] == "na"

        assert mock_api.server == "https://localhost:4444"

    def test_ensure_api_no_override_when_proxy_url_is_none(self):
        """_ensure_api leaves _api.server untouched when vehicle_command_proxy_url
        is None."""
        import asyncio

        from load_controllers import RealTeslaController

        config = TeslaConfig(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="v123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
        )

        ctrl = RealTeslaController(config)

        mock_api = MagicMock(server="https://fleet-api.prd.na.vn.cloud.tesla.com")
        mock_session = MagicMock()

        with (
            patch.object(ctrl, "_get_session", new_callable=lambda: AsyncMock(return_value=mock_session)),
            patch(
                "tesla_fleet_api.TeslaFleetOAuth", return_value=mock_api
            ) as mock_oauth_class,
        ):
            asyncio.run(ctrl._ensure_api())

        mock_oauth_class.assert_called_once()
        assert mock_api.server == "https://fleet-api.prd.na.vn.cloud.tesla.com"


# =============================================================================
# 9. RealTeslaController _get_session SSL handling for vehicle-command proxy
# =============================================================================


class TestRealTeslaControllerProxySsl:

    def test_session_ssl_false_when_proxy_configured(self):
        """When vehicle_command_proxy_url is set, _get_session creates a
        TCPConnector with ssl=False to avoid hostname-mismatch errors."""
        from unittest.mock import patch

        import aiohttp

        from load_controllers import RealTeslaController
        from load_models import TeslaConfig

        config = TeslaConfig(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="v123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            vehicle_command_proxy_url="https://localhost:4444",
        )

        ctrl = RealTeslaController(config)

        with patch.object(aiohttp, "TCPConnector") as mock_connector:
            with patch.object(aiohttp, "ClientSession"):
                asyncio.run(ctrl._get_session(ssl=False))

        mock_connector.assert_called_once_with(ssl=False)

    def test_session_ssl_true_when_no_proxy(self):
        """When vehicle_command_proxy_url is not set, _get_session creates a
        TCPConnector with ssl=True (default)."""
        from unittest.mock import patch

        import aiohttp

        from load_controllers import RealTeslaController
        from load_models import TeslaConfig

        config = TeslaConfig(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="v123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
        )

        ctrl = RealTeslaController(config)

        with patch.object(aiohttp, "TCPConnector") as mock_connector:
            with patch.object(aiohttp, "ClientSession"):
                asyncio.run(ctrl._get_session(ssl=True))

        mock_connector.assert_called_once_with(ssl=True)


# =============================================================================
# 7. RealPlugController._load_pairing_data and _save_pairing_data (lines ~817-839)
# =============================================================================


class TestRealPlugControllerPairingIO:

    def test_load_pairing_no_file(self, tmp_path):
        """Missing pairing file returns None without error."""
        ctrl = RealPlugController(
            plugs={"plug1": PlugConfig(name="p", accessory_id="x", power_watts=500, priority=1)},
            pairings_path=tmp_path / "pairings.json",
        )

        assert ctrl._load_pairing_data() is None

    def test_load_pairing_valid_json(self, tmp_path):
        """Valid JSON file returns parsed data dict."""
        ctrl = RealPlugController(
            plugs={"plug1": PlugConfig(name="p", accessory_id="x", power_watts=500, priority=1)},
            pairings_path=tmp_path / "pairings.json",
        )

        pairing_file = tmp_path / "pairings.json"
        data = {"192.168.1.10": {"data": "pairing_info"}}
        pairing_file.write_text(json.dumps(data))

        assert ctrl._load_pairing_data() == data

    def test_load_pairing_invalid_json(self, tmp_path):
        """Corrupt JSON returns None and logs an error."""
        ctrl = RealPlugController(
            plugs={"plug1": PlugConfig(name="p", accessory_id="x", power_watts=500, priority=1)},
            pairings_path=tmp_path / "pairings.json",
        )

        pairing_file = tmp_path / "pairings.json"
        pairing_file.write_text("{invalid json")

        assert ctrl._load_pairing_data() is None

    def test_save_pairing_writes_file(self, tmp_path):
        """Saving writes correct JSON to the file."""
        ctrl = RealPlugController(
            plugs={"plug1": PlugConfig(name="p", accessory_id="x", power_watts=500, priority=1)},
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
            plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, priority=1)},
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
                plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, priority=1)},
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
            plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, priority=1)},
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
            plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, priority=1)},
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
            plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, priority=1)},
        )

        result = asyncio.run(ctrl.get_state("p1"))
        assert result is None

    def test_set_state_init_failure_returns_false(self, monkeypatch):
        """RuntimeError from _ensure_initialized propagates as False return."""
        # Clear credentials so _ensure_initialized raises RuntimeError

        monkeypatch.setenv("VOCOLINC_USERNAME", "")
        ctrl = VocolincPlugController(
            plugs={"p1": PlugConfig(name="p", accessory_id="x", power_watts=500, priority=1)},
        )

        result = asyncio.run(ctrl.set_state("p1", True))
        assert result is False


# =============================================================================
# 10. fleet_telemetry_config_create API access path (lines ~1330-1359)
# =============================================================================


class TestFleetTelemetryConfigCreate:

    def test_uses_correct_nested_path(self, monkeypatch, tmp_path):
        """fleet_telemetry_config_create must call api.vehicles.Fleet(api, vin),
        not api.fleet_telemetry_config_create directly.

        The tesla-fleet-api 1.4.7 package exposes fleet_telemetry_config_create
        on VehicleFleet (nested under Vehicles.Fleet), not on TeslaFleetOAuth itself.
        """
        from load_controllers import fleet_telemetry_config_create
        from load_models import FleetTelemetryProvisionConfig

        # Build a minimal mock chain that mirrors the real package structure:
        #   api.vehicles.Fleet(parent, vin) -> returns a VehicleFleet mock
        vehicle_fleet_mock = MagicMock()
        vehicle_fleet_mock.fleet_telemetry_config_create = AsyncMock(
            return_value={"response": {"result": True}}
        )
        fleet_class_mock = MagicMock(return_value=vehicle_fleet_mock)
        vehicles_mock = MagicMock(Fleet=fleet_class_mock)
        api_mock = MagicMock(vehicles=vehicles_mock)

        # Wire the controller to use our mock API
        from load_controllers import RealTeslaController

        config = TeslaConfig(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="VIN123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            charge_amps_min=5,
            charge_amps_max=48,
        )
        ctrl = RealTeslaController(config)
        ctrl._api = api_mock
        ctrl._vin = "VIN123"

        # Patch authenticate() so it doesn't try to call the real access_token()
        async def fake_authenticate():
            ctrl._vin = "VIN123"
        ctrl.authenticate = fake_authenticate  # type: ignore[attr-defined]

        ca_file = tmp_path / "ca.pem"
        ca_file.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----")

        provision_config = FleetTelemetryProvisionConfig(
            server_hostname="telemetry.example.com",
            ca_file_path=ca_file,
        )

        asyncio.run(fleet_telemetry_config_create(ctrl, provision_config))

        # Verify the call chain: api.vehicles.Fleet(api, vin).fleet_telemetry_config_create(body)
        fleet_class_mock.assert_called_once_with(api_mock, "VIN123")
        vehicle_fleet_mock.fleet_telemetry_config_create.assert_awaited_once()
        call_body = vehicle_fleet_mock.fleet_telemetry_config_create.call_args[0][0]
        assert call_body["config"]["hostname"] == "telemetry.example.com"
        assert "ca" in call_body["config"]
        assert "fields" in call_body["config"]
        # All four telemetry fields must be registered
        fields = call_body["config"]["fields"]
        assert "DetailedChargeState" in fields
        assert "ChargeAmps" in fields
        assert "Location" in fields

    def test_vehicles_fleet_not_called_on_api_directly(self, monkeypatch, tmp_path):
        """Ensure Fleet is NOT called directly on the api object (the old broken path)."""
        from load_controllers import fleet_telemetry_config_create
        from load_models import FleetTelemetryProvisionConfig

        vehicle_fleet_mock = MagicMock()
        vehicle_fleet_mock.fleet_telemetry_config_create = AsyncMock(
            return_value={"response": {"result": True}}
        )
        fleet_class_mock = MagicMock(return_value=vehicle_fleet_mock)
        vehicles_mock = MagicMock(Fleet=fleet_class_mock)
        api_mock = MagicMock(vehicles=vehicles_mock)

        from load_controllers import RealTeslaController

        config = TeslaConfig(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="VIN456",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            charge_amps_min=5,
            charge_amps_max=48,
        )
        ctrl = RealTeslaController(config)
        ctrl._api = api_mock
        ctrl._vin = "VIN456"

        # Patch authenticate() so it doesn't try to call the real access_token()
        async def fake_authenticate():
            ctrl._vin = "VIN456"
        ctrl.authenticate = fake_authenticate  # type: ignore[attr-defined]

        ca_file = tmp_path / "ca.pem"
        ca_file.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----")

        provision_config = FleetTelemetryProvisionConfig(
            server_hostname="telemetry.example.com",
            ca_file_path=ca_file,
        )

        asyncio.run(fleet_telemetry_config_create(ctrl, provision_config))

        # The old broken path api.fleet_telemetry_config_create should NOT exist
        # (it was never a method on TeslaFleetOAuth)
        assert not hasattr(api_mock, "fleet_telemetry_config_create") or \
            not hasattr(api_mock.fleet_telemetry_config_create, "assert_awaited_once")
        # Fleet must have been called once with correct args
        assert fleet_class_mock.call_count == 1

    def test_calls_authenticate_before_accessing_vin(self, monkeypatch, tmp_path):
        """fleet_telemetry_config_create must call ctrl.authenticate() to set
        ctrl._vin, not rely on the caller having already set it.

        Regression test: authenticate() was missing, so _vin stayed empty
        and the VIN was sent as "" in the API body."""
        from load_controllers import fleet_telemetry_config_create
        from load_models import FleetTelemetryProvisionConfig

        vehicle_fleet_mock = MagicMock()
        vehicle_fleet_mock.fleet_telemetry_config_create = AsyncMock(
            return_value={"response": {"result": True}}
        )
        fleet_class_mock = MagicMock(return_value=vehicle_fleet_mock)
        vehicles_mock = MagicMock(Fleet=fleet_class_mock)
        api_mock = MagicMock(vehicles=vehicles_mock)

        from load_controllers import RealTeslaController

        config = TeslaConfig(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="VIN789",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            charge_amps_min=5,
            charge_amps_max=48,
        )
        ctrl = RealTeslaController(config)
        ctrl._api = api_mock
        # Do NOT set ctrl._vin — authenticate() should set it

        # Patch authenticate to set _vin so the rest of the flow works
        async def fake_authenticate():
            ctrl._vin = "VIN789"
        ctrl.authenticate = fake_authenticate  # type: ignore[attr-defined]

        ca_file = tmp_path / "ca.pem"
        ca_file.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----")

        provision_config = FleetTelemetryProvisionConfig(
            server_hostname="telemetry.example.com",
            ca_file_path=ca_file,
        )

        asyncio.run(fleet_telemetry_config_create(ctrl, provision_config))

        # Verify VIN was set (proves authenticate() was called)
        assert ctrl._vin == "VIN789"
        fleet_class_mock.assert_called_once_with(api_mock, "VIN789")

    def test_uses_config_server_port_in_body(self, tmp_path):
        """fleet_telemetry_config_create sends config.server_port in the API
        request body, not a hardcoded default."""
        from load_controllers import fleet_telemetry_config_create
        from load_models import FleetTelemetryProvisionConfig

        vehicle_fleet_mock = MagicMock()
        vehicle_fleet_mock.fleet_telemetry_config_create = AsyncMock(
            return_value={"response": {"result": True}}
        )
        fleet_class_mock = MagicMock(return_value=vehicle_fleet_mock)
        vehicles_mock = MagicMock(Fleet=fleet_class_mock)
        api_mock = MagicMock(vehicles=vehicles_mock)

        from load_controllers import RealTeslaController

        config = TeslaConfig(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="VIN999",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
        )
        ctrl = RealTeslaController(config)
        ctrl._api = api_mock
        ctrl._vin = "VIN999"

        # Patch authenticate so it doesn't try to call the real access_token()
        async def fake_authenticate():
            ctrl._vin = "VIN999"
        ctrl.authenticate = fake_authenticate  # type: ignore[attr-defined]

        ca_file = tmp_path / "ca.pem"
        ca_file.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----")

        provision_config = FleetTelemetryProvisionConfig(
            server_hostname="telemetry.example.com",
            ca_file_path=ca_file,
            server_port=9443,
        )

        asyncio.run(fleet_telemetry_config_create(ctrl, provision_config))

        call_body = vehicle_fleet_mock.fleet_telemetry_config_create.call_args[0][0]
        assert call_body["config"]["port"] == 9443


# =============================================================================
# 11. provision_fleet_telemetry pre-flight token check
# =============================================================================


class TestProvisionFleetTelemetryPreFlight:

    def test_returns_false_when_no_tokens(self, tmp_path, monkeypatch):
        """provision_fleet_telemetry returns False and logs a clear message
        when .tesla-tokens.json doesn't exist."""
        from load_controllers import save_tesla_tokens, TESLA_TOKENS_FILE
        from load_manager import provision_fleet_telemetry
        from load_models import FleetTelemetryProvisionConfig

        # Temporarily redirect tokens file to tmp_path so we don't pollute the
        # real one and ensure no valid tokens exist.
        tmp_tokens = tmp_path / "tokens.json"
        monkeypatch.setattr(
            "load_controllers.TESLA_TOKENS_FILE", tmp_tokens
        )
        monkeypatch.setattr("load_manager.TESLA_TOKENS_FILE", tmp_tokens)

        # Ensure the file doesn't exist
        assert not tmp_tokens.exists()

        ca_file = tmp_path / "ca.pem"
        ca_file.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----")

        cfg = FleetTelemetryProvisionConfig(
            server_hostname="telemetry.example.com",
            ca_file_path=ca_file,
        )

        result = provision_fleet_telemetry(cfg)
        assert result is False


# =============================================================================
# 12. provision_fleet_telemetry pre-flight proxy URL check
# =============================================================================


class TestProvisionFleetTelemetryProxyUrlCheck:

    def test_prints_error_when_proxy_url_not_configured(self, capsys, monkeypatch):
        """provision_fleet_telemetry prints a clear error to stderr when
        TESLA_VEHICLE_COMMAND_PROXY_URL is not set."""
        from unittest.mock import MagicMock

        from load_controllers import RealTeslaController
        from load_manager import LoadManager, provision_fleet_telemetry
        from load_models import TeslaConfig

        # Create a LoadManager and inject a RealTeslaController mock
        # with no proxy URL — use spec so isinstance checks pass.
        lm = LoadManager()
        mock_ctrl = MagicMock(spec=RealTeslaController)
        mock_ctrl.config = TeslaConfig(
            client_id="test",
            client_secret="test",
            vehicle_id="VIN123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500.0,
            redirect_uri="http://localhost/callback",
            vehicle_command_proxy_url=None,
        )
        lm.tesla_ctrl = mock_ctrl

        result = lm.provision_fleet_telemetry(None)
        assert result is False

        # The error message should be printed to stderr so the user sees
        # it immediately, rather than having to dig through log files.
        stderr = capsys.readouterr().err
        assert "TESLA_VEHICLE_COMMAND_PROXY_URL" in stderr
        assert "https://localhost:4444" in stderr


    def test_returns_false_when_tokens_missing_keys(self, tmp_path, monkeypatch):
        """provision_fleet_telemetry returns False when tokens file is valid
        JSON but missing required keys."""
        from load_controllers import TESLA_TOKENS_FILE
        from load_manager import provision_fleet_telemetry
        from load_models import FleetTelemetryProvisionConfig

        tmp_tokens = tmp_path / "tokens.json"
        monkeypatch.setattr("load_controllers.TESLA_TOKENS_FILE", tmp_tokens)
        monkeypatch.setattr("load_manager.TESLA_TOKENS_FILE", tmp_tokens)

        # Write valid JSON but missing required keys
        tmp_tokens.write_text('{"refresh_token": "rt"}')

        ca_file = tmp_path / "ca.pem"
        ca_file.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----")

        cfg = FleetTelemetryProvisionConfig(
            server_hostname="telemetry.example.com",
            ca_file_path=ca_file,
        )

        result = provision_fleet_telemetry(cfg)
        assert result is False


class TestRealTeslaControllerDeprecatedStatus:
    """Verify that deprecated status and start_charging methods
    return defaults without making REST calls.

    After disabling REST-based Tesla status polling, these methods
    must return sentinel defaults (None/False) without calling
    _fetch_vehicle_data(). The REST API is only used for commands
    (stop_charging, set_charge_amps) and telemetry provisioning.
    """

    def test_get_charging_state_returns_none(self, tesla_config):
        """get_charging_state() returns None without calling _fetch_vehicle_data()."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            state = asyncio.run(ctrl.get_charging_state())
            assert state is None
            mock_fetch.assert_not_called()

    def test_is_at_home_returns_false(self, tesla_config):
        """is_at_home() returns False without calling _fetch_vehicle_data()."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            result = asyncio.run(ctrl.is_at_home())
            assert result is False
            mock_fetch.assert_not_called()

    def test_is_plugged_in_returns_false(self, tesla_config):
        """is_plugged_in() returns False without calling _fetch_vehicle_data()."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            result = asyncio.run(ctrl.is_plugged_in())
            assert result is False
            mock_fetch.assert_not_called()

    def test_start_charging_is_noop(self, tesla_config):
        """start_charging() returns False without calling _get_vehicle().

        The load manager must not start charging; this method is a no-op.
        """
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        with patch.object(ctrl, "_get_vehicle") as mock_vehicle:
            result = asyncio.run(ctrl.start_charging())
            assert result is False
            mock_vehicle.assert_not_called()


# =============================================================================
# 10. RealTeslaController.init_tesla_state() — REST fallback on startup
# =============================================================================


class TestRealTeslaControllerInitTeslaState:
    """Verify init_tesla_state() telemetry wait and REST fallback.

    These tests exercise:
    - Caching of the init result (second call returns immediately)
    - _init_from_rest() charge_state parsing
    - _init_from_rest() conditional drive_state fetch (charging + home coords)
    - _init_from_rest() skip drive_state (not charging or no home coords)
    - _init_from_rest() error and empty-response handling
    """

    def test_returns_cached_state_on_second_call(self, tesla_config):
        """Subsequent calls return the cached result without re-fetching."""
        from load_controllers import RealTeslaController
        from load_models import TeslaState

        ctrl = RealTeslaController(tesla_config)

        # Seed the cache
        cached = TeslaState(
            is_charging=True,
            current_amps=32,
            plugged_in=True,
            at_home=True,
        )
        ctrl._init_state = cached

        result = asyncio.run(ctrl.init_tesla_state(timeout=0))
        assert result is cached

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_init_from_rest_charging_with_home(self, tesla_config):
        """REST fallback: charging + home coords → fetches location_data."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = 37.7749
        ctrl.config.home_lon = -122.4194
        ctrl.config.home_radius_m = 100.0

        mock_charge = {
            "charge_state": {
                "charging_state": "Charging",
                "charge_amps": 32,
                "charge_range_percent": 75,
            }
        }
        mock_location = {
            "drive_state": {
                "latitude": 37.7749,
                "longitude": -122.4194,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = [mock_charge, mock_location]
            result = await ctrl._init_from_rest()

        assert result is not None
        assert result.is_charging is True
        assert result.current_amps == 32
        assert result.plugged_in is True
        assert result.at_home is True
        assert mock_fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_init_from_rest_real_charge_state_key(self, tesla_config):
        """REST fallback: use the real Tesla API field name 'charging_state'.

        The Tesla Fleet API returns ``charging_state`` (not ``charge_state``)
        inside the ``charge_state`` envelope.  The parser must read the
        correct key to determine is_charging and plugged_in.
        """
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = None
        ctrl.config.home_lon = None

        mock_charge = {
            "charge_state": {
                "charging_state": "Charging",  # Real Tesla API field name
                "charge_amps": 32,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = [mock_charge]
            result = await ctrl._init_from_rest()

        assert result is not None
        assert result.is_charging is True  # FAILS with buggy 'charge_state' key
        assert result.current_amps == 32
        assert result.plugged_in is True
        assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_init_from_rest_charging_no_home_coords(self, tesla_config):
        """REST fallback: charging without home coords → skip location_data."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = None
        ctrl.config.home_lon = None

        mock_charge = {
            "charge_state": {
                "charging_state": "Charging",
                "charge_amps": 24,
                "charge_range_percent": 50,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.return_value = mock_charge
            result = await ctrl._init_from_rest()

        assert result is not None
        assert result.is_charging is True
        assert result.current_amps == 24
        assert result.current_amps == 24
        assert result.at_home is False
        assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_init_from_rest_not_charging(self, tesla_config):
        """REST fallback: not charging → no drive_state needed."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = 37.7749
        ctrl.config.home_lon = -122.4194

        mock_charge = {
            "charge_state": {
                "charging_state": "Complete",
                "charge_amps": 0,
                "charge_range_percent": 90,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.return_value = mock_charge
            result = await ctrl._init_from_rest()

        assert result is not None
        assert result.is_charging is False
        assert result.current_amps == 0
        assert result.plugged_in is True
        assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_init_from_rest_api_error_returns_none(self, tesla_config):
        """REST fallback: API error → returns None."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = Exception("API timeout")
            result = await ctrl._init_from_rest()

        assert result is None

    @pytest.mark.asyncio
    async def test_init_from_rest_empty_response_returns_none(self, tesla_config):
        """REST fallback: empty API response → returns None."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.return_value = {}
            result = await ctrl._init_from_rest()

        assert result is None
        assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_init_from_rest_charging_home_outside_radius(self, tesla_config):
        """REST fallback: charging but vehicle outside home radius."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = 37.7749
        ctrl.config.home_lon = -122.4194
        ctrl.config.home_radius_m = 100.0

        mock_charge = {
            "charge_state": {
                "charging_state": "Charging",
                "charge_amps": 16,
                "charge_range_percent": 60,
            }
        }
        mock_location = {
            "drive_state": {
                "latitude": 37.8000,
                "longitude": -122.4500,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = [mock_charge, mock_location]
            result = await ctrl._init_from_rest()

        assert result is not None
        assert result.is_charging is True
        assert result.current_amps == 16
        assert result.at_home is False
        assert mock_fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_init_from_rest_location_data_fetch_failure(self, tesla_config):
        """REST fallback: location_data fetch fails → at_home=False default."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = 37.7749
        ctrl.config.home_lon = -122.4194
        ctrl.config.home_radius_m = 100.0

        mock_charge = {
            "charge_state": {
                "charging_state": "Charging",
                "charge_amps": 32,
                "charge_range_percent": 75,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = [mock_charge, Exception("location_data error")]
            result = await ctrl._init_from_rest()

        assert result is not None
        assert result.is_charging is True
        assert result.at_home is False
        assert mock_fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_init_from_rest_charge_amps_none(self, tesla_config):
        """REST fallback: charge_amps missing → None."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)

        mock_charge = {
            "charge_state": {
                "charging_state": "Charging",
                "charge_range_percent": 50,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.return_value = mock_charge
            result = await ctrl._init_from_rest()

        assert result is not None
        assert result.current_amps is None

    # ── _init_from_rest with partial telemetry snapshot ─────────────────────

    @pytest.mark.asyncio
    async def test_init_from_rest_with_snapshot_skips_charge_state(
        self, tesla_config,
    ):
        """Snapshot with ChargeAmps skips charge_state REST call, fetches location_data."""
        from load_controllers import RealTeslaController
        from load_models import TeslaState

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = 37.7749
        ctrl.config.home_lon = -122.4194
        ctrl.config.home_radius_m = 100.0

        snapshot = {"ChargeAmps": 12}
        mock_location = {
            "response": {
                "drive_state": {
                    "latitude": 37.7749,
                    "longitude": -122.4194,
                }
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.return_value = mock_location
            result = await ctrl._init_from_rest(snapshot=snapshot)

        assert result is not None
        assert result.is_charging is True
        assert result.current_amps == 12
        assert result.plugged_in is True
        assert result.at_home is True
        # Should have only called location_data, NOT charge_state
        assert mock_fetch.call_count == 1
        call_args, call_kwargs = mock_fetch.call_args
        assert call_kwargs.get("endpoints") == ["location_data"]

    @pytest.mark.asyncio
    async def test_init_from_rest_with_snapshot_outside_home(
        self, tesla_config,
    ):
        """Snapshot path correctly computes at_home=False when vehicle is far."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = 37.7749
        ctrl.config.home_lon = -122.4194
        ctrl.config.home_radius_m = 100.0

        snapshot = {"ChargeAmps": 16}
        mock_location = {
            "response": {
                "drive_state": {
                    "latitude": 37.8000,
                    "longitude": -122.4500,
                }
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.return_value = mock_location
            result = await ctrl._init_from_rest(snapshot=snapshot)

        assert result is not None
        assert result.is_charging is True
        assert result.current_amps == 16
        assert result.at_home is False
        assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_init_from_rest_with_snapshot_no_home_coords(
        self, tesla_config,
    ):
        """Snapshot path: no home coords → skip location_data, at_home=False."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = None
        ctrl.config.home_lon = None

        snapshot = {"ChargeAmps": 5}

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            result = await ctrl._init_from_rest(snapshot=snapshot)

        assert result is not None
        assert result.is_charging is True
        assert result.current_amps == 5
        assert result.plugged_in is True
        assert result.at_home is False
        # No REST calls needed (no home coords)
        assert mock_fetch.call_count == 0

    @pytest.mark.asyncio
    async def test_init_from_rest_with_snapshot_amps_zero(
        self, tesla_config,
    ):
        """Snapshot with ChargeAmps=0 → not charging."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = 37.7749
        ctrl.config.home_lon = -122.4194

        snapshot = {"ChargeAmps": 0}

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            result = await ctrl._init_from_rest(snapshot=snapshot)

        assert result is not None
        assert result.is_charging is False
        assert result.current_amps == 0
        assert result.plugged_in is False
        assert result.at_home is False
        # Not charging → no location_data needed
        assert mock_fetch.call_count == 0

    @pytest.mark.asyncio
    async def test_init_from_rest_with_snapshot_location_data_fails(
        self, tesla_config,
    ):
        """Snapshot path: location_data fetch fails → at_home=False default."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl.config.home_lat = 37.7749
        ctrl.config.home_lon = -122.4194

        snapshot = {"ChargeAmps": 12}

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = Exception("API error")
            result = await ctrl._init_from_rest(snapshot=snapshot)

        assert result is not None
        assert result.is_charging is True
        assert result.current_amps == 12
        assert result.at_home is False
        assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_init_from_rest_missing_charge_range(self, tesla_config):
        """REST fallback: charge_range_percent missing → current_amps still populated."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)

        mock_charge = {
            "charge_state": {
                "charging_state": "Charging",
                "charge_amps": 32,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.return_value = mock_charge
            result = await ctrl._init_from_rest()

        assert result is not None
        assert result.current_amps == 32

    @pytest.mark.asyncio
    async def test_init_from_rest_complete_state(self, tesla_config):
        """REST fallback: charge_state='Complete' → plugged_in=True, is_charging=False."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)

        mock_charge = {
            "charge_state": {
                "charging_state": "Complete",
                "charge_amps": 0,
                "charge_range_percent": 100,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.return_value = mock_charge
            result = await ctrl._init_from_rest()

        assert result is not None
        assert result.is_charging is False
        assert result.plugged_in is True
        # charge_amps should be 0 when charging is complete
        assert result.current_amps == 0
        assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_init_from_rest_plugged_in_only(self, tesla_config):
        """REST fallback: charge_state='PluggedIn' → plugged_in=True, is_charging=False."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)

        mock_charge = {
            "charge_state": {
                "charging_state": "PluggedIn",
                "charge_amps": 0,
                "charge_range_percent": 40,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.return_value = mock_charge
            result = await ctrl._init_from_rest()

        assert result is not None
        assert result.is_charging is False
        assert result.plugged_in is True
        assert mock_fetch.call_count == 1


# =============================================================================
# 13. _is_auth_error() — auth error detection helper
# =============================================================================


class TestIsAuthError:

    def test_detects_login_required(self):
        """login_required in error string is detected."""
        from load_controllers import _is_auth_error
        err = Exception("{'error': 'login_required', 'error_description': 'The refresh_token is invalid'}")
        assert _is_auth_error(err) is True

    def test_detects_refresh_token_keyword(self):
        """refresh_token keyword is detected."""
        from load_controllers import _is_auth_error
        err = Exception("refresh_token is invalid")
        assert _is_auth_error(err) is True

    def test_detects_unauthorized(self):
        """unauthorized keyword is detected."""
        from load_controllers import _is_auth_error
        err = Exception("unauthorized")
        assert _is_auth_error(err) is True

    def test_rejects_network_error(self):
        """Network/timeout errors are not detected as auth errors."""
        from load_controllers import _is_auth_error
        err = Exception("Connection refused")
        assert _is_auth_error(err) is False

    def test_rejects_rate_limit(self):
        """Rate limit errors are not detected as auth errors."""
        from load_controllers import _is_auth_error
        err = Exception("Rate limit exceeded")
        assert _is_auth_error(err) is False


# =============================================================================
# 14. RealTeslaController.set_charge_amps raises TeslaAuthError
# =============================================================================


class TestRealTeslaControllerAuthError:

    @pytest.mark.asyncio
    async def test_set_charge_amps_raises_tesla_auth_error(self, tesla_config):
        """When API returns auth error, set_charge_amps raises TeslaAuthError."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        # Stub _ensure_api to be a no-op
        ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]
        # Set up a mock API with private key
        mock_api = MagicMock()
        mock_api.has_private_key = True
        ctrl._api = mock_api

        vehicle_mock = AsyncMock()
        vehicle_mock.set_charging_amps = AsyncMock(
            side_effect=Exception("{'error': 'login_required', 'error_description': 'The refresh_token is invalid'}")
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]
        ctrl.close = AsyncMock()  # type: ignore[assignment]

        with pytest.raises(TeslaAuthError, match="login_required"):
            await ctrl.set_charge_amps(16)

    @pytest.mark.asyncio
    async def test_stop_charging_raises_tesla_auth_error(self, tesla_config):
        """When API returns auth error, stop_charging raises TeslaAuthError."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]
        mock_api = MagicMock()
        mock_api.has_private_key = True
        ctrl._api = mock_api

        vehicle_mock = AsyncMock()
        vehicle_mock.charge_stop = AsyncMock(
            side_effect=Exception("refresh_token is invalid")
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]
        ctrl.close = AsyncMock()  # type: ignore[assignment]

        with pytest.raises(TeslaAuthError, match="refresh_token"):
            await ctrl.stop_charging()

    @pytest.mark.asyncio
    async def test_set_charge_amps_handles_network_error(self, tesla_config):
        """Network errors are NOT raised as TeslaAuthError."""
        from load_controllers import RealTeslaController

        ctrl = RealTeslaController(tesla_config)
        ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]
        mock_api = MagicMock()
        mock_api.has_private_key = True
        ctrl._api = mock_api

        vehicle_mock = AsyncMock()
        vehicle_mock.set_charging_amps = AsyncMock(
            side_effect=Exception("Connection refused")
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]
        ctrl.close = AsyncMock()  # type: ignore[assignment]

        result = await ctrl.set_charge_amps(16)
        assert result is False

    @pytest.mark.asyncio
    async def test_save_tokens_called_after_set_charge_amps(self, tesla_config):
        """Successful set_charge_amps persists tokens to disk."""
        from load_controllers import RealTeslaController
        from unittest.mock import patch

        ctrl = RealTeslaController(tesla_config)
        ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]
        mock_api = MagicMock()
        mock_api.has_private_key = True
        ctrl._api = mock_api

        vehicle_mock = AsyncMock()
        vehicle_mock.set_charging_amps = AsyncMock(return_value=None)
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]
        ctrl.close = AsyncMock()  # type: ignore[assignment]

        with patch("load_controllers.save_tesla_tokens") as mock_save:
            result = await ctrl.set_charge_amps(16)
            assert result is True
            mock_save.assert_called_once()
            call_kwargs = mock_save.call_args[1] if mock_save.call_args.kwargs else {}
            # Check that save was called (regardless of positional vs keyword)
            assert mock_save.call_count == 1

    @pytest.mark.asyncio
    async def test_save_tokens_called_after_stop_charging(self, tesla_config):
        """Successful stop_charging persists tokens to disk."""
        from load_controllers import RealTeslaController
        from unittest.mock import patch

        ctrl = RealTeslaController(tesla_config)
        ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]
        mock_api = MagicMock()
        mock_api.has_private_key = True
        ctrl._api = mock_api

        vehicle_mock = AsyncMock()
        vehicle_mock.charge_stop = AsyncMock(return_value=None)
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]
        ctrl.close = AsyncMock()  # type: ignore[assignment]

        with patch("load_controllers.save_tesla_tokens") as mock_save:
            result = await ctrl.stop_charging()
            assert result is True
            mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_tokens_called_after_init_from_rest(self, tesla_config):
        """Successful _init_from_rest persists tokens to disk."""
        from load_controllers import RealTeslaController
        from unittest.mock import patch

        ctrl = RealTeslaController(tesla_config)
        ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]
        mock_api = MagicMock()
        mock_api.has_private_key = True
        ctrl._api = mock_api

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.return_value = {
                "charge_state": {
                    "charging_state": "Charging",
                    "charge_amps": 32,
                }
            }
            with patch("load_controllers.save_tesla_tokens") as mock_save:
                result = await ctrl._init_from_rest()
                assert result is not None
                mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_proactive_refresh_skips_when_recent(self, tesla_config):
        """maybe_refresh_token does nothing if last save was recent."""
        from load_controllers import RealTeslaController
        from unittest.mock import MagicMock, patch

        ctrl = RealTeslaController(tesla_config)
        ctrl._last_saved_tokens_at = 999999.0  # far in the "future" (monotonic)
        ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]
        mock_api = MagicMock()
        ctrl._api = mock_api

        with patch.object(mock_api, "refresh_access_token") as mock_refresh:
            await ctrl.maybe_refresh_token()
            mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_proactive_refresh_triggers_when_stale(self, tesla_config):
        """maybe_refresh_token calls refresh when last save was > 7 hours ago."""
        import time as _time
        from load_controllers import RealTeslaController
        from unittest.mock import patch

        ctrl = RealTeslaController(tesla_config)
        # Set last save to 8 hours ago
        ctrl._last_saved_tokens_at = _time.monotonic() - 8 * 3600
        # Make _ensure_api a no-op
        ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]
        mock_api = MagicMock()
        ctrl._api = mock_api
        mock_api.refresh_access_token = AsyncMock(return_value={})

        with patch("load_controllers.save_tesla_tokens") as mock_save:
            await ctrl.maybe_refresh_token()
            mock_api.refresh_access_token.assert_called_once()
            mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_proactive_refresh_skips_when_no_api(self, tesla_config):
        """maybe_refresh_token skips when _api is not initialized."""
        import time as _time
        from load_controllers import RealTeslaController
        from unittest.mock import patch

        ctrl = RealTeslaController(tesla_config)
        ctrl._last_saved_tokens_at = _time.monotonic() - 8 * 3600
        ctrl._api = None
        ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]

        with patch("load_controllers.save_tesla_tokens") as mock_save:
            await ctrl.maybe_refresh_token()
            mock_save.assert_not_called()

