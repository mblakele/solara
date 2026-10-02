"""Named constants for shared magic numbers throughout the codebase.

Centralizing raw literals here makes them discoverable, self-documenting,
and easier to change without hunting through every file.
"""

from __future__ import annotations

# ── NBC / Data freshness ─────────────────────────────────────────────

STALE_DATA_THRESHOLD_SECS: int = 80
"""Maximum age (in seconds) of a per-second data point before we consider
the NBC prediction stale and skip the load management cycle. Note: the
dashboard freshness strip uses its own mode-aware thresholds
(FRESHNESS_RELOAD/LIVE_*); this constant gates load-management actions."""

FRESHNESS_RELOAD_WARN_SECS: int = 300
"""Dashboard freshness strip, refresh-driven pages (load management
disabled): data at or past this age (5 min) is shown as ``aging`` (amber).
Without load management the Emporia data typically arrives 120-180 s late
and the page reloads on its own cadence, so normal operation stays green
through ~5 min (worst-case age right before a reload)."""

FRESHNESS_RELOAD_STALE_SECS: int = 420
"""Dashboard freshness strip, refresh-driven pages (load management
disabled): data at or past this age (7 min) is shown as ``stale`` (red)."""

FRESHNESS_LIVE_WARN_SECS: int = 180
"""Dashboard freshness strip, SSE-driven pages (load management enabled):
data at or past this age (3 min) is shown as ``aging`` (amber). The ~30 s
load-management cycle keeps lag around 45-120 s, so 3 min of no update
means several cycles were missed."""

FRESHNESS_LIVE_STALE_SECS: int = 300
"""Dashboard freshness strip, SSE-driven pages (load management enabled):
data at or past this age (5 min) is shown as ``stale`` (red) — updates
have clearly stopped."""

METRICS_UPDATE_FALLBACK_SECS: int = 120
"""Expected seconds until the next dashboard update when nothing else
defines a cadence: no load management (no cycle-driven fetch) and no
detected quantization window. Mirrors the 2-minute fallback reload timer
in ``static/app.js``."""

DATA_STALE_ALERT_THRESHOLD_SECS: int = 300
"""Wall-clock age (seconds) of the most recent NBC data point before the
load manager queues a Telegram data-health alert. Covers both stale
``data_point_at`` and the boot-empty case (age measured from manager
start when no data point has ever been seen). Throttled to one alert
per QH per type (see ``LoadManager._check_stale_data_alert``)."""

PRUNE_WINDOW_SECS: int = 3600
"""Samples older than this many seconds are pruned from EnergyCache."""

# ── Load management defaults ─────────────────────────────────────────

DEFAULT_TARGET_WH: int = -50
"""Default target Wh per quarter-hour when no value is configured."""

HYSTERESIS_PROPORTION: float = 1.0 / 3.0
"""Hysteresis Wh is abs(target_wh) * this proportion."""

DEFAULT_HYSTERESIS_WH: int = 20
"""Fallback hysteresis (Wh) when GapMinder gets no explicit value.

Residential scale: blocks sub-20 Wh noise without swallowing the
hundred-Wh gap errors real plug/Tesla decisions work with. Production
still passes int(abs(target_wh) / 3) explicitly; this only covers
direct GapMinder() construction (mostly tests)."""

MIN_SECONDS_TO_ACT: int = 21
"""Minimum seconds remaining in a quarter-hour before the GapMinder will
allow turn-on or Tesla amp changes.  Prevents actions too close to the
QH boundary where the device would run into the next quarter-hour."""

# ── Sleep / cycle timing ─────────────────────────────────────────────

SLEEP_PROPORTION: float = 0.0833
"""Proportion of config interval used for adaptive sleep
(time_to_close / seconds_remaining)."""

DEFAULT_SLEEP_HINT_SECS: float = 5.0
"""Default sleep hint returned for early-exit statuses
(e.g., stale_data, no_incomplete_qh)."""

MIN_SLEEP_SECS: float = 5.0
"""Minimum sleep duration used by EnergyCache.sleep_interval_adjust."""

# ── Quantization ────────────────────────────────────────────────────

QUANTIZATION_CONFIDENCE_THRESHOLD: float = 0.55
"""Minimum window-purity confidence to accept a detected quantization
period for prediction-window selection and sleep alignment."""

DEFAULT_PREDICTION_WINDOW_SECS: int = 30
"""Fallback prediction window in seconds when no quantization data is
available (previously hardcoded as 60)."""

MIN_QUANTIZATION_WINDOW_SECS: int = 15
"""Minimum detected quantization period (seconds) accepted for the
prediction/settle window mapping.

Shorter detections are almost always artifacts — e.g. the flat-data
case where ``detect_quantization`` reports N=2 with confidence 1.0 for
an all-identical array — so they fall back to
``DEFAULT_PREDICTION_WINDOW_SECS`` instead of collapsing the settle
window to a nonsense value.
"""

SETTLE_WINDOW_DEADBAND_SECS: int = 5
"""Minimal |candidate - committed| difference (seconds) for a candidate
settle window to be considered a real change.

Detector jitter typically reports the true period ±1 s (e.g. 29/30/31
around 30 s); values inside this dead-band are treated as noise and
never seed a change candidate.  Used by
``StateTracker.apply_prediction_window``."""

MIN_SAMPLES_FOR_PREDICTION: int = 5
"""Minimum per-second samples in the current QH required to produce a
reliable NBC prediction.  Below this threshold the pipeline returns
``no_incomplete_qh`` with a short sleep hint instead of acting on a
wildly extrapolated single-sample prediction."""

# ── Gap trend (ramp-aware Tesla stop) ────────────────────────────────

GAP_TREND_WINDOW: int = 3
"""Number of recent (data_point_at, gap) samples retained for slope
estimation.  Three samples yield two consecutive slopes, the minimum
needed to confirm a sustained ramp before acting on it."""

GAP_TREND_EWMA_ALPHA: float = 0.3
"""Weight of the most recent slope in the exponentially weighted moving
average over the window.  Dampens single-cycle spikes while tracking
sustained ramps such as sunset."""

GAP_TREND_MIN_SLOPES: int = 2
"""Slopes required before a gap trend is trusted. A single slope (two
samples) is never enough — clouds and meter jitter flip the sign within
one cycle. Zero slopes are neutral and neither confirm nor break."""

GAP_TREND_MAX_SPAN_SECS: int = 120
"""Largest ``data_point_at`` delta the gap tracker will slope across.

Matches ``TeslaDecider.MAX_DEFER_SECS``, the furthest horizon a stop
decision ever examines. A slope averaged over more seconds than that
describes a regime the decision cannot act on (e.g. a long stall, or a
window spanning a quarter-hour boundary), so the tracker clears history
instead of trusting it. Real production deltas are 30-60 s, because half
the cycles exit early on ``waiting_for_fresh_data``."""

JITTER_GUARD_FRACTION: float = 1.0
"""Turn-on jitter guard: decline when ``churn * seconds_remaining`` reaches
this multiple of the surplus gap it is claiming.

Sized by the overshoot replay (``tests/test_tesla_overshoot_replay.py``),
not by intuition: with 1.0 the guard blocks the oscillation-driven c579
+1 A increase from ``bugs/2026-10-01-tesla-overshoot.log`` and lands the
quarter within ~3 Wh of the -9 Wh target instead of +1.74 Wh. Turn-on
only — turn-off and the ramp-aware stop are protective and unaffected."""

JITTER_FLOOR_WH_PER_S: float = 0.2
"""Minimum churn (Wh/s) the jitter guard will act on at all.

``churn * seconds_remaining >= gap`` collapses to ``churn >= gap / R``,
and on a calm quarter that threshold is tiny: for the 6-8 Wh surplus of
``bugs/2026-10-02-sunrise-marine-layer-jitter.log`` it was 0.0091-0.0124
Wh/s — *below* that quarter's own realized ``sigma_rate`` of 0.0222 Wh/s,
so any nonzero churn reported "excessive jitter" (35 times, on a
morning where nothing was happening).

Sized from both ends:
  * Lower anchor (calm baseline): that log's churn never exceeded 0.0468
    while firing, and the largest single ``|Δslope|`` its quantization
    steps can produce is 0.1032. One ``prediction_w`` step of 0.0025
    Wh/s moved across R≈650 s in 30 s yields ≈0.054 Wh/s of pure
    extrapolation noise, so 0.2 clears roughly two such steps.
  * Upper anchor (the incident): the overshoot replay needs the guard to
    fire at c579 where churn ≈ 0.80 — 4x headroom below this floor.
The comparison is strict (``churn < floor`` declines the guard), so a
value exactly at the floor still participates."""

JITTER_HORIZON_SECS: int = 300
"""Longest remaining-quarter horizon a churn estimate may project over.

Churn is an EWMA of ``|Δslope|`` measured across 30-60 s cycle spacing
with a 3-sample window. Multiplying it by the *whole* remaining quarter
(up to 900 s) projects a rate-change far past the horizon over which it
says anything: 0.2 Wh/s at R=900 becomes a 180 Wh "swing" that vetoes
any realistic surplus. Capped here the same churn blocks gaps up to
60 Wh, so the guard stays responsive to genuinely large early-quarter
surplus while a measured oscillation no longer scales with how much of
the quarter is left. Well above the incident horizons (c579 R=111,
c575 R=209), which are therefore unaffected."""

JITTER_MAX_REMAINING_SECS: int = 600
"""Churn is not measurable while more than this much of a quarter remains.

``predicted_wh = raw_wh + prediction_w * remaining_seconds``; past 600 s
remaining at least two thirds of the projection is extrapolation from a
≤300 s trailing window, so a ``prediction_w`` step — or our own
just-realized action entering that window — moves the gap with no new
information. Seeding the churn EWMA there produced
``bugs/2026-10-02-sunrise-marine-layer-jitter.log`` cluster B: one
quarter-opening sign flip of ``prediction_w`` seeded churn at 1.10 and it
then decayed ×0.7 for five minutes of "excessive jitter" reports. The
tracker still records every sample for the trend; only churn accumulation
waits for the ready window."""

# ── Fetch drift observability ────────────────────────────────────────

DRIFT_REJECTION_ALERT_AFTER: int = 5
"""Number of consecutive drift rejections for the same QH before the
fetch path logs an error that the window head appears permanently
missing and queues a one-time Telegram alert (see ``_drift_rejections``
and ``_drift_alerts`` in metrics.py)."""

# ── Tesla charging / telemetry ───────────────────────────────────────

TESLA_HARD_MAX_AMPS: int = 48
"""Hard absolute ceiling on commanded charge amps regardless of config
(safety cap — never exceed this value)."""

TESLA_CHARGE_AMPS_MIN_DEFAULT: int = 5
"""Default minimum charge amps before the load manager stops charging
instead of reducing further."""

TESLA_CHARGE_AMPS_MAX_DEFAULT: int = 48
"""Default maximum charge amps to command."""

TESLA_NOMINAL_VOLTAGE: int = 240
"""Nominal mains voltage (V) used for watts/amps conversions."""

TESLA_ZERO_AMPS_CLEAR_SAMPLES: int = 2
"""Consecutive reported_amps==0 telemetry samples required to confirm the
car actually stopped drawing power before clearing ``last_commanded_amps``.

A single 0 A frame can be a stale or in-transit sample during ramp-up;
clearing on it drops the in-flight delta and overstates surplus."""

TESLA_TOKEN_REFRESH_INTERVAL_SECS: int = 7 * 3600
"""Proactive Tesla token-refresh window in seconds.

Deliberately shorter than the 8-hour access-token lifetime so the
refresh always happens before the access token expires server-side."""

TESLA_ARBITRATION_COOLDOWN_SECS: int = 300
"""Minimum seconds between REST arbitration polls for ambiguous telemetry.

When MQTT reports positive but uncorroborated ChargeAmps with no active
command (bugs/2026-09-11-tesla-ghost-c.log), only a REST ``charge_state``
read can tell the idle pilot ghost from a real external session. Each
poll can wake an otherwise-sleeping car and costs API quota, so polls
are spaced at least this far apart; between polls the last confirmed
answer is sustained while amps stay positive, and amps dropping to zero
clears it immediately. Five minutes bounds detection delay for a newly
started external session against poll cost during long ghost periods."""

TESLA_HOME_RADIUS_M_DEFAULT: float = 500.0
"""Default radius in metres around home used for at-home detection."""
