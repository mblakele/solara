# Load Management Documentation

## Architecture

Load management runs as a background thread that cycles every N seconds
(configured via `LOAD_MANAGE_INTERVAL_SECS`, default 30s). Each cycle:

1. **NBC Prediction**: Reads quarter-hour energy prediction from `EnergyCache`, a sliding-window cache of per-second samples in `metrics.py`
2. **State Adjustment**: Adjusts raw prediction with pending effect deltas from
   actions already taken this quarter-hour, so decisions account for loads already toggled
3. **GapMinder:** Compares adjusted prediction against target Wh,
   calculates the gap, and uses bin-packing to fit eligible loads into the surplus
4. **Action Execution**: Turns plugs on/off or adjusts Tesla charging amps

Key components:
- `LoadManager`: Orchestrator that runs cycles in a background thread
- `EnergyCache`/`NBCReader`: Stores per-second samples in a sliding window; NBCReader reads QH predictions from it
- `StateTracker`: Tracks device states, pending effects, stale data detection
- `GapMinder`: Decision logic for which loads to toggle
- Controllers: `RealPlugController` (HomeKit), `VocolincPlugController`,
  `RealTeslaController` — or stub versions for testing

## Stale Data Detection

The load manager uses **data-point age** (not fetch time) to determine whether
NBC data is stale. The Emporia VUE API has inherent lag — the most recent
per-second data point in a prediction may be several seconds behind when the
API call completes. The system derives `data_point_at = fetched_at - lag` and
compares it against an 80-second threshold (`STALE_DATA_THRESHOLD_SECS`).

When data is older than that threshold, the cycle is skipped with a
`stale_data` status **regardless of pending effects** — an aged prediction is
unsafe to act on by itself. The pending-effects count in the skip log/diag is
informational only. Data whose most recent point falls in a previous quarter
hour is treated as stale too (`previous_qh`). `run_cycle(force=True)` bypasses
the stale gate for debugging.

If actions were taken after the last data point, the system enters a
`waiting_for_fresh_data` state until the next NBC fetch confirms those actions.

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `LOAD_MANAGE_ENABLED` | `False` | Enable/disable or time range `HH:MM-HH:MM` |
| `LOAD_MANAGE_INTERVAL_SECS` | `30` | Seconds between load management cycles |
| `LOAD_MANAGE_DRY_RUN` | `False` | Log actions without executing them |
| `LOAD_MANAGE_LUMPY_FIRST` | `False` | Lumpy-first residual turn_off (see below) |
| `LOAD_MANAGE_API_KEY` | *(empty, disabled)* | API key for manual trigger endpoint auth |
| `LOAD_PLUG_CONTROLLER` | `stub` | `real` (aiohomekit) or `stub` (in-memory mock) |
| `LOAD_TESLA_CONTROLLER` | `stub` | `real` (tesla-fleet-api) or `stub` (in-memory mock) |
| `LOAD_NBC_DEVICE` | *(unset)* | Device name used for NBC predictions |
| `LOAD_MANAGER_RAMP_AWARE` | `False` | Ramp-aware Tesla stop hastener (see below) |
| `LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA` | `1` | Observation windows the stop deferral must clear |
| `DEBUG` | `False` | Debug logging and the diagnostics section on the index page |
| `TIMEZONE` | `America/Los_Angeles` | Device timezone for display and time ranges |

### Device Configuration (`devices.json`)

Device-specific settings live in `devices.json` (copy from `devices.json.example`).
This section overrides the equivalent env vars:

- **smartmeter**: `device` (equivalent to `LOAD_NBC_DEVICE`), `target_wh` (equivalent to `LOAD_TARGET_WH`)
- **plugs**: `homekit` and `vocolinc` arrays — name, accessory/device id, power, priority
- **tesla**: `vehicle_id`, OAuth endpoints, charging limits, time range
- **timezone**: Device timezone (equivalent to `TIMEZONE` env var)

## LOAD_MANAGE_ENABLED

Controls whether the load management background loop runs. Accepts three
formats:

| Value | Behavior |
|---|---|
| `True`, `1`, `yes` | Always enabled (case-insensitive) |
| `False`, `0`, `no`, empty | Never enabled (default) |
| `HH:MM-HH:MM` | Enabled only during the specified time window |

### Time Range Mode

When a time range is given, load management is active only during that
window. The start time is **inclusive** and the end time is **exclusive**.
Times are evaluated in the device timezone (configured via `TIMEZONE` env
var, defaulting to `America/Los_Angeles`).

Examples:

```
# Active from 6:45 AM to 3:00 PM local time
LOAD_MANAGE_ENABLED=06:45-15:00

# Active all day except nighttime (wraps around midnight)
LOAD_MANAGE_ENABLED=22:00-06:00
```

When a cycle runs outside the configured window, the status response is:

```json
{
  "status": "disabled",
  "diagnostics": {
    "reason": "outside_time_range(06:45-15:00)",
    ...
  }
}
```

When `LOAD_MANAGE_ENABLED=False`, the reason is simply `"disabled"`.

## LOAD_TARGET_WH

`LOAD_TARGET_WH` is the target watt-hour value per metering period
that the load management engine tries to hit. Default is -50 Wh.

How it works:
- NBC predicts how many Wh you'll use in the current quarter-hour (negative = excess solar, positive = grid draw)
- The engine calculates `gap = target_wh - predicted_wh` (after adjusting the
  prediction for pending effects and Tesla in-flight draw)
  - Positive gap (e.g., predicted=-2000, target=-50 → gap=+1950): too much excess solar → turn loads on to absorb it
  - Negative gap (e.g., predicted=2000, target=-50 → gap=-2050): drawing too much from grid → turn eligible loads off

With the default of -50, the system aims to leave a small buffer
of excess solar unabsorbed rather than driving net usage to exactly zero.
Set it closer to 0 to absorb more solar, or more negative (e.g., -100)
to be conservative and leave more surplus on the grid.

### What the target is, and what it is not

`target_wh` is a **control setpoint, not the objective**. It is a flat Wh
number: nothing in the decision path knows a price, a time-of-use period, or
an export credit (`gap = target_wh - predicted_wh`). It stands in for the
real objective — imports cost money, self-consumed generation does not —
at the resolution the engine can act on. Two consequences matter when
reading any accuracy figure:

- **Quarters with no lever.** When no cycle of a quarter ever sees a surplus
  (the prediction stays positive — empirically the 18:00–07:59 local window),
  the target is unreachable without a battery and the loop can only shed.
  The miss there is the house's baseline load, not a control failure. On the
  2026-10-19 corpus 515 of 1,152 replayable quarters are such quarters, so
  any `mean |miss|` quoted over all quarters is diluted by them. Restricting
  to quarters with a lever changes the level (plug-only 23.4 → 31.6 Wh) but
  no ranking among the measured candidates, and car-active quarters are
  never diluted (all are surplus quarters — the car only ever ran on surplus).
- **The miss has two sides, and the loop's effort lands on the smaller one.**
  Over leverable quarters the plug-only miss is ~89% export-side (leftover
  surplus the loop never absorbed, about −28 Wh/quarter) against ~3.5
  Wh/quarter import-side, and the export side is invariant across every
  candidate measured. Candidates that cut imports cut them by absorbing
  less, which moves error to the export side. Weighing that trade correctly
  needs a price ratio between imported and exported Wh; the engine has none,
  so it treats the two sides as equal. That is a deliberate simplification,
  not an oversight, and it is the first thing to revisit if site rates are
  ever fed in.

## Hysteresis

Production `hysteresis_wh = int(abs(target_wh) / 3)` (`load_manager.py`,
proportion in `constants.py:HYSTERESIS_PROPORTION`). With the current
`target_wh = -9` (`devices.json`) that is **3 Wh** — essentially no deadband
against hundred-Wh gap errors (e.g. a 2000 W plug with ~800 s left is
~440 Wh). The old **1000 Wh** figure was the previous `GapMinder` unit-test
default; the fallback is now **20 Wh** (`constants.py:DEFAULT_HYSTERESIS_WH`),
used only when no explicit value is passed. Production always passes
`int(abs(target_wh) / 3)` explicitly — do not rely on the fallback when
reasoning about production over-commit risk.

## Lumpy-first residual turn_off (flagged off by default)

Turn-off shedding packs eligible loads in priority order until the deficit
is covered, with no fit check — so at quarter-open, a 28x extrapolation
of the water heater's own draw dictates how many 260 W plugs shed with it
(`bugs/2026-10-14-water-heater-overshoot.log` c658:
ecoflow + heater on gap 1171, residual −15.7, jackery chased a cycle later;
`bugs/2026-10-14-no-overshoot.log` c817: same batch, quarter landed −28.65
with one plug stranded off most of the quarter).

When `LOAD_MANAGE_LUMPY_FIRST` is true, `_decide_turn_off` (`load_nbc.py`,
`GapMinder.lumpy_first`) decides loads ≥ `LUMPY_PLUG_MIN_WATTS` (1000 W)
first, but only when one of them is implicated in the trailing rate (its
draw dominates what the forecast extrapolates, inferred from
`banked_wh`) *and* the deficit exceeds the smalls' combined capacity
(they could not cover it alone). Shedding it first through the shared
remainder *is* the residual budget — small plugs then pack onto what is
left, so the heater's own draw can no longer dictate their count. Without
banked energy the rate is unknowable and the engine falls back to
status-quo order; turn-on, Tesla, and the jitter guard are untouched.

Measured, not modeled: the divergence audit (`tools/divergence_audit.py`)
over 1,015 corpus quarters finds 9 divergences (11 anchors), unanimously
shedding less and never more than one 260 W plug. The residual carries
NO margin — corpus sizing (`tools/forecast_log_scoring.py` residual
series: sigma 0.29 vs raw 0.18) supports none, and a symmetric margin
demonstrably churns (six flip-flops, −63 vs −28.65 in the replay harness).

## Tesla stop deferral and ramp awareness

At Tesla's 5 A minimum there is nothing to trim: the only shed action is
an all-or-nothing stop (~1200 W × remaining seconds). The static rule
(`TeslaDecider.decide_reduce`, `load_nbc.py`) defers the stop while
`seconds_remaining > gap / (1200/3600)` (capped at `MAX_DEFER_SECS=900`,
one whole quarter-hour, so the gap deadline governs inside a normal
window), so an exact-hit stop lands precisely on target — assuming the
prediction is frozen. A gap the window can never pay (above 300 Wh at
5 A) is stopped immediately instead of deferred to the edge. On a
sustained ramp (sunset, `bugs/2026-09-26-tesla-stop-
charging.log`) the frozen assumption defers one cycle too long.

`GapTrendTracker` (`gap_trend.py`, constants `GAP_TREND_*`) estimates the
adjusted-gap slope across cycles: an EWMA over a 3-sample window keyed on
`data_point_at`, with flat repeats neutral and opposing slopes rejecting.
History clears on two boundaries — a quarter-hour rollover, and a
`data_point_at` delta above `GAP_TREND_MAX_SPAN_SECS` (120 s, the feed's
own cadence: production deltas are 30-60 s, so a longer span is a stall,
not a ramp). The quarter-hour identity is
**derived** via `floor_to_qh(data_point_at)`, not taken from the caller:
`ParsedMetricsQH.qh_name` is the hardcoded literal `"QH1"` for every
incomplete quarter, so keying on it could never detect a rollover. That
bug let a 20:45 sample be slope-fitted against 20:43/20:44 samples from
the previous hour, publishing a bogus −6.1 Wh/s trend
(`bugs/2026-10-01-tesla-overshoot.log`, c587). Harmless for the Tesla stop
— `_ramp_stop_now` only fires when the deficit already exceeds ~⅓ of the
energy the car would draw over the remaining time, a regime where stopping
is right regardless of trend — but it must not survive into plug Phase II,
where a spurious trend could drop a 4857 W load.

Fed in `_stage_compute_gap` on the pending-effect-corrected gap with noise
floor `hysteresis / seconds_remaining`, exposed as `gap_trend_wh_per_s` in
`CycleDiagnostics`/JSON/SSE. When trusted and positive, the exact-hit stop
time `t* = (P·R − G₀)/(P + r)` (trend clamped to half the 5 A rate) stops
now if `t*` falls within one cycle (`DecideContext.cycle_secs`); otherwise
the static rule stands unchanged. Flat, shrinking, or unconfirmed trends
never alter behavior.

The hastener is **opt-in**: `LOAD_MANAGER_RAMP_AWARE` (default `off`, wired
through `Config.ramp_aware` → `LoadManagerConfig` → `GapMinder.ramp_aware` /
`TeslaDecider.ramp_aware`) gates `_ramp_stop_now`. With the flag off the
defer rule still applies, but no trend-based hastening occurs. The fork is
deliberate: the hastener fired 0 times across the corpus (172 deferrals, 14
eligible) and only the `2026-09-26` incident benefits, so the default keeps
the reluctance-to-stop stance and leaves the incident fix one env var away.
The defer rule itself is load-bearing and stays (fixed-car corpus,
2026-10-21: car-active closed-loop mean |miss| 10.34 on vs 18.12 off), and the
buffer cap only helps by no longer overriding the gap deadline: 120 s 12.21,
300 s 11.99, 900 s 10.34 (6 quarters better / 0 worse / 135 unchanged at
120 → 900; plug-only bit-identical). Plug
decisions are intentionally out of scope (Phase I: Tesla stop only).

### The defer deadline is sized in observation windows

`seconds_remaining > safe_defer_secs` asks only whether the buffer outlasts
the quarter; it never asks whether the *next observation* can arrive before
the deadline. `bugs/2026-10-18-tesla-stop-overshoot.log` shows the gap: at
`R=89 s` against a 24 Wh gap the rule deferred (`safe_defer = 72 s`) and the
next decision landed at `R=60 s`, where the remaining 20 Wh of shed could no
longer close the 31.8 Wh deficit.

`TeslaDecider.defer_lookahead_quanta` (keyword-only, **default `1` = shipped
2026-10-19**) withholds K whole observation windows from the deadline, so a
defer always leaves room for the next observation to be acted on. The window
comes from `DecideContext.observability_secs` — the quantisation quantum,
which production fills with the resolved prediction/settle window
(`StateTracker.effective_settle_secs`) and the replay with each quarter's
median data-point spacing — and never from a literal 30 s: `None` (window
unknown) leaves the lookahead inert rather than guessing. It is configured by
`LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA` (read leniently: a malformed value falls
back to the default instead of refusing to start the load manager), and `0`
restores the pre-2026-10-19 static rule. This is the same rule as the ramp
hastener's eligibility band with the trend gate opened
(`R ≤ gap/P + c·(1 + r/P)` at `r = 0`, `c = K·Q`), which is why it partially
subsumes that rule.

**Why it ships as a soundness fix, not as a mean win.** A deferral whose
deadline sits inside the next observation window cannot be observed through —
the promise "I can stop later if needed" is one the loop cannot keep. That is
exactly the incident: at `R=119 s` the deadline was 107 s, one 30 s window
away, so the static rule deferred, the next decision arrived at `R=60 s`, and
the quarter finished +10.81 Wh over the −9 Wh target; taking the stop at
`R=119 s` sheds 39.7 Wh instead of 20.0 Wh and lands the quarter at −8.86 Wh.
Stopping earlier strictly *lowers* consumption — that was verified, not
assumed: in the replay's regressing quarter the landing falls from −5.81 to
−27.13 Wh, and the |miss| rise there is purely the overshoot past the target.

The overshoot against the engine's *claimed* gap is bounded at one quantum
(`R ≤ gap/P + Q` ⟺ `P·R ≤ gap + P·Q`); against the *realized* quarter it is
whatever the forecast got wrong, which no stop rule can bound. Closed-loop on
the corpus that shows up as a wash: car-active mean |miss| K=0 10.45 → K=1
10.34, 4 quarters improve / 5 regress (median delta 0.00), signed miss
+3.36 → +2.43; plug-only is bit-identical (24.87 — the knob is Tesla-only).
K=2 (10.64) improves 6 quarters and regresses 7; K=3 (11.29) is worse still,
so 1 stays the rule. `--ramp-aware` changes no number at any K on this
corpus.

**The incident is characterized, not replayed.** An earlier reading —
"the log regresses under its own fix" (5.91 → 10.89) — was a replay-model
artifact. The replay derives its prediction from its own counterfactual meter
(`cf.raw_wh`), and for that quarter the counterfactual diverges from the
log's raw energy all quarter (up to 8 Wh), carries no anchor at the decisive
R=60 s cycle (19 anchors, median spacing 45 s, while production cycled that
deferral burst every 8–13 s), and lands 7.62 Wh from the logged actual. The
replay is the arbiter for corpus-wide comparisons; for a single incident the
log's own trace is the evidence, and it supports the rule.

**Baseline note (re-measured 2026-10-21).** The replay's counterfactual car
now follows the logged amp transitions instead of holding the opening amps
until a policy command. Every car-active figure quoted elsewhere in this
document is a pre-fix snapshot. On the current corpus (141 car-active / 1199
plug-only) the shipped baseline is 10.34 car-active (median 3.69, mean +2.43,
14.92 actions/q) and 24.87 plug-only; plug-only is bit-identical for every
Tesla-only knob. No ordering reverses: the estimator variants still lose
(prior 19.15, prior-blend@60 14.71, trend-gated 13.29), the zero-margin policy
still wins (turn-on 10/20/40 = 11.68/13.29/18.04; turn-off 10/20/40/60 =
12.26/17.18/31.02/50.15), the defer rule is still load-bearing (10.34 vs 18.12
off), and no rate window still helps both regimes. Two readings move:
lumpy-first is now a wash (10.38 vs 10.34), and the guard bundle is net
*worse* car-active (churn == live 12.28 vs inert 10.34, 44 better / 62 worse,
0 ramp fires) — the reverse of the pre-fix snapshot. No guard is deleted
without an owner decision.

**Two sign conventions, deliberately:**

- **Engine-internal "gap"** (CycleDiagnostics, `gapminder_decide` logs):
  `target_wh - predicted_wh` — positive means surplus to absorb. This is
  control math; do not change it without revisiting the decision engine.
- **Operator-facing NBC-signed figures** (e.g. the Telegram
  "skipped: no actions" log lines): `predicted_wh - target_wh` — negative
  = excess solar, positive = grid draw, matching how NBC predictions are
  displayed everywhere else. Users are used to reading -N as solar;
  these lines say so explicitly (`positive=grid draw`).

## Prediction/settle window adaptation

The prediction window is **adaptive**. Each fetch's `EnergyCache` quantization
period is filtered by `_resolve_prediction_window` (`load_manager.py`): a
period at or above `MIN_QUANTIZATION_WINDOW_SECS` (15 s) with confidence at or
above threshold is taken, anything shorter (the flat-data N=2 artifact) falls
back to `DEFAULT_PREDICTION_WINDOW_SECS` (30 s).
`StateTracker.apply_prediction_window` commits a change only after two
consecutive confirmations and ignores values within
`SETTLE_WINDOW_DEADBAND_SECS` (5 s) of the committed window as detector
jitter, so a genuine meter-behaviour change is tracked but ±1 s noise is not.

### The sensing model behind the window

Why the window tracks the *quantum* specifically: within a quantum the smart
meter cannot measure a real Wh delta — the quantum is too short for the sensor —
so what it reports is a **power at the end of the quantum**, and the per-second
series is that value **held constant across the quantum** (the
contiguity/quantization axiom the pipeline is built on). Because every sample
inside a quantum is the same number, the forecast's trailing mean
(`util.compute_nbc_quarter`) is the time-weighted mean of the `k` overlapping
end-of-quantum readings. The shipped window *is* the detected quantum, so
`k = 1`: the rate is the **latest reading held forward** (a zero-order hold),
and `predicted_wh = raw_wh + remaining_seconds * prediction_w` is "measured
energy so far + latest power × remaining". The consequences are structural. The shipped rate
is already maximally responsive — there is no lag to win back — and a window
shorter than a quantum is degenerate (identical to one quantum when it sits
inside a quantum, an aliasing mix of two when it straddles one, worse with a
non-zero `quantization_offset`). What remains is a bias-variance trade on that
single reading (is the latest reading too noisy to predict the next quantum?),
which is measured at *quantum* resolution by the estimator-responsiveness plan,
not by sweeping seconds. The same model sets the gate's natural buffer: exactly
one quantum answers "could the newest data point predate my action?" under
end-of-quantum reporting; less is unsatisfiable and more is the removed
post-amp-change suppression.

That one window is the `EffectStore` freshness window, so as a *gate* parameter
it reaches decisions through exactly one consumer: the fresh-data gate
`has_pending_effect_since` (an early exit that defers a decision while an
effect taken after the newest data point has not been reflected yet). The same
resolved value is *also* the forecast's trailing-rate length
(`util.compute_nbc_quarter(prediction_window_seconds=...)`), which changes
`predicted_wh` and therefore decisions; the counterfactual below holds the
logged forecast fixed, so it prices the gate only (the forecast-rate axis is
the estimator-responsiveness plan).

That forecast-rate axis was then swept closed-loop at quantum resolution
(`tools/estimator_replay.py --rate-window-quanta`, plan C; fixed-car corpus,
2026-10-21): **no count above one wins.** A two-quantum rate helps plug-only
(24.87 → 24.51 mean |miss|) but regresses car-active (10.34 → 13.13) and
worsens more plug-only quarters than it fixes (163 worse / 126 better), and
three or more windows are worse still in both regimes (car-active 3q 14.16,
4q 12.48, all 23.27). So the shipped `k = 1` stands; the open/closed paradox
is the level's lag, not its point accuracy, and the adaptation's protective
value is the **gate**, not the rate — lengthening the rate to match a slower
quantum is not wanted.

It is priced by `tools/settle_window_counterfactual.py`: it rebuilds the
effect store from the logged `actions=[PendingEffect(...)]` reprs and replays
production's own `EffectStore` once per cycle under the committed window and a
pinned one (default 30 s). Snapshot (corpus/, 2026-10-18, 14 logs, 58,390
cycles; the corpus is refreshed over time, so these figures are a snapshot,
not a fixed baseline): the window is 30 s everywhere except 111 cycles
(15 s/21 s/24 s), and the two arms agree on **every** cycle — 0 gate flips,
no decision changed, the gate firing 2,672 times under both arms. The
adaptation guards a failure mode (a sustained meter change to a slower
quantum) the corpus never exercises; the tool's 60 s committed-window fixture
shows the flip that would appear if the meter genuinely slowed. The detector
period's second consumer, the sparkline's bucket size, is display-only and out
of scope.

The window's second consumer, the post-amp-change suppression
`_suppressed_by_settle`, was **retired 2026-10-18 as unreachable**. Whenever
an active settle effect exists the gate is already true — both compare the
effect's wall-clock and data-point ages against the same window, and the
settle's `now` is at or after `data_point_at` — so production exits on the
gate before reaching the suppression, and it fired in no recorded cycle. Its
support went with it: `StateTracker.get_active_tesla_settle`,
`TeslaSettleTracker.get_active`, the `GapMinder.TESLA_SETTLE_SUPPRESS_WH`
threshold and the vestigial `PendingEffect.suppress_action` field.

## Turn-on jitter guard (removed 2026-10-21)

The turn-on jitter guard and its banked-cover override were removed. The
guard declined a plug or Tesla turn-on when the gap estimate's own
cycle-to-cycle churn exceeded the surplus it claimed.
`GapTrendTracker.churn_wh_per_s`, the `gap_jitter_wh_per_s` diagnostics,
the `excessive_jitter` reason and the `⚠ low confidence` label went with
it.

Why: the closed-loop replay measured the bundle net worse in both regimes
(car-active 12.28 vs 10.34 inert mean |miss| Wh per quarter; plug-only
25.18 vs 24.87). The guard also suppressed actions in quarters whose
forecast later proved right, so the variance it removed did not pay for
the surplus it declined.

`DecideContext.banked_wh` stays: the lumpy-first gate uses it to derive
the quarter's trailing rate. The ramp-aware Tesla stop stays and still
reads `gap_trend_wh_per_s`; the tracker now measures only the slope and
its trust rule, with no churn accumulator.

The offline scorer's `--guards` census (`tools/forecast_log_scoring.py`)
still reads the old `gapminder_jitter_guard` and
`gapminder_banked_override` log lines for historical logs, and rebuilds
the churn EWMA locally for the churn-band report. Production no longer
emits those lines.

## Dry-Run Mode

Set `LOAD_MANAGE_DRY_RUN=True` to test load management without executing actions.
In dry-run mode:
- Actions are calculated and logged but NOT sent to devices
- Device state is NOT updated
- Status response includes `"status": "dry-run"` instead of `"status": "ok"`

Use this to verify configuration before enabling real control. Start with dry-run
enabled, review the logs for a few cycles, then disable when satisfied.

## Smart Plug Configuration

Plug configuration is managed in `devices.json` (copy `devices.json.example`
to `devices.json` and edit). This file is git-ignored and never committed.

### HomeKit Smart Plugs

#### Pairing a New Accessory

Before configuring a plug, pair it with the app:

```bash
uv run python app.py --pair-plug <name> <accessory_id> <pin>
```

- `<name>`: Label for your reference (e.g., "water-heater")
- `<accessory_id>`: IP address or mDNS name of the accessory
- `<pin>`: Setup PIN displayed on the accessory

Pairing data is saved to `.homekit-pairings.json` in the project root.

#### Configuration

Set `LOAD_PLUG_CONTROLLER=real` and add entries to `devices.json`:

```json
"plugs": {
  "homekit": [
    {
      "name": "water_heater",
      "accessory_id": "192.168.1.50",
      "power_watts": 4500,
      "priority": 10
    }
  ]
}
```

- **name**: Label for your reference
- **accessory_id**: Must match the IP/mDNS name used during pairing
- **power_watts**: Approximate power draw when on (used for bin-packing decisions)
- **priority**: Higher number = higher priority (optional, default 0).
  Plugs turn on most-important-first and shed least-important-first.
- **time_range**: Only activate during this window (optional, format `HH:MM-HH:MM`)
- **shed_outside_range**: When `true` with a `time_range`, the plug can still
  be turned off outside its window but never turned on there (off-but-not-on).
  Plugs left ON outside their window also raise a Telegram alert once per
  quarter-hour, bypassing the `telegram.devices` whitelist.

### VOCOlinc Smart Plugs

Set `LOAD_PLUG_CONTROLLER=real` and add entries to `devices.json`:

```json
"plugs": {
  "vocolinc": [
    {
      "name": "floor_lamp",
      "device_name": "LivingRoomLamp",
      "power_watts": 60,
      "priority": 5
    }
  ]
}
```

VOCOlinc credentials still go in `.env`:

```env
VOCOLINC_USERNAME=your@email.com
VOCOLINC_PASSWORD=your_password
```

The `device_name` is the friendly name shown in the VOCOlinc app.
Note: Avoid colons (`:`) in device names as they conflict with the config format.

When both HomeKit and VOCOlinc plugs are configured, a composite controller
is used automatically — each plug routes to its correct backend.

## Tesla Fleet API Setup

Tesla integration uses the [Tesla Fleet API](https://developer.tesla.com/).
Before load management can control your vehicle's charging, you need to
register a Fleet API app and complete OAuth authentication.

### Step 1: Register a Tesla Fleet API App

1. Go to [developer.tesla.com](https://developer.tesla.com/) and sign in with your Tesla account.
2. Navigate to **Fleet API** → **Settings** and create a new app.
3. Set the **Redirect URI** to:
   ```
   http://localhost:8000/callback
   ```
4. After creating the app, note the **Client ID** and **Client Secret**.

### Step 2: Configure Environment Variables

Copy `env.example` to `.env` (if you haven't already) and fill in the Tesla
section. The minimum required variables for Tesla are:

| Variable | Value | Description |
|---|---|---|
| `TESLA_CLIENT_ID` | From Tesla developer portal | Your Fleet API app's Client ID |
| `TESLA_CLIENT_SECRET` | From Tesla developer portal | Your Fleet API app's Client Secret |
| `TESLA_REDIRECT_URI` | `http://localhost:8000/callback` | Must match what you registered |
| `TESLA_VEHICLE_ID` | Your VIN | e.g., `5YJ3E1EA8KF000000` |
| `TESLA_REGION` | `na` (default) | Use `cn` only for China-region accounts |
| `TESLA_PRIVATE_KEY_PATH` | Path to PEM file | Required for vehicle commands (set_amps, etc.) |

Optional configuration:

| Variable | Default | Description |
|---|---|---|
| `TESLA_HOME_LAT` | `37.7749` | Home GPS latitude for "at home" detection |
| `TESLA_HOME_LON` | `-122.4194` | Home GPS longitude |
| `TESLA_HOME_RADIUS_M` | `500` | Max distance (meters) to consider vehicle "at home" |
| `TESLA_CHARGE_AMPS_MIN` | `5` | Minimum charging amps the controller will set |
| `TESLA_CHARGE_AMPS_MAX` | `48` | Maximum charging amps the controller will set |

Once configured, enable the real Tesla controller:

```
LOAD_TESLA_CONTROLLER=real
```

### Step 3: Authenticate

You have two options. Both save tokens automatically to `.tesla-tokens.json`
in the project root.

#### Option A: Web-based (Recommended)

1. Start the dev server:
   ```bash
   uv run python app.py
   ```

2. Open this URL in your browser:
   ```
   http://localhost:8000/api/v1/tesla/auth/initiate
   ```

3. The response includes a `loginUrl`. Open that URL in your browser.

4. Sign in to your Tesla account and authorize the app.

5. Tesla redirects to `http://localhost:8000/callback?code=...`.
   The server automatically exchanges the code for tokens and saves them.
   You'll see a confirmation page.

#### Option B: CLI-only

Run the CLI auth helper from the project root:

```bash
uv run python app.py --tesla-auth
```

1. The command prints an authorization URL. Open it in your browser.
2. Sign in and authorize the app.
3. Tesla redirects to a URL containing `?code=...`. Copy that code value.
4. Paste the code at the terminal prompt.
5. On success, tokens are saved to `.tesla-tokens.json` automatically.

### Step 4: Verify

Check authentication status via the API:

```bash
curl http://localhost:8000/api/v1/tesla/status
```

A successful response looks like:

```json
{
  "configured": true,
  "authenticated": true,
  "message": "Tesla authenticated successfully",
  "expires": "2026-05-01T12:00:00+00:00"
}
```

### Troubleshooting

**"Tesla OAuth not configured"** — This means no valid tokens exist yet.
Complete Step 3 above.

**"Tesla access token check failed"** — The cached token expired and refresh
failed. Re-run the OAuth flow (Step 3) to get fresh tokens.

**Car asleep / unavailable** — If the car is asleep, you'll see an `info`-level
log like `Tesla charging state unavailable (car may be asleep)`. This is
normal and not an error. The car wakes periodically; the next load management
cycle will retry.

**Tokens need refreshing** — Tokens are persisted to `.tesla-tokens.json`
in the project root. The controller auto-refreshes them on startup if the
access token is expired but the refresh token is still valid. If you see
repeated auth failures, delete `.tesla-tokens.json` and re-authenticate.

## API Endpoints

### GET /api/v1/load/status

Read-only endpoint returning current state: enabled flag, target Wh, device states,
pending effects, and last cycle result.

```bash
curl http://localhost:8000/api/v1/load/status
```

### Tesla OAuth Endpoints

- **GET** `/api/v1/tesla/auth/initiate` — Start OAuth flow, returns login URL
- **GET** `/callback` — OAuth redirect handler (exchanges code for tokens)
- **GET** `/api/v1/tesla/status` — Check authentication status and token expiry

## Telegram notifications

- `TelegramConfig` (`telegram_client.py`) — frozen config dataclass with
  `bot_token` and `chat_id`.
- `TelegramClient` (`telegram_client.py`) — async aiohttp client. Fire and
  forget. Returns `bool`. Never raises.
- `TelegramSender` (`telegram.py`) — high-level sender. Loads config from env
  vars and `devices.json`.
- `NotificationEvent` (`telegram.py`) — frozen dataclass for structured
  notifications with `format_message()`.
- `load_telegram_config()` (`telegram.py`) — loads config through the `Config`
  lookup chain (env vars, then `.env`) first, then `devices.json`.
- The device whitelist is the `telegram.devices` section of `devices.json`,
  read in `LoadManager.__init__` / `reload_config()`. There is no env-var
  override; the old `LOAD_TELEGRAM_DEVICES` variable is gone.
- `validate_telegram_devices()` (`device_config.py`) — checks that
  `telegram.devices` keys match plug names after every `_load()`. `"tesla"` is
  accepted as a special name for stop-charging alerts.
- **Whitelist gate.** A notification is sent only when a whitelist is
  configured AND at least one action matches it. Without a whitelist, all
  notifications are blocked.
- Plug messages: `🟢 device → ON` / `🔘 device → OFF`.
- A plug `turn_off` line appends today's ON-time since meter-local midnight:
  `🔘 water heater (06:12 today)`. Format is `MM:SS` under an hour and
  `H:MM:SS` above. Plugs only, never Tesla or `turn_on`.
- Tesla messages: `🔌 Tesla charging stopped` / `⚡ Tesla charging started` /
  `🔋 Tesla charge amps → N A`.
