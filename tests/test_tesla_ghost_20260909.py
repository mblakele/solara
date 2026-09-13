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
from load_models import TeslaConfig, TeslaState, PendingEffect
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


def test_early_exit_refreshes_stale_tesla_display():
    """Stale REST 5A entry must clear when live telemetry shows ghost amps.

    Reproduces the 22:02 UTC fresh-log session: first cycle with no
    telemetry trusts REST (charging 5A) into devices["tesla"]; later
    cycles early-exit at pending_check while MQTT reports uncorroborated
    ChargeAmps=4. The dashboard ("tesla (5)") stayed stale because only
    the commit stage syncs the device entry.
    """
    from load_nbc import make_plug_effect

    mgr = _make_lm()
    mgr.state.sync_tesla_device_state(
        TeslaState(is_charging=True, current_amps=5, plugged_in=True, at_home=True)
    )
    mgr._last_tesla_at_home = True  # noqa: SLF001
    assert mgr.state.devices["tesla"].current_amps == 5

    ctx = _ctx(mgr)
    assert ctx.now_postfetch is not None
    assert ctx.data_point_at is not None
    mgr.state.add_effect(
        make_plug_effect(
            "ecoflow", "turn_off", 800.0, ctx.now_postfetch, ctx.data_point_at
        )
    )
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 4.000000059604645},
        ),
    ):
        result = mgr._stage_pending_check(ctx)
    assert result is not None
    assert result.status == "waiting_for_fresh_data"
    dev = mgr.state.devices.get("tesla")
    assert dev is not None
    assert dev.actual_state is False
    assert dev.current_amps == 0


def test_init_from_rest_complete_reports_zero_amps():
    """REST charging_state=Complete with stale charge_amps=5 must report 0A.

    Reproduces bugs/2026-09-09-tesla-ghost-b.log: the car is plugged in
    past its charge limit (battery 65% > limit 50%), charging_state
    'Complete', charge_rate 0.0 — but charge_amps still holds the old
    5A pilot/request setting. Reporting it as current_amps=5 renders a
    phantom "tesla (5)" in index html despite is_charging=False.
    """
    import asyncio
    from unittest.mock import patch

    from load_controllers import RealTeslaController

    config = TeslaConfig(
        client_id="t",
        client_secret="t",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
    )
    ctrl = RealTeslaController(config)
    mock_charge = {
        "response": {
            "charge_state": {"charging_state": "Complete", "charge_amps": 5}
        }
    }
    with patch.object(ctrl, "_fetch_vehicle_data") as mock_fetch:
        mock_fetch.return_value = mock_charge
        result = asyncio.run(ctrl._init_from_rest(snapshot=None))
    assert result is not None
    assert result.is_charging is False
    assert result.current_amps == 0
    assert result.plugged_in is True


def test_snapshot_complete_with_stale_amps_reports_zero_amps():
    """DetailedChargeState=Complete with retained ChargeAmps=5 → 0A."""
    ts = tesla_state_from_snapshot(
        {"DetailedChargeState": "DetailedChargeStateComplete", "ChargeAmps": 5.0}
    )
    assert ts is not None
    assert ts.is_charging is False
    assert ts.current_amps == 0
    assert ts.plugged_in is True


def test_commanded_amps_echo_reports_charging():
    """Live amps matching our active command confirm charging (ghost-c log).

    Cycle c4: we commanded 10A, MQTT reports ChargeAmps=10.000000149011612,
    and DetailedChargeState/ChargeState never arrive on this feed. The
    command echo is confirmation — must report charging 10A, not idle 0A
    (which fed a 487Wh phantom into tesla_inflight_correction and shed
    ecoflow+jackery).
    """
    mgr = _make_lm()
    mgr._last_tesla_at_home = True  # noqa: SLF001
    mgr.state.last_commanded_amps = 10
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 10.000000149011612},
        ),
    ):
        state, error, url = asyncio.run(mgr._fetch_tesla_state_async())
    assert error is None
    assert url is None
    assert state is not None
    assert state.is_charging is True
    assert state.current_amps == 10
    assert state.plugged_in is True
    assert state.at_home is True


def test_uncommanded_ghost_still_reports_idle():
    """Amps with no active command and no corroboration still report idle."""
    mgr = _make_lm()
    mgr._last_tesla_at_home = True  # noqa: SLF001
    assert mgr.state.last_commanded_amps is None
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 4.000000059604645},
        ),
    ):
        state, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
    assert state is not None
    assert state.is_charging is False
    assert state.current_amps == 0


def test_display_refresh_shows_commanded_charging():
    """Pending-check display refresh surfaces the command echo."""
    mgr = _make_lm()
    mgr._last_tesla_at_home = True  # noqa: SLF001
    mgr.state.last_commanded_amps = 10
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 10.000000149011612},
        ),
    ):
        mgr._refresh_tesla_display_from_telemetry()  # noqa: SLF001
    dev = mgr.state.devices.get("tesla")
    assert dev is not None
    assert dev.actual_state is True
    assert dev.current_amps == 10


def _make_lm_with_real_ctrl_and_clock(clock):
    """LoadManager wired to RealTeslaController + injectable clock."""
    from load_controllers import RealTeslaController

    config = TeslaConfig(
        client_id="test",
        client_secret="test",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
    )
    ctrl = RealTeslaController(config)
    mgr = LoadManager(
        LoadManagerConfig(dry_run=True, config_interval_secs=30, clock=clock)
    )
    mgr.tesla_config = config
    mgr.tesla_ctrl = ctrl
    return mgr, ctrl


def _charging_rest_response(amps=11):
    return {
        "response": {
            "charge_state": {"charging_state": "Charging", "charge_amps": amps}
        }
    }


def _complete_rest_response():
    return {
        "response": {
            "charge_state": {"charging_state": "Complete", "charge_amps": 5}
        }
    }


def test_arbitration_confirms_external_charging():
    """Ambiguous amps + no command → one REST poll arbitrates (ghost-c).

    The car started charging externally (never commanded by us); the
    ChargeAmps-only feed cannot corroborate. A fresh REST poll saying
    Charging must surface charging 11A instead of idle.
    """
    from clock import FakeClock
    from unittest.mock import AsyncMock

    mgr, ctrl = _make_lm_with_real_ctrl_and_clock(FakeClock())
    mgr._last_tesla_at_home = True  # noqa: SLF001
    assert mgr.state.last_commanded_amps is None
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 11.0},
        ),
        patch.object(
            ctrl, "_fetch_vehicle_data", new=AsyncMock(
                return_value=_charging_rest_response()
            ),
        ) as mock_fetch,
    ):
        state, error, _ = asyncio.run(mgr._fetch_tesla_state_async())
    assert error is None
    assert state is not None
    assert state.is_charging is True
    assert state.current_amps == 11
    assert mock_fetch.call_count == 1


def test_arbitration_cooldown_sustains_without_repolling():
    """A confirmed session is sustained between polls, then re-polled."""
    from clock import FakeClock
    from unittest.mock import AsyncMock

    clock = FakeClock()
    mgr, ctrl = _make_lm_with_real_ctrl_and_clock(clock)
    mgr._last_tesla_at_home = True  # noqa: SLF001
    snapshot = {"ChargeAmps": 11.0}
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch("load_manager.get_telemetry_snapshot", return_value=snapshot),
        patch.object(
            ctrl, "_fetch_vehicle_data", new=AsyncMock(
                return_value=_charging_rest_response()
            ),
        ) as mock_fetch,
    ):
        state1, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
        assert state1 is not None and state1.is_charging is True
        assert mock_fetch.call_count == 1
        # Within cooldown: sustained from latch, no new REST call.
        clock.advance(60)
        state2, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
        assert state2 is not None and state2.is_charging is True
        assert state2.current_amps == 11
        assert mock_fetch.call_count == 1


def test_arbitration_repolls_after_cooldown_and_clears_latch():
    """After cooldown a fresh poll runs; idle answer clears the latch."""
    from clock import FakeClock
    from unittest.mock import AsyncMock

    clock = FakeClock()
    mgr, ctrl = _make_lm_with_real_ctrl_and_clock(clock)
    mgr._last_tesla_at_home = True  # noqa: SLF001
    snapshot = {"ChargeAmps": 11.0}
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch("load_manager.get_telemetry_snapshot", return_value=snapshot),
        patch.object(
            ctrl, "_fetch_vehicle_data", new=AsyncMock(
                side_effect=[
                    _charging_rest_response(),
                    _complete_rest_response(),
                ]
            ),
        ) as mock_fetch,
    ):
        state1, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
        assert state1 is not None and state1.is_charging is True
        clock.advance(301)
        state2, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
        assert state2 is not None and state2.is_charging is False
        assert state2.current_amps == 0
        assert mock_fetch.call_count == 2


def test_arbitration_negative_result_cools_down():
    """REST saying Complete → idle, and no poll storm while ambiguous."""
    from clock import FakeClock
    from unittest.mock import AsyncMock

    clock = FakeClock()
    mgr, ctrl = _make_lm_with_real_ctrl_and_clock(clock)
    mgr._last_tesla_at_home = True  # noqa: SLF001
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 5.0},
        ),
        patch.object(
            ctrl, "_fetch_vehicle_data", new=AsyncMock(
                return_value=_complete_rest_response()
            ),
        ) as mock_fetch,
    ):
        state1, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
        assert state1 is not None and state1.is_charging is False
        assert state1.current_amps == 0
        clock.advance(60)
        state2, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
        assert state2 is not None and state2.is_charging is False
        assert mock_fetch.call_count == 1


def test_arbitration_latch_clears_when_amps_drop():
    """Amps dropping to zero ends the session: latch clears, idle reported."""
    from clock import FakeClock
    from unittest.mock import AsyncMock

    clock = FakeClock()
    mgr, ctrl = _make_lm_with_real_ctrl_and_clock(clock)
    mgr._last_tesla_at_home = True  # noqa: SLF001
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch.object(
            ctrl, "_fetch_vehicle_data", new=AsyncMock(
                return_value=_charging_rest_response()
            ),
        ) as mock_fetch,
    ):
        with patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 11.0},
        ):
            state1, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
            assert state1 is not None and state1.is_charging is True
        # Car stops: amps read 0 → idle, latch cleared, no REST needed.
        with patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 0},
        ):
            state2, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
            assert state2 is not None and state2.is_charging is False
            assert mock_fetch.call_count == 1
        # Amps return after cooldown → latch is gone, so re-poll.
        clock.advance(301)
        with patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 11.0},
        ):
            state3, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
            assert state3 is not None and state3.is_charging is True
            assert mock_fetch.call_count == 2


def test_arbitration_skipped_for_stub_controller():
    """Stub controller (tests/odd configs) never attempts REST arbitration."""
    mgr = _make_lm()
    mgr._last_tesla_at_home = True  # noqa: SLF001
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch(
            "load_manager.get_telemetry_snapshot",
            return_value={"ChargeAmps": 11.0},
        ),
    ):
        state, _, _ = asyncio.run(mgr._fetch_tesla_state_async())
    assert state is not None
    assert state.is_charging is False
    assert state.current_amps == 0


def test_stop_clears_arbitration_latch():
    """Our own stop command clears a latched arbitration (no restart loop)."""
    from load_models import CycleContext

    mgr = _make_lm()
    mgr._last_rest_arbitration_charging = True  # noqa: SLF001
    now = datetime(2026, 9, 11, 21, 2, 53, tzinfo=timezone.utc)
    ctx = CycleContext(now=now, force=False)
    ctx.sentinel_on = False
    ctx.qh_name = "QH1"
    ctx.predicted_wh = -50.0
    ctx.adjusted_wh = -50.0
    ctx.gap_wh = 40.0
    ctx.now_postfetch = now
    ctx.seconds_remaining = 700
    ctx.data_point_at = now
    ctx.actions = []
    ctx.succeeded_effects = [
        PendingEffect(
            device_name="tesla",
            action="turn_off",
            timestamp=now,
            data_point_at=now,
            power_watts=-240.0,
        )
    ]
    mgr._stage_commit(ctx)  # noqa: SLF001
    assert mgr._last_rest_arbitration_charging is False  # noqa: SLF001
