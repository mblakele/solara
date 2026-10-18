"""Lumpy-first residual turn_off wiring (Subtask 4).

Drives the production ``GapMinder.decide`` with the logged c658/c817
opening belief states, flag off (must reproduce history exactly) and
flag on (residual prescriptions). The flag defaults off: unwired
behavior is byte-identical by construction, asserted below.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from config import Config
from load_manager import LoadManager, LoadManagerConfig
from load_models import DeviceState, PlugConfig
from load_nbc import DecideContext, GapMinder, StateTracker

NOW = datetime(2026, 10, 14, 23, 16, 5, tzinfo=timezone.utc)
TARGET_WH = -9.0

# bugs/2026-10-14-water-heater-overshoot.log c658.
A_PREDICTED = 1162.1072625
A_BANKED = 74.3072625
A_REMAINING = 835
# bugs/2026-10-14-no-overshoot.log c817.
B_PREDICTED = 1137.23475
B_BANKED = 37.907825
B_REMAINING = 859


def _ctx(
    predicted_wh: float,  # noqa: ARG001 (documents the belief under test)
    banked_wh: float | None,
    seconds_remaining: int,
) -> DecideContext:
    """Decision context with all three plugs wanted on."""
    plugs = {
        "ecoflow": PlugConfig(
            name="ecoflow", accessory_id="e1",
            power_watts=260.0, priority=50,
        ),
        "water heater": PlugConfig(
            name="water heater", accessory_id="w1",
            power_watts=4857.0, priority=60,
        ),
        "jackery": PlugConfig(
            name="jackery", accessory_id="j1",
            power_watts=260.0, priority=100,
        ),
    }
    state = StateTracker()
    for name in plugs:
        state.devices[name] = DeviceState(
            name=name, desired_state=True, actual_state=True, last_toggle=None,
        )
    return DecideContext(
        now=NOW,
        seconds_remaining=seconds_remaining,
        state=state,
        plugs=plugs,
        tesla=None,
        dry_run=False,
        data_point_at=NOW,
        banked_wh=banked_wh,
    )


def _turn_off_names(engine: GapMinder, ctx: DecideContext, predicted_wh: float) -> list[str]:
    """Device names the engine turns off, in decision order."""
    return [
        a.device_name
        for a in engine.decide(
            ctx=ctx, predicted_wh=predicted_wh, target_wh=TARGET_WH
        )
        if a.action == "turn_off"
    ]


def test_default_off_matches_history_c658() -> None:
    """Unwired engine sheds ecoflow + heater, sparing jackery."""
    engine = GapMinder(hysteresis_wh=3)
    assert _turn_off_names(
        engine, _ctx(A_PREDICTED, A_BANKED, A_REMAINING), A_PREDICTED
    ) == ["ecoflow", "water heater"]


def test_default_off_matches_history_c817() -> None:
    """Unwired engine sheds ecoflow + heater (raw batch)."""
    engine = GapMinder(hysteresis_wh=3)
    assert _turn_off_names(
        engine, _ctx(B_PREDICTED, B_BANKED, B_REMAINING), B_PREDICTED
    ) == ["ecoflow", "water heater"]


def test_flag_on_sheds_heater_plus_residual_smalls_c658() -> None:
    """Wired engine sheds heater first, then ecoflow on the residual."""
    engine = GapMinder(hysteresis_wh=3, lumpy_first=True)
    assert _turn_off_names(
        engine, _ctx(A_PREDICTED, A_BANKED, A_REMAINING), A_PREDICTED
    ) == ["water heater", "ecoflow"]


def test_flag_on_holds_smalls_c817() -> None:
    """Wired engine sheds heater only; residual sits hold-side."""
    engine = GapMinder(hysteresis_wh=3, lumpy_first=True)
    assert _turn_off_names(
        engine, _ctx(B_PREDICTED, B_BANKED, B_REMAINING), B_PREDICTED
    ) == ["water heater"]


def test_flag_on_idle_heater_falls_back_to_production_order() -> None:
    """Heater ON but not implicated: plain priority order, ecoflow only."""
    engine = GapMinder(hysteresis_wh=3, lumpy_first=True)
    assert _turn_off_names(
        engine, _ctx(30.0, 20.0, 800), 30.0
    ) == ["ecoflow"]


def test_flag_on_banked_none_falls_back() -> None:
    """No banked energy, no implied rate: status quo even when wired."""
    engine = GapMinder(hysteresis_wh=3, lumpy_first=True)
    assert _turn_off_names(
        engine, _ctx(A_PREDICTED, None, A_REMAINING), A_PREDICTED
    ) == ["ecoflow", "water heater"]


def test_flag_defaults_off() -> None:
    """Fresh engines keep today's behavior unless asked."""
    assert GapMinder(hysteresis_wh=3).lumpy_first is False


def test_flag_plumbs_through_load_manager() -> None:
    """LoadManagerConfig True reaches the engine; default stays off."""
    wired = LoadManager(LoadManagerConfig(dry_run=True, lumpy_first=True))
    assert wired.engine.lumpy_first is True
    plain = LoadManager(LoadManagerConfig(dry_run=True))
    assert plain.engine.lumpy_first is False


def test_flag_reads_env() -> None:
    """LOAD_MANAGE_LUMPY_FIRST=true enables the engine flag."""
    mgr = LoadManager(LoadManagerConfig(
        dry_run=True,
        config=Config(overrides={"LOAD_MANAGE_LUMPY_FIRST": "True"}),
    ))
    assert mgr.engine.lumpy_first is True


# ── 4a: make the reordering observable ──────────────────────────────────
# The reorder emits the same action=turn_off lines either way, so before
# this the flag's effect was invisible in a log. A dedicated INFO event when
# the lumpy lead is actually taken is the census hook.


def _decide(engine: GapMinder, ctx: DecideContext, predicted_wh: float) -> None:
    engine.decide(ctx=ctx, predicted_wh=predicted_wh, target_wh=TARGET_WH)


def test_lumpy_lead_is_logged(caplog) -> None:
    """Wired engine announces the lead it took (names in the record)."""
    engine = GapMinder(hysteresis_wh=3, lumpy_first=True)
    with caplog.at_level(logging.INFO, logger="load_nbc"):
        _decide(engine, _ctx(A_PREDICTED, A_BANKED, A_REMAINING), A_PREDICTED)
    events = [r for r in caplog.records if r.getMessage().startswith("gapminder_lumpy_first")]
    assert len(events) == 1
    assert getattr(events[0], "lead", None) == ["water heater"]


def test_no_lumpy_log_when_flag_off(caplog) -> None:
    """Unwired engine reorders nothing, so nothing is announced."""
    engine = GapMinder(hysteresis_wh=3)
    with caplog.at_level(logging.INFO, logger="load_nbc"):
        _decide(engine, _ctx(A_PREDICTED, A_BANKED, A_REMAINING), A_PREDICTED)
    assert not [r for r in caplog.records if r.getMessage().startswith("gapminder_lumpy_first")]


def test_no_lumpy_log_when_not_implicated(caplog) -> None:
    """Heater idle: the flag is on but takes no lead, so nothing is logged."""
    engine = GapMinder(hysteresis_wh=3, lumpy_first=True)
    with caplog.at_level(logging.INFO, logger="load_nbc"):
        _decide(engine, _ctx(30.0, 20.0, 800), 30.0)
    assert not [r for r in caplog.records if r.getMessage().startswith("gapminder_lumpy_first")]
