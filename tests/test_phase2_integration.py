"""Integration tests verifying CycleResult dataclasses are used in production code.

These tests verify that run_cycle() returns CycleResult instances (not dicts),
and that the result attributes are accessible via typed fields.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from load_controllers import (
    PlugController,
    TeslaController,
)
from load_manager import LoadManager
from load_models import (
    CycleDiagnostics,
    CycleResult,
    CycleStatus,
    PendingEffect,
    TeslaConfig,
)
from energy_cache import EnergyCache


def _make_manager(interval=30, **kwargs):
    """Create a minimal LoadManager with stub controllers."""
    plug_ctrl = PlugController({})
    tesla_ctrl = TeslaController(
        TeslaConfig(
            client_id="test-id",
            client_secret="test-secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="vehicle-123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
        )
    )

    def metrics_fetch():
        return None  # no data → will hit early returns

    energy_cache = EnergyCache()
    return LoadManager(
        metrics_fetch=metrics_fetch,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=tesla_ctrl,
        target_wh=-500,
        config_interval_secs=interval,
        **kwargs,
    )


class TestRunCycleReturnsCycleResult:
    """Verify run_cycle() returns CycleResult type, not dict."""

    def test_run_cycle_returns_cycle_result_instance(self):
        """run_cycle() returns a CycleResult, not a dict."""
        lm = _make_manager(interval=30)
        result = lm.run_cycle()
        assert isinstance(result, CycleResult), (
            f"Expected CycleResult, got {type(result).__name__}"
        )

    def test_run_cycle_result_status_field(self):
        """CycleResult.status is accessible as an attribute."""
        lm = _make_manager(interval=30)
        result = lm.run_cycle()
        assert hasattr(result, "status")
        assert isinstance(result.status, str)

    def test_run_cycle_result_diagnostics_type(self):
        """CycleResult.diagnostics is CycleDiagnostics or None."""
        lm = _make_manager(interval=30)
        result = lm.run_cycle()
        assert result.diagnostics is None or isinstance(
            result.diagnostics, CycleDiagnostics
        )

    def test_run_cycle_result_sleep_hint_attribute(self):
        """CycleResult.sleep_hint is accessible as a float attribute."""
        lm = _make_manager(interval=30)
        result = lm.run_cycle()
        assert hasattr(result, "sleep_hint")
        # sleep_hint should be a positive float (config_interval_secs default)
        assert result.sleep_hint > 0

    def test_run_cycle_result_actions_is_list(self):
        """CycleResult.actions is a list of PendingEffect."""
        lm = _make_manager(interval=30)
        result = lm.run_cycle()
        assert hasattr(result, "actions")
        assert isinstance(result.actions, list)

    def test_run_cycle_result_to_dict_matches_dict_structure(self):
        """CycleResult.to_dict() produces a dict with expected keys."""
        lm = _make_manager(interval=30)
        result = lm.run_cycle()
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "status" in d
        assert "qh" in d
        assert "sleep_hint" in d
        assert "actions" in d

    def test_run_cycle_result_dict_compatible(self):
        """CycleResult.to_dict() values match dict attribute access for old keys."""
        lm = _make_manager(interval=30)
        result = lm.run_cycle()
        d = result.to_dict()
        assert d["status"] == result.status
        assert d["sleep_hint"] == result.sleep_hint
        assert d["actions"] == result.actions

    def test_disabled_cycle_result_has_typed_status(self):
        """Disabled run_cycle returns CycleResult with status='disabled'."""
        lm = _make_manager(interval=30, enabled=False)

        result = lm.run_cycle()
        assert result.status == "disabled"
        assert isinstance(result, CycleResult)

    def test_disabled_cycle_result_sleep_hint(self):
        """Disabled run_cycle returns sleep_hint = config_interval."""
        lm = _make_manager(interval=30, enabled=False)

        result = lm.run_cycle()
        assert result.sleep_hint == 30

    def test_run_cycle_includes_sleep_hint(self):
        """run_cycle() returns sleep_hint in the CycleResult."""
        lm = _make_manager(interval=30)
        result = lm.run_cycle(force=True)
        assert hasattr(result, "sleep_hint")
        assert isinstance(result.sleep_hint, (int, float))

    def test_run_cycle_result_has_all_cycle_attributes(self):
        """CycleResult has all the fields that were dict keys in the old code."""
        lm = _make_manager(interval=30)
        result = lm.run_cycle()
        for attr in ("status", "qh", "predicted_wh", "adjusted_wh",
                     "target_wh", "actions", "diagnostics", "sleep_hint",
                     "sleep_hint_at", "gap_wh", "predicted_wh"):
            assert hasattr(result, attr), (
                f"CycleResult is missing attribute: {attr}"
            )

    def test_run_cycle_result_diagnostics_attributes(self):
        """CycleResult.diagnostics has expected attributes when present."""
        lm = _make_manager(interval=30)
        result = lm.run_cycle()
        if result.diagnostics is not None:
            for attr in ("gap_wh", "hysteresis_wh", "reason"):
                assert hasattr(result.diagnostics, attr), (
                    f"CycleDiagnostics is missing attribute: {attr}"
                )


class TestCycleResultStatusValues:
    """Verify that all expected status strings work with CycleStatus type."""

    def test_all_status_strings_accepted(self):
        """All expected status strings are valid CycleResult statuses."""
        valid_statuses: list[CycleStatus] = [
            "ok",
            "dry-run",
            "disabled",
            "no_incomplete_qh",
            "stale_data",
            "previous_qh",
            "waiting_for_fresh_data",
        ]
        for status in valid_statuses:
            result = CycleResult(status=status)
            assert result.status == status


class TestCycleResultSerializesToDict:
    """Verify CycleResult.to_dict() is compatible with app.py expectations."""

    def test_to_dict_has_expected_keys(self):
        """CycleResult.to_dict() has all keys that app.py accesses."""
        result = CycleResult(
            status="ok",
            qh="QH1",
            predicted_wh=-800.0,
            adjusted_wh=-750.0,
            target_wh=-500,
            actions=[],
            diagnostics=CycleDiagnostics(
                gap_wh=-300.0,
                hysteresis_wh=50,
                reason="ok",
            ),
            sleep_hint=30.0,
            sleep_hint_at="2025-01-15T12:00:00+00:00",
        )
        d = result.to_dict()
        expected_keys = {
            "status", "qh", "predicted_wh", "adjusted_wh",
            "target_wh", "current_wh", "estimated_wh",
            "actions", "diagnostics", "sleep_hint",
            "sleep_hint_at", "gap_wh", "pending_effects_count",
            "candidates",
        }
        assert expected_keys.issubset(set(d.keys()))

    def test_to_dict_diagnostics_is_dict(self):
        """CycleResult.to_dict() converts diagnostics to dict, not dataclass."""
        diag = CycleDiagnostics(
            gap_wh=-300.0,
            hysteresis_wh=50,
            reason="ok",
        )
        result = CycleResult(
            status="ok",
            diagnostics=diag,
        )
        d = result.to_dict()
        assert isinstance(d["diagnostics"], dict)
        assert d["diagnostics"]["reason"] == "ok"

    def test_to_dict_diagnostics_none_when_not_set(self):
        """CycleResult.to_dict() sets diagnostics to None when not provided."""
        result = CycleResult(status="disabled")
        d = result.to_dict()
        assert d["diagnostics"] is None

    def test_to_dict_actions_are_dicts(self):
        """CycleResult.to_dict() converts PendingEffect actions to dicts."""
        action = PendingEffect(
            device_name="Test Plug",
            action="turn_on",
            timestamp=datetime.now(timezone.utc),
            data_point_at=datetime.now(timezone.utc),
            power_watts=100.0,
        )
        result = CycleResult(
            status="ok",
            actions=[action],
        )
        d = result.to_dict()
        assert isinstance(d["actions"], list)
        assert isinstance(d["actions"][0], dict)
        assert d["actions"][0]["action"] == "turn_on"
        assert d["actions"][0]["device_name"] == "Test Plug"

    def test_to_dict_candidates_none_when_not_set(self):
        """CycleResult.to_dict() sets candidates to None when not provided."""
        result = CycleResult(status="disabled")
        d = result.to_dict()
        assert d["candidates"] is None
