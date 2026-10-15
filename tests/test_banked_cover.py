"""Banked-cover override: banked export de-risks plug turn-ons under jitter.

When the turn-on jitter guard fires but the already-banked quarter energy
(``qh1.raw_wh``, the webapp Period number) covers a plug's full-quarter
cost even under zero further export — ``banked + capacity <= target +
hysteresis`` — the plug may still turn on. Evaluated against production
triples from ``bugs/2026-10-09-T17-jitter.log``:

* 17:12 (gap 25.1, churn 0.3653, R=179, banked −22.09): 260 W cap 12.9,
  −22.09 + 12.9 = −9.2 ≤ −6 → acts (user manually turned jackery on
  ~2 min later).
* 17:10 (gap 22.9, churn 0.6045, R=299, banked −13.92): cap 21.6,
  +7.7 > −6 → stays blocked.

Plugs only: c579 (gap 7.688, churn 0.7986, R=111, banked −49.4) would
"cover" the 7.4 Wh +1 A bet 6× over, so a Tesla increase must never take
this path — the incident protection depends on it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import patch

from energy_cache import EnergyCache, EnergyCacheData
from load_controllers import PlugController
from load_manager import LoadManager, LoadManagerConfig
from load_models import CycleContext, PlugConfig, TeslaState
from load_nbc import DecideContext, GapMinder, NBCFetchResult, StateTracker

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


def _plug_ctx(
    banked_wh: float | None,
    seconds_remaining: int,
    jitter: float | None,
    tesla: TeslaState | None = None,
) -> DecideContext:
    """Turn-on ctx with one 260 W off plug (jackery-shaped)."""
    state = StateTracker()
    plugs = {
        "jackery": PlugConfig(
            name="jackery", accessory_id="j", power_watts=260.0, priority=100
        ),
    }
    return DecideContext(
        now=fixed_now,
        seconds_remaining=seconds_remaining,
        state=state,
        plugs=plugs,
        tesla=tesla,
        gap_jitter_wh_per_s=jitter,
        banked_wh=banked_wh,
    )


def test_covered_plug_acts_despite_jitter() -> None:
    """17:12 triple: bank covers the bet, so the turn-on proceeds."""
    engine = GapMinder(hysteresis_wh=3)
    assert engine.turn_on_jitter_guard_fires(25.1, 0.3653, 179) is True
    ctx = _plug_ctx(banked_wh=-22.09, seconds_remaining=179, jitter=0.3653)
    actions = engine.decide(ctx=ctx, predicted_wh=-34.1, target_wh=-9.0)
    assert [(a.action, a.device_name) for a in actions] == [
        ("turn_on", "jackery")
    ]


def test_uncovered_plug_stays_blocked() -> None:
    """17:10 triple: bank does not cover a full-R plug run, guard stands."""
    engine = GapMinder(hysteresis_wh=3)
    assert engine.turn_on_jitter_guard_fires(22.9, 0.6045, 299) is True
    ctx = _plug_ctx(banked_wh=-13.92, seconds_remaining=299, jitter=0.6045)
    actions = engine.decide(ctx=ctx, predicted_wh=-31.9, target_wh=-9.0)
    assert actions == []


def test_banked_none_behaves_as_today() -> None:
    """No banked value (old callers): guard fires, no override."""
    engine = GapMinder(hysteresis_wh=3)
    ctx = _plug_ctx(banked_wh=None, seconds_remaining=179, jitter=0.3653)
    actions = engine.decide(ctx=ctx, predicted_wh=-34.1, target_wh=-9.0)
    assert actions == []
    assert (
        engine.turn_on_jitter_guard_fires(25.1, ctx.gap_jitter_wh_per_s, 179)
        is True
    )


def test_tesla_increase_never_takes_cover_path() -> None:
    """c579 shape (deep bank, charging Tesla): no set_amps via override."""
    engine = GapMinder(hysteresis_wh=3)
    tesla = TeslaState(
        is_charging=True, current_amps=12, plugged_in=True, at_home=True
    )
    ctx = _plug_ctx(
        banked_wh=-49.4, seconds_remaining=111, jitter=0.7986, tesla=tesla
    )
    assert engine.turn_on_jitter_guard_fires(7.688, 0.7986, 111) is True
    # Gap widened to 8.5 so the 8.0 Wh plug fits the budget too: a leaky
    # implementation (override bypassing the guard wholesale) would emit
    # both turn_on and set_amps here; the correct one emits turn_on only.
    actions = engine.decide(ctx=ctx, predicted_wh=-17.5, target_wh=-9.0)
    assert [(a.action, a.device_name) for a in actions] == [
        ("turn_on", "jackery")
    ]


def test_cover_boundary_is_inclusive(caplog) -> None:
    """banked + capacity == target + hysteresis still acts (>=, not >)."""
    engine = GapMinder(hysteresis_wh=3)
    # 260 W × 150 s = 10.83 Wh; banked −16.84 → −6.01 ≤ −9 + 3.
    ctx = _plug_ctx(banked_wh=-16.84, seconds_remaining=150, jitter=0.5)
    assert engine.turn_on_jitter_guard_fires(20.0, 0.5, 150) is True
    with caplog.at_level(logging.INFO, logger="load_nbc"):
        actions = engine.decide(ctx=ctx, predicted_wh=-29.0, target_wh=-9.0)
    assert [(a.action, a.device_name) for a in actions] == [
        ("turn_on", "jackery")
    ]
    assert any(
        r.getMessage().startswith("gapminder_banked_override")
        for r in caplog.records
    )


# --- Plumbing: raw_wh from the cache to the decision context -----------


def test_energy_cache_exposes_banked_raw_wh() -> None:
    """The QH1 dict carries raw_wh (70×0.001 + 30×0.003 → 160 Wh)."""
    import pytest

    data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
    cache = EnergyCache()
    cache._data = EnergyCacheData(
        samples=[0.001] * 70 + [0.003] * 30,
        data_start=data_start,
        last_sample_at=data_start,
        last_fetch_at=data_start,
        sample_count=100,
        quantization_seconds=30,
        quantization_offset=0,
        quantization_confidence=1.0,
    )
    result = cache.get_current_qh(
        datetime(2025, 6, 15, 14, 1, 0, tzinfo=timezone.utc)
    )
    assert result is not None
    assert result["raw_wh"] == pytest.approx(160.0, abs=0.01)


def test_fetch_stage_forwards_banked_to_ctx() -> None:
    """_stage_nbc_fetch populates ctx.banked_wh (None when absent)."""
    lm = LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))
    ctx = CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
    data_point = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    qh_result = NBCFetchResult(
        qh_name="QH1", predicted_wh=-34.1, seconds_remaining=179,
        data_point_at=data_point, samples_used=721, raw_wh=-22.09,
    )
    with patch.object(lm.nbc_reader, "get_current_qh", return_value=qh_result):
        assert lm._stage_nbc_fetch(ctx) is None
    assert ctx.banked_wh == -22.09

    bare = NBCFetchResult(
        qh_name="QH1", predicted_wh=-34.1, seconds_remaining=179,
        data_point_at=data_point, samples_used=721,
    )
    ctx2 = CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
    with patch.object(lm.nbc_reader, "get_current_qh", return_value=bare):
        assert lm._stage_nbc_fetch(ctx2) is None
    assert ctx2.banked_wh is None


@patch("config._lookup")
def test_decide_actions_forwards_banked_to_engine(mock_config) -> None:
    """Covered banked through the manager path turns the plug on."""
    mock_config.return_value = "America/Los_Angeles"
    plugs = {
        "heater": PlugConfig(
            name="heater", accessory_id="h1", power_watts=500.0, priority=10
        ),
    }
    mgr = LoadManager(LoadManagerConfig(
        metrics_fetch=lambda: None,
        plug_ctrl=PlugController(plugs),
        tesla_ctrl=None,
        target_wh=-9,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    ))
    # Guard fires (0.3653 × 150 = 54.8 ≥ 2 × 25.1); banked −31 covers the
    # 500 W × 179 s = 24.9 Wh run (−31 + 24.9 = −6.1 ≤ −6).
    mgr._last_gap_jitter_wh_per_s = 0.3653
    fake_now = datetime(2025, 6, 15, 19, 0, 0, tzinfo=timezone.utc)

    actions = mgr._decide_actions(
        mgr.plugs, None, -34.1, fake_now, 179, False, fake_now,
        banked_wh=-31.0,
    )
    assert [(a.action, a.device_name) for a in actions] == [
        ("turn_on", "heater")
    ]

    # Same cycle without the bank: guard stands, nothing acts.
    mgr2 = LoadManager(LoadManagerConfig(
        metrics_fetch=lambda: None,
        plug_ctrl=PlugController(plugs),
        tesla_ctrl=None,
        target_wh=-9,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    ))
    mgr2._last_gap_jitter_wh_per_s = 0.3653
    actions = mgr2._decide_actions(
        mgr2.plugs, None, -34.1, fake_now, 179, False, fake_now,
        banked_wh=None,
    )
    assert actions == []
