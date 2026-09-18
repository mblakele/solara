"""Tesla device time_range gates REST fallback and arbitration.

Outside the configured tesla.time_range window, LoadManager must not make
any Tesla REST calls (vehicle_data quota + car wake). Telemetry (free MQTT)
may still be used; the REST fallback and REST arbitration must be skipped.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from unittest.mock import AsyncMock, patch

from clock import FakeClock
from load_manager import LoadManager, LoadManagerConfig
from load_models import TeslaConfig


def _make_lm_with_real_ctrl_and_range(clock, start, end):
    """LoadManager with RealTeslaController and a tesla time_range."""
    from load_controllers import RealTeslaController

    config = TeslaConfig(
        client_id="test",
        client_secret="test",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
        time_range=(start, end),
    )
    ctrl = RealTeslaController(config)
    mgr = LoadManager(
        LoadManagerConfig(dry_run=True, config_interval_secs=30, clock=clock)
    )
    mgr.tesla_config = config
    mgr.tesla_ctrl = ctrl
    return mgr, ctrl


def test_rest_fallback_skipped_outside_tesla_time_range():
    """No telemetry + outside time_range must not call REST."""
    # 10:00-19:00 UTC window; now is 02:00 UTC (outside).
    clock = FakeClock(datetime(2026, 9, 14, 2, 0, 0, tzinfo=timezone.utc))
    mgr, ctrl = _make_lm_with_real_ctrl_and_range(
        clock, time(10, 0), time(19, 0)
    )
    with (
        patch("load_manager.has_telemetry", return_value=False),
        patch(
            "load_manager.device_config.get_timezone", return_value="UTC"
        ),
        patch.object(
            ctrl, "_fetch_vehicle_data", new=AsyncMock()
        ) as mock_fetch,
        patch.object(
            ctrl, "init_tesla_state", wraps=ctrl.init_tesla_state
        ) as mock_init,
    ):
        state, error, _ = asyncio.run(
            mgr._fetch_tesla_state_async(now=clock.now())
        )
    assert state is None
    assert error is None
    assert mock_init.call_count == 0
    assert mock_fetch.call_count == 0


def test_rest_arbitration_skipped_outside_tesla_time_range():
    """Ambiguous amps + outside time_range must not poll REST."""
    clock = FakeClock(datetime(2026, 9, 14, 2, 0, 0, tzinfo=timezone.utc))
    mgr, ctrl = _make_lm_with_real_ctrl_and_range(
        clock, time(10, 0), time(19, 0)
    )
    mgr._last_tesla_at_home = True  # noqa: SLF001
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 11.0},
        ),
        patch(
            "load_manager.device_config.get_timezone", return_value="UTC"
        ),
        patch.object(
            ctrl, "_fetch_vehicle_data", new=AsyncMock()
        ) as mock_fetch,
    ):
        state, error, _ = asyncio.run(
            mgr._fetch_tesla_state_async(now=clock.now())
        )
    assert error is None
    assert state is not None
    assert state.is_charging is False
    assert mock_fetch.call_count == 0
