"""Tests for AsyncPhaseResult and the split async-phase helpers."""

from datetime import datetime, timezone
from unittest.mock import patch

from load_manager import LoadManager, LoadManagerConfig
from load_models import AsyncPhaseResult, CycleContext


def _lm() -> LoadManager:
    """Default LoadManager with minimal config, no real controllers."""
    return LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))


def _ctx() -> CycleContext:
    """CycleContext with pipeline fields populated through Stage 4."""
    return CycleContext(
        now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        now_postfetch=datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc),
        gap_wh=500.0,
        adjusted_wh=-500.0,
        seconds_remaining=450,
        qh_name="QH2",
        data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


def test_async_phase_result_defaults():
    """Unset fields default to empty/zero/False."""
    res = AsyncPhaseResult()
    assert res.tesla_state is None
    assert res.tesla_error is None
    assert res.tesla_login_url is None
    assert res.succeeded_effects == []
    assert res.actions == []
    assert res.gap_wh == 0.0
    assert res.adjusted_wh == 0.0
    assert res.sentinel_on is False


def test_stage_async_phase_applies_result_fields():
    """_stage_async_phase copies AsyncPhaseResult fields onto ctx."""
    lm = _lm()
    ctx = _ctx()
    with patch.object(lm, "_cycle_async_phase") as mock_async:
        mock_async.return_value = AsyncPhaseResult(
            tesla_state=None,
            tesla_error="err",
            tesla_login_url="url",
            succeeded_effects=[],
            actions=[],
            gap_wh=300.0,
            adjusted_wh=-700.0,
            sentinel_on=True,
        )
        lm._stage_async_phase(ctx)
    assert ctx.tesla_state is None
    assert ctx.tesla_error == "err"
    assert ctx.tesla_login_url == "url"
    assert ctx.succeeded_effects == []
    assert ctx.actions == []
    assert ctx.gap_wh == 300.0
    assert ctx.adjusted_wh == -700.0
    assert ctx.sentinel_on is True


def test_timed_records_stage_seconds():
    """_timed returns the wrapped call value and records its duration."""
    lm = _lm()
    ctx = _ctx()
    out = lm._timed(ctx, "probe", lambda: 42)
    assert out == 42
    assert "probe" in ctx.timings
    assert ctx.timings["probe"] >= 0.0


def test_suppressed_by_settle_false_without_effects():
    """No settle effects means no suppression."""
    lm = _lm()
    ctx = _ctx()
    assert (
        lm._suppressed_by_settle(-100.0, ctx.now, "QH2", ctx.data_point_at)
        is False
    )
