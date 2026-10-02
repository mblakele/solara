"""Regression: a calm marine-layer morning must not report excessive jitter.

``bugs/2026-10-02-sunrise-marine-layer-jitter.log`` — 08:08-08:23 PDT, 53-76
minutes after sunrise, thick marine layer, even grey light. Load management
reported ``reason="excessive_jitter"`` (index: ``⚠ jitter detected``) 35 times
while zero turn-on actions existed to protect, and the churn it measured was
below the quarter's own realized forecast error. Two mechanisms:

* **cluster A** (QH 15:00, 19 firings, churn 0.0147-0.0468 Wh/s): the guard is
  ``churn * R >= gap``, i.e. ``churn >= gap / R``, and here that threshold was
  0.0091-0.0124 Wh/s — *below* the same quarter's realized ``sigma_rate`` of
  0.0222 Wh/s. Any quantization step of ``prediction_w`` tripped it. Fixed by
  ``JITTER_FLOOR_WH_PER_S``.

* **cluster B** (QH 15:15, 16 firings, churn 0.7714 decaying to 0.0917): the
  quarter-opening prediction was 96 % extrapolation
  (``raw 1.779 + 0.057483 * 869 = +51.73 Wh``) and one sign flip of
  ``prediction_w`` — our own ``jackery`` ``turn_off`` seen through that window —
  moved the gap 69.42 Wh in a single cycle, seeding churn at 1.10 Wh/s which
  then decayed x0.7 per update for five minutes. Fixed by
  ``JITTER_MAX_REMAINING_SECS`` (the churn window waits for observed data),
  with the floor catching the tail.

Every tuple below is copied verbatim from the log's ``gapminder_jitter_guard``
lines, so these are exactly the inputs production judged. ``seconds_remaining``
in that log is data-derived (``qh_start + 900 - R``), which is what lets
``cluster_b`` be replayed through the real tracker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from constants import (
    JITTER_FLOOR_WH_PER_S,
    JITTER_HORIZON_SECS,
    JITTER_MAX_REMAINING_SECS,
)
from gap_trend import GapTrendTracker
from load_nbc import GapMinder

# QH 15:00-15:15 — 19 guard firings, churn never above 0.0468 Wh/s.
# (gap_wh, jitter_wh_per_s, seconds_remaining)
CLUSTER_A: list[tuple[float, float, int]] = [
    (6.070921250000001, 0.01973691583333336, 664),
    (6.070921250000001, 0.01973691583333336, 656),
    (7.779641250000001, 0.014728545250000023, 629),
    (6.149165416666667, 0.04370194000833335, 599),
    (6.141827083333332, 0.046822733005833325, 569),
    (6.177118166666665, 0.03320220727075, 539),
    (6.177118166666665, 0.03320220727075, 509),
    (6.177118166666665, 0.03320220727075, 501),
    (6.177118166666665, 0.03320220727075, 493),
    (6.221579333333333, 0.023372150089524982, 484),
    (6.221579333333333, 0.023372150089524982, 476),
    (6.213562583333335, 0.016662978396000803, 449),
    (7.421440916666668, 0.02382303571053387, 419),
    (7.398691583333331, 0.02898240166404041, 389),
    (6.367742083333333, 0.030369682831494896, 359),
    (6.367742083333333, 0.030369682831494896, 329),
    (6.367742083333333, 0.030369682831494896, 321),
    (6.444381833333331, 0.031951471732046405, 304),
    (6.444381833333331, 0.031951471732046405, 296),
]

# QH 15:15-15:30 — 16 guard firings, churn 0.7714 decaying to 0.0917.
# Ordered oldest first; ``seconds_remaining`` doubles as the tracker key
# (data_point_at = QH start + 900 - R).
CLUSTER_B: list[tuple[float, float, int]] = [
    (8.69158766666667, 0.7713728861111112, 719),
    (3.070155416666662, 0.596175342777778, 689),
    (3.0693165833333342, 0.4735286741111113, 659),
    (3.0693165833333342, 0.4735286741111113, 629),
    (3.0693165833333342, 0.4735286741111113, 621),
    (3.0693165833333342, 0.4735286741111113, 613),
    (3.148417666666667, 0.3318739656277778, 604),
    (3.148417666666667, 0.3318739656277778, 596),
    (3.1432808333333337, 0.23275864968944446, 569),
    (4.650065333333336, 0.17805026811594446, 539),
    (3.253265166666665, 0.15367103434782783, 509),
    (3.203901749999998, 0.1210440915434795, 479),
    (3.203901749999998, 0.1210440915434795, 449),
    (3.203901749999998, 0.1210440915434795, 441),
    (4.49591516666667, 0.09168456533043567, 424),
    (4.49591516666667, 0.09168456533043567, 416),
]

QH_1515 = datetime(2026, 10, 2, 15, 15, 0, tzinfo=timezone.utc)


def _engine() -> GapMinder:
    """Guard under test (hysteresis 3, as production ran it in the log)."""
    return GapMinder(hysteresis_wh=3)


def test_cluster_a_would_all_have_fired_under_the_bare_rule() -> None:
    """Pin that cluster A *is* the bug, not an artefact of this table.

    The shipped rule was ``churn * R >= gap`` with no floor: all 19 logged
    triples satisfy it, so all 19 reported ``excessive_jitter``. If this
    assertion fails the extracted numbers no longer describe the log.
    """
    assert len(CLUSTER_A) == 19
    assert all(jitter * seconds >= gap for gap, jitter, seconds in CLUSTER_A)
    # ...and the threshold the bare rule used, gap / R, sat *below* that
    # quarter's own realized forecast error (sigma_rate 0.0222 Wh/s): the
    # guard was firing on quantization, not on swing.
    assert all(gap / seconds < 0.0222 for gap, _, seconds in CLUSTER_A)


def test_calm_marine_layer_quarter_never_reports_excessive_jitter() -> None:
    """Every one of cluster A's 19 firings is now silent.

    Under the floor a churn of 0.0468 Wh/s at R=569 is a 26.6 Wh "swing"
    against a 6.14 Wh surplus — but 0.0468 sits under
    ``JITTER_FLOOR_WH_PER_S`` because it is the forecast window stepping
    between quantization levels, not an oscillating estimate.
    """
    engine = _engine()
    for gap, jitter, seconds in CLUSTER_A:
        assert jitter < JITTER_FLOOR_WH_PER_S, (gap, jitter, seconds)
        assert engine.turn_on_jitter_guard_fires(gap, jitter, seconds) is False


def test_floor_alone_would_not_have_covered_cluster_b() -> None:
    """Prove the readiness gate is load-bearing, not redundant.

    Nine of cluster B's sixteen firings carry churn at or above the floor
    (up to 0.7714 Wh/s), so the floor by itself would have left them
    reported. They are only silenced because the churn never accumulates
    from the quarter-opening extrapolation in the first place.
    """
    over_floor = [t for t in CLUSTER_B if t[1] >= JITTER_FLOOR_WH_PER_S]
    assert len(over_floor) == 9
    assert over_floor[0][1] == 0.7713728861111112
    # Seven of the nine sit before the ready window opens (R >= 604); the
    # other two are the first and second ready samples (R 596, 569), where
    # churn is still unmeasurable — three are needed before it reseeds.
    assert sum(1 for _, _, s in over_floor if s > JITTER_MAX_REMAINING_SECS) == 7
    assert min(seconds for _, _, seconds in over_floor) == 569


def test_quarter_opening_transient_replay_never_fires() -> None:
    """Replay QH 15:15 through the real tracker with the production gate.

    The ready window only starts once ``seconds_remaining`` drops to
    ``JITTER_MAX_REMAINING_SECS``, so the -60.73 -> +8.69 flip cannot seed
    the EWMA; churn reseeds from the settled tail and peaks at 0.0643 —
    a third of the floor. All 16 logged firings come back False.
    """
    engine = _engine()
    tracker = GapTrendTracker()
    observed: list[float] = []
    for gap, _, seconds in CLUSTER_B:
        tracker.update(
            QH_1515 + timedelta(seconds=900 - seconds),
            gap,
            churn_ready=seconds <= JITTER_MAX_REMAINING_SECS,
        )
        churn = tracker.churn_wh_per_s
        observed.append(churn)
        assert (
            engine.turn_on_jitter_guard_fires(gap, churn, seconds) is False
        ), (gap, churn, seconds)

    # Cycles through R=569 are gated outright (no ready sample yet, or
    # fewer than three): churn stays 0.0.
    assert observed[:9] == [0.0] * 9
    # The settled tail reseeds small and stays well under the floor.
    assert max(observed) == 0.0643273288271606
    assert max(observed) < JITTER_FLOOR_WH_PER_S


def test_incident_still_fires() -> None:
    """The guard keeps its teeth: the 2026-10-01 overshoot still trips it.

    ``bugs/2026-10-01-tesla-overshoot.log`` c579 raised the Tesla by 1 A
    into an oscillating gap; the replay pins the guard firing there with
    churn around 0.80 Wh/s at R=111. Both gates are far below that.
    """
    assert JITTER_FLOOR_WH_PER_S == 0.2
    assert JITTER_HORIZON_SECS == 300
    assert JITTER_MAX_REMAINING_SECS == 600
    # R below the horizon cap, churn four times the floor.
    assert _engine().turn_on_jitter_guard_fires(7.688, 0.7986, 111) is True
