"""Behaviour parity for the REST arbitration verdict.

These tests run against the pre-refactor ``LoadManager``. They assert
observable behaviour only — returned Tesla state, REST poll counts, and
cooldown timing — so they stay green when the two private fields move into
``RestArbitrationMachine``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from clock import FakeClock
from load_controllers import RealTeslaController, TeslaController
from load_manager import LoadManager, LoadManagerConfig
from load_models import CycleContext, PendingEffect, TeslaConfig


def _make_lm(clock: FakeClock) -> tuple[LoadManager, RealTeslaController]:
    """Build a LoadManager wired to a real controller and a fake clock."""
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
    mgr._last_tesla_at_home = True  # noqa: SLF001
    return mgr, ctrl


def _charging_response(amps: int = 11) -> dict:
    """REST payload for a charging car."""
    return {
        "response": {
            "charge_state": {"charging_state": "Charging", "charge_amps": amps}
        }
    }


def _complete_response() -> dict:
    """REST payload for a car that is plugged in but not charging."""
    return {
        "response": {
            "charge_state": {"charging_state": "Complete", "charge_amps": 5}
        }
    }


def _fetch(
    mgr: LoadManager,
    snapshot: dict,
) -> tuple:
    """Run one async Tesla fetch with telemetry patched."""
    with (
        patch("load_manager.has_telemetry", return_value=True),
        patch("load_manager.get_telemetry_snapshot", return_value=snapshot),
    ):
        return asyncio.run(mgr._fetch_tesla_state_async())  # noqa: SLF001


def test_external_charging_polls_once_then_sustains() -> None:
    """One REST poll confirms an external session; the next cycle reuses it."""
    clock = FakeClock()
    mgr, ctrl = _make_lm(clock)
    with patch.object(
        ctrl,
        "_fetch_vehicle_data",
        new=AsyncMock(return_value=_charging_response()),
    ) as fetch:
        state1, error, _ = _fetch(mgr, {"ChargeAmps": 11.0})
        clock.advance(60)
        state2, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
    assert error is None
    assert state1 is not None and state1.is_charging is True
    assert state1.current_amps == 11
    assert state2 is not None and state2.is_charging is True
    assert state2.current_amps == 11
    assert fetch.call_count == 1


def test_repoll_after_cooldown_reports_idle() -> None:
    """After the cooldown a fresh poll runs; an idle answer is reported."""
    clock = FakeClock()
    mgr, ctrl = _make_lm(clock)
    with patch.object(
        ctrl,
        "_fetch_vehicle_data",
        new=AsyncMock(
            side_effect=[_charging_response(), _complete_response()]
        ),
    ) as fetch:
        state1, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
        assert state1 is not None and state1.is_charging is True
        clock.advance(301)
        state2, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
    assert state2 is not None and state2.is_charging is False
    assert state2.current_amps == 0
    assert fetch.call_count == 2


def test_negative_answer_cools_down() -> None:
    """A definitive idle answer blocks a new poll for the cooldown."""
    clock = FakeClock()
    mgr, ctrl = _make_lm(clock)
    with patch.object(
        ctrl,
        "_fetch_vehicle_data",
        new=AsyncMock(return_value=_complete_response()),
    ) as fetch:
        state1, _, _ = _fetch(mgr, {"ChargeAmps": 5.0})
        assert state1 is not None and state1.is_charging is False
        clock.advance(60)
        state2, _, _ = _fetch(mgr, {"ChargeAmps": 5.0})
    assert state2 is not None and state2.is_charging is False
    assert fetch.call_count == 1


def test_zero_amps_clears_verdict_but_keeps_cooldown() -> None:
    """Zero amps ends the session; the poll clock still blocks a re-poll."""
    clock = FakeClock()
    mgr, ctrl = _make_lm(clock)
    with patch.object(
        ctrl,
        "_fetch_vehicle_data",
        new=AsyncMock(return_value=_charging_response()),
    ) as fetch:
        state1, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
        assert state1 is not None and state1.is_charging is True
        clock.advance(10)
        state2, _, _ = _fetch(mgr, {"ChargeAmps": 0})
        assert state2 is not None and state2.is_charging is False
        # The verdict is gone, so positive amps cannot sustain charging.
        clock.advance(60)
        state3, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
        assert state3 is not None and state3.is_charging is False
        # The cooldown has ended, so a fresh poll is allowed.
        clock.advance(231)
        state4, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
    assert state4 is not None and state4.is_charging is True
    assert fetch.call_count == 2


def test_stub_controller_never_polls() -> None:
    """A stub controller (tests and odd configs) does not arbitrate."""
    clock = FakeClock()
    mgr = LoadManager(
        LoadManagerConfig(dry_run=True, config_interval_secs=30, clock=clock)
    )
    mgr.tesla_ctrl = TeslaController(None)  # type: ignore[arg-type]
    mgr._last_tesla_at_home = True  # noqa: SLF001
    state, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
    assert state is not None and state.is_charging is False


def test_successful_stop_clears_verdict() -> None:
    """Our own stop invalidates the verdict, so a trim cannot restart the car."""
    clock = FakeClock()
    mgr, ctrl = _make_lm(clock)
    with patch.object(
        ctrl,
        "_fetch_vehicle_data",
        new=AsyncMock(return_value=_charging_response()),
    ) as fetch:
        state1, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
        assert state1 is not None and state1.is_charging is True
        now = clock.now()
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
        # Inside the cooldown the cleared verdict must report idle, not
        # sustain the pre-stop charging answer.
        state2, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
    assert state2 is not None and state2.is_charging is False
    assert fetch.call_count == 1


def test_transport_failure_retries_next_cycle() -> None:
    """A failed poll is not an answer: the next cycle polls again."""
    clock = FakeClock()
    mgr, ctrl = _make_lm(clock)
    with patch.object(
        ctrl,
        "_init_from_rest",
        new=AsyncMock(return_value=None),
    ) as rest:
        state1, _, _ = _fetch(mgr, {"ChargeAmps": 8.0})
        assert state1 is not None and state1.is_charging is False
        clock.advance(60)
        state2, _, _ = _fetch(mgr, {"ChargeAmps": 8.0})
    assert state2 is not None and state2.is_charging is False
    assert rest.call_count == 2


def test_outside_time_range_never_polls() -> None:
    """Outside the Tesla window the arbitration poll is skipped."""
    clock = FakeClock()
    mgr, ctrl = _make_lm(clock)
    with (
        patch.object(mgr, "_is_tesla_in_range", return_value=False),  # noqa: SLF001
        patch.object(
            ctrl, "_fetch_vehicle_data", new=AsyncMock()
        ) as fetch,
    ):
        state, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
    assert state is not None and state.is_charging is False
    assert fetch.call_count == 0


def test_corroborated_idle_clears_verdict() -> None:
    """Authoritative idle telemetry invalidates a latched charging answer."""
    clock = FakeClock()
    mgr, ctrl = _make_lm(clock)
    with patch.object(
        ctrl,
        "_fetch_vehicle_data",
        new=AsyncMock(return_value=_charging_response()),
    ) as fetch:
        state1, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
        assert state1 is not None and state1.is_charging is True
        idle_snapshot = {
            "DetailedChargeState": "DetailedChargeStateComplete",
            "ChargeAmps": 5.0,
        }
        state2, _, _ = _fetch(mgr, idle_snapshot)
        assert state2 is not None and state2.is_charging is False
        # Inside the cooldown the cleared verdict cannot sustain charging.
        state3, _, _ = _fetch(mgr, {"ChargeAmps": 11.0})
    assert state3 is not None and state3.is_charging is False
    assert fetch.call_count == 1
