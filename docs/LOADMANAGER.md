# Load Management Documentation

## Architecture

Load management runs as a background thread that cycles every N seconds
(configured via `LOAD_MANAGE_INTERVAL_SECS`, default 30s). Each cycle:

1. **NBC Prediction**: Fetches quarter-hour energy prediction from the Emporia VUE API
   (cached with 60s TTL to avoid rate limits)
2. **State Adjustment**: Adjusts raw prediction with pending effect deltas from
   actions already taken this quarter-hour, so decisions account for loads already toggled
3. **GapMinder:** Compares adjusted prediction against target Wh,
   calculates the gap, and uses bin-packing to fit flexible loads into the surplus
4. **Action Execution**: Turns plugs on/off or adjusts Tesla charging amps

Key components:
- `LoadManager`: Orchestrator that runs cycles in a background thread
- `NBCCache`/`NBCReader`: Fetches and caches quarter-hour predictions (60s TTL)
- `StateTracker`: Tracks device states, pending effects, stale data detection
- `GapMinder`: Decision logic for which loads to toggle
- Controllers: `RealPlugController` (HomeKit), `VocolincPlugController`,
  `RealTeslaController` — or stub versions for testing

## Stale Data Detection

The load manager uses **data-point age** (not fetch time) to determine whether
NBC data is stale. The Emporia VUE API has inherent lag — the most recent
per-second data point in a prediction may be several seconds behind when the
API call completes. The system derives `data_point_at = fetched_at - lag` and
compares it against a 120-second threshold.

When data is stale **and** there are pending effects (actions taken since the
last data point that may not yet be reflected), the cycle is skipped to avoid
double-counting load. When data is stale but there are no pending effects, the
cycle proceeds normally since there's nothing to wait for.

If actions were taken after the last data point, the system enters a
`waiting_for_fresh_data` state until the next NBC fetch confirms those actions.

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `LOAD_MANAGE_ENABLED` | `False` | Enable/disable or time range `HH:MM-HH:MM` |
| `LOAD_TARGET_WH` | `-500` | Target Wh per quarter-hour (negative = excess solar buffer) |
| `LOAD_NBC_DEVICE` | *(required)* | Device name for NBC predictions (e.g., "EM1-XXXX") |
| `LOAD_MANAGE_INTERVAL_SECS` | `30` | Seconds between load management cycles |
| `LOAD_MANAGE_DRY_RUN` | `False` | Log actions without executing them |
| `LOAD_MANAGE_API_KEY` | *(empty, disabled)* | API key for manual trigger endpoint auth |
| `LOAD_PLUG_CONTROLLER` | `stub` | `real` (aiohomekit) or `stub` (in-memory mock) |
| `LOAD_TESLA_CONTROLLER` | `stub` | `real` (tesla-fleet-api) or `stub` (in-memory mock) |

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
that the load management engine tries to hit. Default is -500 Wh.

How it works:
- NBC predicts how many Wh you'll use in the current quarter-hour (negative = excess solar, positive = grid draw)
- The engine calculates `gap = predicted_wh - target_wh`
  - Negative gap (e.g., predicted=-2000, target=-500 → gap=-1500): too much excess solar → turn loads on to absorb it
  - Positive gap (e.g., predicted=2000, target=-500 → gap=2500): drawing too much from grid → turn flexible loads off

With the default of -500, the system aims to leave a small buffer
of excess solar unabsorbed rather than driving net usage to exactly zero.
Set it closer to 0 to absorb more solar, or more negative (e.g., -2000)
to be conservative and leave more surplus on the grid.

## Dry-Run Mode

Set `LOAD_MANAGE_DRY_RUN=True` to test load management without executing actions.
In dry-run mode:
- Actions are calculated and logged but NOT sent to devices
- Device state is NOT updated
- Status response includes `"status": "dry-run"` instead of `"status": "ok"`

Use this to verify configuration before enabling real control. Start with dry-run
enabled, review the logs for a few cycles, then disable when satisfied.

## Smart Plug Configuration

### HomeKit Smart Plugs

#### Pairing a New Accessory

Before configuring a plug in `.env`, pair it with the app:

```bash
uv run python app.py --pair-plug <name> <accessory_id> <pin>
```

- `<name>`: Label for your reference (e.g., "water-heater")
- `<accessory_id>`: IP address or mDNS name of the accessory
- `<pin>`: Setup PIN displayed on the accessory

Pairing data is saved to `.homekit-pairings.json` in the project root.

#### Configuration

Set `LOAD_PLUG_CONTROLLER=real` and configure each plug:

```env
# Format: LOAD_PLUG_<NAME>=<accessory_id>:<power_watts>:<role>[:<priority>]
LOAD_PLUG_WATER_HEATER=192.168.1.50:4500:flexible:10
LOAD_PLUG_POOL_PUMP=TasmotaPlug:1500:flexible:20
```

- **accessory_id**: Must match the IP/mDNS name used during pairing
- **power_watts**: Approximate power draw when on (used for bin-packing decisions)
- **role**: `flexible` (can be toggled on/off) or `fixed` (always-on, tracked only)
- **priority**: Lower number = higher priority for activation (optional, default 0)

### VOCOlinc Smart Plugs

Configure credentials and devices in `.env`:

```env
VOCOLINC_USERNAME=your@email.com
VOCOLINC_PASSWORD=your_password

# Format: LOAD_VOCOLINC_PLUG_<NAME>=<device_name>:<power_watts>:<role>[:<priority>]
LOAD_VOCOLINC_PLUG_FLOOR_LAMP=LivingRoomLamp:60:flexible:5
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

### POST /api/v1/load/manage

Manually trigger a load management cycle. Append `?force=true` to bypass stale-data check (debug only).

Requires `X-API-Key` header matching `LOAD_MANAGE_API_KEY` env var when set.

```bash
curl -X POST http://localhost:8000/api/v1/load/manage \
  -H "X-API-Key: your-api-key"
```

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
