"""Tests for StateTracker."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from constants import DEFAULT_PREDICTION_WINDOW_SECS
from load_manager import (
    PlugConfig,
    DeviceState,
    PendingEffect,
    StateTracker,
    TeslaState,
    TeslaVehicleTelemetry,
)

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)

def test_can_toggle_true_when_never_toggled():
    """True when device never toggled."""
    tracker = StateTracker()
    now = datetime.now(timezone.utc)
    assert tracker.can_toggle("plug", now) is True


def test_can_toggle_on_true_after_debounce():
    """True after MIN_TOGGLE_ON_SECS elapsed."""
    tracker = StateTracker()
    tracker.devices["plug"] = DeviceState(
        name="plug",
        last_toggle=datetime.now(timezone.utc) - timedelta(seconds=91),
        actual_state=True,
    )
    now = datetime.now(timezone.utc)
    assert tracker.can_toggle("plug", now) is True


def test_can_toggle_on_false_before_debounce():
    """False before MIN_TOGGLE_ON_SECS elapsed."""
    tracker = StateTracker()

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        tracker.devices["plug"] = DeviceState(
            name="plug",
            last_toggle=fixed_now - timedelta(seconds=30),
            actual_state=True,
        )

    assert tracker.can_toggle("plug", fixed_now) is False


def test_can_toggle_off_true_after_debounce():
    """True after MIN_TOGGLE_OFF_SECS elapsed."""
    tracker = StateTracker()

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        tracker.devices["plug"] = DeviceState(
            name="plug",
            last_toggle=fixed_now - timedelta(seconds=91),
            actual_state=False,
        )

    assert tracker.can_toggle("plug", fixed_now) is True


def test_can_toggle_off_false_before_debounce():
    """False before MIN_TOGGLE_OFF_SECS elapsed."""
    tracker = StateTracker()

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        tracker.devices["plug"] = DeviceState(
            name="plug",
            last_toggle=fixed_now - timedelta(seconds=30),
            actual_state=False,
        )

    assert tracker.can_toggle("plug", fixed_now) is False


def test_has_pending_effect_since():
    """True when effect after NBC fetch."""
    tracker = StateTracker()
    tracker.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=datetime(2025, 1, 1, 0, 0, 30, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 1, 1, 0, 0, 10, tzinfo=timezone.utc),
            power_watts=1000.0,
        )
    )
    assert tracker.has_pending_effect_since(datetime(2025, 1, 1, tzinfo=timezone.utc)) is True


def test_has_pending_effect_since_uses_buffer():
    """has_pending_effect_since includes effects within the prediction-window buffer
    before the NBC timestamp."""
    tracker = StateTracker()
    nbc_ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Effect timestamp is 15s before NBC timestamp — inside the 30s buffer.
    tracker.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=nbc_ts - timedelta(seconds=15),
            data_point_at=nbc_ts - timedelta(seconds=60),
            power_watts=500.0,
        )
    )
    # Should be detected because it's within the buffer window.
    assert tracker.has_pending_effect_since(nbc_ts) is True


def test_has_pending_effect_since_outside_buffer():
    """has_pending_effect_since returns False when effect is older than buffer."""
    tracker = StateTracker()
    nbc_ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Effect timestamp is 90s before NBC timestamp — outside the 60s buffer.
    tracker.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=nbc_ts - timedelta(seconds=90),
            data_point_at=nbc_ts - timedelta(seconds=120),
            power_watts=500.0,
        )
    )
    assert tracker.has_pending_effect_since(nbc_ts) is False


def test_watts_to_wh():
    """Test calculation of wh impact of a load in watts."""
    wh = StateTracker.watts_to_wh(300, 600)
    assert wh == 50


def test_wh_to_watts():
    """Test conversion of watt-hours to average power in watts."""
    w = StateTracker.wh_to_watts(50, 600)
    assert w == pytest.approx(300.0)


def test_watts_to_amps():
    """Test conversion of power in watts to integer amps."""
    amps = StateTracker.watts_to_amps(2400)
    assert amps == 10


def test_delta_amps_to_wh():
    """Test conversion of amp change over duration to watt-hours."""
    wh = StateTracker.delta_amps_to_wh(10, 600)
    assert wh == pytest.approx(400.0)


def test_wh_to_amps():
    """Test conversion of watt-hours to float amp change over duration."""
    amps = StateTracker.wh_to_amps(400, 600)
    assert amps == 10.0


def test_wh_to_amps_non_integer():
    """wh_to_amps returns float for non-integer ratios."""
    amps = StateTracker.wh_to_amps(250, 656)
    assert amps == pytest.approx(5.7165, rel=1e-3)


def test_estimated_current_wh_adds_pending():
    """Adds pending effect delta to NBC prediction."""
    tracker = StateTracker()
    now = datetime.now(timezone.utc)
    tracker.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_watts=200.0,
        )
    )
    estimated = tracker.estimated_current_wh(1000.0, seconds_remaining=900)
    assert pytest.approx(estimated) == 1050.0


def test_estimated_current_wh_no_pending():
    """Returns raw prediction when no pending effects."""
    tracker = StateTracker()
    estimated = tracker.estimated_current_wh(1000.0, seconds_remaining=900)
    assert pytest.approx(estimated) == 1000.0


def test_estimated_current_wh_multiple_effects():
    """Sums all pending effect deltas."""
    tracker = StateTracker()
    now = datetime.now(timezone.utc)
    tracker.pending_effects.extend([
        PendingEffect(
            device_name="a", action="turn_on",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_watts=200.0,
        ),
        PendingEffect(
            device_name="b", action="turn_off",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_watts=-100.0,
        ),
    ])
    estimated = tracker.estimated_current_wh(1000.0, seconds_remaining=900)
    assert pytest.approx(estimated) == 1025.0


def test_estimated_current_wh_dynamic_power_watts():
    """Plug effects with power_watts compute Wh dynamically from
    seconds_remaining."""
    tracker = StateTracker()
    now = datetime.now(timezone.utc)
    # A turn_on effect for a 2000W plug
    tracker.pending_effects.append(
        PendingEffect(
            device_name="heater",
            action="turn_on",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_watts=2000.0,
        )
    )
    # At 600s remaining: 2000W * 600/3600 = 333.33... Wh
    estimated = tracker.estimated_current_wh(1000.0, seconds_remaining=600)
    assert pytest.approx(estimated) == 1000.0 + 2000.0 * 600 / 3600
    # At 300s remaining: 2000W * 300/3600 = 166.67... Wh
    estimated_late = tracker.estimated_current_wh(
        1000.0, seconds_remaining=300
    )
    assert pytest.approx(estimated_late) == 1000.0 + 2000.0 * 300 / 3600


def test_pending_since_count_empty():
    """Returns 0 when no pending effects."""
    tracker = StateTracker()
    now = datetime.now(timezone.utc)
    assert tracker.pending_since_count(now) == 0


def test_prune_old_effects_boundary_not_pruned_early():
    """Boundary: effect with data_point_at == dp_cutoff survives one more cycle.

    When the latest data_point_at advances exactly prediction_window_seconds
    past the effect's data_point_at, the dp_cutoff equals the effect's
    data_point_at.  With strict > the effect is prematurely pruned even
    though the data has only just caught up — the next cycle may still need
    the adjustment.

    With >= the effect survives because data_point_at >= dp_cutoff is True,
    giving the predictor one more cycle to absorb the effect before pruning.
    """
    tracker = StateTracker(prediction_window_seconds=60)
    T = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    tracker.pending_effects.append(
        PendingEffect(
            device_name="boundary",
            action="turn_on",
            timestamp=T - timedelta(seconds=80),   # wall age > 60 s
            data_point_at=T - timedelta(seconds=60),  # dp age == 60 s (boundary)
            power_watts=1000.0,
        )
    )

    # now_postfetch is ahead of data_point_at (wall clock advanced more
    # than data did, as happens in real cycles with API latency).
    pruned = tracker.prune_old_effects(T, T + timedelta(seconds=34))

    # Boundary case: the effect should NOT be pruned yet.
    assert pruned == 0, "effect at dp_cutoff boundary should survive"
    assert len(tracker.pending_effects) == 1


def test_pending_since_count_uses_buffer():
    """pending_since_count applies the prediction-window buffer (no longer strict)."""
    tracker = StateTracker()
    nbc_ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Effect timestamp is 15s before NBC timestamp — inside the 30s buffer.
    tracker.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=nbc_ts - timedelta(seconds=15),
            data_point_at=nbc_ts - timedelta(seconds=60),
            power_watts=500.0,
        )
    )
    assert tracker.pending_since_count(nbc_ts) == 1


def test_pending_since_count_outside_buffer():
    """pending_since_count returns 0 when effect is older than the buffer."""
    tracker = StateTracker()
    nbc_ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    tracker.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=nbc_ts - timedelta(seconds=90),
            data_point_at=nbc_ts - timedelta(seconds=120),
            power_watts=500.0,
        )
    )
    assert tracker.pending_since_count(nbc_ts) == 0


def test_has_pending_effect_since_checks_data_point_at():
    """has_pending_effect_since also checks data_point_at with the buffer."""
    tracker = StateTracker()
    nbc_ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    # timestamp is before the buffer, but data_point_at is within it.
    tracker.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=nbc_ts - timedelta(seconds=90),
            data_point_at=nbc_ts - timedelta(seconds=15),
            power_watts=500.0,
        )
    )
    assert tracker.has_pending_effect_since(nbc_ts) is True


def test_misleading_count_has_pending_but_count_is_zero():
    """The log message 'Pending effects (0) not yet reflected' is fixed.

    This test verifies the invariant: if has_pending_effect_since returns
    True, then pending_since_count is at least 1.

    Before the fix, an effect within the buffer triggered the waiting path
    (has_pending_effect_since=True) but the count was 0 because the strict
    check excluded it. After the fix, both methods use the same buffer.
    """
    tracker = StateTracker()
    nbc_ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    tracker.pending_effects.append(
        PendingEffect(
            device_name="jackery",
            action="turn_on",
            timestamp=nbc_ts - timedelta(seconds=15),
            data_point_at=nbc_ts - timedelta(seconds=60),
            power_watts=270.0,
        )
    )

    assert tracker.has_pending_effect_since(nbc_ts) is True
    assert tracker.pending_since_count(nbc_ts) >= 1


class TestTeslaTelemetryState:
    """Tests for Tesla telemetry state fields on StateTracker (TeslaVehicleTelemetry)."""

    def test_has_fresh_telemetry_default_false(self) -> None:
        """has_fresh_telemetry defaults to False on a fresh StateTracker."""
        tracker = StateTracker()
        assert tracker.has_fresh_telemetry is False

    def test_tesla_telemetry_state_default_none(self) -> None:
        """tesla_telemetry_state defaults to None on a fresh StateTracker."""
        tracker = StateTracker()
        assert tracker.tesla_telemetry_state is None

    def test_to_dict_includes_has_fresh_telemetry(self) -> None:
        """to_dict() includes the has_fresh_telemetry field."""
        tracker = StateTracker()
        d = tracker.to_dict()
        assert "has_fresh_telemetry" in d
        assert d["has_fresh_telemetry"] is False

    def test_to_dict_includes_tesla_telemetry_state(self) -> None:
        """to_dict() includes the tesla_telemetry_state field."""
        tracker = StateTracker()
        d = tracker.to_dict()
        assert "tesla_telemetry_state" in d
        assert d["tesla_telemetry_state"] is None


class TestTeslaInflightWh:
    """Tests for StateTracker.tesla_inflight_wh()."""

    def test_no_command_returns_zero(self) -> None:
        """Returns 0 when no command has been issued."""
        tracker = StateTracker()
        result = tracker.tesla_inflight_wh(reported_amps=5, seconds_remaining=900)
        assert result == 0.0

    def test_no_report_returns_zero(self) -> None:
        """Returns 0 when no amps data is reported."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        result = tracker.tesla_inflight_wh(reported_amps=None, seconds_remaining=900)
        assert result == 0.0

    def test_charging_at_commanded_level_returns_zero(self) -> None:
        """Returns 0 when car is already at the commanded amp level."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        result = tracker.tesla_inflight_wh(reported_amps=18, seconds_remaining=900)
        assert result == 0.0

    def test_charging_at_reduced_level_returns_partial_delta(self) -> None:
        """Returns positive Wh when car is charging below commanded level."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        # delta = 18 - 5 = 13 A, 900s remaining, 240V
        # wh = 13 * 240 * 900 / 3600 = 780 Wh
        result = tracker.tesla_inflight_wh(reported_amps=5, seconds_remaining=900)
        assert pytest.approx(result) == 13 * 240 * 900 / 3600

    def test_charging_stopped_clears_command_and_returns_zero(self) -> None:
        """When car stops charging (amps=0), cleans up and returns 0.

        This is the regression test for the leak after charging completes.
        """
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        result = tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900)
        assert result == 0.0
        # last_commanded_amps should be cleared so it doesn't leak
        assert tracker.last_commanded_amps is None

    def test_one_amp_with_old_command_clears_state(self) -> None:
        """When car reports 1 amp and the command is old (beyond settle),
        treat it as stale and clear last_commanded_amps.

        A delta of 1 from a previous command is stale — the car never reached
        that level, so the in-flight correction should be zero.
        """
        tracker = StateTracker(prediction_window_seconds=60)
        tracker.last_commanded_amps = 18
        tracker.pending_effects.append(PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now - timedelta(seconds=200),
            data_point_at=fixed_now - timedelta(seconds=200),
            power_watts=0, target_amps=18,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        ))
        result = tracker.tesla_inflight_wh(
            reported_amps=1, seconds_remaining=900, now=fixed_now,
        )
        assert result == 0.0
        assert tracker.last_commanded_amps is None

    def test_one_amp_during_ramp_up_does_not_clear_state(self) -> None:
        """When car reports 1 amp during ramp-up (command is recent),
        do NOT clear last_commanded_amps — the car is still ramping.

        The car briefly reports 1A while transitioning from stopped to the
        commanded amp level.  Clearing state here would lose the in-flight
        delta and cause the next cycle to over-allocate.
        """
        tracker = StateTracker(prediction_window_seconds=60)
        tracker.last_commanded_amps = 20
        tracker.pending_effects.append(PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now - timedelta(seconds=10),
            data_point_at=fixed_now - timedelta(seconds=10),
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        ))
        result = tracker.tesla_inflight_wh(
            reported_amps=1, seconds_remaining=900, now=fixed_now,
        )
        assert result == 0.0  # not confirmed yet, but no inflight Wh to report
        # last_commanded_amps must survive — the ramp is still in progress
        assert tracker.last_commanded_amps == 20

    def test_positive_delta_returns_positive_wh(self) -> None:
        """Positive amp delta (car charging less than commanded) returns positive Wh."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        # Car is at 10 A, commanded 18 A, 450s remaining
        # delta = 8 A, wh = 8 * 240 * 450 / 3600 = 240
        result = tracker.tesla_inflight_wh(reported_amps=10, seconds_remaining=450)
        assert pytest.approx(result) == 8 * 240 * 450 / 3600

    def test_negative_delta_returns_negative_wh(self) -> None:
        """Negative amp delta (car charging more than commanded) returns negative Wh."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 10
        # Car is at 15 A but only commanded 10 A, 900s remaining
        # delta = -5 A, wh = -5 * 240 * 900 / 3600 = -300
        result = tracker.tesla_inflight_wh(reported_amps=15, seconds_remaining=900)
        assert pytest.approx(result) == -5 * 240 * 900 / 3600

    def test_no_command_stays_none_when_called(self) -> None:
        """Calling with no command doesn't mutate state."""
        tracker = StateTracker()
        assert tracker.last_commanded_amps is None
        tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900)
        assert tracker.last_commanded_amps is None

    def test_one_amp_with_data_point_at_keeps_alive(self) -> None:
        """When wall clock past settle but data_point_at recent, keep command."""
        tracker = StateTracker(prediction_window_seconds=60)  # settle = 60s
        tracker.last_commanded_amps = 20
        command_dp = fixed_now - timedelta(seconds=10)
        # Wall clock is 200s past (well past 60s settle)
        tracker.pending_effects.append(PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now - timedelta(seconds=200),
            data_point_at=command_dp,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        ))
        # But data_point_at has only advanced 55s (within 60s settle)
        result = tracker.tesla_inflight_wh(
            reported_amps=1, seconds_remaining=900,
            now=fixed_now, data_point_at=command_dp + timedelta(seconds=55),
        )
        assert result == 0.0
        assert tracker.last_commanded_amps == 20  # not cleared

    def test_settle_expired_car_below_target_clears(self) -> None:
        """After settle window, car below target → clear stale state, return 0."""
        tracker = StateTracker(prediction_window_seconds=60)
        tracker.last_commanded_amps = 24
        # Increase recorded 200s ago — well past 60s settle window
        tracker.pending_effects.append(PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now - timedelta(seconds=200),
            data_point_at=fixed_now - timedelta(seconds=200),
            power_watts=0, target_amps=24,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        ))
        result = tracker.tesla_inflight_wh(
            reported_amps=10, seconds_remaining=746, now=fixed_now,
        )
        assert result == 0.0
        assert tracker.last_commanded_amps is None

    def test_settle_active_car_below_target_preserves_state(self) -> None:
        """During settle window, car below target → return delta, keep state."""
        tracker = StateTracker(prediction_window_seconds=60)
        tracker.last_commanded_amps = 24
        # Increase recorded 30s ago — within 60s settle window
        tracker.pending_effects.append(PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now - timedelta(seconds=30),
            data_point_at=fixed_now - timedelta(seconds=30),
            power_watts=0, target_amps=24,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        ))
        result = tracker.tesla_inflight_wh(
            reported_amps=10, seconds_remaining=746, now=fixed_now,
        )
        # delta = 24 - 10 = 14 A, wh = 14 * 240 * 746 / 3600 = 696.27
        assert pytest.approx(result) == 14 * 240 * 746 / 3600
        assert tracker.last_commanded_amps == 24

    def test_one_amp_with_both_expired_via_data(self) -> None:
        """When both wall and data measures exceed settle, clear state."""
        tracker = StateTracker(prediction_window_seconds=60)  # settle = 120s
        tracker.last_commanded_amps = 20
        command_dp = fixed_now - timedelta(seconds=200)
        tracker.pending_effects.append(PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now - timedelta(seconds=200),
            data_point_at=command_dp,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        ))
        # Both timestamps are past 120s settle
        result = tracker.tesla_inflight_wh(
            reported_amps=1, seconds_remaining=900,
            now=fixed_now, data_point_at=command_dp + timedelta(seconds=200),
        )
        assert result == 0.0
        assert tracker.last_commanded_amps is None  # cleared as stale


class TestEffectiveSettleSecs:
    """Tests for StateTracker.effective_settle_secs."""

    def test_default_prediction_window(self) -> None:
        """With default prediction_window_seconds, settle matches the default."""
        tracker = StateTracker()
        assert tracker.effective_settle_secs == DEFAULT_PREDICTION_WINDOW_SECS

    def test_custom_prediction_window_30(self) -> None:
        """With prediction_window_seconds=30, settle is 30."""
        tracker = StateTracker(prediction_window_seconds=30)
        assert tracker.effective_settle_secs == 30

    def test_small_prediction_window(self) -> None:
        """With prediction_window_seconds=10, settle is 10."""
        tracker = StateTracker(prediction_window_seconds=10)
        assert tracker.effective_settle_secs == 10


class TestSyncTeslaDeviceState:
    """Tests for StateTracker.sync_tesla_device_state()."""

    def test_creates_entry_when_charging(self) -> None:
        """Tesla entry appears in devices when vehicle is charging."""
        tracker = StateTracker()
        ts = TeslaState(is_charging=True, current_amps=8, plugged_in=True, at_home=True)
        tracker.sync_tesla_device_state(ts)
        assert "tesla" in tracker.devices
        dev = tracker.devices["tesla"]
        assert dev.actual_state is True
        assert dev.current_amps == 8

    def test_shows_not_charging_state(self) -> None:
        """Tesla entry shows actualState=False when not charging."""
        tracker = StateTracker()
        ts = TeslaState(is_charging=False, current_amps=0, plugged_in=True, at_home=True)
        tracker.sync_tesla_device_state(ts)
        assert "tesla" in tracker.devices
        assert tracker.devices["tesla"].actual_state is False

    def test_removes_entry_when_state_none(self) -> None:
        """Tesla entry is removed when tesla_state is None."""
        tracker = StateTracker()
        ts = TeslaState(is_charging=True, current_amps=8, plugged_in=True, at_home=True)
        tracker.sync_tesla_device_state(ts)
        assert "tesla" in tracker.devices
        tracker.sync_tesla_device_state(None)
        assert "tesla" not in tracker.devices

    def test_desired_state_true_when_commanded(self) -> None:
        """desired_state is True when last_commanded_amps is set."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 8
        ts = TeslaState(is_charging=True, current_amps=8, plugged_in=True, at_home=True)
        tracker.sync_tesla_device_state(ts)
        assert tracker.devices["tesla"].desired_state is True

    def test_desired_state_false_when_no_command(self) -> None:
        """desired_state is False when no command is pending."""
        tracker = StateTracker()
        ts = TeslaState(is_charging=True, current_amps=8, plugged_in=True, at_home=True)
        tracker.sync_tesla_device_state(ts)
        assert tracker.devices["tesla"].desired_state is False

    def test_last_toggle_from_command_timestamp(self) -> None:
        """last_toggle reflects the last Tesla command time."""
        tracker = StateTracker()
        tracker.pending_effects.append(PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=8,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        ))
        ts = TeslaState(is_charging=True, current_amps=8, plugged_in=True, at_home=True)
        tracker.sync_tesla_device_state(ts)
        assert tracker.devices["tesla"].last_toggle == fixed_now

    def test_includes_in_to_dict(self) -> None:
        """Tesla entry appears in to_dict() output."""
        tracker = StateTracker()
        ts = TeslaState(is_charging=True, current_amps=8, plugged_in=True, at_home=True)
        tracker.sync_tesla_device_state(ts)
        d = tracker.to_dict()
        assert "tesla" in d["devices"]
        assert d["devices"]["tesla"]["actual_state"] is True
        assert d["devices"]["tesla"]["current_amps"] == 8


class TestIsSettling:
    """Tests for the unified is_settling() method."""

    def test_increase_settle_active(self) -> None:
        """is_settling(direction='increase') returns True within window."""
        tracker = StateTracker(prediction_window_seconds=60)
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=30), current_qh="QH1",
            direction="increase",
        ) is True

    def test_increase_settle_expired(self) -> None:
        """is_settling(direction='increase') returns False after window."""
        tracker = StateTracker(prediction_window_seconds=30)
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=61), current_qh="QH1",
            direction="increase",
        ) is False

    def test_increase_settle_expires_on_qh_change(self) -> None:
        """A new QH expires the settle window."""
        tracker = StateTracker()
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=10), current_qh="QH2",
            direction="increase",
        ) is False

    def test_decrease_settle_active(self) -> None:
        """is_settling(direction='decrease') returns True within window."""
        tracker = StateTracker(prediction_window_seconds=60)
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=10,
            direction="decrease", suppress_action="turn_on",
            qh_name="QH1",
        )
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=30), current_qh="QH1",
            direction="decrease",
        ) is True

    def test_decrease_settle_expired(self) -> None:
        """is_settling(direction='decrease') returns False after window."""
        tracker = StateTracker(prediction_window_seconds=30)
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=10,
            direction="decrease", suppress_action="turn_on",
            qh_name="QH1",
        )
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=61), current_qh="QH1",
            direction="decrease",
        ) is False

    def test_no_settle_returns_false(self) -> None:
        """is_settling returns False when no settle effects exist."""
        tracker = StateTracker()
        assert tracker.is_settling(
            fixed_now, current_qh="QH1", direction="increase",
        ) is False

    def test_settle_with_data_point_at_lag_persists(self) -> None:
        """Settle persists when data_point_at lags even if wall clock expired."""
        tracker = StateTracker(prediction_window_seconds=60)
        record_dp = fixed_now - timedelta(seconds=50)
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=record_dp,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        tracker.pending_effects.append(eff)
        advanced_wall = fixed_now + timedelta(seconds=130)
        advanced_dp = record_dp + timedelta(seconds=50)
        assert tracker.is_settling(
            advanced_wall, current_qh="QH1", data_point_at=advanced_dp,
            direction="increase",
        ) is True

    def test_settle_both_expired(self) -> None:
        """Settle expires when both wall clock and data_point_at exceed window."""
        tracker = StateTracker(prediction_window_seconds=60)
        record_dp = fixed_now - timedelta(seconds=50)
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=record_dp,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        tracker.pending_effects.append(eff)
        advanced_wall = fixed_now + timedelta(seconds=130)
        advanced_dp = record_dp + timedelta(seconds=130)
        assert tracker.is_settling(
            advanced_wall, current_qh="QH1", data_point_at=advanced_dp,
            direction="increase",
        ) is False

    def test_wrong_direction_returns_false(self) -> None:
        """is_settling with wrong direction returns False."""
        tracker = StateTracker(prediction_window_seconds=60)
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=30), current_qh="QH1",
            direction="decrease",
        ) is False

    def test_data_point_at_none_falls_back_to_wall(self) -> None:
        """When data_point_at is None, falls back to wall-clock-only check."""
        tracker = StateTracker(prediction_window_seconds=30)
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        tracker.pending_effects.append(eff)
        # Within wall window — should be True
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=20), current_qh="QH1",
            direction="increase",
        ) is True
        # Past wall window — should be False
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=40), current_qh="QH1",
            direction="increase",
        ) is False

    def test_decrease_settle_expires_on_qh_change(self) -> None:
        """A new QH expires the decrease settle window."""
        tracker = StateTracker()
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=10,
            direction="decrease", suppress_action="turn_on",
            qh_name="QH1",
        )
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=10), current_qh="QH2",
            direction="decrease",
        ) is False


class TestLatestTeslaCommand:
    """Tests for _latest_tesla_command()."""

    def test_returns_none_when_empty(self) -> None:
        """Returns None when no Tesla effects exist."""
        tracker = StateTracker()
        assert tracker._latest_tesla_command() is None

    def test_returns_none_for_plug_effects(self) -> None:
        """Returns None when only plug effects exist."""
        tracker = StateTracker()
        eff = PendingEffect(
            device_name="pool_pump", action="turn_on",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=1000,
        )
        tracker.pending_effects.append(eff)
        assert tracker._latest_tesla_command() is None

    def test_returns_most_recent_tesla_effect(self) -> None:
        """Returns the most recent Tesla set_amps effect."""
        tracker = StateTracker()
        eff1 = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now - timedelta(seconds=10),
            data_point_at=fixed_now - timedelta(seconds=10),
            power_watts=0, target_amps=10,
            direction="decrease", suppress_action="turn_on",
            qh_name="QH1",
        )
        eff2 = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        tracker.pending_effects.extend([eff1, eff2])
        assert tracker._latest_tesla_command() is eff2

    def test_returns_turn_on_off_effect(self) -> None:
        """Returns Tesla turn_on/turn_off effects too."""
        tracker = StateTracker()
        eff = PendingEffect(
            device_name="tesla", action="turn_off",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0,
        )
        tracker.pending_effects.append(eff)
        assert tracker._latest_tesla_command() is eff


class TestClearTeslaSettleEffects:
    """Tests for clear_tesla_settle_effects()."""

    def test_removes_tesla_set_amps_effects(self) -> None:
        """Removes Tesla set_amps effects from pending_effects."""
        tracker = StateTracker()
        eff1 = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        eff2 = PendingEffect(
            device_name="pool_pump", action="turn_on",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=1000,
        )
        tracker.pending_effects.extend([eff1, eff2])
        tracker.clear_tesla_settle_effects()
        assert len(tracker.pending_effects) == 1
        assert tracker.pending_effects[0].device_name == "pool_pump"

    def test_keeps_tesla_turn_on_off_effects(self) -> None:
        """Keeps Tesla turn_on/turn_off effects (only set_amps removed)."""
        tracker = StateTracker()
        eff1 = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        eff2 = PendingEffect(
            device_name="tesla", action="turn_off",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0,
        )
        tracker.pending_effects.extend([eff1, eff2])
        tracker.clear_tesla_settle_effects()
        assert len(tracker.pending_effects) == 1
        assert tracker.pending_effects[0].action == "turn_off"

    def test_noop_when_empty(self) -> None:
        """No-op when no effects exist."""
        tracker = StateTracker()
        tracker.clear_tesla_settle_effects()
        assert len(tracker.pending_effects) == 0


class TestPendingEffectDirection:
    """Tests for PendingEffect direction/suppress_action/qh_name fields."""

    def test_plug_effect_has_none_direction(self) -> None:
        """Plug effects have direction=None by default."""
        eff = PendingEffect(
            device_name="pool_pump", action="turn_on",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=1000,
        )
        assert eff.direction is None
        assert eff.suppress_action is None
        assert eff.qh_name is None

    def test_tesla_increase_effect(self) -> None:
        """Tesla increase effect has correct metadata."""
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=20,
            direction="increase", suppress_action="turn_off",
            qh_name="QH1",
        )
        assert eff.direction == "increase"
        assert eff.suppress_action == "turn_off"
        assert eff.qh_name == "QH1"

    def test_tesla_decrease_effect(self) -> None:
        """Tesla decrease effect has correct metadata."""
        eff = PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=fixed_now, data_point_at=fixed_now,
            power_watts=0, target_amps=10,
            direction="decrease", suppress_action="turn_on",
            qh_name="QH1",
        )
        assert eff.direction == "decrease"
        assert eff.suppress_action == "turn_on"
        assert eff.qh_name == "QH1"
