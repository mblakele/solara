"""Regression for bugs/2026-09-09-tesla-ghost.log.

Production showed persistent ChargeAmps 2-4A with no DetailedChargeState
ever arriving. Code treated amps-alone as charging:

* tesla_state_from_snapshot inferred charging from ChargeAmps>0
* _stage_pending_check blocked all decisions via external_tesla_charge
* _init_from_rest skipped charge_state REST on amps-alone

ChargeAmps is the pilot/limit setting, not measured draw. Require
charging-state corroboration (DetailedChargeState or ChargeState).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

from load_manager import LoadManager, LoadManagerConfig
from load_models import TeslaConfig
from mqtt_telemetry import tesla_state_from_snapshot


def test_snapshot_amps_alone_returns_none():
    """Ghost amps (3.0000000447) alone must not infer charging."""
    ts = tesla_state_from_snapshot({"ChargeAmps": 3.0000000447034836})
    assert ts is None


def test_snapshot_amps_with_chargestate_charging_returns_charging():
    """Corroborated amps (ChargeState=Charging) still reports charging."""
    ts = tesla_state_from_snapshot(
        {"ChargeAmps": 3.0000000447034836, "ChargeState": "Charging"}
    )
    assert ts is not None
    assert ts.is_charging is True
    assert ts.current_amps == 3


def test_snapshot_amps_with_disconnected_returns_none():
    """ChargeAmps>0 with ChargeState=Disconnected must not infer charging."""
    ts = tesla_state_from_snapshot(
        {"ChargeAmps": 3.0000000447034836, "ChargeState": "Disconnected"}
    )
    assert ts is None


def _make_lm() -> LoadManager:
    mgr = LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))
    from load_models import TeslaState  # noqa: F401
    from load_controllers import TeslaController

    mgr.tesla_ctrl = TeslaController(None)  # type: ignore[arg-type]
    return mgr


def _ctx(mgr: LoadManager):
    from load_models import CycleContext

    now = datetime(2026, 9, 9, 21, 14, 6, tzinfo=timezone.utc)
    data_point = datetime(2026, 9, 9, 21, 13, 33, tzinfo=timezone.utc)
    ctx = CycleContext(now=now, force=False)
    ctx.data_point_at = data_point
    ctx.now_postfetch = now
    ctx.seconds_remaining = 400
    mgr.state.pending_effects.clear()
    assert mgr.state.last_commanded_amps is None
    return ctx


def test_pending_check_amps_alone_does_not_block():
    """Ghost amps alone must not trigger external_tesla_charge wait."""
    mgr = _make_lm()
    ctx = _ctx(mgr)
    charge_update = datetime(2026, 9, 9, 21, 13, 53, tzinfo=timezone.utc)
    with (
        patch("load_manager.get_field_update_at", return_value=charge_update),
        patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 4.000000059604645},
        ),
    ):
        result = mgr._stage_pending_check(ctx)
    assert result is None


def test_pending_check_corrobated_blocks():
    """Corroborated charging still waits for fresh data."""
    mgr = _make_lm()
    ctx = _ctx(mgr)
    charge_update = datetime(2026, 9, 9, 21, 13, 53, tzinfo=timezone.utc)
    with (
        patch("load_manager.get_field_update_at", return_value=charge_update),
        patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 12, "ChargeState": "Charging"},
        ),
    ):
        result = mgr._stage_pending_check(ctx)
    assert result is not None
    assert result.diagnostics is not None
    assert result.diagnostics.reason == "external_tesla_charge"


def test_init_from_rest_amps_alone_fetches_charge_state():
    """Amps-alone snapshot must not skip the charge_state REST call."""
    from load_controllers import RealTeslaController

    config = TeslaConfig(
        client_id="t",
        client_secret="t",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
    )
    ctrl = RealTeslaController(config)
    snapshot = {"ChargeAmps": 3.0000000447034836}
    mock_charge = {"response": {"charge_state": {"charging_state": "Disconnected"}}}
    with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
        mock_fetch.return_value = mock_charge
        result = asyncio.run(ctrl._init_from_rest(snapshot=snapshot))
    assert result is not None
    assert result.is_charging is False
    endpoints = [
        c.kwargs.get("endpoints") if hasattr(c, "kwargs") else c[1].get("endpoints")
        for c in mock_fetch.call_args_list
    ]
    assert ["charge_state"] in endpoints
