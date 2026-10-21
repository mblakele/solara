"""banked_wh plumbing: raw_wh from the cache to the decision context.

The banked-cover override was removed 2026-10-21, but the lumpy-first gate
still reads ``DecideContext.banked_wh`` to derive the quarter's trailing
rate, so the value must keep reaching the engine.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from energy_cache import EnergyCache, EnergyCacheData
from load_controllers import PlugController
from load_manager import LoadManager, LoadManagerConfig
from load_models import CycleContext, PlugConfig
from load_nbc import GapMinder, NBCFetchResult

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


def test_energy_cache_exposes_banked_raw_wh() -> None:
    """The QH1 dict carries raw_wh (70×0.001 + 30×0.003 → 160 Wh)."""
    data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
    cache = EnergyCache()
    cache._data = EnergyCacheData(  # pylint: disable=protected-access
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
        assert lm._stage_nbc_fetch(ctx) is None  # pylint: disable=protected-access
    assert ctx.banked_wh == -22.09

    bare = NBCFetchResult(
        qh_name="QH1", predicted_wh=-34.1, seconds_remaining=179,
        data_point_at=data_point, samples_used=721,
    )
    ctx2 = CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
    with patch.object(lm.nbc_reader, "get_current_qh", return_value=bare):
        assert lm._stage_nbc_fetch(ctx2) is None  # pylint: disable=protected-access
    assert ctx2.banked_wh is None


@patch("config._lookup")
def test_decide_actions_forwards_banked_to_engine(mock_config) -> None:
    """The manager passes banked_wh through to the engine's DecideContext."""
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
    seen: list[float | None] = []
    original = GapMinder.decide

    def spy(self, ctx, predicted_wh, target_wh):  # type: ignore[no-untyped-def]
        seen.append(ctx.banked_wh)
        return original(self, ctx, predicted_wh, target_wh)

    GapMinder.decide = spy  # type: ignore[method-assign]
    try:
        mgr._decide_actions(  # pylint: disable=protected-access
            mgr.plugs, None, -34.1, fixed_now, 179, False, fixed_now,
            banked_wh=-31.0,
        )
        mgr._decide_actions(  # pylint: disable=protected-access
            mgr.plugs, None, -34.1, fixed_now, 179, False, fixed_now,
            banked_wh=None,
        )
    finally:
        GapMinder.decide = original  # type: ignore[method-assign]
    assert seen == [-31.0, None]
