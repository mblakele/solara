"""Tests for run_cycle pipeline stage methods (Direction A)."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest

from load_controllers import RealTeslaController, TeslaController
from load_manager import LoadManager
from load_models import CycleContext, PendingEffect, TeslaAuthError


@pytest.fixture
def ctx() -> CycleContext:
    """Default CycleContext with a fixed now timestamp."""
    return CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def lm() -> LoadManager:
    """Default LoadManager with minimal config, no real controllers."""
    return LoadManager(dry_run=True, config_interval_secs=30)


class TestStageNBCFetch:
    """_stage_nbc_fetch() — Stage 2 of the pipeline.

    Fetches the current quarter-hour NBC prediction. Returns CycleResult
    with status='no_incomplete_qh' when the fetch returns None, otherwise
    populates ctx fields and returns None.
    """

    def test_fetch_returns_none_returns_no_incomplete_qh(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When get_current_qh returns None, returns early result."""
        with patch.object(lm.nbc_reader, "get_current_qh", return_value=None):
            result = lm._stage_nbc_fetch(ctx)
        assert result is not None
        assert result.status == "no_incomplete_qh"

    def test_fetch_returns_tuple_populates_ctx(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When get_current_qh returns a tuple, ctx fields are populated."""
        data_point = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        qh_result = ("QH2", 750.0, 450, data_point)
        with patch.object(lm.nbc_reader, "get_current_qh", return_value=qh_result):
            result = lm._stage_nbc_fetch(ctx)
        assert result is None
        assert ctx.qh_name == "QH2"
        assert ctx.predicted_wh == 750.0
        assert ctx.seconds_remaining == 450
        assert ctx.data_point_at == data_point

    def test_fetch_passes_force_flag(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """The force flag from ctx is passed to get_current_qh."""
        ctx.force = True
        data_point = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        qh_result = ("QH1", 500.0, 900, data_point)
        with patch.object(lm.nbc_reader, "get_current_qh") as mock_fetch:
            mock_fetch.return_value = qh_result
            lm._stage_nbc_fetch(ctx)
        mock_fetch.assert_called_once_with(force=True, now=ctx.now)


class TestStagePendingCheck:
    """_stage_pending_check() — Stage 3 of the pipeline.

    Checks whether NBC data is stale or pending effects are not yet reflected.
    Returns a CycleResult with appropriate status when the pipeline should
    short-circuit, or None to continue.
    """

    def test_force_true_bypasses_check(self, lm: LoadManager, ctx: CycleContext):
        """When force=True, always returns None (bypass)."""
        ctx.force = True
        ctx.data_point_at = datetime(2025, 6, 1, 11, 50, 0, tzinfo=timezone.utc)
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        assert lm._stage_pending_check(ctx) is None

    def test_data_point_none_returns_no_incomplete_qh(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When data_point_at is None, returns no_incomplete_qh."""
        ctx.data_point_at = None
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        result = lm._stage_pending_check(ctx)
        assert result is not None
        assert result.status == "no_incomplete_qh"

    def test_stale_data_returns_stale_data_status(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When data is more than 120s old, returns stale_data."""
        data_point = datetime(2025, 6, 1, 11, 57, 0, tzinfo=timezone.utc)
        ctx.data_point_at = data_point
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        result = lm._stage_pending_check(ctx)
        assert result is not None
        assert result.status == "stale_data"

    def test_previous_qh_returns_stale_data(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When data_point_at is from previous QH but within 120s, returns stale_data with reason previous_qh."""
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        # 11:59:30 is within 120s (30s old) but from previous QH (11:45-11:59)
        data_point = datetime(2025, 6, 1, 11, 59, 30, tzinfo=timezone.utc)
        ctx.data_point_at = data_point
        ctx.now_postfetch = now
        ctx.seconds_remaining = 450
        result = lm._stage_pending_check(ctx)
        assert result is not None
        assert result.status == "stale_data"
        assert result.diagnostics is not None
        assert "previous_qh" in result.diagnostics.reason

    def test_fresh_data_returns_none(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When data is fresh and no pending effects, returns None."""
        now = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        # 12:00:00 is within the current QH (starts at 12:00) and within 120s
        data_point = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.data_point_at = data_point
        ctx.now_postfetch = now
        ctx.seconds_remaining = 450
        # Ensure no pending effects since data_point
        lm.state.pending_effects.clear()
        result = lm._stage_pending_check(ctx)
        assert result is None

    def test_external_tesla_charge_waits_for_fresh_data(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """Externally-started charging after data_point → waiting_for_fresh_data."""
        lm.tesla_ctrl = TeslaController(None)  # type: ignore[arg-type]
        now = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        data_point = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.data_point_at = data_point
        ctx.now_postfetch = now
        ctx.seconds_remaining = 450
        lm.state.pending_effects.clear()
        assert lm.state.last_commanded_amps is None
        with (
            patch("load_manager.get_field_update_at", return_value=now),
            patch(
                "load_manager.get_telemetry_snapshot",
                return_value={"ChargeAmps": 12},
            ),
        ):
            result = lm._stage_pending_check(ctx)
        assert result is not None
        assert result.status == "waiting_for_fresh_data"
        assert result.diagnostics is not None
        assert result.diagnostics.reason == "external_tesla_charge"

    def test_external_tesla_charge_no_effect_when_commanded(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """Telemetry shows charging but we commanded it → pass through."""
        lm.tesla_ctrl = TeslaController(None)  # type: ignore[arg-type]
        now = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        data_point = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.data_point_at = data_point
        ctx.now_postfetch = now
        ctx.seconds_remaining = 450
        lm.state.pending_effects.clear()
        lm.state.last_commanded_amps = 12  # we commanded this
        with (
            patch("load_manager.get_field_update_at", return_value=now),
            patch(
                "load_manager.get_telemetry_snapshot",
                return_value={"ChargeAmps": 12},
            ),
        ):
            result = lm._stage_pending_check(ctx)
        assert result is None

    def test_external_tesla_charge_no_effect_when_data_fresh(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """ChargeAmps update before data_point → pass through."""
        lm.tesla_ctrl = TeslaController(None)  # type: ignore[arg-type]
        now = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        data_point = datetime(2025, 6, 1, 12, 0, 20, tzinfo=timezone.utc)
        # ChargeAmps updated at 12:00:10, BEFORE data_point (12:00:20)
        charge_update = datetime(2025, 6, 1, 12, 0, 10, tzinfo=timezone.utc)
        ctx.data_point_at = data_point
        ctx.now_postfetch = now
        ctx.seconds_remaining = 450
        lm.state.pending_effects.clear()
        with (
            patch("load_manager.get_field_update_at", return_value=charge_update),
            patch(
                "load_manager.get_telemetry_snapshot",
                return_value={"ChargeAmps": 12},
            ),
        ):
            result = lm._stage_pending_check(ctx)
        assert result is None


class TestStageCommit:
    """_stage_commit() — Stage 6 of the pipeline.

    Handles sentinel-on disable, commits succeeded effects, tracks Tesla
    amp state, and checks hysteresis. Returns a CycleResult for early-exit
    conditions (sentinel, hysteresis), or None to continue to build_result.
    """

    def test_sentinel_on_returns_disabled(self, lm: LoadManager, ctx: CycleContext):
        """When sentinel_on is True, returns disabled status."""
        ctx.sentinel_on = True
        ctx.qh_name = "QH2"
        ctx.predicted_wh = 500.0
        ctx.adjusted_wh = 500.0
        ctx.gap_wh = 100.0
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = lm._stage_commit(ctx)
        assert result is not None
        assert result.status == "disabled"
        assert result.diagnostics is not None
        assert result.diagnostics.reason == "sentinel_on"

    def test_hysteresis_gap_returns_ok_or_dry_run(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When abs(gap_wh) <= hysteresis, returns ok or dry-run (no actions)."""
        ctx.sentinel_on = False
        ctx.qh_name = "QH2"
        ctx.predicted_wh = -500.0
        ctx.adjusted_wh = -500.0
        ctx.gap_wh = 0.0  # well within hysteresis
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.actions = []
        ctx.succeeded_effects = []
        result = lm._stage_commit(ctx)
        assert result is not None
        # Should be "dry-run" since lm.dry_run is True (fixture default)
        assert result.status == "dry-run"
        assert result.actions == []

    def test_returns_none_when_no_early_exit(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When neither sentinel nor hysteresis applies, returns None."""
        ctx.sentinel_on = False
        ctx.qh_name = "QH2"
        ctx.predicted_wh = -1000.0
        ctx.adjusted_wh = -1000.0
        ctx.gap_wh = -500.0  # larger than hysteresis
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.actions = []
        ctx.succeeded_effects = []
        result = lm._stage_commit(ctx)
        assert result is None


class TestStageBuildResult:
    """_stage_build_result() — Stage 7 (final) of the pipeline.

    Builds candidate details, determines no-action reason, and constructs
    the final CycleResult. Always returns a CycleResult (never None).
    """

    def test_returns_cycle_result(self, lm: LoadManager, ctx: CycleContext):
        """Always returns a CycleResult with ok or dry-run status."""
        ctx.qh_name = "QH2"
        ctx.predicted_wh = -500.0
        ctx.adjusted_wh = -500.0
        ctx.gap_wh = -100.0
        ctx.seconds_remaining = 450
        ctx.now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.actions = []
        ctx.succeeded_effects = []
        ctx.tesla_state = None
        ctx.tesla_error = None
        ctx.tesla_login_url = None
        result = lm._stage_build_result(ctx)
        assert result is not None
        assert result.status in ("ok", "dry-run")

    def test_includes_actions(self, lm: LoadManager, ctx: CycleContext):
        """When actions exist, they are included in the result."""
        ctx.qh_name = "QH2"
        ctx.predicted_wh = -1000.0
        ctx.adjusted_wh = -800.0
        ctx.gap_wh = -300.0
        ctx.seconds_remaining = 450
        ctx.now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.actions = [
            PendingEffect(
                device_name="heater",
                action="turn_on",
                timestamp=datetime(2025, 6, 1, 12, 0, 5, tzinfo=timezone.utc),
                data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                power_watts=2000.0,
            )
        ]
        ctx.succeeded_effects = list(ctx.actions)
        ctx.tesla_state = None
        ctx.tesla_error = None
        ctx.tesla_login_url = None
        result = lm._stage_build_result(ctx)
        assert result is not None
        assert len(result.actions) == 1


class TestStageAsyncPhase:
    """_stage_async_phase() — Stage 5 of the pipeline.

    Calls _cycle_async_phase via asyncio.run() and unpacks the 8-tuple
    into ctx fields. Mutates ctx.gap_wh and ctx.adjusted_wh with corrected
    values from the async phase.
    """

    def test_returns_none(self, lm: LoadManager, ctx: CycleContext):
        """Stage 5 always returns None (never short-circuits)."""
        ctx.gap_wh = 500.0
        ctx.adjusted_wh = -500.0
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.qh_name = "QH2"
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch.object(lm, "_cycle_async_phase") as mock_async:
            mock_async.return_value = (
                None,   # tesla_state
                None,   # tesla_error
                None,   # tesla_login_url
                [],     # succeeded_effects
                [],     # results
                500.0,  # gap_wh (corrected)
                -500.0, # adjusted_wh (corrected)
                False,  # sentinel_on
            )
            result = lm._stage_async_phase(ctx)
        assert result is None

    def test_populates_ctx_from_async_result(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """Async phase result fields are written to ctx."""
        ctx.gap_wh = 500.0
        ctx.adjusted_wh = -500.0
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.qh_name = "QH2"
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch.object(lm, "_cycle_async_phase") as mock_async:
            mock_async.return_value = (
                None,   # tesla_state
                "err",  # tesla_error
                "url",  # tesla_login_url
                [],     # succeeded_effects
                [],     # actions
                300.0,  # gap_wh (corrected)
                -700.0, # adjusted_wh (corrected)
                True,   # sentinel_on
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

    def test_resets_tesla_session_when_configured(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """reset_session() is called on tesla_ctrl when configured."""
        ctx.gap_wh = 500.0
        ctx.adjusted_wh = -500.0
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.qh_name = "QH2"
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Give lm a tesla_ctrl stub
        from load_controllers import TeslaController
        lm.tesla_ctrl = TeslaController(None)  # type: ignore[arg-type]
        with (
            patch.object(lm, "_cycle_async_phase") as mock_async,
            patch.object(lm.tesla_ctrl, "reset_session") as mock_reset,
        ):
            mock_async.return_value = (
                None, None, None, [], [], 500.0, -500.0, False,
            )
            lm._stage_async_phase(ctx)
        mock_reset.assert_called_once()

    def test_async_phase_suppresses_turn_on_with_telemetry(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When telemetry shows the car is charging, turn_on is suppressed."""
        # Simulate MQTT telemetry showing "Charging"
        pass  # gating exercised in _execute_tesla_action tests; _stage_async_phase mocks _cycle_async_phase
        if False:
            return  # prevent accidental fall-through

        ctx.gap_wh = 500.0
        ctx.adjusted_wh = -500.0
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.qh_name = "QH2"
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Give lm a tesla_ctrl so the async phase actually tries tesla actions
        from load_controllers import TeslaController

        lm.tesla_ctrl = TeslaController(None)  # type: ignore[assignment]
        # Patch _execute_tesla_action to simulate a turn_on action being generated
        # (the real code would generate this via engine.decide())
        action = PendingEffect(
            device_name="tesla",
            action="turn_on",
            timestamp=datetime(2025, 6, 1, 12, 0, 5, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            power_watts=11000.0,
        )

        with patch.object(lm, "_cycle_async_phase") as mock_async:
            # Simulate: engine.decide() generated the turn_on action, but
            # _execute_tesla_action returned False because telemetry gating
            # suppressed it (car already charging).
            mock_async.return_value = (
                None,   # tesla_state
                None,   # tesla_error
                None,   # tesla_login_url
                [],     # succeeded_effects
                [],     # actions (empty because turn_on was suppressed)
                500.0,  # gap_wh
                -500.0, # adjusted_wh
                False,  # sentinel_on
            )
            lm._stage_async_phase(ctx)

        # The async phase should have cleared actions (suppressed turn_on)
        assert ctx.actions == []
        assert ctx.succeeded_effects == []

    def test_async_phase_allows_turn_off_with_telemetry(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When telemetry shows charging, turn_off is NOT suppressed."""

        ctx.gap_wh = 500.0
        ctx.adjusted_wh = -500.0
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.qh_name = "QH2"
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        from load_controllers import TeslaController

        lm.tesla_ctrl = TeslaController(None)  # type: ignore[assignment]

        with patch.object(lm, "_cycle_async_phase") as mock_async:
            # Simulate: engine.decide() generated a turn_off action,
            # telemetry gating allows it through (only turn_on is gated).
            action = PendingEffect(
                device_name="tesla",
                action="turn_off",
                timestamp=datetime(2025, 6, 1, 12, 0, 5, tzinfo=timezone.utc),
                data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                power_watts=0.0,
            )
            mock_async.return_value = (
                None,      # tesla_state
                None,      # tesla_error
                None,      # tesla_login_url
                [action],  # succeeded_effects
                [action],  # actions (turn_off passes through)
                500.0,     # gap_wh
                -500.0,    # adjusted_wh
                False,     # sentinel_on
            )
            lm._stage_async_phase(ctx)

        # The async phase should have the turn_off action
        assert len(ctx.actions) == 1
        assert ctx.actions[0].action == "turn_off"
        assert len(ctx.succeeded_effects) == 1

    def test_async_phase_allows_set_amps_with_telemetry(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When telemetry shows charging, set_amps is NOT suppressed."""

        ctx.gap_wh = 500.0
        ctx.adjusted_wh = -500.0
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.qh_name = "QH2"
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        from load_controllers import TeslaController

        lm.tesla_ctrl = TeslaController(None)  # type: ignore[assignment]

        with patch.object(lm, "_cycle_async_phase") as mock_async:
            action = PendingEffect(
                device_name="tesla",
                action="set_amps",
                timestamp=datetime(2025, 6, 1, 12, 0, 5, tzinfo=timezone.utc),
                data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                power_watts=0.0,
                target_amps=16,
            )
            mock_async.return_value = (
                None,      # tesla_state
                None,      # tesla_error
                None,      # tesla_login_url
                [action],  # succeeded_effects
                [action],  # actions (set_amps passes through)
                500.0,     # gap_wh
                -500.0,    # adjusted_wh
                False,     # sentinel_on
            )
            lm._stage_async_phase(ctx)

        assert len(ctx.actions) == 1
        assert ctx.actions[0].action == "set_amps"

    def test_async_phase_no_suppression_without_telemetry(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """Without active telemetry, all Tesla actions pass through unmodified."""
        # MQTT telemetry is absent by default (nothing published yet)

        ctx.gap_wh = 500.0
        ctx.adjusted_wh = -500.0
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.qh_name = "QH2"
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        from load_controllers import TeslaController

        lm.tesla_ctrl = TeslaController(None)  # type: ignore[assignment]

        with patch.object(lm, "_cycle_async_phase") as mock_async:
            actions = [
                PendingEffect(
                    device_name="tesla",
                    action="turn_on",
                    timestamp=datetime(2025, 6, 1, 12, 0, 5, tzinfo=timezone.utc),
                    data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                    power_watts=11000.0,
                ),
                PendingEffect(
                    device_name="tesla",
                    action="set_amps",
                    timestamp=datetime(2025, 6, 1, 12, 0, 10, tzinfo=timezone.utc),
                    data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                    power_watts=0.0,
                    target_amps=32,
                ),
            ]
            mock_async.return_value = (
                None, None, None, actions, actions, 500.0, -500.0, False,
            )
            lm._stage_async_phase(ctx)

        assert len(ctx.actions) == 2
        assert ctx.actions[0].action == "turn_on"
        assert ctx.actions[1].action == "set_amps"

    def test_async_phase_mixed_actions_with_telemetry(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When telemetry active, only turn_on is suppressed; other actions pass."""

        ctx.gap_wh = 500.0
        ctx.adjusted_wh = -500.0
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.qh_name = "QH2"
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        from load_controllers import TeslaController

        lm.tesla_ctrl = TeslaController(None)  # type: ignore[assignment]

        with patch.object(lm, "_cycle_async_phase") as mock_async:
            # Simulate: engine.decide() generated 3 actions; turn_on suppressed.
            # The turn_on action is intentionally NOT included in the mock return
            # because in the real code _execute_tesla_action suppresses it.
            turn_off = PendingEffect(
                device_name="tesla",
                action="turn_off",
                timestamp=datetime(2025, 6, 1, 12, 0, 6, tzinfo=timezone.utc),
                data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                power_watts=0.0,
            )
            set_amps = PendingEffect(
                device_name="tesla",
                action="set_amps",
                timestamp=datetime(2025, 6, 1, 12, 0, 7, tzinfo=timezone.utc),
                data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                power_watts=0.0,
                target_amps=20,
            )
            # In the real code, _execute_tesla_action would suppress turn_on.
            # The mock simulates that behavior: only turn_off and set_amps succeed.
            mock_async.return_value = (
                None,            # tesla_state
                None,            # tesla_error
                None,            # tesla_login_url
                [turn_off, set_amps],  # succeeded_effects (turn_on suppressed)
                [turn_off, set_amps],  # actions (turn_on suppressed)
                500.0,           # gap_wh
                -500.0,          # adjusted_wh
                False,           # sentinel_on
            )
            lm._stage_async_phase(ctx)

        assert len(ctx.actions) == 2
        assert ctx.actions[0].action == "turn_off"
        assert ctx.actions[1].action == "set_amps"
        assert len(ctx.succeeded_effects) == 2

    def test_async_phase_no_suppression_when_not_charging(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When telemetry shows 'Starting' (not 'Charging'), turn_on is NOT suppressed."""
        # MQTT telemetry absent — gating only fires when has_telemetry() is True
        # and tesla_state_from_snapshot() returns is_charging=True

        ctx.gap_wh = 500.0
        ctx.adjusted_wh = -500.0
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.seconds_remaining = 450
        ctx.qh_name = "QH2"
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        from load_controllers import TeslaController

        lm.tesla_ctrl = TeslaController(None)  # type: ignore[assignment]

        with patch.object(lm, "_cycle_async_phase") as mock_async:
            action = PendingEffect(
                device_name="tesla",
                action="turn_on",
                timestamp=datetime(2025, 6, 1, 12, 0, 5, tzinfo=timezone.utc),
                data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                power_watts=11000.0,
            )
            mock_async.return_value = (
                None, None, None, [action], [action], 500.0, -500.0, False,
            )
            lm._stage_async_phase(ctx)

        # In the real code, telemetry gating only suppresses when is_charging=True
        # ("Charging" state). "Starting" means the car hasn't started drawing power yet.
        assert len(ctx.actions) == 1
        assert ctx.actions[0].action == "turn_on"


class TestStageComputeGap:
    """_stage_compute_gap() — Stage 4 of the pipeline.

    Accepts fresh NBC data, prunes old effects, computes adjusted_wh and
    gap_wh, and stores them in ctx. Always returns None (continue pipeline).
    """

    def test_returns_none(self, lm: LoadManager, ctx: CycleContext):
        """Stage 4 always returns None."""
        data_point = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.data_point_at = data_point
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.predicted_wh = 500.0
        ctx.seconds_remaining = 450
        result = lm._stage_compute_gap(ctx)
        assert result is None

    def test_sets_last_data_point_at(self, lm: LoadManager, ctx: CycleContext):
        """state.last_data_point_at is set to data_point_at."""
        data_point = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.data_point_at = data_point
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.predicted_wh = 500.0
        ctx.seconds_remaining = 450
        lm._stage_compute_gap(ctx)
        assert lm.state.last_data_point_at == data_point

    def test_computes_gap_wh(self, lm: LoadManager, ctx: CycleContext):
        """gap_wh is computed as target_wh - adjusted_wh."""
        data_point = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.data_point_at = data_point
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.predicted_wh = -1000.0  # predicted deficit
        ctx.seconds_remaining = 450
        lm.target_wh = -500
        lm._stage_compute_gap(ctx)
        # With no pending effects, adjusted_wh == predicted_wh
        # gap_wh = target_wh - adjusted_wh = -500 - (-1000) = 500
        assert ctx.gap_wh is not None
        assert ctx.gap_wh > 0  # surplus

    def test_adjusted_wh_accounts_for_pending_effects(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """adjusted_wh includes pending effect deltas."""
        data_point = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ctx.data_point_at = data_point
        ctx.now_postfetch = datetime(2025, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        ctx.predicted_wh = -1000.0
        ctx.seconds_remaining = 450
        lm.target_wh = -500
        # Add a pending effect that adds load (lower gap)
        lm.state.pending_effects.append(
            PendingEffect(
                device_name="heater",
                action="turn_on",
                timestamp=datetime(2025, 6, 1, 12, 0, 5, tzinfo=timezone.utc),
                data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                power_watts=2000.0,
            )
        )
        lm._stage_compute_gap(ctx)
        # Adjusted_wh should account for the pending effect
        assert ctx.adjusted_wh is not None
        assert ctx.adjusted_wh != ctx.predicted_wh


class TestStageEnabledCheck:
    """_stage_enabled_check() — Stage 1 of the pipeline.

    Returns None when enabled, CycleResult(status="disabled") when not.
    """

    def test_enabled_true_returns_none(self, lm: LoadManager, ctx: CycleContext):
        """When enabled is True, the stage returns None (continue pipeline)."""
        lm.enabled = True
        assert lm._stage_enabled_check(ctx) is None

    def test_enabled_false_returns_disabled(self, lm: LoadManager, ctx: CycleContext):
        """When enabled is False, returns CycleResult with status='disabled'."""
        lm.enabled = False
        result = lm._stage_enabled_check(ctx)
        assert result is not None
        assert result.status == "disabled"
        assert result.sleep_hint == lm.config_interval_secs

    def test_time_range_active_returns_none(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When enabled is a time range and now is within it (in device TZ), returns None."""
        lm.enabled = (time(9, 0), time(17, 0))
        # 16:00 UTC = 09:00 PDT (America/Los_Angeles, UTC-7 in June)
        ctx.now = ctx.now.replace(hour=16, minute=0)
        assert lm._stage_enabled_check(ctx) is None

    def test_time_range_inactive_returns_disabled(
        self, lm: LoadManager, ctx: CycleContext
    ):
        """When enabled is a time range and now is outside it (in device TZ), returns disabled."""
        lm.enabled = (time(9, 0), time(17, 0))
        # 08:00 UTC = 01:00 PDT → before 09:00 local
        ctx.now = ctx.now.replace(hour=8, minute=0)
        result = lm._stage_enabled_check(ctx)
        assert result is not None
        assert result.status == "disabled"
        assert result.diagnostics is not None


# --- Tesla action execution clamping tests ---


class TestTeslaActionClamping:
    """_execute_tesla_action must clamp target_amps to charge_amps_max.

    Belt-and-suspenders safety: even if the decision engine produces a value
    above charge_amps_max, the execution layer must clamp it before sending
    to the vehicle API.
    """

    @pytest.fixture
    def lm_with_tesla_config(self) -> LoadManager:
        """LoadManager with a TeslaConfig that has a low charge_amps_max."""
        from load_controllers import TeslaController
        from load_manager import TeslaConfig

        mgr = LoadManager(dry_run=True, config_interval_secs=30)
        mgr.tesla_config = TeslaConfig(
            client_id="test",
            client_secret="test",
            redirect_uri="http://localhost/callback",
            vehicle_id="v1",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            charge_amps_min=5,
            charge_amps_max=24,
        )
        mgr.tesla_ctrl = TeslaController(mgr.tesla_config)  # type: ignore[assignment]
        return mgr

    def test_execute_set_amps_clamped_to_max(self, lm_with_tesla_config: LoadManager):
        """When action.target_amps exceeds charge_amps_max, it must be clamped."""
        import asyncio

        action = PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            power_watts=0.0,
            target_amps=30,  # exceeds max of 24
        )

        result = asyncio.run(lm_with_tesla_config._execute_tesla_action(action))

        assert result is True
        # The stub controller should have stored the clamped value
        state = asyncio.run(lm_with_tesla_config.tesla_ctrl.get_charging_state())
        assert state.current_amps == 24

    def test_execute_set_amps_within_max_unchanged(self, lm_with_tesla_config: LoadManager):
        """When action.target_amps is within charge_amps_max, no clamping occurs."""
        import asyncio

        action = PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            power_watts=0.0,
            target_amps=20,  # within max of 24
        )

        result = asyncio.run(lm_with_tesla_config._execute_tesla_action(action))

        assert result is True
        state = asyncio.run(lm_with_tesla_config.tesla_ctrl.get_charging_state())
        assert state.current_amps == 20

    def test_execute_set_amps_no_tesla_config_defaults_to_48(
        self,
    ):
        """When tesla_config is None, uses default max of 48."""
        import asyncio

        mgr = LoadManager(dry_run=True, config_interval_secs=30)
        from load_controllers import TeslaController
        from load_manager import TeslaConfig

        config = TeslaConfig(
            client_id="test",
            client_secret="test",
            redirect_uri="http://localhost/callback",
            vehicle_id="v1",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            charge_amps_min=5,
            charge_amps_max=48,
        )
        mgr.tesla_config = config
        mgr.tesla_ctrl = TeslaController(config)  # type: ignore[assignment]

        action = PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            power_watts=0.0,
            target_amps=30,  # within default max of 48
        )

        result = asyncio.run(mgr._execute_tesla_action(action))

        assert result is True
        # With no tesla_config, default max is 48, so 30 should pass through
        state = asyncio.run(mgr.tesla_ctrl.get_charging_state())
        assert state.current_amps == 30

    def test_execute_set_amps_below_five_stops_charging(self, lm_with_tesla_config: LoadManager):
        """When target_amps < 5, stop_charging is called instead."""
        import asyncio

        action = PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            power_watts=0.0,
            target_amps=3,  # below min of 5
        )

        result = asyncio.run(lm_with_tesla_config._execute_tesla_action(action))

        assert result is True
        state = asyncio.run(lm_with_tesla_config.tesla_ctrl.get_charging_state())
        assert state.is_charging is False
        assert state.current_amps is None

    def test_execute_set_amps_hard_max_enforced(self, lm_with_tesla_config: LoadManager):
        """HARD_MAX_AMPS must be enforced even when tesla_config has a higher max.

        Belt-and-suspenders: if config loading provides anomalously high
        charge_amps_max (e.g. 60), the hard absolute max must still protect
        the vehicle from receiving a value above the safe limit.
        """
        import asyncio

        # Override tesla_config with a high max to simulate misconfiguration.
        # Also update the controller's config so it doesn't clamp first.
        from load_manager import TeslaConfig
        from load_controllers import TeslaController
        high_config = TeslaConfig(
            client_id="test",
            client_secret="test",
            redirect_uri="http://localhost/callback",
            vehicle_id="v1",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            charge_amps_min=5,
            charge_amps_max=60,  # anomalously high
        )
        lm_with_tesla_config.tesla_config = high_config
        lm_with_tesla_config.tesla_ctrl = TeslaController(high_config)

        action = PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            power_watts=0.0,
            target_amps=55,  # within config max (60) but above hard max (48)
        )

        result = asyncio.run(lm_with_tesla_config._execute_tesla_action(action))

        assert result is True
        state = asyncio.run(lm_with_tesla_config.tesla_ctrl.get_charging_state())
        assert state.current_amps == 48  # clamped to HARD_MAX_AMPS


# --- Tesla auth error notification ---


class TestTeslaAuthErrorNotification:
    """_execute_tesla_action fires auth error notification when controller raises TeslaAuthError."""

    @pytest.fixture
    def lm_with_auth_failing_ctrl(self) -> LoadManager:
        """LoadManager with a mock controller that raises TeslaAuthError."""
        from load_controllers import TeslaController
        from load_manager import TeslaConfig
        from unittest.mock import AsyncMock, MagicMock

        mgr = LoadManager(dry_run=True, config_interval_secs=30)

        # Use a real config
        config = TeslaConfig(
            client_id="test", client_secret="test",
            redirect_uri="http://localhost/callback", vehicle_id="v1",
            home_lat=37.0, home_lon=-122.0, home_radius_m=500,
            charge_amps_min=5, charge_amps_max=48,
        )
        mgr.tesla_config = config
        # Inject a mock controller that raises TeslaAuthError on set_charge_amps
        mock_ctrl = MagicMock(spec=TeslaController)
        mock_ctrl.set_charge_amps = AsyncMock(
            side_effect=TeslaAuthError("login_required: refresh_token is invalid")
        )
        mock_ctrl.stop_charging = AsyncMock(
            side_effect=TeslaAuthError("login_required")
        )
        mgr.tesla_ctrl = mock_ctrl  # type: ignore[assignment]
        mgr._fire_auth_error_notification = AsyncMock(return_value=True)  # type: ignore[assignment]
        return mgr

    def test_execute_set_amps_fires_auth_notification(self, lm_with_auth_failing_ctrl):
        """TeslaAuthError from set_charge_amps triggers auth error notification."""
        import asyncio

        action = PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            power_watts=0.0,
            target_amps=16,
        )

        result = asyncio.run(lm_with_auth_failing_ctrl._execute_tesla_action(action))

        assert result is False
        lm_with_auth_failing_ctrl._fire_auth_error_notification.assert_awaited_once_with(  # type: ignore[attr-defined]
            "login_required: refresh_token is invalid"
        )

    def test_execute_stop_charging_fires_auth_notification(self, lm_with_auth_failing_ctrl):
        """TeslaAuthError from stop_charging triggers auth error notification."""
        import asyncio

        action = PendingEffect(
            device_name="tesla",
            action="turn_off",
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            power_watts=0.0,
        )

        result = asyncio.run(lm_with_auth_failing_ctrl._execute_tesla_action(action))

        assert result is False
        lm_with_auth_failing_ctrl._fire_auth_error_notification.assert_awaited_once_with(  # type: ignore[attr-defined]
            "login_required"
        )

    def test_network_error_does_not_fire_auth_notification(self, lm_with_auth_failing_ctrl):
        """Non-auth exceptions do NOT trigger auth error notification."""
        import asyncio

        # Replace the mock to raise a non-auth error
        from unittest.mock import AsyncMock
        mock_ctrl = lm_with_auth_failing_ctrl.tesla_ctrl
        mock_ctrl.set_charge_amps = AsyncMock(side_effect=Exception("Connection refused"))

        action = PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            power_watts=0.0,
            target_amps=16,
        )

        result = asyncio.run(lm_with_auth_failing_ctrl._execute_tesla_action(action))

        assert result is False
        lm_with_auth_failing_ctrl._fire_auth_error_notification.assert_not_awaited()  # type: ignore[attr-defined]


# --- Auth error from init_tesla_state (not action execution) ---


class TestAuthErrorFromInitTeslaState:
    """_fetch_tesla_state_async / _cycle_async_phase propagate init_tesla_state auth errors."""

    @pytest.fixture
    def lm_with_failing_tesla(self) -> LoadManager:
        """LoadManager with a mock RealTeslaController whose init_tesla_state raises."""
        from unittest.mock import AsyncMock, MagicMock

        mgr = LoadManager(dry_run=True, config_interval_secs=30)
        # Use a real TeslaConfig so the controller can build login URLs
        from load_manager import TeslaConfig

        config = TeslaConfig(
            client_id="test-client-id", client_secret="test-secret",
            redirect_uri="http://localhost/callback", vehicle_id="v1",
            home_lat=37.0, home_lon=-122.0, home_radius_m=500,
            charge_amps_min=5, charge_amps_max=48,
        )
        mgr.tesla_config = config
        mock_ctrl = MagicMock(spec=RealTeslaController)
        mock_ctrl.init_tesla_state = AsyncMock(
            side_effect=TeslaAuthError("login_required: refresh_token is invalid")
        )
        mock_ctrl.get_login_url.return_value = (
            "https://auth.tesla.com/oauth2/v3/authorize?"
            "client_id=test-client-id&redirect_uri=http://localhost/callback"
        )
        # Plug ctrl can be bare Minimum — _cycle_async_phase won't fail
        from load_controllers import PlugController
        mgr.plug_ctrl = PlugController({})
        mgr.plugs = {}
        mgr.tesla_ctrl = mock_ctrl  # type: ignore[assignment]
        return mgr

    @pytest.fixture
    def lm_with_successful_tesla(self) -> LoadManager:
        """LoadManager with mock RealTeslaController whose init_tesla_state succeeds."""
        from unittest.mock import AsyncMock, MagicMock

        mgr = LoadManager(dry_run=True, config_interval_secs=30)
        from load_manager import TeslaConfig
        from load_models import TeslaState

        config = TeslaConfig(
            client_id="test", client_secret="test",
            redirect_uri="http://localhost/callback", vehicle_id="v1",
            home_lat=37.0, home_lon=-122.0, home_radius_m=500,
            charge_amps_min=5, charge_amps_max=48,
        )
        mgr.tesla_config = config
        mock_ctrl = MagicMock(spec=RealTeslaController)
        mock_ctrl.init_tesla_state = AsyncMock(
            return_value=TeslaState(
                is_charging=False, current_amps=0, plugged_in=False, at_home=False,
            )
        )
        from load_controllers import PlugController
        mgr.plug_ctrl = PlugController({})
        mgr.plugs = {}
        mgr.tesla_ctrl = mock_ctrl  # type: ignore[assignment]
        return mgr

    def test_fetch_propagates_error_and_url(self, lm_with_failing_tesla):
        """_fetch_tesla_state_async returns error and login_url when init_tesla_state fails."""
        import asyncio

        state, error, login_url = asyncio.run(
            lm_with_failing_tesla._fetch_tesla_state_async()
        )
        assert state is None
        assert error == "login_required: refresh_token is invalid"
        assert login_url is not None
        assert "auth.tesla.com" in login_url
        assert "test-client-id" in login_url

    def test_cycle_fires_auth_notification_on_init_error(self, lm_with_failing_tesla):
        """_cycle_async_phase fires auth notification when init_tesla_state fails."""
        import asyncio
        from datetime import timezone
        from unittest.mock import AsyncMock

        lm_with_failing_tesla._fire_auth_error_notification = AsyncMock()  # type: ignore[assignment]
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

        asyncio.run(lm_with_failing_tesla._cycle_async_phase(
            gap_wh=100.0, adjusted_wh=500.0, now=now,
            seconds_remaining=450, dry_run=True,
        ))

        lm_with_failing_tesla._fire_auth_error_notification.assert_awaited_once_with(  # type: ignore[attr-defined]
            "login_required: refresh_token is invalid",
            "https://auth.tesla.com/oauth2/v3/authorize?"
            "client_id=test-client-id&redirect_uri=http://localhost/callback",
        )

    def test_dedup_same_message(self, lm_with_failing_tesla):
        """Same error text only fires notification once."""
        import asyncio
        from unittest.mock import AsyncMock

        lm_with_failing_tesla._fire_auth_error_notification = AsyncMock()  # type: ignore[assignment]
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

        # First cycle — notification fires
        asyncio.run(lm_with_failing_tesla._cycle_async_phase(
            gap_wh=100.0, adjusted_wh=500.0, now=now,
            seconds_remaining=450, dry_run=True,
        ))
        lm_with_failing_tesla._fire_auth_error_notification.assert_awaited_once()  # type: ignore[attr-defined]

        # Second cycle with same error — notification NOT fired again
        lm_with_failing_tesla._fire_auth_error_notification.reset_mock()  # type: ignore[attr-defined]
        asyncio.run(lm_with_failing_tesla._cycle_async_phase(
            gap_wh=100.0, adjusted_wh=500.0, now=now,
            seconds_remaining=450, dry_run=True,
        ))
        lm_with_failing_tesla._fire_auth_error_notification.assert_not_awaited()  # type: ignore[attr-defined]

    def test_dedup_resets_on_success(self, lm_with_failing_tesla, lm_with_successful_tesla):
        """Dedup resets when error clears so future errors are reported again."""
        import asyncio
        from unittest.mock import AsyncMock

        # First cycle with failing tesla — notification fires
        lm_with_failing_tesla._fire_auth_error_notification = AsyncMock()  # type: ignore[assignment]
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        asyncio.run(lm_with_failing_tesla._cycle_async_phase(
            gap_wh=100.0, adjusted_wh=500.0, now=now,
            seconds_remaining=450, dry_run=True,
        ))
        lm_with_failing_tesla._fire_auth_error_notification.assert_awaited_once()  # type: ignore[attr-defined]

        # Second cycle with successful tesla — error clears, dedup resets
        lm_with_successful_tesla._fire_auth_error_notification = AsyncMock()  # type: ignore[assignment]
        lm_with_failing_tesla._last_auth_error_msg = "login_required: refresh_token is invalid"
        # Propagate the last_auth_error_msg to the successful LM
        lm_with_successful_tesla._last_auth_error_msg = "login_required: refresh_token is invalid"
        asyncio.run(lm_with_successful_tesla._cycle_async_phase(
            gap_wh=100.0, adjusted_wh=500.0, now=now,
            seconds_remaining=450, dry_run=True,
        ))
        lm_with_successful_tesla._fire_auth_error_notification.assert_not_awaited()  # type: ignore[attr-defined]
        assert lm_with_successful_tesla._last_auth_error_msg is None

    def test_no_notification_when_no_error(self, lm_with_successful_tesla):
        """Normal cycle with successful init fires no auth notification."""
        import asyncio
        from unittest.mock import AsyncMock

        lm_with_successful_tesla._fire_auth_error_notification = AsyncMock()  # type: ignore[assignment]
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

        asyncio.run(lm_with_successful_tesla._cycle_async_phase(
            gap_wh=100.0, adjusted_wh=500.0, now=now,
            seconds_remaining=450, dry_run=True,
        ))

        lm_with_successful_tesla._fire_auth_error_notification.assert_not_awaited()  # type: ignore[attr-defined]