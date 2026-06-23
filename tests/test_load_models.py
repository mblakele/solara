"""Tests for load_models data models."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from load_models import PendingEffect, TeslaVehicleTelemetry


class TestPendingEffect:
    """Tests for PendingEffect dataclass."""

    def test_power_watts_required(self) -> None:
        """PendingEffect.power_watts must always be provided — never optional."""
        now = datetime.now(timezone.utc)

        # Creating a PendingEffect without power_watts should be a type error.
        # This test verifies the invariant at runtime: power_watts has no default.
        with pytest.raises(TypeError):
            PendingEffect(
                device_name="water_heater",
                action="turn_on",
                timestamp=now,
                data_point_at=now,
            )


class TestTeslaVehicleTelemetry:
    """Tests for TeslaVehicleTelemetry dataclass (frozen telemetry model)."""

    def test_frozen_dataclass(self) -> None:
        """TeslaVehicleTelemetry must be immutable (frozen)."""
        ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        state = TeslaVehicleTelemetry(
            timestamp=ts,
            vehicle_id=777,
            is_charging=True,
            current_amps=32,
            plugged_in=True,
            at_home=True,
        )
        with pytest.raises(Exception):
            state.is_charging = False  # type: ignore[misc]

    def test_all_nullable_fields(self) -> None:
        """All state fields may be None except timestamp and vehicle_id."""
        ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        state = TeslaVehicleTelemetry(
            timestamp=ts,
            vehicle_id="Tesla-42",
            is_charging=None,
            current_amps=None,
            plugged_in=None,
            at_home=None,
        )
        assert state.is_charging is None
        assert state.current_amps is None
        assert state.plugged_in is None
        assert state.at_home is None

    def test_vehicle_id_accepts_int(self) -> None:
        """vehicle_id accepts integer IDs."""
        ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        state = TeslaVehicleTelemetry(
            timestamp=ts,
            vehicle_id=777,
            is_charging=True,
            current_amps=32,
            plugged_in=True,
            at_home=True,
        )
        assert state.vehicle_id == 777

    def test_vehicle_id_accepts_str(self) -> None:
        """vehicle_id accepts string VINs."""
        ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        state = TeslaVehicleTelemetry(
            timestamp=ts,
            vehicle_id="7SAYGDED7TF55937X",
            is_charging=True,
            current_amps=32,
            plugged_in=True,
            at_home=True,
        )
        assert state.vehicle_id == "7SAYGDED7TF55937X"

    def test_default_values(self) -> None:
        """Boolean fields default to None."""
        ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        state = TeslaVehicleTelemetry(
            timestamp=ts,
            vehicle_id=1,
        )
        assert state.is_charging is None
        assert state.plugged_in is None
        assert state.at_home is None
