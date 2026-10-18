"""Ramp-aware Tesla stop flag: shipped default off (LOAD_MANAGER_RAMP_AWARE).

The ramp-aware hastener stops one cycle early on a rising deficit
(``bugs/2026-09-26-tesla-stop-charging.log``, c54). It is opt-in: the
default is **off**, so the static defer rule alone applies unless
``LOAD_MANAGER_RAMP_AWARE`` enables the trend-based hastener. These tests
pin the default, the incident behaviour on both settings, and the config
plumbing that mirrors ``LOAD_MANAGE_LUMPY_FIRST``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from config import Config
from load_manager import LoadManager, LoadManagerConfig
from load_models import TeslaState
from load_nbc import DecideContext, GapMinder, StateTracker, TeslaDecider

NOW = datetime(2026, 9, 26, 23, 55, 34, tzinfo=timezone.utc)


def _ctx(seconds_remaining: int, trend: float | None) -> DecideContext:
    """DecideContext with a charging Tesla at minimum amps and a gap trend."""
    return DecideContext(
        now=NOW,
        seconds_remaining=seconds_remaining,
        state=StateTracker(),
        plugs={},
        tesla=TeslaState(
            is_charging=True, current_amps=5, plugged_in=True, at_home=True
        ),
        gap_trend_wh_per_s=trend,
        cycle_secs=30,
    )


def test_ramp_aware_defaults_off() -> None:
    """Both the decider and the engine start with the hastener disabled."""
    assert TeslaDecider().ramp_aware is False
    assert GapMinder(hysteresis_wh=3).ramp_aware is False


def test_default_defers_on_the_incident_cycle() -> None:
    """c54 (R=149, gap=45.1, trend=+0.25) defers while the flag is off."""
    assert TeslaDecider().decide_reduce(_ctx(149, 0.25), 45.1) is None


def test_ramp_aware_on_stops_one_cycle_early() -> None:
    """The same c54 cycle stops once the hastener is enabled."""
    action = TeslaDecider(ramp_aware=True).decide_reduce(_ctx(149, 0.25), 45.1)
    assert action is not None
    assert action.action == "turn_off"


def test_flag_plumbs_through_load_manager() -> None:
    """LoadManagerConfig True reaches the engine; the default stays off."""
    wired = LoadManager(LoadManagerConfig(dry_run=True, ramp_aware=True))
    assert wired.engine.ramp_aware is True
    plain = LoadManager(LoadManagerConfig(dry_run=True))
    assert plain.engine.ramp_aware is False


def test_flag_reads_env() -> None:
    """LOAD_MANAGER_RAMP_AWARE=true enables the engine flag."""
    mgr = LoadManager(
        LoadManagerConfig(
            dry_run=True,
            config=Config(overrides={"LOAD_MANAGER_RAMP_AWARE": "True"}),
        )
    )
    assert mgr.engine.ramp_aware is True
