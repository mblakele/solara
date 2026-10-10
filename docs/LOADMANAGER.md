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
| `LOAD_MANAGE_API_KEY` | *(empty, disabled)* | API key for manual trigger endpoint auth |
| `LOAD_PLUG_CONTROLLER` | `stub` | `real` (aiohomekit) or `stub` (in-memory mock) |
| `LOAD_TESLA_CONTROLLER` | `stub` | `real` (tesla-fleet-api) or `stub` (in-memory mock) |

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

## Tesla stop deferral and ramp awareness

At Tesla's 5 A minimum there is nothing to trim: the only shed action is
an all-or-nothing stop (~1200 W × remaining seconds). The static rule
(`TeslaDecider.decide_reduce`, `load_nbc.py`) defers the stop while
`seconds_remaining > gap / (1200/3600)` (capped at `MAX_DEFER_SECS=120`),
so an exact-hit stop lands precisely on target — assuming the prediction
is frozen. On a sustained ramp (sunset, `bugs/2026-09-26-tesla-stop-
charging.log`) the frozen assumption defers one cycle too long.

`GapTrendTracker` (`gap_trend.py`, constants `GAP_TREND_*`) estimates the
adjusted-gap slope across cycles: an EWMA over a 3-sample window keyed on
`data_point_at`, with flat repeats neutral and opposing slopes rejecting.
History clears on two boundaries — a quarter-hour rollover, and a
`data_point_at` delta above `GAP_TREND_MAX_SPAN_SECS` (120 s, matching the
furthest horizon a defer decision examines). The quarter-hour identity is
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
never alter behavior. Plug
decisions are intentionally out of scope (Phase I: Tesla stop only).

**Two sign conventions, deliberately:**

- **Engine-internal "gap"** (CycleDiagnostics, `gapminder_decide` logs):
  `target_wh - predicted_wh` — positive means surplus to absorb. This is
  control math; do not change it without revisiting the decision engine.
- **Operator-facing NBC-signed figures** (e.g. the Telegram
  "skipped: no actions" log lines): `predicted_wh - target_wh` — negative
  = excess solar, positive = grid draw, matching how NBC predictions are
  displayed everywhere else. Users are used to reading -N as solar;
  these lines say so explicitly (`positive=grid draw`).

## Turn-on jitter guard

Sometimes the adjusted-gap estimate oscillates so hard that its own
cycle-to-cycle swing exceeds the gap it is claiming
(`bugs/2026-10-01-tesla-overshoot.log`: `12.7 → 28.3 → 3.4 → 7.7 → −1.2`
Wh across five data points, producing two Tesla increases that overshot
the −9 Wh target to +1.74 Wh — miss +10.7). `GapTrendTracker.churn_wh_per_s`
(`gap_trend.py`) measures this as an EWMA of `|Δslope|` — the jitter —
and reports it as `gap_jitter_wh_per_s` in
`CycleDiagnostics`/JSON/SSE even while the trend itself stays untrusted
(a trend that never confirms is exactly where jitter matters).

`GapMinder.turn_on_jitter_guard_fires()` (`load_nbc.py`) declines a
turn-on cycle when `churn × seconds_remaining ≥ JITTER_GUARD_FRACTION ×
gap` (constant `2.0` in `constants.py`, sized by the replays in
`tests/test_tesla_overshoot_replay.py` and
`tests/test_undershoot_jitter_retune.py`, not by intuition): the estimate
is swinging harder than its verdict, so the verdict is not actionable
information. Three gates keep that comparison honest, all sized by the
same evidence-and-replay discipline:

* `JITTER_FLOOR_WH_PER_S` (`0.2`) — churn below this is the forecast
  window stepping between quantization levels, not oscillation. Without
  it `churn ≥ gap / R` collapses to ~0.01 Wh/s on a calm quarter, where
  it measured 0.0091–0.0124 — *below* that quarter's own realized
  `sigma_rate` of 0.0222 — and reported "excessive jitter" 35 times
  (`bugs/2026-10-02-sunrise-marine-layer-jitter.log`, cluster A, churn
  0.0147–0.0468). The incident's churn (≈0.80 at c579) is 4× above it.
* `JITTER_HORIZON_SECS` (`150`) — the swing is projected only over
  `min(seconds_remaining, 150)`. Churn is measured from 30–60 s cycle
  spacing with a 3-sample window; scaling it by the whole remaining
  quarter (up to 900 s) projects a rate-change far past the horizon it
  speaks to, so a low churn could veto any early-quarter surplus.
* `JITTER_MAX_REMAINING_SECS` (`600`) — churn is not *measured* until at
  least a third of the quarter has real data. `predicted_wh = raw +
  prediction_w × remaining_seconds`, so past 600 s remaining at least two
  thirds of the projection is extrapolation from a ≤300 s trailing
  window; seeding the EWMA there measured one quarter-opening sign flip
  — our own `jackery` `turn_off` seen through that window — as 1.10 Wh/s
  of oscillation that then decayed ×0.7 for five minutes (cluster B).
  `GapTrendTracker.update(..., churn_ready=)` keeps the sample in the
  trend window (slope, trust rule and `gap_trend_wh_per_s` unchanged)
  and withholds it only from the churn accumulator.

* Banked cover (`GapMinder.banked_cover_cap_wh`, plugs only) — when the
  guard fires but the already-banked quarter energy (`qh1.raw_wh`,
  negative = net export, plumbed as `DecideContext.banked_wh`) covers a
  plug's full remaining cost even under zero further export
  (`banked + capacity ≤ target + hysteresis`), that plug may still turn
  on via a Tesla-less `_decide_turn_on` pass (`gapminder_banked_override`
  INFO line). Bin-packing budget, debounce, and `MIN_SECONDS_TO_ACT`
  still apply, and the cover is inherently runway-aware (big early bets
  stay blocked, shrinking late ones release). Tesla increases never take
  this path: c579's raw −49.4 would "cover" the 7.4 Wh +1 A bet the
  guard exists to block. Sized by `tests/test_banked_cover.py` against
  `bugs/2026-10-09-T17-jitter.log` triples (5 of 15 anchored guard
  events act, 10 stay blocked).

`load_nbc.jitter_swing_wh()` is the single implementation of the
capped swing, used by the predicate, by `decide()`'s
`gapminder_jitter_guard` INFO line and by the manager's
`gap_jitter ... swing_wh=` DEBUG line, so log, reason and decision can
never disagree.

The guard returns no actions, logs `gapminder_jitter_guard`,
and `_decide_actions` recomputes the *same predicate* to set
`reason="excessive_jitter"` — the outcome travels through the shared
query rather than a `DecideContext` field because that dataclass is
frozen, so decision and report can never disagree. The index forecast
card's period label then becomes `⚠ low confidence` in red (the cycle
`status` itself stays `ok`) — the same format as the other abnormal
labels `⚠ waiting for data` and `⚠ stale data`.

Regression coverage for both clusters of the marine-layer log — all 35
logged triples, replayed through the real tracker — lives in
`tests/test_marine_layer_jitter.py`, alongside the assertion that the
guard still fires on the 2026-10-01 incident.

Scope: **turn-on only**. Turn-off shedding and the ramp-aware Tesla stop
are protective and never guarded; within hysteresis the guard never
fires (nothing was due anyway); a churn value that is not yet measurable
(0.0) never fires, which is why the incident's *first* increase — taken
before the oscillation was measurable — is deliberately allowed. In the
replay the guarded variant lands the incident quarter within ~3 Wh of
the −9 Wh target instead of +1.74 Wh (acceptance: `|miss| < 10.7`).

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
