"""Tests for StateTracker."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from constants import DEFAULT_PREDICTION_WINDOW_SECS
from load_models import PlugConfig, DeviceState, PendingEffect, TeslaState
from load_nbc import StateTracker, nominal_voltage

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


def make_settle_effect(
    age_secs: int = 10,
    target_amps: int = 20,
    *,
    direction: str = "increase",
    suppress_action: str = "turn_off",
    data_point_at: datetime | None = None,
) -> PendingEffect:
    """Build a Tesla ``set_amps`` pending effect for settle-window tests.

    Args:
        age_secs: How long ago the command was issued.
        target_amps: The commanded amp level.
        direction: Effect direction metadata.
        suppress_action: Suppress metadata for the opposite action.
        data_point_at: Defaults to the same age as the command timestamp.
    """
    return PendingEffect(
        device_name="tesla", action="set_amps",
        timestamp=fixed_now - timedelta(seconds=age_secs),
        data_point_at=data_point_at
        if data_point_at is not None
        else fixed_now - timedelta(seconds=age_secs),
        power_watts=0, target_amps=target_amps,
        direction=direction, suppress_action=suppress_action,
        qh_name="QH1",
    )

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
    tracker.devices["plug"] = DeviceState(
        name="plug",
        last_toggle=fixed_now - timedelta(seconds=30),
        actual_state=True,
    )
    assert tracker.can_toggle("plug", fixed_now) is False


def test_can_toggle_off_true_after_debounce():
    """True after MIN_TOGGLE_OFF_SECS elapsed."""
    tracker = StateTracker()
    tracker.devices["plug"] = DeviceState(
        name="plug",
        last_toggle=fixed_now - timedelta(seconds=91),
        actual_state=False,
    )
    assert tracker.can_toggle("plug", fixed_now) is True


def test_can_toggle_off_false_before_debounce():
    """False before MIN_TOGGLE_OFF_SECS elapsed."""
    tracker = StateTracker()
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


def test_prune_preserves_unreflected_command():
    """Effect whose command was sent after the NBC data point must survive.

    Regression guard: command timestamp > data_point_at means the NBC
    prediction doesn't reflect the load change yet.  Pruning it causes
    the next cycle to act on a stale surplus/deficit.
    """
    tracker = StateTracker(prediction_window_seconds=30)
    dp = datetime(2026, 7, 7, 19, 12, 0, tzinfo=timezone.utc)
    now = dp + timedelta(seconds=64)

    tracker.pending_effects.append(
        PendingEffect(
            device_name="tesla", action="set_amps",
            timestamp=dp + timedelta(seconds=4),          # command after data
            data_point_at=dp - timedelta(seconds=30, microseconds=6),  # below dp_cutoff
            power_watts=1440.0, target_amps=11, direction="increase",
        )
    )

    pruned = tracker.prune_old_effects(dp, now)
    assert pruned == 0, "effect with timestamp > data_point_at must survive"
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
        result = tracker.tesla_inflight_wh(reported_amps=5, seconds_remaining=900, now=fixed_now)
        assert result == 0.0

    def test_no_report_returns_zero(self) -> None:
        """Returns 0 when no amps data is reported."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        result = tracker.tesla_inflight_wh(reported_amps=None, seconds_remaining=900, now=fixed_now)
        assert result == 0.0

    def test_charging_at_commanded_level_returns_zero(self) -> None:
        """Returns 0 when car is already at the commanded amp level."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        result = tracker.tesla_inflight_wh(reported_amps=18, seconds_remaining=900, now=fixed_now)
        assert result == 0.0

    def test_charging_at_reduced_level_returns_partial_delta(self) -> None:
        """Returns positive Wh when car is charging below commanded level."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        # delta = 18 - 5 = 13 A, 900s remaining, 240V
        # wh = 13 * 240 * 900 / 3600 = 780 Wh
        result = tracker.tesla_inflight_wh(reported_amps=5, seconds_remaining=900, now=fixed_now)
        assert pytest.approx(result) == 13 * 240 * 900 / 3600

    def test_positive_delta_returns_positive_wh(self) -> None:
        """Positive amp delta (car charging less than commanded) returns positive Wh."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        # Car is at 10 A, commanded 18 A, 450s remaining
        # delta = 8 A, wh = 8 * 240 * 450 / 3600 = 240
        result = tracker.tesla_inflight_wh(reported_amps=10, seconds_remaining=450, now=fixed_now)
        assert pytest.approx(result) == 8 * 240 * 450 / 3600

    def test_negative_delta_returns_negative_wh(self) -> None:
        """Negative amp delta (car charging more than commanded) returns negative Wh."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 10
        # Car is at 15 A but only commanded 10 A, 900s remaining
        # delta = -5 A, wh = -5 * 240 * 900 / 3600 = -300
        result = tracker.tesla_inflight_wh(reported_amps=15, seconds_remaining=900, now=fixed_now)
        assert pytest.approx(result) == -5 * 240 * 900 / 3600

    def test_no_command_stays_none_when_called(self) -> None:
        """Calling with no command doesn't mutate state."""
        tracker = StateTracker()
        assert tracker.last_commanded_amps is None
        tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900, now=fixed_now)
        assert tracker.last_commanded_amps is None

    def test_settle_expired_car_below_target_clears(self) -> None:
        """After settle window, car below target → clear stale state, return 0."""
        tracker = StateTracker(prediction_window_seconds=60)
        tracker.last_commanded_amps = 24
        # Increase recorded 200s ago — well past 60s settle window
        tracker.pending_effects.append(make_settle_effect(age_secs=200, target_amps=24))
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
        tracker.pending_effects.append(make_settle_effect(age_secs=30, target_amps=24))
        result = tracker.tesla_inflight_wh(
            reported_amps=10, seconds_remaining=746, now=fixed_now,
        )
        # delta = 24 - 10 = 14 A, wh = 14 * 240 * 746 / 3600 = 696.27
        assert pytest.approx(result) == 14 * 240 * 746 / 3600
        assert tracker.last_commanded_amps == 24

    def test_one_amp_with_both_expired_via_data(self) -> None:
        """When both wall and data measures exceed settle, clear state."""
        tracker = StateTracker(prediction_window_seconds=60)
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


class TestTeslaInflightAccounting:
    """Zero-amp confirmation and ramp-up credit (plan 3.5, fixes A5).

    The old behavior cleared the command on a single 0 A telemetry frame
    and returned 0 Wh during 1 A ramp-up — both overstate surplus because
    in-flight amp commands stop being accounted before they are confirmed.
    """

    def _settle_effect(self, age_secs: int = 10) -> PendingEffect:
        """Build a recent set_amps settle effect targeting the Tesla device."""
        return make_settle_effect(age_secs=age_secs)

    # ── Zero-amp confirmation ──────────────────────────────────────────

    def test_single_zero_keeps_command_returns_full_delta(self) -> None:
        """First zero report: command kept, full commanded delta returned."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        result = tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900, now=fixed_now)
        assert pytest.approx(result) == 18 * 240 * 900 / 3600
        assert tracker.last_commanded_amps == 18

    def test_second_consecutive_zero_clears_and_returns_zero(self) -> None:
        """Second consecutive zero confirms the car stopped: clear + 0 Wh."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        first = tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900, now=fixed_now)
        assert pytest.approx(first) == 18 * 240 * 900 / 3600
        second = tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900, now=fixed_now)
        assert second == 0.0
        assert tracker.last_commanded_amps is None

    def test_nonzero_report_resets_zero_amp_counter(self) -> None:
        """A non-zero report resets confirmation; an isolated zero never clears."""
        tracker = StateTracker()
        tracker.last_commanded_amps = 18
        tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900, now=fixed_now)
        # Car resumes charging below commanded level — counter must reset.
        resumed = tracker.tesla_inflight_wh(reported_amps=5, seconds_remaining=900, now=fixed_now)
        assert pytest.approx(resumed) == 13 * 240 * 900 / 3600
        assert tracker.last_commanded_amps == 18
        # Isolated zero after reset: still only one consecutive sample.
        isolated = tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900, now=fixed_now)
        assert pytest.approx(isolated) == 18 * 240 * 900 / 3600
        assert tracker.last_commanded_amps == 18

    def test_new_command_resets_zero_amp_counter(self) -> None:
        """Recording a fresh amp command resets zero-amp confirmation."""
        tracker = StateTracker()
        tracker.record_tesla_amp_command(18)
        tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900, now=fixed_now)
        # New command issued while the old count was already at 1.
        tracker.record_tesla_amp_command(12)
        result = tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900, now=fixed_now)
        assert pytest.approx(result) == 12 * 240 * 900 / 3600
        assert tracker.last_commanded_amps == 12

    def test_record_none_clears_counter_too(self) -> None:
        """Recording None (stop/turn_off) clears both command and counter."""
        tracker = StateTracker()
        tracker.record_tesla_amp_command(18)
        tracker.tesla_inflight_wh(reported_amps=0, seconds_remaining=900, now=fixed_now)
        tracker.record_tesla_amp_command(None)
        assert tracker.last_commanded_amps is None
        # Next isolated zero is again just a single unconfirmed sample.

    # ── Ramp-up credit ─────────────────────────────────────────────────

    def test_rampup_one_amp_credits_unconfirmed_delta(self) -> None:
        """Wall-clock ramp-up branch credits (commanded - reported) delta."""
        tracker = StateTracker(prediction_window_seconds=60)
        tracker.last_commanded_amps = 20
        tracker.pending_effects.append(self._settle_effect(age_secs=10))
        result = tracker.tesla_inflight_wh(
            reported_amps=1, seconds_remaining=900, now=fixed_now,
        )
        assert pytest.approx(result) == (20 - 1) * 240 * 900 / 3600
        assert tracker.last_commanded_amps == 20

    def test_rampup_one_amp_data_age_branch_credits_unconfirmed_delta(self) -> None:
        """Data-point-age ramp-up branch credits the unconfirmed delta too."""
        tracker = StateTracker(prediction_window_seconds=60)
        tracker.last_commanded_amps = 20
        effect = self._settle_effect(age_secs=200)
        tracker.pending_effects.append(effect)
        # Wall clock expired (200s > 60s) but data age is fresh (55s < 60s).
        result = tracker.tesla_inflight_wh(
            reported_amps=1, seconds_remaining=900,
            now=fixed_now,
            data_point_at=effect.data_point_at + timedelta(seconds=55),
        )
        assert pytest.approx(result) == (20 - 1) * 240 * 900 / 3600
        assert tracker.last_commanded_amps == 20

    def test_one_amp_stale_command_still_clears(self) -> None:
        """Beyond BOTH settle measures, 1 A remains stale → clear + 0 Wh."""
        tracker = StateTracker(prediction_window_seconds=60)
        tracker.last_commanded_amps = 20
        tracker.pending_effects.append(self._settle_effect(age_secs=200))
        result = tracker.tesla_inflight_wh(
            reported_amps=1, seconds_remaining=900, now=fixed_now,
        )
        assert result == 0.0
        assert tracker.last_commanded_amps is None



    # ── Effective settle window ────────────────────────────────────────

    @pytest.mark.parametrize("window", [None, 30, 10])
    def test_effective_settle_matches_prediction_window(self, window: int | None) -> None:
        """effective_settle_secs mirrors prediction_window_seconds (default when None)."""
        if window is None:
            tracker = StateTracker()
            assert (
                tracker.effective_settle_secs == DEFAULT_PREDICTION_WINDOW_SECS
            )
        else:
            tracker = StateTracker(prediction_window_seconds=window)
            assert tracker.effective_settle_secs == window


class TestApplyPredictionWindow:
    """Tests for StateTracker.apply_prediction_window() hysteresis.

    A candidate window is committed only when the same value appears on two
    consecutive calls; values within the dead-band of the committed window
    are treated as detector jitter and never even seed a candidate.
    """

    def test_commits_after_two_consecutive_calls(self) -> None:
        """A candidate window commits only after two consecutive calls."""
        tracker = StateTracker(prediction_window_seconds=30)
        tracker.apply_prediction_window(120)
        assert tracker.effective_settle_secs == 30  # candidate, not committed
        tracker.apply_prediction_window(120)
        assert tracker.effective_settle_secs == 120

    def test_interleaved_values_never_commit(self) -> None:
        """Alternating candidates never accumulate confirmations."""
        tracker = StateTracker(prediction_window_seconds=30)
        tracker.apply_prediction_window(120)
        tracker.apply_prediction_window(60)
        tracker.apply_prediction_window(120)
        tracker.apply_prediction_window(60)
        assert tracker.effective_settle_secs == 30

    def test_deadband_jitter_around_committed_window_ignored(self) -> None:
        """±1 s detector jitter never churns the committed window."""
        tracker = StateTracker(prediction_window_seconds=30)
        tracker.apply_prediction_window(120)
        tracker.apply_prediction_window(120)
        assert tracker.effective_settle_secs == 120
        tracker.apply_prediction_window(121)
        assert tracker.effective_settle_secs == 120
        tracker.apply_prediction_window(119)
        assert tracker.effective_settle_secs == 120

    def test_committed_window_can_shrink_back_toward_default(self) -> None:
        """When quantization disappears, the window reverts toward default."""
        tracker = StateTracker(prediction_window_seconds=30)
        tracker.apply_prediction_window(120)
        tracker.apply_prediction_window(120)
        assert tracker.effective_settle_secs == 120
        tracker.apply_prediction_window(30)
        tracker.apply_prediction_window(30)
        assert tracker.effective_settle_secs == 30


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
        tracker.pending_effects.append(make_settle_effect(age_secs=0, target_amps=8))
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
        eff = make_settle_effect(age_secs=0)
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=30), current_qh="QH1",
            direction="increase",
        ) is True

    def test_increase_settle_expired(self) -> None:
        """is_settling(direction='increase') returns False after window."""
        tracker = StateTracker(prediction_window_seconds=30)
        eff = make_settle_effect(age_secs=0)
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=61), current_qh="QH1",
            direction="increase",
        ) is False

    def test_increase_settle_expires_on_qh_change(self) -> None:
        """A new QH expires the settle window."""
        tracker = StateTracker()
        eff = make_settle_effect(age_secs=0)
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=10), current_qh="QH2",
            direction="increase",
        ) is False

    def test_decrease_settle_active(self) -> None:
        """is_settling(direction='decrease') returns True within window."""
        tracker = StateTracker(prediction_window_seconds=60)
        eff = make_settle_effect(age_secs=0, target_amps=10, direction="decrease", suppress_action="turn_on")
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=30), current_qh="QH1",
            direction="decrease",
        ) is True

    def test_decrease_settle_expired(self) -> None:
        """is_settling(direction='decrease') returns False after window."""
        tracker = StateTracker(prediction_window_seconds=30)
        eff = make_settle_effect(age_secs=0, target_amps=10, direction="decrease", suppress_action="turn_on")
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
        eff = make_settle_effect(age_secs=0)
        tracker.pending_effects.append(eff)
        assert tracker.is_settling(
            fixed_now + timedelta(seconds=30), current_qh="QH1",
            direction="decrease",
        ) is False

    def test_data_point_at_none_falls_back_to_wall(self) -> None:
        """When data_point_at is None, falls back to wall-clock-only check."""
        tracker = StateTracker(prediction_window_seconds=30)
        eff = make_settle_effect(age_secs=0)
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
        eff = make_settle_effect(age_secs=0, target_amps=10, direction="decrease", suppress_action="turn_on")
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
        eff1 = make_settle_effect(age_secs=10, target_amps=10, direction="decrease", suppress_action="turn_on")
        eff2 = make_settle_effect(age_secs=0)
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
        eff1 = make_settle_effect(age_secs=0)
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
        eff1 = make_settle_effect(age_secs=0)
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



# =============================================================================
# Thread-safe snapshots (plan subtask 2.1, fixes R1)
# =============================================================================


class TestThreadSafeSnapshots:
    """Cross-thread readers must consume atomic copies, not live dicts.

    The load-management thread mutates ``devices``/``pending_effects``
    while Flask request threads iterate them via load_status() and
    state.to_dict(). Lazy iteration over the live structures raises
    intermittent "dictionary changed size during iteration" 500s.
    """

    def _tracker_with_device(self) -> StateTracker:
        tracker = StateTracker()
        tracker.devices["plug_a"] = DeviceState(
            name="plug_a", desired_state=True
        )
        tracker.pending_effects.append(
            PendingEffect(
                device_name="plug_a",
                action="turn_on",
                timestamp=fixed_now,
                data_point_at=fixed_now,
                power_watts=100.0,
            )
        )
        return tracker

    def test_snapshot_devices_returns_copy(self):
        """snapshot_devices() returns an independent copy."""
        tracker = self._tracker_with_device()
        snapshot = tracker.snapshot_devices()
        assert set(snapshot) == {"plug_a"}

        tracker.set_device_state(
            "plug_b", DeviceState(name="plug_b", desired_state=False)
        )
        assert "plug_b" not in snapshot
        assert "plug_b" in tracker.devices

    def test_snapshot_effects_returns_copy(self):
        """snapshot_effects() returns an independent copy."""
        tracker = self._tracker_with_device()
        snapshot = tracker.snapshot_effects()
        assert len(snapshot) == 1

        from load_models import PendingEffect as PE

        tracker.add_effect(
            PE(
                device_name="plug_b",
                action="turn_off",
                timestamp=fixed_now,
                data_point_at=fixed_now,
                power_watts=-50.0,
            )
        )
        assert len(snapshot) == 1
        assert len(tracker.pending_effects) == 2

    def test_to_dict_isolated_from_later_mutations(self):
        """to_dict() output reflects a point-in-time snapshot."""
        tracker = self._tracker_with_device()
        data = tracker.to_dict()

        tracker.set_device_state(
            "plug_c", DeviceState(name="plug_c", desired_state=False)
        )
        tracker.snapshot_effects()  # exercise lock re-entrancy paths
        assert set(data["devices"]) == {"plug_a"}
        assert len(data["pending_effects"]) == 1

    def test_load_status_reads_snapshots_not_live_dicts(self):
        """/api/v1/load/status builds devices/effects from snapshots."""
        import app as app_mod
        from load_manager import LoadManager, LoadManagerConfig

        lm = LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))
        lm.state.devices["plug_a"] = DeviceState(
            name="plug_a", desired_state=True
        )

        calls = {"devices": False, "effects": False}

        def fake_device_snapshot():
            calls["devices"] = True
            return {}

        def fake_effect_snapshot():
            calls["effects"] = True
            return []

        lm.state.snapshot_devices = fake_device_snapshot
        lm.state.snapshot_effects = fake_effect_snapshot

        client = app_mod.app.test_client()
        client.testing = True
        with patch("app._get_load_manager", return_value=lm):
            response = client.get("/api/v1/load/status")

        assert response.status_code == 200
        data = response.get_json()
        assert calls["devices"] is True, (
            "load_status must read device state via snapshot_devices()"
        )
        assert calls["effects"] is True, (
            "load_status must read pending effects via snapshot_effects()"
        )
        assert data["devices"] == {}
        assert data["pendingEffects"] == []

    def test_hammer_mutations_vs_snapshots(self):
        """Concurrent mutation + snapshotting never raises RuntimeError."""
        import threading

        tracker = self._tracker_with_device()
        errors: list[BaseException] = []
        stop = threading.Event()

        def writer():
            index = 0
            while not stop.is_set():
                try:
                    name = f"w{index % 7}"
                    tracker.set_device_state(
                        name, DeviceState(name=name, desired_state=True)
                    )
                    if index % 5 == 0:
                        with tracker._state_lock:
                            tracker.devices.pop(name, None)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    return
                index += 1

        writer_thread = threading.Thread(target=writer, daemon=True)
        writer_thread.start()
        try:
            for _ in range(5000):
                snapshot = tracker.snapshot_devices()
                effects = tracker.snapshot_effects()
                assert isinstance(snapshot, dict)
                assert isinstance(effects, list)
        finally:
            stop.set()
            writer_thread.join(timeout=5)

        assert errors == [], f"writer hit {errors[0]!r}"


class TestNominalVoltageConfig:
    """TESLA_NOMINAL_VOLTAGE env threading through conversions (plan 3.6)."""

    @pytest.mark.parametrize("bad", ["abc", "", "0", "-10", "  "])
    def test_invalid_value_falls_back_to_default(self, monkeypatch, bad) -> None:
        """Non-numeric or non-positive values fall back to 240 V."""
        monkeypatch.setenv("TESLA_NOMINAL_VOLTAGE", bad)
        assert nominal_voltage() == 240.0

    def test_default_is_240(self, monkeypatch) -> None:
        """With no env override, conversions use the 240 V default."""
        monkeypatch.delenv("TESLA_NOMINAL_VOLTAGE", raising=False)
        assert nominal_voltage() == 240.0

    def test_env_override_honored(self, monkeypatch) -> None:
        """A valid TESLA_NOMINAL_VOLTAGE overrides the default."""
        monkeypatch.setenv("TESLA_NOMINAL_VOLTAGE", "208")
        assert nominal_voltage() == 208.0

    def test_conversions_honor_override(self, monkeypatch) -> None:
        """All four amp/watt conversions read the configured voltage."""
        monkeypatch.setenv("TESLA_NOMINAL_VOLTAGE", "208")
        assert StateTracker.amps_to_watts(10) == pytest.approx(2080.0)
        assert StateTracker.watts_to_amps(2080.0) == 10
        assert StateTracker.delta_amps_to_wh(10, 3600) == pytest.approx(2080.0)
        assert StateTracker.wh_to_amps(2080.0, 3600) == pytest.approx(10.0)

    def test_conversions_default_unchanged(self, monkeypatch) -> None:
        """Without env override the historical 240 V math still applies."""
        monkeypatch.delenv("TESLA_NOMINAL_VOLTAGE", raising=False)
        assert StateTracker.amps_to_watts(10) == pytest.approx(2400.0)
        assert StateTracker.watts_to_amps(2400.0) == 10
        assert StateTracker.delta_amps_to_wh(10, 3600) == pytest.approx(2400.0)
        assert StateTracker.wh_to_amps(2400.0, 3600) == pytest.approx(10.0)
