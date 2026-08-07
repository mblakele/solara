"""Tests for load_models data models."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from load_models import (
    PendingEffect,
    TeslaVehicleTelemetry,
    parse_charge_amps,
    unwrap_telemetry_value,
)


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


class TestUnwrapTelemetryValue:
    """unwrap_telemetry_value() strips the fleet-telemetry envelope."""

    def test_wrapped_envelope(self) -> None:
        """A {"value": ..., "createdAt": ...} envelope is unwrapped."""
        assert unwrap_telemetry_value({"value": 32.0, "createdAt": "now"}) == 32.0

    def test_raw_scalar(self) -> None:
        """Raw scalar payloads pass through unchanged."""
        assert unwrap_telemetry_value(16.0) == 16.0

    def test_raw_dict_without_value_key(self) -> None:
        """Dicts without a "value" key are returned as-is (same object)."""
        raw = {"latitude": 37.0, "longitude": -122.0}
        assert unwrap_telemetry_value(raw) is raw

    def test_none_passthrough(self) -> None:
        """None passes through unchanged."""
        assert unwrap_telemetry_value(None) is None


class TestParseChargeAmps:
    """parse_charge_amps() converts a ChargeAmps telemetry value to int amps."""

    def test_raw_float(self) -> None:
        """A raw float is rounded to an int."""
        assert parse_charge_amps(16.0) == 16

    def test_wrapped_value(self) -> None:
        """An enveloped value is unwrapped then rounded."""
        assert parse_charge_amps({"value": 32.0}) == 32

    def test_none_returns_none(self) -> None:
        """None (missing value) returns None."""
        assert parse_charge_amps(None) is None

    def test_wrapped_none_returns_none(self) -> None:
        """An envelope wrapping None returns None."""
        assert parse_charge_amps({"value": None}) is None

    def test_non_numeric_returns_none(self) -> None:
        """Non-numeric payloads return None instead of raising."""
        assert parse_charge_amps("abc") is None

    def test_zero_rounds_to_zero(self) -> None:
        """Zero amps parses to 0 (callers treat it as not charging)."""
        assert parse_charge_amps(0.0) == 0
