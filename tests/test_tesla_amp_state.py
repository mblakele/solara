"""Truth table for the pure Tesla amp lifecycle classifier."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from constants import TESLA_ZERO_AMPS_CLEAR_SAMPLES
from load_models import PendingEffect, TeslaAmpState, classify_tesla_amp
from load_nbc import EffectStore, TeslaSettleTracker

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def decide(**overrides: object):
    """Classify with a charging baseline, overridden per case."""
    base: dict[str, object] = {
        "commanded_amps": 18,
        "reported_amps": None,
        "zero_samples": 0,
        "has_settle": False,
        "settle_open": False,
        "command_window_open": False,
    }
    base.update(overrides)
    return classify_tesla_amp(**base)  # type: ignore[arg-type]


def test_no_command_is_idle() -> None:
    """No command means no contribution and no mutation."""
    decision = decide(commanded_amps=None)
    assert decision.state is TeslaAmpState.IDLE
    assert decision.credit_amps == 0.0
    assert decision.clear_command is False
    assert decision.zero_samples == 0
    assert decision.reason == "idle"


def test_no_report_keeps_the_command_and_counter() -> None:
    """A missing report must not move anything, including the counter."""
    decision = decide(reported_amps=None, zero_samples=1)
    assert decision.state is TeslaAmpState.AWAITING_REPORT
    assert decision.credit_amps == 0.0
    assert decision.clear_command is False
    assert decision.zero_samples == 1
    assert decision.reason == "awaiting_report"


def test_first_zero_keeps_the_full_command() -> None:
    """One zero frame is unconfirmed; the full commanded delta stays."""
    decision = decide(reported_amps=0)
    assert decision.state is TeslaAmpState.AWAITING_STOP
    assert decision.credit_amps == 18.0
    assert decision.clear_command is False
    assert decision.zero_samples == 1
    assert decision.reason == "stop_unconfirmed"


def test_second_zero_confirms_the_stop() -> None:
    """The threshold-th consecutive zero clears the command."""
    decision = decide(reported_amps=0, zero_samples=1)
    assert decision.state is TeslaAmpState.IDLE
    assert decision.credit_amps == 0.0
    assert decision.clear_command is True
    assert decision.zero_samples == 0
    assert decision.reason == "stop_confirmed"


def test_zero_threshold_is_configurable() -> None:
    """The classifier reads the threshold it is given, not a literal 2."""
    decision = decide(
        reported_amps=0, zero_samples=1, zero_clear_samples=3
    )
    assert decision.state is TeslaAmpState.AWAITING_STOP
    assert decision.zero_samples == 2
    assert decision.clear_command is False


def test_default_zero_threshold_is_the_shipped_constant() -> None:
    """The default threshold is TESLA_ZERO_AMPS_CLEAR_SAMPLES."""
    assert TESLA_ZERO_AMPS_CLEAR_SAMPLES == 2
    assert decide(reported_amps=0, zero_samples=1).clear_command is True


def test_matched_report_is_confirmed() -> None:
    """A report equal to the command is a confirmation."""
    decision = decide(reported_amps=18)
    assert decision.state is TeslaAmpState.CONFIRMED
    assert decision.credit_amps == 0.0
    assert decision.clear_command is False
    assert decision.zero_samples == 0
    assert decision.reason == "confirmed"


def test_matched_one_amp_command_is_confirmed_not_stale() -> None:
    """A 1 A command matched by 1 A is confirmation, even past the window."""
    decision = decide(
        commanded_amps=1,
        reported_amps=1,
        command_window_open=False,
        has_settle=True,
        settle_open=False,
    )
    assert decision.state is TeslaAmpState.CONFIRMED
    assert decision.clear_command is False


def test_one_amp_inside_the_window_credits_the_unconfirmed_portion() -> None:
    """A 1 A report during ramp-up credits commanded - 1."""
    decision = decide(reported_amps=1, command_window_open=True)
    assert decision.state is TeslaAmpState.RAMPING
    assert decision.credit_amps == 17.0
    assert decision.clear_command is False
    assert decision.zero_samples == 0
    assert decision.reason == "window_open"


def test_one_amp_outside_the_window_is_stale() -> None:
    """A 1 A report after the window is a failed command."""
    decision = decide(reported_amps=1, command_window_open=False)
    assert decision.state is TeslaAmpState.EXPIRED
    assert decision.credit_amps == 0.0
    assert decision.clear_command is True
    assert decision.zero_samples == 0
    assert decision.reason == "ramp_stale"


def test_delta_outside_every_settle_window_expires() -> None:
    """A directed settle effect plus closed windows clears a stale delta."""
    decision = decide(
        reported_amps=10,
        has_settle=True,
        settle_open=False,
    )
    assert decision.state is TeslaAmpState.EXPIRED
    assert decision.clear_command is True
    assert decision.reason == "settle_expired"


def test_delta_inside_a_settle_window_credits_the_difference() -> None:
    """An open settle window keeps the unconfirmed delta."""
    decision = decide(
        reported_amps=10,
        has_settle=True,
        settle_open=True,
    )
    assert decision.state is TeslaAmpState.SETTLING
    assert decision.credit_amps == 8.0
    assert decision.clear_command is False
    assert decision.reason == "settle_open"


def test_delta_without_a_directed_settle_effect_never_expires() -> None:
    """No directed set_amps effect means the expiry gate stays inert."""
    decision = decide(
        reported_amps=10,
        has_settle=False,
        settle_open=False,
    )
    assert decision.state is TeslaAmpState.SETTLING
    assert decision.credit_amps == 8.0
    assert decision.clear_command is False


def test_nonzero_report_resets_the_zero_counter() -> None:
    """Any non-zero report clears the accumulated zero confirmation."""
    decision = decide(reported_amps=10, zero_samples=1, settle_open=True)
    assert decision.zero_samples == 0


def test_negative_delta_is_signed() -> None:
    """A report above the command credits negative Wh, as before."""
    decision = decide(commanded_amps=10, reported_amps=15, settle_open=True)
    assert decision.state is TeslaAmpState.SETTLING
    assert decision.credit_amps == -5.0


@pytest.mark.parametrize(
    ("state", "value"),
    [
        (TeslaAmpState.IDLE, "idle"),
        (TeslaAmpState.AWAITING_REPORT, "awaiting_report"),
        (TeslaAmpState.AWAITING_STOP, "awaiting_stop"),
        (TeslaAmpState.RAMPING, "ramping"),
        (TeslaAmpState.SETTLING, "settling"),
        (TeslaAmpState.CONFIRMED, "confirmed"),
        (TeslaAmpState.EXPIRED, "expired"),
    ],
)
def test_state_values_serialize_as_strings(
    state: TeslaAmpState, value: str
) -> None:
    """The enum values are the log and test strings."""
    assert state == value
    assert isinstance(state, str)


# === Tracker integration ===


def _effect(age_secs: int = 10, direction: str | None = "increase") -> PendingEffect:
    """Build a Tesla set_amps effect aged by the wall clock."""
    return PendingEffect(
        device_name="tesla",
        action="set_amps",
        timestamp=NOW - timedelta(seconds=age_secs),
        data_point_at=NOW - timedelta(seconds=age_secs),
        power_watts=0.0,
        target_amps=18,
        direction=direction,  # type: ignore[arg-type]
        qh_name="QH1",
    )


def _tracker(*effects: PendingEffect) -> TeslaSettleTracker:
    """Build a tracker over the given effects with a 60 s window."""
    store = EffectStore()
    for item in effects:
        store.add(item)
    return TeslaSettleTracker(store, window_secs=60)


def test_classify_is_read_only() -> None:
    """Classify reports the decision and writes nothing."""
    t = _tracker(_effect(age_secs=10))
    t.record_command(18)
    first = t.classify(10, NOW)
    second = t.classify(10, NOW)
    assert first == second
    assert first.state is TeslaAmpState.SETTLING
    assert first.reason == "settle_open"
    assert t.last_commanded_amps == 18
    assert t._zero_amp_samples == 0


def test_classify_awaits_a_report_without_moving_the_counter() -> None:
    """A missing report is a named state, not an early return."""
    t = _tracker()
    t.record_command(18)
    decision = t.classify(None, NOW)
    assert decision.state is TeslaAmpState.AWAITING_REPORT


def test_classify_counts_zero_frames() -> None:
    """The tracker feeds its applied counter into the classifier."""
    t = _tracker()
    t.record_command(18)
    t.inflight_wh(0, 900, NOW)
    assert t._zero_amp_samples == 1
    assert t.classify(0, NOW).state is TeslaAmpState.AWAITING_STOP
    t.inflight_wh(0, 900, NOW)
    assert t.last_commanded_amps is None


def test_classify_reports_ramp_and_stale() -> None:
    """The command window decides between ramp and stale."""
    fresh = _tracker(_effect(age_secs=10))
    fresh.record_command(18)
    assert fresh.classify(1, NOW).state is TeslaAmpState.RAMPING
    stale = _tracker(_effect(age_secs=300))
    stale.record_command(18)
    assert stale.classify(1, NOW).state is TeslaAmpState.EXPIRED
    assert stale.classify(1, NOW).reason == "ramp_stale"


def test_classify_reports_settle_expiry() -> None:
    """A closed settle window names the expiry reason."""
    t = _tracker(_effect(age_secs=300))
    t.record_command(18)
    decision = t.classify(10, NOW)
    assert decision.state is TeslaAmpState.EXPIRED
    assert decision.reason == "settle_expired"
