"""Tests for LoadManager cycle helpers (early-exit builder, eligibility)."""

from datetime import datetime, time, timezone

from load_manager import LoadManager, LoadManagerConfig
from load_models import CycleContext, DeviceState, PlugConfig


def _lm() -> LoadManager:
    """Default LoadManager with minimal config, no real controllers."""
    return LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))


def _ctx() -> CycleContext:
    """Default CycleContext with a fixed now timestamp."""
    return CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))


def test_early_exit_builds_result_shape():
    """_early_exit fills status, reason, diagnostics defaults, sleep hints."""
    lm = _lm()
    ctx = _ctx()
    result = lm._early_exit(ctx, "disabled", "disabled", 30)
    assert result.status == "disabled"
    assert result.diagnostics is not None
    assert result.diagnostics.reason == "disabled"
    assert result.diagnostics.hysteresis_wh == lm.engine.HYSTERESIS_WH
    assert result.diagnostics.plugs_configured == list(lm.plugs.keys())
    assert result.diagnostics.tesla_configured == (lm.tesla_ctrl is not None)
    assert result.sleep_hint == 30
    assert result.sleep_hint_at == ctx.now.isoformat()


def test_early_exit_carries_qh_fields_and_candidates():
    """Optional qh/prediction/candidate args land on the result."""
    lm = _lm()
    ctx = _ctx()
    candidates = lm._build_candidate_details(ctx.now, 100, None, None, False)
    result = lm._early_exit(
        ctx, "stale_data", "stale_data", 5,
        qh="QH1", predicted_wh=10.0, adjusted_wh=12.0,
        gap_wh=-2.0, seconds_remaining=100,
        data_point_at=ctx.now, pending_effects_count=3,
        candidates=candidates,
    )
    assert result.qh == "QH1"
    assert result.predicted_wh == 10.0
    assert result.adjusted_wh == 12.0
    assert result.diagnostics is not None
    assert result.diagnostics.gap_wh == -2.0
    assert result.diagnostics.pending_effects_count == 3
    assert result.candidates == candidates
    assert result.diagnostics.candidates == candidates


def test_local_time_converts_to_device_tz():
    """_local_time maps aware UTC to device-local wall time."""
    lm = _lm()
    local = lm._local_time(datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
    assert (local.hour, local.minute) == (5, 0)  # PDT in June


def test_eligible_plugs_filters_sentinel_and_range():
    """_eligible_plugs drops sentinels and out-of-range plugs."""
    lm = _lm()
    lm.plugs = {
        "ok": PlugConfig(name="ok", accessory_id="a", power_watts=100.0),
        "guard": PlugConfig(
            name="guard", accessory_id="b", power_watts=100.0, sentinel=True
        ),
        "night": PlugConfig(
            name="night", accessory_id="c", power_watts=100.0,
            time_range=(time(0, 0), time(1, 0)),
        ),
    }
    lm.sentinel_names = frozenset({"guard"})
    eligible, outside = lm._eligible_plugs(_ctx().now)
    assert set(eligible) == {"ok"}
    assert outside == ["night"]


def test_is_sentinel_on():
    """is_sentinel_on reflects actual_state of sentinel devices."""
    lm = _lm()
    lm.sentinel_names = frozenset({"guard"})
    assert lm.is_sentinel_on() is False
    lm.state.set_device_state(
        "guard", DeviceState(name="guard", actual_state=True)
    )
    assert lm.is_sentinel_on() is True


def test_is_tesla_in_range_without_config():
    """Without Tesla config there is no time restriction."""
    lm = _lm()
    assert lm.tesla_config is None
    assert lm._is_tesla_in_range(_ctx().now) is True
