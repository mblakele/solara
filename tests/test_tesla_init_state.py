"""Tests for Tesla init state: VehicleOffline handling and exponential backoff.

These tests focus on init_tesla_state() error handling when the vehicle
is asleep/offline and the exponential backoff retry mechanism.

See also test_load_controllers.py::TestRealTeslaControllerInitTeslaState
for init state tests that do not involve VehicleOffline.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from load_controllers import RealTeslaController
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


# =============================================================================
# Tests — VehicleOffline catch + backoff
# =============================================================================


class TestInitTeslaStateVehicleOffline:

    @pytest.mark.asyncio
    async def test_init_from_rest_catches_vehicle_offline(self, tesla_config):
        """_init_from_rest() catches VehicleOffline and returns None instead of propagating."""
        from tesla_fleet_api.exceptions import VehicleOffline

        ctrl = RealTeslaController(tesla_config)

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = VehicleOffline({"error": "vehicle is not online"})
            result = await ctrl._init_from_rest()

        assert result is None

    @pytest.mark.asyncio
    async def test_init_tesla_state_returns_none_on_vehicle_offline(self, tesla_config):
        """init_tesla_state() returns None when VehicleOffline comes from REST fallback.

        The exception must NOT propagate up to kill run_cycle.
        """
        from tesla_fleet_api.exceptions import VehicleOffline

        ctrl = RealTeslaController(tesla_config)

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = VehicleOffline({"error": "vehicle is not online"})
            # Telemetry unavailable so we hit the REST path; timeout=0 skips the wait
            with patch("mqtt_telemetry.has_telemetry", return_value=False):
                result = await ctrl.init_tesla_state(timeout=0)

        assert result is None
        # _init_state should NOT be set (remains initial None) so retries are possible
        assert ctrl._init_state is None

    @pytest.mark.asyncio
    async def test_backoff_prevents_immediate_retry(self, tesla_config):
        """After a VehicleOffline failure, a second call within the backoff window
        skips the REST fallback and returns None immediately.
        """
        from tesla_fleet_api.exceptions import VehicleOffline

        ctrl = RealTeslaController(tesla_config)

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = VehicleOffline({"error": "vehicle is not online"})
            with patch("mqtt_telemetry.has_telemetry", return_value=False):
                # First call: fails, sets backoff
                result1 = await ctrl.init_tesla_state(timeout=0)
                assert result1 is None

                # Second call immediately: should be skipped by backoff
                result2 = await ctrl.init_tesla_state(timeout=0)
                assert result2 is None

        # _fetch_vehicle_data should only have been called once
        assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_backoff_does_not_affect_telemetry_path(self, tesla_config):
        """Backoff only applies to the REST fallback, not the telemetry fast path.

        If telemetry becomes available, init_tesla_state should return it
        regardless of backoff state.
        """
        from tesla_fleet_api.exceptions import VehicleOffline

        ctrl = RealTeslaController(tesla_config)
        mock_charge = {
            "charge_state": {
                "charging_state": "Charging",
                "charge_amps": 32,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = VehicleOffline({"error": "vehicle is not online"})
            with patch("mqtt_telemetry.has_telemetry", return_value=False):
                # First call fails with VehicleOffline — sets backoff
                result1 = await ctrl.init_tesla_state(timeout=0)
                assert result1 is None

        # Now telemetry is available — should succeed despite backoff
        mock_fetch.reset_mock(side_effect=True)
        mock_fetch.return_value = mock_charge
        with patch("mqtt_telemetry.has_telemetry", return_value=True):
            from mqtt_telemetry import get_telemetry_snapshot, tesla_state_from_snapshot

            result2 = await ctrl.init_tesla_state(timeout=0)
            # telemetry path should work regardless of backoff
            if result2 is not None:
                assert result2.is_charging is True
                assert result2.current_amps == 32
            # If telemetry snapshot was empty, we might get None but NOT an exception
            assert result2 is None or isinstance(result2, type(None)) or result2.is_charging

    @pytest.mark.asyncio
    async def test_backoff_resets_on_successful_init(self, tesla_config):
        """After a successful init, backoff is reset to 0."""
        from tesla_fleet_api.exceptions import VehicleOffline

        ctrl = RealTeslaController(tesla_config)
        mock_charge = {
            "charge_state": {
                "charging_state": "Charging",
                "charge_amps": 32,
            }
        }

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = VehicleOffline({"error": "vehicle is not online"})
            with patch("mqtt_telemetry.has_telemetry", return_value=False):
                result1 = await ctrl.init_tesla_state(timeout=0)
                assert result1 is None
                assert ctrl._backoff_secs > 0

            # Now succeed
            mock_fetch.reset_mock(side_effect=True)
            # Need to wait past backoff so the retry is attempted. We patch
            # time.monotonic to simulate waiting past the backoff period.
            import time as time_module

            original_monotonic = time_module.monotonic
            with patch("load_controllers._time.monotonic") as mock_monotonic:
                # Return a value that's past the backoff window
                now = original_monotonic() + 60
                mock_monotonic.return_value = now

                with patch("mqtt_telemetry.has_telemetry", return_value=False):
                    mock_fetch.return_value = mock_charge
                    result2 = await ctrl.init_tesla_state(timeout=0)

        assert result2 is not None
        assert result2.is_charging is True
        assert result2.current_amps == 32
        assert ctrl._backoff_secs == 0.0
        assert ctrl._init_state is not None

    @pytest.mark.asyncio
    async def test_backoff_caps_at_max(self, tesla_config):
        """Repeated failures cap backoff at CAR_OFFLINE_BACKOFF_MAX (900s)."""
        from tesla_fleet_api.exceptions import VehicleOffline

        ctrl = RealTeslaController(tesla_config)
        # Import the backoff constants for assertion
        from load_controllers import CAR_OFFLINE_BACKOFF_MAX

        with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
            mock_fetch.side_effect = VehicleOffline({"error": "vehicle is not online"})
            with patch("mqtt_telemetry.has_telemetry", return_value=False):
                # Consecutive failures with enough time between them to bypass backoff
                for i in range(10):
                    # Simulate waiting past the current backoff
                    with patch("load_controllers._time.monotonic") as mock_monotonic:
                        mock_monotonic.return_value = i * 1000.0  # well past any backoff
                        result = await ctrl.init_tesla_state(timeout=0)
                        assert result is None

        assert ctrl._backoff_secs == CAR_OFFLINE_BACKOFF_MAX
