"""Tests for the "ghost Tesla amps" bug.

Regression guard for bugs/2026-08-31-ghost-tesla-amps.log: when live MQTT
telemetry goes stale / reports a disconnected car (fields all None, no positive
ChargeAmps), ``tesla_state_from_snapshot`` returns ``None``. The fast path in
``_fetch_tesla_state_async`` then falls through to
``RealTeslaController.init_tesla_state(timeout=0)``, which short-circuits on its
cached ``_init_state`` — the last state ever seen, e.g. "charging @ 7 A, plugged
in, at home". That stale object is then served as the *live* Tesla state, a ghost
that makes load management think the car is still drawing 7 A even though it is
not charging and not at the geofenced home location.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from load_manager import LoadManager, LoadManagerConfig
from load_models import TeslaConfig, TeslaState


def _make_lm_with_real_ctrl() -> LoadManager:
    """Build a LoadManager wired to a RealTeslaController with stale cache."""
    from load_controllers import RealTeslaController

    config = TeslaConfig(
        client_id="test",
        client_secret="test",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
        home_lat=37.55303,
        home_lon=-122.25198,
        home_radius_m=500.0,
    )
    ctrl = RealTeslaController(config)
    # Seed the stale cached init state — the ghost (car WAS charging at 7 A).
    ctrl._init_state = TeslaState(  # noqa: SLF001
        is_charging=True, current_amps=7, plugged_in=True, at_home=True,
    )
    mgr = LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))
    mgr.tesla_config = config
    mgr.tesla_ctrl = ctrl
    return mgr


class TestGhostTeslaAmps:
    """_fetch_tesla_state_async must not resurrect stale cached charging state."""

    def test_all_none_telemetry_does_not_return_stale_charging_state(self):
        """A disconnected/None telemetry snapshot must not yield the ghost 7 A.

        Regression: with all charge fields None (parses to ``None``), the old
        code fell through to ``init_tesla_state`` and returned the stale cached
        ``_init_state`` (is_charging=True, current_amps=7). It must instead
        report the vehicle as NOT charging.
        """
        mgr = _make_lm_with_real_ctrl()

        telemetry_snapshot = {
            "ChargeAmps": None,
            "DetailedChargeState": None,
            "ChargeState": None,
            "Location": {"latitude": 37.55, "longitude": -122.25},
        }
        with patch("load_manager.has_telemetry", return_value=True), patch(
            "load_manager.get_telemetry_snapshot", return_value=telemetry_snapshot
        ):
            state, error, url = asyncio.run(mgr._fetch_tesla_state_async())

        assert error is None
        assert url is None
        assert state is not None
        # The ghost must NOT leak through: the vehicle is not charging.
        assert state.is_charging is False, (
            f"Expected is_charging=False (telemetry says disconnected), "
            f"got is_charging={state.is_charging} (stale cached ghost)"
        )
        assert state.current_amps in (0, None), (
            f"Expected current_amps 0/None, got {state.current_amps}"
        )
        assert state.plugged_in is False, (
            f"Expected plugged_in=False, got {state.plugged_in}"
        )

    def test_no_stale_amps_when_chargeamps_field_missing(self):
        """Snapshot lacking a positive ChargeAmps must not yield stale amps."""
        mgr = _make_lm_with_real_ctrl()

        # DetailedChargeState absent and ChargeAmps absent → parses to None.
        telemetry_snapshot = {
            "ChargeState": None,
            "Location": {"latitude": 37.55, "longitude": -122.25},
        }
        with patch("load_manager.has_telemetry", return_value=True), patch(
            "load_manager.get_telemetry_snapshot", return_value=telemetry_snapshot
        ):
            state, _, _ = asyncio.run(mgr._fetch_tesla_state_async())

        assert state is not None
        assert state.is_charging is False
        assert state.current_amps in (0, None)

    def test_preserves_last_known_at_home_when_location_absent(self):
        """When Location is absent but _last_tesla_at_home is seeded, the
        not-charging state must preserve the known at_home value.

        This is the most common real-world path: telemetry charge state ticks
        at 15 s intervals while Location arrives on a slower 120 s interval.
        """
        mgr = _make_lm_with_real_ctrl()
        # Previously seeded (from an earlier Location/REST snapshot).
        mgr._last_tesla_at_home = True  # noqa: SLF001

        telemetry_snapshot = {
            "ChargeAmps": None,
            "DetailedChargeState": None,
        }
        with patch("load_manager.has_telemetry", return_value=True), patch(
            "load_manager.get_telemetry_snapshot", return_value=telemetry_snapshot
        ), patch.object(
            mgr.tesla_ctrl, "init_tesla_state", return_value=None,
        ) as mock_init:
            state, error, url = asyncio.run(mgr._fetch_tesla_state_async())

        assert error is None
        assert url is None
        assert state is not None
        assert state.is_charging is False
        assert state.current_amps in (0, None)
        assert state.plugged_in is False
        # at_home preserved from the seeded value, no REST round-trip needed.
        assert state.at_home is True
        mock_init.assert_not_called()
