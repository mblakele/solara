"""Tests for PendingEffect factories and TeslaDecider (from GapMinder)."""

import logging
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from load_models import PlugConfig, TeslaState
from load_nbc import (
    DecideContext,
    GapMinder,
    StateTracker,
    TeslaDecider,
    make_plug_effect,
    make_tesla_set_amps,
    make_tesla_stop,
)

FIXED_NOW = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


def _ctx(**overrides) -> DecideContext:
    """Build a DecideContext with a charging Tesla and no plugs by default."""
    base = {
        "now": FIXED_NOW,
        "seconds_remaining": 900,
        "state": StateTracker(),
        "plugs": {},
        "tesla": TeslaState(
            is_charging=True, current_amps=10, plugged_in=True, at_home=True
        ),
    }
    base.update(overrides)
    return DecideContext(**base)  # type: ignore[arg-type]


def test_make_plug_effect_signs():
    """turn_on is positive load, turn_off is negative (signed invariant)."""
    on = make_plug_effect("heater", "turn_on", 1000.0, FIXED_NOW)
    assert on.power_watts == 1000.0
    assert on.timestamp == FIXED_NOW
    assert on.data_point_at == FIXED_NOW
    off = make_plug_effect(
        "heater", "turn_off", 1000.0, FIXED_NOW, data_point_at=FIXED_NOW
    )
    assert off.power_watts == -1000.0


def test_make_plug_effect_rejects_bad_action():
    """Unknown actions raise instead of silently building an effect."""
    with pytest.raises(ValueError):
        make_plug_effect("heater", "blink", 1000.0, FIXED_NOW)  # type: ignore[arg-type]


def test_make_tesla_set_amps_and_stop():
    """Tesla factories set device/action/amps with volt-derived watts."""
    up = make_tesla_set_amps(16, 6, FIXED_NOW)
    assert up.device_name == "tesla"
    assert up.action == "set_amps"
    assert up.target_amps == 16
    assert up.power_watts > 0
    stop = make_tesla_stop(10, FIXED_NOW)
    assert stop.action == "turn_off"
    assert stop.power_watts < 0


def test_decider_increase_matches_gapminder():
    """TeslaDecider.decide_increase agrees with GapMinder._decide_tesla_amps."""
    engine = GapMinder(hysteresis_wh=3)
    ctx = _ctx()
    assert (
        engine.tesla_decider.decide_increase(ctx, 200.0)
        == engine._decide_tesla_amps(ctx, 200.0)
    )


def test_decider_reduce_matches_gapminder():
    """TeslaDecider.decide_reduce agrees with GapMinder._decide_tesla_reduce."""
    engine = GapMinder(hysteresis_wh=3)
    ctx = _ctx()
    assert (
        engine.tesla_decider.decide_reduce(ctx, 200.0, stop_allowed=False)
        == engine._decide_tesla_reduce(ctx, 200.0, stop_allowed=False)
    )


def test_decider_clamps_to_config_max():
    """Decider clamps target amps to its configured max."""
    decider = TeslaDecider(charge_amps_min=5, charge_amps_max=24)
    assert decider.charge_amps_max == 24
    action = decider.decide_increase(_ctx(), 10000.0)
    assert action is not None
    assert action.target_amps is not None
    assert action.target_amps <= 24


def test_clamped_increase_reports_actual_amp_delta():
    """A clamped increase reports the delta actually commanded.

    The requested gap asks for far more than the 10 A ceiling allows.
    ``power_watts`` must reflect the real 5 A increase (5 → 10), not the
    unclamped request, because the priority path subtracts this delta from
    the remaining gap.
    """
    ctx = replace(
        _ctx(),
        tesla=TeslaState(
            is_charging=True, current_amps=5, plugged_in=True, at_home=True
        ),
    )
    decider = TeslaDecider(charge_amps_min=5, charge_amps_max=10)
    action = decider.decide_increase(ctx, 10000.0)
    assert action is not None
    assert action.target_amps == 10
    assert action.power_watts == pytest.approx(StateTracker.amps_to_watts(5))


def test_gapminder_owns_configured_decider():
    """GapMinder wires its amp limits into TeslaDecider."""
    engine = GapMinder(charge_amps_min=6, charge_amps_max=32)
    assert engine.charge_amps_min == 6
    assert engine.tesla_decider.charge_amps_min == 6
    assert engine.tesla_decider.charge_amps_max == 32


def test_reduce_rounds_up_to_the_step_that_covers_the_gap():
    """A ``ceil``, not a floor: any gap above zero books a whole quantum.

    At R=600 s and 240 V one amp is 40 Wh, so a 20 Wh deficit takes a 1-amp
    step (20 Wh of overshoot) and a 100 Wh deficit takes 3 (2.5 quanta,
    rounded up). The floor that would decline or undershoot that step was
    measured and rejected (``tools/estimator_replay.py --amp-floor``,
    removed 2026-10-18; verdict in ``AGENTS.md``).
    """
    twenty = _ctx(seconds_remaining=600)
    hundred = _ctx(seconds_remaining=600)
    decider = GapMinder(hysteresis_wh=3).tesla_decider
    assert decider.decide_reduce(twenty, 20.0).target_amps == 9
    assert decider.decide_reduce(hundred, 100.0).target_amps == 7


def test_eligible_plugs_turn_on_order():
    """_eligible_plugs(want_on=True) returns off-plugs, highest priority first."""
    state = StateTracker()
    plugs = {
        "low": PlugConfig(name="low", accessory_id="a", power_watts=500.0, priority=1),
        "high": PlugConfig(name="high", accessory_id="b", power_watts=500.0, priority=9),
    }
    engine = GapMinder()
    ctx = _ctx(state=state, plugs=plugs, tesla=None)
    names = [name for _, name, _ in engine._eligible_plugs(ctx, want_on=True)]
    assert names == ["high", "low"]


def test_eligible_plugs_turn_off_order():
    """_eligible_plugs(want_on=False) returns on-plugs, lowest priority first."""
    from load_models import DeviceState

    state = StateTracker()
    plugs = {
        "low": PlugConfig(name="low", accessory_id="a", power_watts=500.0, priority=1),
        "high": PlugConfig(name="high", accessory_id="b", power_watts=500.0, priority=9),
    }
    state.set_device_state("low", DeviceState(name="low", desired_state=True))
    state.set_device_state("high", DeviceState(name="high", desired_state=True))
    engine = GapMinder()
    ctx = _ctx(state=state, plugs=plugs, tesla=None)
    names = [name for _, name, _ in engine._eligible_plugs(ctx, want_on=False)]
    assert names == ["low", "high"]


def _min_amps_ctx(seconds_remaining, reduce_trend=None, cycle_secs=30):
    """DecideContext with Tesla at minimum amps (stop-or-defer branch)."""
    return _ctx(
        seconds_remaining=seconds_remaining,
        tesla=TeslaState(
            is_charging=True, current_amps=5, plugged_in=True, at_home=True
        ),
        gap_trend_wh_per_s=reduce_trend,
        cycle_secs=cycle_secs,
    )


class TestRampAwareStop:
    """Ramp-adjusted Tesla stop (bugs/2026-09-26-tesla-stop-charging.log).

    Static safe_defer assumes a frozen prediction; on a sustained rising
    deficit the exact-hit stop time falls before the next cycle, so the
    decider stops one cycle early. Flat/unknown trends keep static
    behavior byte-for-byte.
    """

    def test_ramp_stops_one_cycle_early(self):
        """c54: R=149, gap=45.1, trend=+0.25 → stop (static defers)."""
        decider = TeslaDecider(ramp_aware=True)
        action = decider.decide_reduce(_min_amps_ctx(149, 0.25), 45.1)
        assert action is not None
        assert action.device_name == "tesla"
        assert action.action == "turn_off"

    def test_flat_none_trend_defers_parity(self):
        """Same c54 numbers with no trend → defer (static parity)."""
        decider = TeslaDecider(ramp_aware=True)
        assert decider.decide_reduce(_min_amps_ctx(149), 45.1) is None

    def test_zero_trend_defers(self):
        """Explicit zero trend falls back to the static rule."""
        decider = TeslaDecider(ramp_aware=True)
        assert decider.decide_reduce(_min_amps_ctx(149, 0.0), 45.1) is None

    def test_negative_trend_defers(self):
        """Shrinking deficit never hastens a stop."""
        decider = TeslaDecider(ramp_aware=True)
        assert decider.decide_reduce(_min_amps_ctx(149, -0.25), 45.1) is None

    def test_trend_beyond_next_cycle_defers(self):
        """c52: R=184, gap=38.3, trend=+0.25 → t*≈39s > 30s cadence."""
        decider = TeslaDecider(ramp_aware=True)
        assert decider.decide_reduce(_min_amps_ctx(184, 0.25), 38.3) is None

    def test_huge_trend_clamped(self):
        """trend=+1.0 is clamped to P/2: t*≈33s still defers."""
        decider = TeslaDecider(ramp_aware=True)
        assert decider.decide_reduce(_min_amps_ctx(200, 1.0), 50.0) is None

    def test_ramp_stop_inside_static_window(self):
        """Ramp never delays: R=119, gap=56.3 stops with or without trend."""
        decider = TeslaDecider(ramp_aware=True)
        assert decider.decide_reduce(_min_amps_ctx(119, 0.25), 56.3) is not None
        assert decider.decide_reduce(_min_amps_ctx(119), 56.3) is not None

    def test_defer_stop_off_stops_instead_of_deferring(self):
        """With defer off, a no-trend case that would defer now stops."""
        ctx = _min_amps_ctx(149)  # no trend -> static defers
        shipped = GapMinder(hysteresis_wh=3).tesla_decider.decide_reduce(ctx, 45.1)
        undeferred = GapMinder(
            hysteresis_wh=3, defer_stop=False
        ).tesla_decider.decide_reduce(ctx, 45.1)
        assert shipped is None
        assert undeferred is not None
        assert undeferred.action == "turn_off"

    def test_defer_stop_on_is_the_shipped_behavior(self):
        """Flag-on parity with a decider built without the knob."""
        ctx = _min_amps_ctx(149)
        default = GapMinder(hysteresis_wh=3).tesla_decider.decide_reduce(ctx, 45.1)
        on = GapMinder(hysteresis_wh=3, defer_stop=True).tesla_decider.decide_reduce(
            ctx, 45.1
        )
        assert on == default

    def test_defer_stop_direct_decider(self):
        """TeslaDecider honors defer_stop directly."""
        ctx = _min_amps_ctx(149)
        assert TeslaDecider(defer_stop=False).decide_reduce(ctx, 45.1) is not None
        assert TeslaDecider(defer_stop=True).decide_reduce(ctx, 45.1) is None


class TestDeferQuantumLookahead:
    """A defer must be covered by at least one observation window.

    bugs/2026-10-18-tesla-stop-overshoot.log: at ``R=89 s`` against a 24.0 Wh
    gap the static rule deferred (``safe_defer = 24 / 0.3333 = 72 s``) and the
    next decision landed at ``R=60 s`` — past the point where the shed could
    still close the gap. A defer whose deadline is closer than one
    quantisation window is not a defer: the next observation cannot arrive in
    time to act on it. The lookahead is counted in windows, never in literal
    seconds, so it tracks the deployment's real quantum.
    """

    GAP_WH = 24.0
    """Gap whose static safe-defer window is exactly 72 s."""

    WINDOW = 30
    """Observability window (one quantisation quantum)."""

    def _ctx(self, seconds_remaining, window=WINDOW):
        """A min-amps context carrying the observability window."""
        return replace(_min_amps_ctx(seconds_remaining), observability_secs=window)

    def test_static_rule_defers_the_incident_cycle(self):
        """Control: with the lookahead off the incident cycle still defers."""
        assert (
            TeslaDecider(defer_lookahead_quanta=0).decide_reduce(
                self._ctx(89), self.GAP_WH
            )
            is None
        )

    def test_shipped_rule_stops_the_incident_cycle(self):
        """Default construction stops it: 89 < 72 + one 30 s window."""
        action = TeslaDecider().decide_reduce(self._ctx(89), self.GAP_WH)
        assert action is not None
        assert action.action == "turn_off"

    def test_one_quantum_lookahead_stops_the_incident_cycle(self):
        """R=89, safe_defer=72 + one 30 s window → stop (89 < 102)."""
        action = TeslaDecider(defer_lookahead_quanta=1).decide_reduce(
            self._ctx(89), self.GAP_WH
        )
        assert action is not None
        assert action.device_name == "tesla"
        assert action.action == "turn_off"

    def test_deadline_equal_to_one_window_still_stops(self):
        """The test is strict (>) so R == safe_defer + window stops."""
        action = TeslaDecider(defer_lookahead_quanta=1).decide_reduce(
            self._ctx(102), self.GAP_WH
        )
        assert action is not None

    def test_one_second_past_the_deadline_defers(self):
        """R=103 leaves more than one window: still a real defer."""
        assert (
            TeslaDecider(defer_lookahead_quanta=1).decide_reduce(
                self._ctx(103), self.GAP_WH
            )
            is None
        )

    def test_without_an_observability_window_the_lookahead_is_inert(self):
        """Unknown quantum falls back to shipped behaviour, never guesses."""
        assert (
            TeslaDecider(defer_lookahead_quanta=1).decide_reduce(
                self._ctx(89, window=None), self.GAP_WH
            )
            is None
        )

    def test_two_quanta_extend_the_deadline(self):
        """quanta=2 buys 60 s: R=132 stops, R=133 defers."""
        decider = TeslaDecider(defer_lookahead_quanta=2)
        assert decider.decide_reduce(self._ctx(132), self.GAP_WH) is not None
        assert decider.decide_reduce(self._ctx(133), self.GAP_WH) is None

    def test_deferral_log_names_the_lookahead(self, caplog):
        """A deferral reports the window it weighed, appended last."""
        caplog.set_level(logging.INFO, logger="load_nbc")
        assert (
            TeslaDecider(defer_lookahead_quanta=1).decide_reduce(
                self._ctx(103), self.GAP_WH
            )
            is None
        )
        assert "safe_defer=72s, gap=24.0 Wh" in caplog.text
        assert "defer_lookahead=30s" in caplog.text

    def test_static_log_reports_zero_lookahead(self, caplog):
        """The knob-off line keeps the parser-visible fields adjacent."""
        caplog.set_level(logging.INFO, logger="load_nbc")
        decider = TeslaDecider(defer_lookahead_quanta=0)
        assert decider.decide_reduce(self._ctx(89), self.GAP_WH) is None
        assert "safe_defer=72s, gap=24.0 Wh" in caplog.text
        assert "defer_lookahead=0s" in caplog.text

    def test_gapminder_forwards_the_knob(self):
        """The engine's opt-in knob reaches the decider it owns."""
        assert GapMinder(defer_lookahead_quanta=2).tesla_decider.defer_lookahead_quanta == 2

    def test_gapminder_default_is_shipped(self):
        """Default construction ships the one-window rule."""
        assert GapMinder().tesla_decider.defer_lookahead_quanta == 1


class TestDeferHonoursTheGapDeadline:
    """The stop deadline comes from the gap, not from a fixed buffer.

    ``safe_defer = min(cap, gap / P)`` with ``P = 1200/3600 Wh/s`` puts the
    stop at ``R = 3 * gap`` — the second at which a 5 A draw has consumed
    exactly the gap. A 120 s cap overrode that deadline for every gap above
    50 Wh, so the car ran to ``R = 150 s`` instead: the 2026-10-20 18:52
    cycle deferred at ``R=479, gap=155.0`` (deadline 465 s), and letting the
    car run to the cap cost about 105 Wh of avoidable draw
    (``corpus/2026-10-20-0006b7dea172f40756dc9c0fd354ae5b.log``; the user's
    hand stop landed that quarter 10.9 Wh from target). The cap now sits at
    a whole window, so the gap deadline governs — and a gap the window can
    never pay is stopped immediately rather than deferred to the edge.
    """

    def test_stop_fires_at_the_gap_deadline(self):
        """R=479 against 155.0 Wh: 465 s + one 30 s window is due."""
        ctx = replace(_min_amps_ctx(479), observability_secs=30)
        action = TeslaDecider().decide_reduce(ctx, 155.0)
        assert action is not None
        assert action.device_name == "tesla"
        assert action.action == "turn_off"

    def test_the_cycle_before_the_deadline_still_defers(self):
        """R=496 keeps one second of headroom over 465 s + 30 s."""
        ctx = replace(_min_amps_ctx(496), observability_secs=30)
        assert TeslaDecider().decide_reduce(ctx, 155.0) is None

    def test_gap_the_window_cannot_pay_stops_now(self):
        """R=686 against 345.5 Wh: no later stop can close it, so act now."""
        action = TeslaDecider().decide_reduce(_min_amps_ctx(686), 345.5)
        assert action is not None
        assert action.action == "turn_off"

    def test_2026_10_20_incident_cycle_still_defers(self):
        """Parity with the one engine stop: R=119, gap=29.4, one window."""
        ctx = replace(_min_amps_ctx(119), observability_secs=30)
        assert TeslaDecider().decide_reduce(ctx, 29.4) is None
        assert TeslaDecider().decide_reduce(
            replace(_min_amps_ctx(110), observability_secs=30), 29.4
        ) is not None

    def test_safe_defer_is_gap_derived(self):
        """155 Wh buys 465 s; 29.4 Wh keeps the log's 88 s truncation."""
        decider = TeslaDecider()
        assert decider.safe_defer_secs(155.0) == 465
        assert decider.safe_defer_secs(29.4) == 88

    def test_cap_is_a_whole_window(self):
        """A gap beyond 300 Wh defers to nothing — the cap is the window."""
        assert TeslaDecider().safe_defer_secs(400.0) == 900

    def test_explicit_cap_still_reproduces_the_old_arm(self):
        """The diagnostic knob keeps the pre-change deadline reachable."""
        assert TeslaDecider(max_defer_secs=120).safe_defer_secs(155.0) == 120
