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


# =============================================================================
# Signed pending-effect invariant (plan subtask 3.4, fixes A4)
# =============================================================================


class TestSignedEffectInvariant:
    """power_watts is a SIGNED impact on net load, enforced at construction.

    turn_on adds load (>= 0 W); turn_off sheds it (<= 0 W). This closes
    the phantom-load bug where a turned-OFF plug was counted as ADDED
    load by estimated_current_wh. Tesla set_amps is exempt: its impact
    is accounted separately via tesla_inflight_wh.
    """

    def _effect(self, action, watts, device="plug_a"):
        from load_models import PendingEffect
        from datetime import datetime, timezone

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return PendingEffect(
            device_name=device,
            action=action,
            timestamp=now,
            data_point_at=now,
            power_watts=watts,
        )

    def test_turn_off_rejects_positive_watts(self):
        with pytest.raises(ValueError, match="turn_off"):
            self._effect("turn_off", 60.0)

    def test_turn_off_accepts_negative_watts(self):
        effect = self._effect("turn_off", -60.0)
        assert effect.power_watts == -60.0

    def test_turn_on_rejects_negative_watts(self):
        with pytest.raises(ValueError, match="turn_on"):
            self._effect("turn_on", -60.0)

    def test_turn_on_accepts_positive_and_zero(self):
        assert self._effect("turn_on", 60.0).power_watts == 60.0
        assert self._effect("turn_on", 0.0).power_watts == 0.0

    def test_set_amps_exempt_from_sign_rule(self):
        """set_amps may carry any sign; it is not added to estimates."""
        assert self._effect("set_amps", 20.0, device="tesla") is not None
        assert self._effect("set_amps", -20.0, device="tesla") is not None

    def test_estimated_current_wh_subtracts_turn_off(self):
        from datetime import datetime, timezone
        from load_nbc import StateTracker

        tracker = StateTracker()
        tracker.add_effect(self._effect("turn_off", -60.0))
        adjusted = tracker.estimated_current_wh(100.0, seconds_remaining=900)
        assert adjusted == pytest.approx(100.0 - 15.0)  # -60 W * 900 s


class TestCycleResultSerialization:
    """CycleResult/CycleDiagnostics.to_dict() serialization contracts.

    These pin the JSON payload shape consumed by app.py routes and SSE
    events: nested dataclasses become dicts, datetimes become ISO strings,
    and the result stays json.dumps-able.
    """

    def _pending_effect(self) -> PendingEffect:
        now = datetime.now(timezone.utc)
        return PendingEffect(
            device_name="Test Plug",
            action="turn_on",
            timestamp=now,
            data_point_at=now,
            power_watts=100.0,
        )

    def test_diagnostics_to_dict_iso_formats_datetimes(self) -> None:
        from load_models import CycleDiagnostics

        diag = CycleDiagnostics(
            gap_wh=-300.0,
            hysteresis_wh=50,
            seconds_remaining=45,
            data_point_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
            reason="ok",
        )
        d = diag.to_dict()
        assert d["data_point_at"] == "2026-05-01T12:00:00+00:00"

    def test_diagnostics_to_dict_preserves_none_quantization(self) -> None:
        from load_models import CycleDiagnostics

        d = CycleDiagnostics(gap_wh=-1.0, hysteresis_wh=50, reason="ok").to_dict()
        assert d["quantization_seconds"] is None
        assert d["settle_window_secs"] is None

    def test_result_to_dict_nests_dicts_not_dataclasses(self) -> None:
        import json

        from load_models import (
            CandidateDetailPlug,
            CandidateDetailTesla,
            CycleDiagnostics,
            CycleResult,
        )

        plug = CandidateDetailPlug(
            name="Plug",
            power_watts=100.0,
            capacity_wh=1200.0,
            can_toggle=True,
        )
        tesla = CandidateDetailTesla(name="Model 3", state_available=True)
        result = CycleResult(
            status="ok",
            actions=[self._pending_effect()],
            diagnostics=CycleDiagnostics(gap_wh=-1.0, hysteresis_wh=50, reason="ok"),
            candidates=[plug, tesla],
        )
        d = result.to_dict()
        assert isinstance(d["diagnostics"], dict)
        assert isinstance(d["actions"][0], dict)
        assert d["actions"][0]["device_name"] == "Test Plug"
        assert isinstance(d["candidates"], list)
        assert all(isinstance(c, dict) for c in d["candidates"])
        # The whole payload must remain JSON-serializable for the API/SSE layers.
        json.dumps(d)

    def test_result_to_dict_minimal_defaults(self) -> None:
        from load_models import CycleResult

        d = CycleResult(status="disabled").to_dict()
        assert d["status"] == "disabled"
        assert d["actions"] == []
        assert d["diagnostics"] is None
        assert d["candidates"] is None
