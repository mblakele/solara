"""Retune pin: bugs/2026-10-09-undershoot.log guards vs c579 incident.

The 2026-10-09 morning undershot both quarters (miss -14.3 / -17.7 Wh
against target -9) while the jitter guard fired 17 times on churn
0.20-0.45 Wh/s against 20-37 Wh gaps (swing/gap ratios 1.5-3.9 at the
300 s horizon). The 2026-10-01 c579 incident the guard was sized for
fires at ratio ~11.5 (churn 0.80, R=111, gap 7.69), so the guard can be
retuned to fraction 2.0 / horizon 150 s and still keep its teeth: every
triple below must stay quiet while c579 still fires.
"""

from __future__ import annotations

from constants import JITTER_GUARD_FRACTION, JITTER_HORIZON_SECS
from load_nbc import GapMinder

# (gap_wh, jitter_wh_per_s, seconds_remaining) copied from the log's
# gapminder_jitter_guard lines (15:36-15:57). Measured at fraction 2.0 /
# horizon 150: four stay quiet; the R=171 churn-0.30 triple still fires
# (swing 45.5 vs 2x20.1=40.3, ratio 2.25) and is pinned separately below.
QUIET_TRIPLES: list[tuple[float, float, int]] = [
    (19.930811250000005, 0.25985032083333337, 479),
    (23.75108, 0.20103213083333335, 471),
    (35.15259641666667, 0.44501098608333345, 261),
    (37.16818875, 0.33619640251416666, 171),
]

# Highest-churn survivor: still (correctly) fires — churn 0.30 Wh/s is
# 5.5x the quarter's sigma_rate, genuine oscillation, not quantization.
FIRING_TRIPLE: tuple[float, float, int] = (
    20.138299583333335, 0.3032267120083388, 171,
)


def _engine() -> GapMinder:
    return GapMinder(hysteresis_wh=3)


def test_undershoot_triples_stay_quiet() -> None:
    """Four of five 2026-10-09 guard triples stay quiet after the retune."""
    engine = _engine()
    for gap, jitter, seconds in QUIET_TRIPLES:
        assert engine.turn_on_jitter_guard_fires(gap, jitter, seconds) is False, (
            gap,
            jitter,
            seconds,
        )


def test_undershoot_highest_churn_triple_still_fires() -> None:
    """The R=171 churn-0.30 triple still fires (ratio 2.25 over the bar)."""
    gap, jitter, seconds = FIRING_TRIPLE
    assert _engine().turn_on_jitter_guard_fires(gap, jitter, seconds) is True


def test_incident_c579_still_fires() -> None:
    """c579 (churn 0.80, R=111, gap 7.69, ratio ~11.5) still fires at 2.0."""
    assert _engine().turn_on_jitter_guard_fires(7.688, 0.7986, 111) is True


def test_retuned_constants() -> None:
    """Pin the retuned values so a revert breaks loudly."""
    assert JITTER_GUARD_FRACTION == 2.0
    assert JITTER_HORIZON_SECS == 150
