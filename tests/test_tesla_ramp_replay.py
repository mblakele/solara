"""Incident replay: bugs/2026-09-26-tesla-stop-charging.log c36→c55.

Steps the turn_off deficits from the log through GapTrendTracker +
TeslaDecider and asserts the outcome difference:
- static logic (no trend) defers at every step including c54 — this is
  the reported bug (manager never stopped; user stopped manually at c55);
- ramp logic defers until c54 (R=149) and stops there, landing ~-13.5 Wh
  (miss 4.5) instead of the static ~+7.6 Wh (miss 16.6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from load_models import TeslaState
from load_nbc import DecideContext, StateTracker, TeslaDecider
from gap_trend import GapTrendTracker

# (seconds_remaining, turn_off deficit Wh) from the log, c36 → c54.
INCIDENT_POINTS: list[tuple[int, float]] = [
    (419, 17.7),
    (389, 36.8),
    (359, 45.3),
    (329, 45.3),
    (321, 45.3),
    (304, 54.3),
    (296, 54.3),
    (269, 54.3),
    (261, 54.3),
    (244, 43.6),
    (236, 43.6),
    (209, 43.6),
    (201, 43.6),
    (184, 38.3),
    (176, 38.3),
    (149, 45.1),
]

BASE = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
TARGET_WH = -9
# Predicted Wh at c54 and c55 from the log (for landed estimates).
PREDICTED_C54 = 36.1
PREDICTED_C55 = 47.3
TESLA_5A_WH_PER_S = 5 * 240 / 3600


def _tesla_5a() -> TeslaState:
    return TeslaState(
        is_charging=True, current_amps=5, plugged_in=True, at_home=True
    )


def _replay(use_trend: bool) -> list:
    """Run the incident sequence; return one decide_reduce result per step."""
    decider = TeslaDecider()
    tracker = GapTrendTracker()
    results = []
    for i, (remaining, gap) in enumerate(INCIDENT_POINTS):
        data_point_at = BASE + timedelta(seconds=30 * i)
        trend: float | None = None
        if use_trend:
            rate, trusted = tracker.update(
                data_point_at, gap, qh_name="QH1",
                noise_floor=3 / remaining,
            )
            trend = rate if trusted else None
        else:
            tracker.update(
                data_point_at, gap, qh_name="QH1",
                noise_floor=3 / remaining,
            )
        ctx = DecideContext(
            now=data_point_at + timedelta(seconds=30),
            seconds_remaining=remaining,
            state=StateTracker(),
            plugs={},
            tesla=_tesla_5a(),
            gap_trend_wh_per_s=trend,
            cycle_secs=30,
        )
        results.append(decider.decide_reduce(ctx, gap))
    return results


def test_static_logic_reproduces_incident() -> None:
    """Without trend input every step defers — the reported bug."""
    results = _replay(use_trend=False)
    assert all(action is None for action in results)


def test_ramp_logic_stops_at_c54() -> None:
    """With trend input only the last step (R=149) stops."""
    results = _replay(use_trend=True)
    assert all(action is None for action in results[:-1])
    final = results[-1]
    assert final is not None
    assert final.device_name == "tesla"
    assert final.action == "turn_off"


def test_ramp_landing_beats_static_landing() -> None:
    """Stopping at c54 (R=149) misses by ~4.5 Wh vs ~16.6 Wh at c55."""
    ramp_landed = PREDICTED_C54 - 149 * TESLA_5A_WH_PER_S
    static_landed = PREDICTED_C55 - 119 * TESLA_5A_WH_PER_S
    assert abs(ramp_landed - TARGET_WH) < 5.0
    assert abs(static_landed - TARGET_WH) > 10.0
    assert abs(ramp_landed - TARGET_WH) < abs(static_landed - TARGET_WH)
