"""Defer-lookahead quanta: the shipped one-window rule (2026-10-19).

A Tesla stop may only be deferred while the next observation can still
arrive before the safe-defer deadline. The static rule promised buffer it
could not observe through (``bugs/2026-10-18-tesla-stop-overshoot.log``:
deferred at R=89 against a 72 s deadline, next cycle at R=60, and the
quarter finished +10.81 Wh over target). The shipped policy is **one**
observation window of lookahead, configured by
``LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA`` (default 1).

The window is counted in quanta, never seconds: the seconds come from
``DecideContext.observability_secs`` (production passes the resolved
prediction/settle window), so a deployment whose meter quantises at 15 s or
60 s sizes the rule correctly without any literal 30.

These tests pin the shipped default, the incident cycle on both settings,
the "unknown window stays inert" rule, and the config/env plumbing that
mirrors ``LOAD_MANAGER_RAMP_AWARE``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config as config_module
from config import Config
from load_manager import LoadManager, LoadManagerConfig
from load_models import TeslaState
from load_nbc import DecideContext, GapMinder, StateTracker, TeslaDecider

NOW = datetime(2026, 10, 18, 22, 43, 33, tzinfo=timezone.utc)
"""The incident's R=89 cycle, where the static rule deferred and lost the stop."""


def _ctx(seconds_remaining: int, window: int | None) -> DecideContext:
    """DecideContext: charging Tesla at minimum amps, gap 24.0 Wh, R=89."""
    return DecideContext(
        now=NOW,
        seconds_remaining=seconds_remaining,
        state=StateTracker(),
        plugs={},
        tesla=TeslaState(
            is_charging=True, current_amps=5, plugged_in=True, at_home=True
        ),
        observability_secs=window,
    )


def test_shipped_default_is_one_window() -> None:
    """The decider and the engine ship with one window of lookahead."""
    assert TeslaDecider().defer_lookahead_quanta == 1
    assert GapMinder(hysteresis_wh=3).defer_lookahead_quanta == 1


def test_incident_cycle_stops_under_the_shipped_rule() -> None:
    """R=89 against a 72 s deadline leaves 17 s — under one 30 s window."""
    action = TeslaDecider().decide_reduce(_ctx(89, 30), 24.0)
    assert action is not None
    assert action.action == "turn_off"


def test_incident_cycle_defers_without_the_lookahead() -> None:
    """``0`` reproduces the pre-2026-10-19 static rule byte-for-byte."""
    assert TeslaDecider(defer_lookahead_quanta=0).decide_reduce(_ctx(89, 30), 24.0) is None


def test_unknown_window_stays_inert() -> None:
    """No resolved quantum means no lookahead — never guess a granularity."""
    assert TeslaDecider().decide_reduce(_ctx(89, None), 24.0) is None


def test_two_windows_withhold_a_defer_a_single_one_allows() -> None:
    """R=110 clears 72 s + 30 s but not 72 s + 60 s."""
    assert TeslaDecider().decide_reduce(_ctx(110, 30), 24.0) is None
    assert TeslaDecider(defer_lookahead_quanta=2).decide_reduce(_ctx(110, 30), 24.0) is not None


def test_config_default_is_one_window(monkeypatch) -> None:
    """The env var is unset in production, so the default is the shipped rule."""
    monkeypatch.delenv("LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA", raising=False)
    monkeypatch.setattr(config_module, "_get_env_data", dict)
    assert Config().defer_lookahead_quanta == 1


def test_flag_reads_env() -> None:
    """LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA=2 reaches the engine; 0 disables."""
    two = LoadManager(
        LoadManagerConfig(
            dry_run=True,
            config=Config(overrides={"LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA": "2"}),
        )
    )
    assert two.engine.defer_lookahead_quanta == 2
    off = LoadManager(
        LoadManagerConfig(
            dry_run=True,
            config=Config(overrides={"LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA": "0"}),
        )
    )
    assert off.engine.defer_lookahead_quanta == 0


def test_flag_plumbs_through_load_manager_config(monkeypatch) -> None:
    """The dataclass field is honoured, and defaults to the shipped rule."""
    monkeypatch.delenv("LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA", raising=False)
    monkeypatch.setattr(config_module, "_get_env_data", dict)
    explicit = LoadManager(LoadManagerConfig(dry_run=True, defer_lookahead_quanta=3))
    assert explicit.engine.defer_lookahead_quanta == 3
    default = LoadManager(LoadManagerConfig(dry_run=True))
    assert default.engine.defer_lookahead_quanta == 1


def test_malformed_flag_falls_back_to_the_shipped_default(caplog) -> None:
    """A typo'd value is an operator slip in an optimization, not a crash."""
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg = Config(overrides={"LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA": "soon"})
        assert cfg.defer_lookahead_quanta == 1
    assert "Invalid LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA" in caplog.text


def test_negative_flag_clamps_to_the_static_rule(caplog) -> None:
    """A negative count would make the defer *more* permissive than static."""
    with caplog.at_level(logging.WARNING, logger="config"):
        cfg = Config(overrides={"LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA": "-3"})
        assert cfg.defer_lookahead_quanta == 0
    assert "Negative LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA" in caplog.text


def test_malformed_flag_does_not_block_startup(caplog) -> None:
    """LoadManager still constructs when the env value is unusable."""
    with caplog.at_level(logging.WARNING, logger="config"):
        manager = LoadManager(
            LoadManagerConfig(
                dry_run=True,
                config=Config(
                    overrides={"LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA": "lots"}
                ),
            )
        )
    assert manager.engine.defer_lookahead_quanta == 1
