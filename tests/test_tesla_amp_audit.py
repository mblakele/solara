"""Audit logging and read-only state for the Tesla amp lifecycle.

Subtask 4 of the tesla-amp-lifecycle plan.  Tests verify that:

* ``inflight_wh()`` logs one DEBUG line per state change, with the state,
  command, report, counter, and reason.
* No log line fires when the state does not change across calls.
* ``StateTracker.tesla_amp_state()`` returns the same decision as
  ``TeslaSettleTracker.classify()`` for the same inputs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from load_models import PendingEffect, TeslaAmpState
from load_nbc import EffectStore, StateTracker, TeslaSettleTracker

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _tracker(*effects) -> TeslaSettleTracker:
    """Build a tracker over the given effects with a 60 s window."""
    store = EffectStore()
    for item in effects:
        store.add(item)
    return TeslaSettleTracker(store, window_secs=60)


def _state_tracker(*effects) -> StateTracker:
    """Build a StateTracker and populate its settle tracker with effects."""
    st = StateTracker(prediction_window_seconds=60)
    for item in effects:
        st._effects.add(item)
    return st


# ── Audit logging ────────────────────────────────────────────────────────


def test_inflight_wh_logs_a_state_change(caplog):
    """A new state transition emits one debug record."""
    caplog.set_level(logging.DEBUG, logger="load_nbc")
    t = _tracker()
    t.record_command(18)

    # First call: AWAITING_REPORT (no report yet → awaiting_report)
    t.inflight_wh(None, 900, NOW)
    assert len([r for r in caplog.records if r.levelname == "DEBUG"
                and "[tesla_amp]" in r.getMessage()]) >= 1

    # Second call: still AWAITING_REPORT — no new log.
    t.inflight_wh(None, 900, NOW)
    assert len([r for r in caplog.records if r.levelname == "DEBUG"
                and "[tesla_amp]" in r.getMessage()]) == 1


def test_inflight_wh_logs_each_transition(caplog):
    """Every new state transition emits its own debug record."""
    caplog.set_level(logging.DEBUG, logger="load_nbc")
    t = _tracker()
    t.record_command(18)

    # AWAITING_REPORT → AWAITING_STOP (first zero frame).
    t.inflight_wh(0, 900, NOW)
    first_msg = caplog.messages[0]
    assert "awaiting_stop" in first_msg
    assert "cmd=18" in first_msg
    assert "reported=0" in first_msg

    # Second zero → IDLE (stop confirmed).
    t.inflight_wh(0, 900, NOW)
    second_msg = caplog.messages[1]
    assert "idle" in second_msg
    assert "reason=stop_confirmed" in second_msg


def test_inflight_wh_logs_the_expired_command(caplog):
    """A clearing transition logs the command that just expired."""
    caplog.set_level(logging.DEBUG, logger="load_nbc")
    ts = NOW - timedelta(seconds=300)
    effect = PendingEffect(
        device_name="tesla",
        action="set_amps",
        timestamp=ts,
        data_point_at=ts,
        power_watts=0.0,
        target_amps=18,
        direction="increase",
        qh_name="QH1",
    )
    t = _tracker(effect)
    t.record_command(18)

    t.inflight_wh(10, 900, NOW)

    messages = [r.getMessage() for r in caplog.records
                if "[tesla_amp]" in r.getMessage()]
    assert messages
    assert "cmd=18" in messages[-1]
    assert "reason=settle_expired" in messages[-1]


def test_inflight_wh_does_not_log_same_state_twice(caplog):
    """Repeated zero frames at the threshold do not re-log the same state."""
    caplog.set_level(logging.DEBUG, logger="load_nbc")
    t = _tracker()
    t.record_command(18)

    # Reach IDLE via stop_confirmed.
    t.inflight_wh(0, 900, NOW)
    t.inflight_wh(0, 900, NOW)
    first_log_count = len([r for r in caplog.records
                           if "[tesla_amp]" in r.getMessage()])

    # Now with no command the state is IDLE — calling again should not log.
    t.last_commanded_amps = None
    t.inflight_wh(None, 900, NOW)
    assert len([r for r in caplog.records
                if "[tesla_amp]" in r.getMessage()]) == first_log_count


# ── Read-only state API ──────────────────────────────────────────────────


def test_tesla_amp_state_returns_classify_result():
    """StateTracker.tesla_amp_state() mirrors classify()."""
    st = _state_tracker()
    st.record_tesla_amp_command(18)

    decision = st.tesla_amp_state(NOW)
    classify = st.settle.classify(None, NOW)
    assert decision.state == classify.state
    assert decision.credit_amps == classify.credit_amps
    assert decision.clear_command == classify.clear_command
    assert decision.reason == classify.reason


def test_tesla_amp_state_with_data_point():
    """tesla_amp_state() with data_point_at mirrors classify()."""
    st = _state_tracker()
    st.record_tesla_amp_command(18)

    decision = st.tesla_amp_state(NOW, data_point_at=NOW)
    classify = st.settle.classify(None, NOW, data_point_at=NOW)
    assert decision.state == classify.state
    assert decision.credit_amps == classify.credit_amps


def test_tesla_amp_state_accepts_a_report():
    """tesla_amp_state() can classify an actual amp report."""
    st = _state_tracker()
    st.record_tesla_amp_command(18)

    decision = st.tesla_amp_state(NOW, reported_amps=10)

    assert decision.state is TeslaAmpState.SETTLING
    assert decision.credit_amps == 8.0


def test_tesla_amp_state_idle():
    """No command → IDLE."""
    st = _state_tracker()
    decision = st.tesla_amp_state(NOW)
    assert decision.state is TeslaAmpState.IDLE
