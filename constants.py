"""Named constants for shared magic numbers throughout the codebase.

Centralizing raw literals here makes them discoverable, self-documenting,
and easier to change without hunting through every file.
"""

from __future__ import annotations

# ── NBC / Data freshness ─────────────────────────────────────────────

STALE_DATA_THRESHOLD_SECS: int = 120
"""Maximum age (in seconds) of a per-second data point before we consider
the NBC prediction stale and skip the load management cycle."""

PRUNE_WINDOW_SECS: int = 3600
"""Samples older than this many seconds are pruned from EnergyCache."""

# ── Load management defaults ─────────────────────────────────────────

DEFAULT_TARGET_WH: int = -50
"""Default target Wh per quarter-hour when no value is configured."""

HYSTERESIS_PROPORTION: float = 1.0 / 3.0
"""Hysteresis Wh is abs(target_wh) * this proportion."""

# ── Sleep / cycle timing ─────────────────────────────────────────────

SLEEP_PROPORTION: float = 0.0833
"""Proportion of config interval used for adaptive sleep
(time_to_close / seconds_remaining)."""

DEFAULT_SLEEP_HINT_SECS: float = 5.0
"""Default sleep hint returned for early-exit statuses
(e.g., stale_data, no_incomplete_qh)."""

MIN_SLEEP_SECS: float = 5.0
"""Minimum sleep duration (clamped in _calculate_adaptive_sleep)."""

# ── Quantization ────────────────────────────────────────────────────

QUANTIZATION_CONFIDENCE_THRESHOLD: float = 0.55
"""Minimum window-purity confidence to accept a detected quantization
period for prediction-window selection and sleep alignment."""
