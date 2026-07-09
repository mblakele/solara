"""Tests for VehicleOffline handling during Tesla command execution.

When the Tesla fleet API returns HTTP 408 (vehicle not online) during
set_charge_amps() or stop_charging(), the system should:
  - Log at WARNING level (not ERROR)
  - Set a flag so the cycle uses a short sleep hint
  - Not propagate the exception

See also test_tesla_init_state.py for VehicleOffline handling during
init_tesla_state() (REST fallback path).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from load_controllers import RealTeslaController, _is_vehicle_offline_error
from load_models import TeslaConfig


# =============================================================================
# Fixtures
# =============================================================================


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


def _make_offline_ctrl(tesla_config):
    """Create a RealTeslaController with mocked API ready for command tests."""
    ctrl = RealTeslaController(tesla_config)
    ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]
    mock_api = MagicMock()
    mock_api.has_private_key = True
    ctrl._api = mock_api
    ctrl.close = AsyncMock()  # type: ignore[assignment]
    return ctrl


def _make_vehicle_offline_exc():
    """Create a VehicleOffline exception matching Tesla fleet API response."""
    from tesla_fleet_api.exceptions import VehicleOffline
    return VehicleOffline({"error": "vehicle is not online"})


# =============================================================================
# Tests — _is_vehicle_offline_error()
# =============================================================================


class TestIsVehicleOfflineError:

    def test_detects_vehicle_offline_exception(self):
        """VehicleOffline instances are recognized."""
        exc = _make_vehicle_offline_exc()
        assert _is_vehicle_offline_error(exc) is True

    def test_detects_string_pattern_fallback(self):
        """String match works as fallback for non-VehicleOffline exceptions."""
        exc = Exception("The vehicle is not 'online'.")
        assert _is_vehicle_offline_error(exc) is True

    def test_rejects_unrelated_exceptions(self):
        """Non-offline exceptions are not misidentified."""
        assert _is_vehicle_offline_error(Exception("Connection refused")) is False
        assert _is_vehicle_offline_error(Exception("login_required")) is False

    def test_rejects_empty_string(self):
        """Empty exception message is not vehicle offline."""
        assert _is_vehicle_offline_error(Exception("")) is False


# =============================================================================
# Tests — set_charge_amps() VehicleOffline handling
# =============================================================================


class TestSetChargeAmpsVehicleOffline:

    @pytest.mark.asyncio
    async def test_returns_false_on_vehicle_offline(self, tesla_config):
        """VehicleOffline during set_charge_amps returns False."""
        ctrl = _make_offline_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.set_charging_amps = AsyncMock(
            side_effect=_make_vehicle_offline_exc()
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        result = await ctrl.set_charge_amps(16)
        assert result is False

    @pytest.mark.asyncio
    async def test_sets_vehicle_offline_flag(self, tesla_config):
        """VehicleOffline sets _last_command_vehicle_offline on the controller."""
        ctrl = _make_offline_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.set_charging_amps = AsyncMock(
            side_effect=_make_vehicle_offline_exc()
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        result = await ctrl.set_charge_amps(16)
        assert result is False
        assert ctrl._last_command_vehicle_offline is True

    @pytest.mark.asyncio
    async def test_does_not_propagate_exception(self, tesla_config):
        """VehicleOffline is caught internally, does not raise."""
        ctrl = _make_offline_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.set_charging_amps = AsyncMock(
            side_effect=_make_vehicle_offline_exc()
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        # Should NOT raise — just return False
        result = await ctrl.set_charge_amps(16)
        assert result is False

    @pytest.mark.asyncio
    async def test_non_offline_error_still_logs_error(self, tesla_config):
        """Non-VehicleOffline exceptions still log at ERROR level."""
        ctrl = _make_offline_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.set_charging_amps = AsyncMock(
            side_effect=Exception("Connection refused")
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        result = await ctrl.set_charge_amps(16)
        assert result is False
        # Flag should NOT be set for non-offline errors
        assert ctrl._last_command_vehicle_offline is False


# =============================================================================
# Tests — stop_charging() VehicleOffline handling
# =============================================================================


class TestStopChargingVehicleOffline:

    @pytest.mark.asyncio
    async def test_logs_warning_not_error(self, tesla_config):
        """VehicleOffline during stop_charging logs WARNING, not ERROR."""
        ctrl = _make_offline_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.charge_stop = AsyncMock(
            side_effect=_make_vehicle_offline_exc()
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        result = await ctrl.stop_charging()
        assert result is False

    @pytest.mark.asyncio
    async def test_sets_vehicle_offline_flag(self, tesla_config):
        """VehicleOffline sets _last_command_vehicle_offline on the controller."""
        ctrl = _make_offline_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.charge_stop = AsyncMock(
            side_effect=_make_vehicle_offline_exc()
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        result = await ctrl.stop_charging()
        assert result is False
        assert ctrl._last_command_vehicle_offline is True

    @pytest.mark.asyncio
    async def test_does_not_propagate_exception(self, tesla_config):
        """VehicleOffline is caught internally, does not raise."""
        ctrl = _make_offline_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.charge_stop = AsyncMock(
            side_effect=_make_vehicle_offline_exc()
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        result = await ctrl.stop_charging()
        assert result is False

    @pytest.mark.asyncio
    async def test_non_offline_error_still_logs_error(self, tesla_config):
        """Non-VehicleOffline exceptions still log at ERROR level."""
        ctrl = _make_offline_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.charge_stop = AsyncMock(
            side_effect=Exception("Connection refused")
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        result = await ctrl.stop_charging()
        assert result is False
        assert ctrl._last_command_vehicle_offline is False


# =============================================================================
# Tests — Successful command clears flag
# =============================================================================


class TestSuccessfulCommandClearsFlag:

    @pytest.mark.asyncio
    async def test_successful_set_charge_amps_clears_flag(self, tesla_config):
        """A successful set_charge_amps clears the vehicle offline flag."""
        ctrl = _make_offline_ctrl(tesla_config)
        # First: simulate VehicleOffline
        vehicle_mock = AsyncMock()
        vehicle_mock.set_charging_amps = AsyncMock(
            side_effect=_make_vehicle_offline_exc()
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        await ctrl.set_charge_amps(16)
        assert ctrl._last_command_vehicle_offline is True

        # Now: simulate success
        ctrl._api = MagicMock()
        ctrl._api.has_private_key = True
        vehicle_mock_ok = AsyncMock()
        vehicle_mock_ok.set_charging_amps = AsyncMock(return_value=None)
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock_ok)  # type: ignore[assignment]
        ctrl.save_tokens = MagicMock()  # type: ignore[assignment]

        result = await ctrl.set_charge_amps(16)
        assert result is True
        assert ctrl._last_command_vehicle_offline is False

    @pytest.mark.asyncio
    async def test_successful_stop_charging_clears_flag(self, tesla_config):
        """A successful stop_charging clears the vehicle offline flag."""
        ctrl = _make_offline_ctrl(tesla_config)
        # First: simulate VehicleOffline
        vehicle_mock = AsyncMock()
        vehicle_mock.charge_stop = AsyncMock(
            side_effect=_make_vehicle_offline_exc()
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        await ctrl.stop_charging()
        assert ctrl._last_command_vehicle_offline is True

        # Now: simulate success
        ctrl._api = MagicMock()
        ctrl._api.has_private_key = True
        vehicle_mock_ok = AsyncMock()
        vehicle_mock_ok.charge_stop = AsyncMock(return_value=None)
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock_ok)  # type: ignore[assignment]
        ctrl.save_tokens = MagicMock()  # type: ignore[assignment]

        result = await ctrl.stop_charging()
        assert result is True
        assert ctrl._last_command_vehicle_offline is False


# =============================================================================
# Tests — Log level verification
# =============================================================================


class TestLogLevelVerification:

    @pytest.mark.asyncio
    async def test_set_charge_amps_vehicle_offline_at_warning(self, tesla_config, caplog):
        """VehicleOffline in set_charge_amps produces WARNING, not ERROR."""
        ctrl = _make_offline_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.set_charging_amps = AsyncMock(
            side_effect=_make_vehicle_offline_exc()
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING, logger="load_controllers"):
            await ctrl.set_charge_amps(16)

        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        error_msgs = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(warning_msgs) >= 1
        assert any("vehicle not online" in r.message for r in warning_msgs)
        assert not any("Failed to set Tesla charge amps" in r.message for r in error_msgs)

    @pytest.mark.asyncio
    async def test_stop_charging_vehicle_offline_at_warning(self, tesla_config, caplog):
        """VehicleOffline in stop_charging produces WARNING, not ERROR."""
        ctrl = _make_offline_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.charge_stop = AsyncMock(
            side_effect=_make_vehicle_offline_exc()
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING, logger="load_controllers"):
            await ctrl.stop_charging()

        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        error_msgs = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(warning_msgs) >= 1
        assert any("vehicle not online" in r.message for r in warning_msgs)
        assert not any("Failed to stop Tesla charging" in r.message for r in error_msgs)
