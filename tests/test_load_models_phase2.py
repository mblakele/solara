"""Tests for Phase 2 dataclasses in load_models.py:
CycleDiagnostics, CandidateDetailPlug, CandidateDetailTesla,
CandidateDetail, CycleResult.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from load_models import (
    CandidateDetail,
    CandidateDetailPlug,
    CandidateDetailTesla,
    CycleDiagnostics,
    CycleResult,
    CycleStatus,
    PendingEffect,
    PlugConfig,
    PlugAction,
    TeslaConfig,
)


class TestCycleDiagnostics(unittest.TestCase):
    """Tests for CycleDiagnostics dataclass."""

    def test_required_fields(self):
        """CycleDiagnostics requires gap_wh, hysteresis_wh, reason."""
        diag = CycleDiagnostics(
            gap_wh=-300.0,
            hysteresis_wh=50,
            reason="ok",
        )
        self.assertEqual(diag.gap_wh, -300.0)
        self.assertEqual(diag.hysteresis_wh, 50)
        self.assertIsNone(diag.seconds_remaining)
        self.assertIsNone(diag.data_point_at)
        self.assertEqual(diag.reason, "ok")
        self.assertIsNone(diag.pending_effects_count)
        self.assertIsNone(diag.tesla_configured)
        self.assertIsNone(diag.tesla_state)
        self.assertIsNone(diag.tesla_error)
        self.assertIsNone(diag.tesla_login_url)
        self.assertIsNone(diag.plugs_configured)

    def test_all_fields_populated(self):
        """CycleDiagnostics can hold all fields."""
        now = datetime.now(timezone.utc)
        diag = CycleDiagnostics(
            gap_wh=-300.0,
            hysteresis_wh=50,
            seconds_remaining=45,
            data_point_at=now,
            reason="ok",
            pending_effects_count=2,
            tesla_configured=True,
            tesla_state={"is_charging": True},
            tesla_error=None,
            tesla_login_url=None,
            plugs_configured=["living_room_plug", "garage_plug"],
        )
        self.assertEqual(diag.gap_wh, -300.0)
        self.assertEqual(diag.hysteresis_wh, 50)
        self.assertEqual(diag.seconds_remaining, 45)
        self.assertEqual(diag.data_point_at, now)
        self.assertEqual(diag.reason, "ok")
        self.assertEqual(diag.pending_effects_count, 2)
        self.assertTrue(diag.tesla_configured)
        self.assertEqual(diag.tesla_state, {"is_charging": True})
        self.assertIsNone(diag.tesla_error)
        self.assertIsNone(diag.tesla_login_url)
        self.assertEqual(diag.plugs_configured, ["living_room_plug", "garage_plug"])

    def test_to_dict_required_only(self):
        """CycleDiagnostics.to_dict() includes only non-None optional fields."""
        diag = CycleDiagnostics(
            gap_wh=-300.0,
            hysteresis_wh=50,
            reason="ok",
        )
        d = diag.to_dict()
        self.assertEqual(d["gap_wh"], -300.0)
        self.assertEqual(d["hysteresis_wh"], 50)
        self.assertEqual(d["seconds_remaining"], None)
        self.assertEqual(d["data_point_at"], None)
        self.assertEqual(d["reason"], "ok")
        self.assertEqual(d["pending_effects_count"], None)
        self.assertEqual(d["tesla_configured"], None)
        self.assertEqual(d["tesla_state"], None)
        self.assertEqual(d["tesla_error"], None)
        self.assertEqual(d["tesla_login_url"], None)
        self.assertEqual(d["plugs_configured"], None)

    def test_to_dict_all_fields(self):
        """CycleDiagnostics.to_dict() includes all populated fields."""
        now = datetime.now(timezone.utc)
        diag = CycleDiagnostics(
            gap_wh=-300.0,
            hysteresis_wh=50,
            seconds_remaining=45,
            data_point_at=now,
            reason="hysteresis",
            pending_effects_count=3,
            tesla_configured=True,
            tesla_state={"is_charging": False},
            tesla_error=None,
            tesla_login_url="https://example.com/login",
            plugs_configured=["kitchen_plug"],
        )
        d = diag.to_dict()
        self.assertEqual(d["gap_wh"], -300.0)
        self.assertEqual(d["hysteresis_wh"], 50)
        self.assertEqual(d["seconds_remaining"], 45)
        self.assertEqual(d["data_point_at"], now.isoformat())
        self.assertEqual(d["reason"], "hysteresis")
        self.assertEqual(d["pending_effects_count"], 3)
        self.assertEqual(d["tesla_configured"], True)
        self.assertEqual(d["tesla_state"], {"is_charging": False})
        self.assertEqual(d["tesla_login_url"], "https://example.com/login")
        self.assertEqual(d["plugs_configured"], ["kitchen_plug"])

    def test_to_dict_json_serializable(self):
        """CycleDiagnostics.to_dict() produces JSON-serializable output."""
        now = datetime.now(timezone.utc)
        diag = CycleDiagnostics(
            gap_wh=-300.0,
            hysteresis_wh=50,
            seconds_remaining=45,
            data_point_at=now,
            reason="ok",
            tesla_state={"is_charging": True},
            plugs_configured=["plug1"],
        )
        # Should not raise
        json.dumps(diag.to_dict())


class TestCandidateDetailPlug(unittest.TestCase):
    """Tests for CandidateDetailPlug dataclass."""

    def test_all_fields(self):
        """CandidateDetailPlug has all expected fields."""
        detail = CandidateDetailPlug(
            name="Kitchen Plug",
            power_watts=150.0,
            capacity_wh=1800.0,
            can_toggle=True,
            desired_state=True,
            actual_state=False,
        )
        self.assertEqual(detail.name, "Kitchen Plug")
        self.assertEqual(detail.power_watts, 150.0)
        self.assertEqual(detail.capacity_wh, 1800.0)
        self.assertTrue(detail.can_toggle)
        self.assertTrue(detail.desired_state)
        self.assertFalse(detail.actual_state)

    def test_optional_defaults_to_none(self):
        """CandidateDetailPlug optional fields default to None."""
        detail = CandidateDetailPlug(
            name="Simple Plug",
            power_watts=100.0,
            capacity_wh=1200.0,
            can_toggle=False,
        )
        self.assertIsNone(detail.desired_state)
        self.assertIsNone(detail.actual_state)

    def test_to_dict(self):
        """CandidateDetailPlug.to_dict() produces correct dict."""
        detail = CandidateDetailPlug(
            name="Living Room",
            power_watts=200.0,
            capacity_wh=2400.0,
            can_toggle=True,
            desired_state=True,
            actual_state=False,
        )
        d = detail.to_dict()
        self.assertEqual(d["name"], "Living Room")
        self.assertEqual(d["power_watts"], 200.0)
        self.assertEqual(d["capacity_wh"], 2400.0)
        self.assertTrue(d["can_toggle"])
        self.assertTrue(d["desired_state"])
        self.assertFalse(d["actual_state"])

    def test_to_dict_json_serializable(self):
        """CandidateDetailPlug.to_dict() is JSON serializable."""
        detail = CandidateDetailPlug(
            name="Test",
            power_watts=50.0,
            capacity_wh=600.0,
            can_toggle=True,
            desired_state=False,
            actual_state=False,
        )
        json.dumps(detail.to_dict())


class TestCandidateDetailTesla(unittest.TestCase):
    """Tests for CandidateDetailTesla dataclass."""

    def test_defaults(self):
        """CandidateDetailTesla has sensible defaults."""
        detail = CandidateDetailTesla()
        self.assertEqual(detail.name, "tesla")
        self.assertFalse(detail.state_available)
        self.assertIsNone(detail.error)
        self.assertIsNone(detail.is_charging)
        self.assertIsNone(detail.current_amps)
        self.assertIsNone(detail.plugged_in)
        self.assertIsNone(detail.at_home)

    def test_all_fields_populated(self):
        """CandidateDetailTesla can hold all fields."""
        detail = CandidateDetailTesla(
            name="Model 3",
            state_available=True,
            error=None,
            is_charging=True,
            current_amps=32.0,
            plugged_in=True,
            at_home=True,
        )
        self.assertEqual(detail.name, "Model 3")
        self.assertTrue(detail.state_available)
        self.assertIsNone(detail.error)
        self.assertTrue(detail.is_charging)
        self.assertEqual(detail.current_amps, 32.0)
        self.assertTrue(detail.plugged_in)
        self.assertTrue(detail.at_home)

    def test_with_error(self):
        """CandidateDetailTesla can hold an error message."""
        detail = CandidateDetailTesla(
            name="Model Y",
            state_available=False,
            error="Tesla API timeout",
        )
        self.assertFalse(detail.state_available)
        self.assertEqual(detail.error, "Tesla API timeout")

    def test_to_dict(self):
        """CandidateDetailTesla.to_dict() produces correct dict."""
        detail = CandidateDetailTesla(
            name="Model 3",
            state_available=True,
            is_charging=True,
            current_amps=32.0,
            plugged_in=True,
            at_home=True,
        )
        d = detail.to_dict()
        self.assertEqual(d["name"], "Model 3")
        self.assertTrue(d["state_available"])
        self.assertTrue(d["is_charging"])
        self.assertEqual(d["current_amps"], 32.0)
        self.assertTrue(d["plugged_in"])
        self.assertTrue(d["at_home"])

    def test_to_dict_json_serializable(self):
        """CandidateDetailTesla.to_dict() is JSON serializable."""
        detail = CandidateDetailTesla(
            name="Test",
            state_available=True,
            is_charging=False,
            plugged_in=True,
            at_home=True,
        )
        json.dumps(detail.to_dict())


class TestCandidateDetail(unittest.TestCase):
    """Tests for CandidateDetail union type."""

    def test_plug_variant(self):
        """CandidateDetail can be a CandidateDetailPlug."""
        plug: CandidateDetail = CandidateDetailPlug(
            name="Plug",
            power_watts=100.0,
            capacity_wh=1200.0,
            can_toggle=True,
        )
        # isinstance check works
        self.assertIsInstance(plug, CandidateDetailPlug)
        self.assertNotIsInstance(plug, CandidateDetailTesla)

    def test_tesla_variant(self):
        """CandidateDetail can be a CandidateDetailTesla."""
        tesla: CandidateDetail = CandidateDetailTesla()
        self.assertIsInstance(tesla, CandidateDetailTesla)
        self.assertNotIsInstance(tesla, CandidateDetailPlug)


class TestCycleResult(unittest.TestCase):
    """Tests for CycleResult dataclass."""

    def test_disabled_result(self):
        """CycleResult for disabled status."""
        result = CycleResult(status="disabled")
        self.assertEqual(result.status, "disabled")
        self.assertIsNone(result.qh)
        self.assertIsNone(result.predicted_wh)
        self.assertIsNone(result.adjusted_wh)
        self.assertIsNone(result.target_wh)
        self.assertIsNone(result.current_wh)
        self.assertIsNone(result.estimated_wh)
        self.assertEqual(result.actions, [])
        self.assertIsNone(result.diagnostics)
        self.assertEqual(result.sleep_hint, 0.0)
        self.assertIsNone(result.sleep_hint_at)
        self.assertIsNone(result.gap_wh)
        self.assertIsNone(result.pending_effects_count)
        self.assertIsNone(result.candidates)

    def test_hysteresis_result(self):
        """CycleResult for hysteresis status."""
        diag = CycleDiagnostics(
            gap_wh=-100.0,
            hysteresis_wh=50,
            reason="hysteresis",
        )
        result = CycleResult(
            status="previous_qh",
            qh="QH2",
            predicted_wh=-800.0,
            adjusted_wh=-750.0,
            target_wh=-500,
            current_wh=-750.0,
            estimated_wh=-700.0,
            diagnostics=diag,
            sleep_hint=30.0,
            sleep_hint_at="2025-01-15T12:00:00+00:00",
            gap_wh=-100.0,
            pending_effects_count=2,
        )
        self.assertEqual(result.status, "previous_qh")
        self.assertEqual(result.qh, "QH2")
        self.assertEqual(result.predicted_wh, -800.0)
        self.assertEqual(result.target_wh, -500)
        self.assertEqual(result.diagnostics, diag)
        self.assertEqual(result.sleep_hint, 30.0)
        self.assertEqual(result.sleep_hint_at, "2025-01-15T12:00:00+00:00")
        self.assertEqual(result.pending_effects_count, 2)

    def test_normal_result_with_actions(self):
        """CycleResult for normal execution with actions."""
        action = PendingEffect(
            device_name="Kitchen Plug",
            action="turn_on",
            timestamp=datetime.now(timezone.utc),
            data_point_at=datetime.now(timezone.utc),
            power_watts=150.0,
        )
        plug_detail = CandidateDetailPlug(
            name="Kitchen Plug",
            power_watts=150.0,
            capacity_wh=1800.0,
            can_toggle=True,
            desired_state=True,
            actual_state=False,
        )
        diag = CycleDiagnostics(
            gap_wh=-300.0,
            hysteresis_wh=50,
            seconds_remaining=45,
            reason="ok",
            pending_effects_count=1,
            plugs_configured=["Kitchen Plug"],
        )
        result = CycleResult(
            status="ok",
            qh="QH3",
            predicted_wh=-900.0,
            adjusted_wh=-850.0,
            target_wh=-500,
            current_wh=-850.0,
            estimated_wh=-700.0,
            actions=[action],
            diagnostics=diag,
            sleep_hint=15.0,
            sleep_hint_at="2025-01-15T12:01:00+00:00",
            gap_wh=-300.0,
            pending_effects_count=1,
            candidates=[plug_detail],
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.qh, "QH3")
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0], action)
        self.assertEqual(len(result.candidates), 1)
        self.assertIsInstance(result.candidates[0], CandidateDetailPlug)

    def test_dry_run_result(self):
        """CycleResult can represent dry-run status."""
        result = CycleResult(
            status="dry-run",
            qh="QH1",
            predicted_wh=-800.0,
            target_wh=-500,
            actions=[],
            diagnostics=CycleDiagnostics(
                gap_wh=-300.0,
                hysteresis_wh=50,
                reason="ok",
            ),
            sleep_hint=30.0,
        )
        self.assertEqual(result.status, "dry-run")

    def test_stale_data_result(self):
        """CycleResult for stale_data status."""
        result = CycleResult(
            status="stale_data",
            qh="QH2",
            predicted_wh=-600.0,
            target_wh=-500,
            diagnostics=CycleDiagnostics(
                gap_wh=None,
                hysteresis_wh=50,
                reason="stale_data",
            ),
        )
        self.assertEqual(result.status, "stale_data")

    def test_no_incomplete_qh_result(self):
        """CycleResult for no_incomplete_qh status."""
        result = CycleResult(
            status="no_incomplete_qh",
            qh="QH4",
            predicted_wh=-400.0,
            target_wh=-500,
            diagnostics=CycleDiagnostics(
                gap_wh=100.0,
                hysteresis_wh=50,
                reason="no_incomplete_qh",
            ),
        )
        self.assertEqual(result.status, "no_incomplete_qh")

    def test_default_actions_is_empty_list(self):
        """CycleResult.actions defaults to empty list."""
        result = CycleResult(status="disabled")
        self.assertEqual(result.actions, [])

    def test_default_sleep_hint_is_zero(self):
        """CycleResult.sleep_hint defaults to 0.0."""
        result = CycleResult(status="disabled")
        self.assertEqual(result.sleep_hint, 0.0)

    def test_to_dict_full(self):
        """CycleResult.to_dict() produces full dict with all fields."""
        action = PendingEffect(
            device_name="Test Plug",
            action="turn_on",
            timestamp=datetime.now(timezone.utc),
            data_point_at=datetime.now(timezone.utc),
            power_watts=100.0,
        )
        plug_detail = CandidateDetailPlug(
            name="Test Plug",
            power_watts=100.0,
            capacity_wh=1200.0,
            can_toggle=True,
            desired_state=True,
            actual_state=False,
        )
        diag = CycleDiagnostics(
            gap_wh=-300.0,
            hysteresis_wh=50,
            seconds_remaining=45,
            data_point_at=datetime.now(timezone.utc),
            reason="ok",
            pending_effects_count=1,
            tesla_configured=False,
            plugs_configured=["Test Plug"],
        )
        result = CycleResult(
            status="ok",
            qh="QH1",
            predicted_wh=-800.0,
            adjusted_wh=-750.0,
            target_wh=-500,
            current_wh=-750.0,
            estimated_wh=-700.0,
            actions=[action],
            diagnostics=diag,
            sleep_hint=30.0,
            sleep_hint_at="2025-01-15T12:00:00+00:00",
            gap_wh=-300.0,
            pending_effects_count=1,
            candidates=[plug_detail],
        )
        d = result.to_dict()
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["qh"], "QH1")
        self.assertEqual(d["predicted_wh"], -800.0)
        self.assertEqual(d["target_wh"], -500)
        self.assertEqual(d["sleep_hint"], 30.0)
        self.assertEqual(d["sleep_hint_at"], "2025-01-15T12:00:00+00:00")
        self.assertEqual(d["gap_wh"], -300.0)
        self.assertEqual(d["pending_effects_count"], 1)
        self.assertIsInstance(d["diagnostics"], dict)
        self.assertIsInstance(d["actions"], list)
        self.assertIsInstance(d["candidates"], list)
        self.assertEqual(len(d["actions"]), 1)
        self.assertEqual(len(d["candidates"]), 1)

    def test_to_dict_minimal(self):
        """CycleResult.to_dict() works with minimal fields."""
        result = CycleResult(status="disabled")
        d = result.to_dict()
        self.assertEqual(d["status"], "disabled")
        self.assertIsNone(d["qh"])
        self.assertIsNone(d["predicted_wh"])
        self.assertIsNone(d["diagnostics"])
        self.assertEqual(d["actions"], [])
        self.assertIsNone(d["candidates"])

    def test_to_dict_json_serializable(self):
        """CycleResult.to_dict() produces JSON-serializable output."""
        action = PendingEffect(
            device_name="Plug",
            action="turn_on",
            timestamp=datetime.now(timezone.utc),
            data_point_at=datetime.now(timezone.utc),
            power_watts=100.0,
        )
        diag = CycleDiagnostics(
            gap_wh=-300.0,
            hysteresis_wh=50,
            seconds_remaining=45,
            data_point_at=datetime.now(timezone.utc),
            reason="ok",
            pending_effects_count=1,
            tesla_configured=True,
            tesla_state={"is_charging": True},
            plugs_configured=["Plug"],
        )
        detail = CandidateDetailPlug(
            name="Plug",
            power_watts=100.0,
            capacity_wh=1200.0,
            can_toggle=True,
            desired_state=True,
            actual_state=False,
        )
        result = CycleResult(
            status="ok",
            qh="QH1",
            predicted_wh=-800.0,
            target_wh=-500,
            actions=[action],
            diagnostics=diag,
            sleep_hint=30.0,
            sleep_hint_at="2025-01-15T12:00:00+00:00",
            gap_wh=-300.0,
            pending_effects_count=1,
            candidates=[detail],
        )
        # Should not raise
        json.dumps(result.to_dict())

    def test_to_dict_candidates_mixed(self):
        """CycleResult.to_dict() handles mixed candidate types."""
        plug = CandidateDetailPlug(
            name="Plug",
            power_watts=100.0,
            capacity_wh=1200.0,
            can_toggle=True,
        )
        tesla = CandidateDetailTesla(
            name="Model 3",
            state_available=True,
            is_charging=True,
            plugged_in=True,
            at_home=True,
        )
        result = CycleResult(
            status="ok",
            candidates=[plug, tesla],
        )
        d = result.to_dict()
        self.assertEqual(len(d["candidates"]), 2)
        # Should be JSON serializable
        json.dumps(d)


class TestCycleStatusLiteral(unittest.TestCase):
    """Tests for CycleStatus literal type."""

    def test_valid_statuses(self):
        """All expected status strings are valid CycleStatus values."""
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
            # Just verifying the type is accepted
            result = CycleResult(status=status)
            self.assertEqual(result.status, status)


if __name__ == "__main__":
    unittest.main()
