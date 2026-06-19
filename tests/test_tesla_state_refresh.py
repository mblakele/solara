"""Tests for Tesla state refresh from MQTT telemetry.

Verifies that _fetch_tesla_state_async uses live telemetry ChargeAmps
instead of stale cached controller state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

from load_manager import LoadManager, TeslaConfig
from load_models import TeslaState


class TestFetchTeslaStateAsync:
    """_fetch_tesla_state_async must prefer live telemetry over stale cache.

    Regression guard for stale-current_amps bug documented in
    bugs/2026-06-09-tesla-amps.log: the fast path discarded telemetry
    state when "Location" was absent from the MQTT snapshot, falling
    through to init_tesla_state(timeout=0) which returned stale cached
    _init_state from the controller.
    """

    FIXED_NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)

    def _make_lm(self) -> LoadManager:
        """Create a minimal LoadManager with a stub Tesla controller."""
        from load_controllers import TeslaController
        from load_manager import TeslaConfig

        # Minimal TeslaConfig so tesla_ctrl is initialized.
        config = TeslaConfig(
            client_id="test",
            client_secret="test",
            redirect_uri="http://localhost/callback",
            vehicle_id="v1",
        )
        mgr = LoadManager(dry_run=True, config_interval_secs=30)
        mgr.tesla_config = config
        mgr.tesla_ctrl = TeslaController(config)
        return mgr

    def test_uses_telemetry_chargeamps_when_location_missing(self):
        """When telemetry has ChargeAmps but no Location, live amps are used."""
        mgr = self._make_lm()
        # Set a stale last_commanded_amps / cached state baseline.
        mgr.state.last_commanded_amps = 11

        telemetry_snapshot = {"ChargeAmps": 12}
        has_telemetry_patcher = patch(
            "load_manager.has_telemetry", return_value=True
        )
        get_snapshot_patcher = patch(
            "load_manager.get_telemetry_snapshot",
            return_value=telemetry_snapshot,
        )

        with has_telemetry_patcher, get_snapshot_patcher:
            state, error, url = asyncio.run(mgr._fetch_tesla_state_async())

        assert error is None
        assert url is None
        assert state is not None
        # current_amps must be the live telemetry value (12), not stale (11)
        assert state.current_amps == 12, (
            f"Expected telemetry amps (12), got {state.current_amps}"
        )
        assert state.is_charging is True
        assert state.plugged_in is True

    def test_preserves_at_home_when_location_missing(self):
        """When Location telemetry is absent, at_home is preserved from last known."""
        mgr = self._make_lm()
        # Simulate a prior cycle where Location was available and at_home=True.
        mgr._last_tesla_at_home = True  # noqa: SLF001

        telemetry_snapshot = {"ChargeAmps": 12}
        has_telemetry_patcher = patch(
            "load_manager.has_telemetry", return_value=True
        )
        get_snapshot_patcher = patch(
            "load_manager.get_telemetry_snapshot",
            return_value=telemetry_snapshot,
        )

        with has_telemetry_patcher, get_snapshot_patcher:
            state, error, url = asyncio.run(mgr._fetch_tesla_state_async())

        assert state is not None
        # at_home must be True (preserved), not False (from missing Location)
        assert state.at_home is True, (
            f"Expected at_home=True (preserved), got at_home={state.at_home}"
        )

    def test_seeds_last_tesla_at_home_from_init_state(self):
        """When init_tesla_state returns at_home=True, _last_tesla_at_home is seeded."""
        from load_controllers import RealTeslaController

        mgr = self._make_lm()
        config = TeslaConfig(
            client_id="test",
            client_secret="test",
            redirect_uri="http://localhost/callback",
            vehicle_id="v1",
            home_lat=37.0,
            home_lon=-122.0,
        )
        mgr.tesla_ctrl = RealTeslaController(config)

        with patch.object(
            mgr.tesla_ctrl, "init_tesla_state",
            return_value=TeslaState(
                is_charging=True, current_amps=15, plugged_in=True, at_home=True,
            ),
        ), patch("load_manager.has_telemetry", return_value=False):
            state, _, _ = asyncio.run(mgr._fetch_tesla_state_async())

        assert state is not None
        assert state.at_home is True
        assert mgr._last_tesla_at_home is True  # noqa: SLF001

    def test_preserves_at_home_when_seeded_from_init_state(self):
        """Telemetry without Location preserves at_home seeded from REST init."""
        mgr = self._make_lm()
        # Simulate that a prior REST init seeded _last_tesla_at_home=True.
        mgr._last_tesla_at_home = True  # noqa: SLF001

        telemetry_snapshot = {"ChargeAmps": 12}  # no Location
        with patch("load_manager.has_telemetry", return_value=True), patch(
            "load_manager.get_telemetry_snapshot",
            return_value=telemetry_snapshot,
        ):
            state, _, _ = asyncio.run(mgr._fetch_tesla_state_async())

        assert state is not None
        assert state.at_home is True, (
            f"Expected at_home=True (preserved from seed), got at_home={state.at_home}"
        )

    def test_falls_through_to_rest_when_home_unseeded(self):
        """When Location is absent and _last_tesla_at_home is None (never
        seeded), falls through to init_tesla_state REST fallback to obtain
        at_home instead of returning at_home=False from missing Location.

        Regression test for the bootstrapping race documented in
        bugs/2026-06-09-tesla-at-home-2.log: ChargeAmps arrives before
        Location (15s vs 120s publish intervals), the telemetry fast path
        succeeds with at_home=False from missing Location, and the REST
        fallback that would seed _last_tesla_at_home is never reached.
        """
        from load_controllers import RealTeslaController

        mgr = self._make_lm()
        config = TeslaConfig(
            client_id="test",
            client_secret="test",
            redirect_uri="http://localhost/callback",
            vehicle_id="v1",
            home_lat=37.0,
            home_lon=-122.0,
        )
        mgr.tesla_ctrl = RealTeslaController(config)

        telemetry_snapshot = {"ChargeAmps": 12}  # no Location
        rest_state = TeslaState(
            is_charging=True, current_amps=12, plugged_in=True, at_home=True,
        )

        with patch("load_manager.has_telemetry", return_value=True), patch(
            "load_manager.get_telemetry_snapshot",
            return_value=telemetry_snapshot,
        ), patch.object(
            mgr.tesla_ctrl, "init_tesla_state",
            return_value=rest_state,
        ) as mock_init:
            state, _, _ = asyncio.run(mgr._fetch_tesla_state_async())

        assert mock_init.called, (
            "init_tesla_state should be called when Location is missing and "
            "_last_tesla_at_home is None"
        )
        assert state is not None
        # at_home must be True (from REST init), not False (from missing Location)
        assert state.at_home is True, (
            f"Expected at_home=True (from REST fallback), got at_home={state.at_home}"
        )
        assert mgr._last_tesla_at_home is True  # noqa: SLF001

    def test_at_home_updated_from_location_snapshot(self):
        """When Location telemetry IS present, at_home reflects the live location."""
        mgr = self._make_lm()
        # Even if we had a previous at_home=True, Location data overrides.
        mgr._last_tesla_at_home = True  # noqa: SLF001

        telemetry_snapshot = {
            "ChargeAmps": 16,
            "Location": {
                "latitude": 37.5,    # well away from home (37.0, -122.0)
                "longitude": -122.5,  # default radius is 500m
            },
        }
        has_telemetry_patcher = patch(
            "load_manager.has_telemetry", return_value=True
        )
        get_snapshot_patcher = patch(
            "load_manager.get_telemetry_snapshot",
            return_value=telemetry_snapshot,
        )

        with has_telemetry_patcher, get_snapshot_patcher:
            state, error, url = asyncio.run(mgr._fetch_tesla_state_async())

        assert state is not None
        # Location shows vehicle far from home → at_home=False
        assert state.at_home is False, (
            f"Expected at_home=False (from Location), got at_home={state.at_home}"
        )
        # And _last_tesla_at_home must be updated to reflect the new value
        assert mgr._last_tesla_at_home is False  # noqa: SLF001
