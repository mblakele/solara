"""Red-phase tests: desired-state fallback for device pills.

When a pending effect has been pruned but the controller has not yet
confirmed the new state, the dashboard should keep showing pending
(faded) rather than flapping to off/on.
"""

import asyncio
from datetime import datetime, timezone
import unittest

from clock import FakeClock
from load_controllers import PlugController
from load_manager import LoadManager, LoadManagerConfig
from load_models import DeviceState, PlugConfig
from tests.helpers import _make_metrics_with_wh


class TestDesiredFallbackDisplay(unittest.TestCase):
    """Template should use desired_state when no pending effect exists."""

    def _render(self, devices: dict, effects: list) -> str:
        from flask import render_template
        import app as app_mod
        from tests.test_app import realistic_metrics

        metrics = realistic_metrics()
        nbc = metrics["devices"][0]["nbc"]
        nbc = dict(nbc, QH1=dict(nbc["QH1"], predicted_wh=-800.0))
        metrics = {"devices": [dict(metrics["devices"][0], nbc=nbc)]}
        load_management = {
            "target_wh": -50.0,
            "last_cycle_result": {
                "diagnostics": {"hysteresis_wh": 50.0, "gap_wh": 750.0}
            },
            "state": {"devices": devices, "pending_effects": effects},
        }
        with app_mod.app.test_request_context():
            return render_template(
                "_metrics.html",
                metrics=metrics,
                load_management=load_management,
                freshness=None,
            )

    def test_desired_true_actual_false_shows_pending_on(self) -> None:
        """No pending effect, desired=True, actual=False -> pending-on."""
        html = self._render(
            devices={
                "water_heater": {
                    "state": "pending_on",
                    "desired_state": True,
                    "actual_state": False,
                    "current_amps": None,
                },
            },
            effects=[],
        )
        assert "device-pill--pending-on" in html

    def test_desired_false_actual_true_shows_pending_off(self) -> None:
        """No pending effect, desired=False, actual=True -> pending-off."""
        html = self._render(
            devices={
                "water_heater": {
                    "state": "pending_off",
                    "desired_state": False,
                    "actual_state": True,
                    "current_amps": None,
                },
            },
            effects=[],
        )
        assert "device-pill--pending-off" in html


class TestPlugStatePillMapping(unittest.TestCase):
    """The template reads `state`; it does not re-derive it from the pair."""

    def _render_state(self, state: str) -> str:
        return TestDesiredFallbackDisplay()._render(
            devices={
                "water_heater": {
                    "state": state,
                    # Deliberately opposite to `state` so the test proves the
                    # template trusts the derived field.
                    "desired_state": False,
                    "actual_state": False,
                    "current_amps": None,
                },
            },
            effects=[],
        )

    def test_on_maps_to_solid_green(self) -> None:
        assert "device-pill--on" in self._render_state("on")

    def test_off_maps_to_grey(self) -> None:
        html = self._render_state("off")
        assert "device-pill--off" in html
        assert "device-pill--on" not in html

    def test_pending_on_maps_to_faded_green(self) -> None:
        assert "device-pill--pending-on" in self._render_state("pending_on")

    def test_pending_off_maps_to_blue(self) -> None:
        assert "device-pill--pending-off" in self._render_state("pending_off")

    def test_unknown_maps_to_grey(self) -> None:
        html = self._render_state("unknown")
        assert "device-pill--off" in html
        assert "device-pill--on" not in html


class TestSyncPreservesUnconfirmedDesired(unittest.TestCase):
    """Sync must not treat our own unconfirmed command as external."""

    def test_no_flip_when_actual_unchanged(self) -> None:
        now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
        plugs = {
            "heater": PlugConfig(
                name="heater", accessory_id="h1",
                power_watts=2000.0, priority=10,
            ),
        }
        plug_ctrl = PlugController(plugs)
        mgr = LoadManager(LoadManagerConfig(
            metrics_fetch=lambda: _make_metrics_with_wh("main_panel", -2000.0),
            plug_ctrl=plug_ctrl,
            tesla_ctrl=None,
            target_wh=-500,
            nbc_device="main_panel",
            enabled=True,
            dry_run=False,
        ))
        mgr._clock = FakeClock(now)
        mgr.plug_ctrl._state["heater"] = False  # type: ignore[attr-defined]
        mgr.state.devices["heater"] = DeviceState(
            name="heater", desired_state=True, actual_state=False,
            last_toggle=now,
        )
        asyncio.run(mgr._sync_plug_states())
        dev = mgr.state.devices["heater"]
        assert dev.desired_state is True
        assert mgr.state.snapshot_effects() == []
