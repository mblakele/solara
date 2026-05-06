# Implementation Plan: Load Management Feature

## Architecture Overview

```
┌───────────────────────────────────────────────────────┐
│  Flask App (app.py)                                    │
│  ┌──────────────────┐   ┌──────────────────────────┐  │
│  │ Background Thread │   │ POST /api/v1/load/manage │  │
│  │ (~30s interval)   │   │ GET  /api/v1/load/status │  │
│  └────────┬─────────┘   └──────────────┬───────────┘  │
│           │                            │               │
│           ▼                            │               │
│  ┌────────────────────────────────────────────────┐   │
│  │          LoadManager (load_manager.py)         │   │
│  │  ┌─────────────┐ ┌──────────┐ ┌─────────────┐ │   │
│  │  │ NBCReader+  │ │ PlugCtrl │ │ TeslaCtrl   │ │   │
│  │  │ Cache       │ │(async→)  │ │ (async→)    │ │   │
│  │  └──────┬──────┘ └────┬─────┘ └──────┬──────┘ │   │
│  │         │              │              │         │   │
│  │  ┌───────────────────────────────────────────┐ │   │
│  │  │  TetrisEngine + StateTracker + NBC Cache  │ │   │
│  │  └───────────────────────────────────────────┘ │   │
│  └────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────┘
```

## Key Design Decisions (from user feedback)

1. **NBC Caching**: Historical quarters don't change. Cache HourlyProjection result, only refetch when current QH changes or cache expires (~60s TTL). This avoids hammering the Emporia API every 30s while still getting fresh predictions.
2. **User-specified device**: `LOAD_NBC_DEVICE=<device_name>` env var selects which VUE device's NBC drives decisions.
3. **"Tetris" load matching**: DecisionEngine tries to fit available loads into the surplus gap, considering each load's capacity (watts × remaining QH seconds). Some loads saturate (Tesla hits charge limit) — engine must detect and skip saturated loads.
4. **Tesla charge amps**: Fine-grained control beyond start/stop. Engine adjusts amps up/down to rightsize charging to match available surplus after accounting for other loads.

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `load_manager.py` | **Create** | Core module: all classes below |
| `tests/test_load_manager.py` | **Create** | Unit + mock tests |
| `app.py` | Modify | Background thread, endpoints |
| `pyproject.toml` | Modify | Add async deps |

## ENV Variable Schema

```ini
# === Load Management Toggle ===
LOAD_MANAGE_ENABLED=True

# === NBC Target (Wh per QH) ===
# Negative = target excess solar export, Positive = target consumption
LOAD_TARGET_WH=-500

# === Device Selection ===
LOAD_NBC_DEVICE=main_panel  # name of VUE device whose NBC drives decisions

# === Hysteresis & Debounce (hardcoded) ===
# Hysteresis margin: 1000 Wh
# Min toggle interval: 60s per device
# Stale data threshold: 120s

# === Smart Plug Configuration ===
# Format: LOAD_PLUG_<NAME>=<accessory_id>:<power_watts>:<role>[:<priority>]
# role = "flexible" (can be turned off) or "fixed" (only turn on, never off)
# priority = integer, lower = higher priority (default 0)
LOAD_PLUG_WATER_HEATER=abc123def456:4500:flexible:10
LOAD_PLUG_POOL_PUMP=789xyz000:1500:flexible:20

# === Tesla Vehicle Configuration ===
TESLA_CLIENT_ID=my-client-id
TESLA_CLIENT_SECRET=my-client-secret
TESLA_REDIRECT_URI=http://localhost:8000/api/v1/tesla/callback
TESLA_VEHICLE_ID=1234567890abcdef

# Tesla charge amp range (for fine-grained control)
TESLA_CHARGE_AMPS_MIN=5    # minimum amps to set when charging
TESLA_CHARGE_AMPS_MAX=48   # maximum amps vehicle supports

# === Tesla Home Location ===
TESLA_HOME_LAT=37.7749
TESLA_HOME_LON=-122.4194
TESLA_HOME_RADIUS_M=500
```

---

## Module Design: `load_manager.py`

### Data Classes

```python
@dataclass
class PlugConfig:
    name: str              # "water_heater"
    accessory_id: str      # HomeKit accessory ID
    power_watts: float     # estimated draw in watts
    role: Literal["flexible", "fixed"]  # can we turn it off?
    priority: int = 0      # lower = higher priority for activation

@dataclass
class TeslaConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    vehicle_id: str
    home_lat: float
    home_lon: float
    home_radius_m: float
    charge_amps_min: int = 5
    charge_amps_max: int = 48

@dataclass
class DeviceState:
    name: str
    last_toggle: datetime          # when we last sent on/off/amp command
    desired_state: bool            # what we think the device should be (on/off)
    actual_state: bool | None      # last known state from API
    current_amps: int | None       # for Tesla: current charge amps

@dataclass
class PendingEffect:
    device_name: str
    action: Literal["turn_on", "turn_off", "set_amps"]
    timestamp: datetime
    power_delta_wh: float          # expected Wh impact for remaining QH seconds
```

### NBCReader + Cache

```python
class NBCCache:
    """Caches HourlyProjection results to avoid redundant API calls.
    
    Historical quarters don't change, so we only refetch when the current
    incomplete quarter changes or the cache TTL expires (~60s).
    """
    
    def __init__(self, ttl_seconds: int = 60):
        self._cache: dict[str, Any] | None = None
        self._cached_at: datetime | None = None
        self._cached_qh: str | None = None  # which QH was incomplete at cache time
        self._ttl = timedelta(seconds=ttl_seconds)
    
    def get_or_fetch(self, device_name: str) -> tuple[dict[str, Any] | None, bool]:
        """Return (nbc_data, was_fresh_from_cache).
        
        Returns cached data if: not expired AND same incomplete QH as before.
        Fetches fresh data otherwise. Returns (None, False) if no incomplete QH.
        """

class NBCReader:
    """Reads current QH predicted_wh from metrics for a specific device."""
    
    def __init__(self, cache: NBCCache):
        self.cache = cache
    
    def get_current_qh(
        self, device_name: str
    ) -> tuple[str, float, int] | None:
        """Return (qh_name, predicted_wh, seconds_remaining) for incomplete QH.
        
        Uses cache to avoid redundant API calls. Returns None if all quarters
        are complete or no data available.
        """
```

### StateTracker

```python
class StateTracker:
    """In-memory state of managed devices and pending effects."""
    
    def __init__(self):
        self.devices: dict[str, DeviceState] = {}
        self.pending_effects: list[PendingEffect] = []
        self.last_nbc_fetch: datetime | None = None
        self.last_nbc_predicted_wh: float | None = None
    
    def is_stale(self, now: datetime) -> bool:
        """Check if NBC data is >120s old."""
    
    def has_pending_effect_since(self, nbc_timestamp: datetime) -> bool:
        """Return True if we took an action AFTER the last NBC data point.
        
        Pending effect isn't reflected in NBC prediction yet → wait for fresh data.
        """
    
    def estimated_current_wh(self, nbc_predicted_wh: float) -> float:
        """Estimate actual current Wh by adding pending effects to NBC prediction."""
    
    def can_toggle(self, device_name: str, now: datetime) -> bool:
        """Check debounce: has MIN_TOGGLE_SECS elapsed since last toggle?"""
    
    def get_load_capacity_wh(
        self, plug: PlugConfig, seconds_remaining: int
    ) -> float:
        """Return Wh this load can contribute if turned on for remaining QH seconds."""
```

### TetrisEngine (renamed from LoadDecisionEngine)

The core algorithm is a **bin-packing-style** fit: given a surplus gap (negative = excess solar to consume), try loads in priority order until the gap is filled or no more loads available.

```python
class TetrisEngine:
    """Fit flexible loads into the NBC surplus gap like tetris pieces."""
    
    HYSTERESIS_WH = 1000
    MIN_TOGGLE_SECS = 60
    STALE_THRESHOLD_SECS = 120
    
    def decide(
        self,
        predicted_wh: float,
        target_wh: float,
        seconds_remaining: int,
        state: StateTracker,
        plugs: dict[str, PlugConfig],
        tesla: TeslaState | None,  # None if not available/safe
    ) -> list[PendingEffect]:
        """Return ordered list of actions to take.
        
        Algorithm (excess solar case — predicted < target):
        1. gap = target_wh - predicted_wh  (positive = how much load we need to add)
           If abs(gap) <= HYSTERESIS_WH → return []
        2. Build candidate list of loads that can be turned on:
           - Fixed plugs (always candidates for turning on)
           - Flexible plugs that are currently OFF
           - Tesla (if available, plugged in, at home, not at charge limit)
        3. Sort candidates by priority (lower number = higher priority)
        4. For each candidate:
           a. Skip if can_toggle() fails (debounce)
           b. Compute capacity_wh = watts × seconds_remaining / 1000
           c. If capacity_wh <= gap → turn it on, subtract from gap
           d. If capacity_wh > gap AND device supports partial control (Tesla amps)
              → set amps to consume exactly the remaining gap
           e. If capacity_wh > gap AND no partial control → skip (would overshoot)
        5. Return collected actions
        
        Algorithm (over-target case — predicted > target):
        1. gap = predicted_wh - target_wh  (positive = how much load to remove)
           If abs(gap) <= HYSTERESIS_WH → return []
        2. Build candidate list of loads that can be turned OFF:
           - Flexible plugs currently ON (skip fixed plugs)
           - Tesla charging (can stop or reduce amps)
        3. Sort by priority (lower = turn off first? or reverse?)
        4. For each candidate:
           a. Skip if debounce fails
           b. Compute savings_wh = watts × seconds_remaining / 1000
           c. If savings_wh <= gap → turn it off, subtract from gap
           d. Tesla: reduce amps instead of stopping if possible
        5. Return collected actions
        
        Algorithm (Tesla amp adjustment):
        - After plug decisions, if Tesla is charging and there's still a residual gap:
          Compute target_amps = clamp(gap / (seconds_remaining * voltage / 1000), min, max)
          If target_amps differs from current by > threshold → set_amps action
        """
```

### PlugController (async)

```python
class PlugController:
    """Controls HomeKit smart plugs via aiohomekit."""
    
    def __init__(self, plugs: dict[str, PlugConfig]):
        self.plugs = plugs
    
    async def get_state(self, name: str) -> bool | None:
        """Query HomeKit accessory on/off state. Returns None on error."""
    
    async def set_state(self, name: str, on: bool) -> bool:
        """Turn plug on or off. Returns True on success."""
```

### TeslaController (async)

```python
class TeslaController:
    """Controls Tesla vehicle charging via tesla-fleet-api."""
    
    def __init__(self, config: TeslaConfig):
        self.config = config
    
    async def authenticate(self) -> None:
        """Perform OAuth flow or load cached token."""
    
    async def is_at_home(self) -> bool:
        """Check vehicle GPS against home lat/lon using haversine distance."""
    
    async def is_plugged_in(self) -> bool:
        """Check chargeState indicates plugged in (Charging or Connected)."""
    
    async def get_charge_limit_pct(self) -> float | None:
        """Get target SOC percentage. Returns None if at limit or error."""
    
    async def is_at_charge_limit(self) -> bool:
        """Check if vehicle has reached its charge limit (saturated load)."""
    
    async def start_charging(self) -> bool:
        """Send charge_start command."""
    
    async def stop_charging(self) -> bool:
        """Send charge_stop command."""
    
    async def set_charge_amps(self, amps: int) -> bool:
        """Set charging amps within [min, max] range."""
    
    async def get_charging_state(self) -> TeslaState | None:
        """Return full state: charging_bool, current_amps, soc_pct, plugged_in."""


@dataclass
class TeslaState:
    is_charging: bool
    current_amps: int | None
    soc_percent: float | None
    plugged_in: bool
    at_home: bool
    at_charge_limit: bool
```

### LoadManager (main orchestrator)

```python
class LoadManager:
    """Top-level orchestrator that runs the load management loop."""
    
    def __init__(self):
        self.nbc_cache = NBCCache()
        self.nbc_reader = NBCReader(self.nbc_cache)
        self.state = StateTracker()
        self.plug_ctrl = PlugController(load_plugs_from_env())
        tesla_config = load_tesla_config_from_env()
        self.tesla_ctrl = TeslaController(tesla_config) if tesla_config else None
        self.engine = TetrisEngine()
        self.target_wh = config("LOAD_TARGET_WH", default=-500, cast=int)
        self.nbc_device = config("LOAD_NBC_DEVICE", default="", cast=str)
        self.enabled = config("LOAD_MANAGE_ENABLED", default="False", cast=bool)
    
    def __init__(self):
        ...  # (all fields from above)
        self._lock = threading.Lock()       # protects all state mutations
    
    def run_cycle(self, force: bool = False) -> dict[str, Any]:
        """Execute one load management cycle. Returns status dict.
        
        Thread-safe: acquires self._lock for entire cycle to prevent
        race conditions with concurrent endpoint calls.
        """
        with self._lock:
            if not self.enabled:
                return {"status": "disabled"}
        
        # 1. Get NBC data (uses cache)
        qh_result = self.nbc_reader.get_current_qh(self.nbc_device)
        if qh_result is None:
            return {"status": "no_incomplete_qh"}
        qh_name, predicted_wh, seconds_remaining = qh_result
        
        # 2. Check stale / pending effects
        now = datetime.now(timezone.utc)
        self.state.last_nbc_fetch = now
        
        if self.state.is_stale(now):
            logger.warning("NBC data stale, skipping cycle")
            return {"status": "stale_data"}
        
        if self.state.has_pending_effect_since(...):
            logger.info("Pending effects not yet reflected, waiting for fresh data")
            return {"status": "waiting_for_fresh_data"}
        
        # 3. Check Tesla safety (async wrapped)
        tesla_state = None
        if self.tesla_ctrl:
            tesla_state = asyncio.run(self.tesla_ctrl.get_charging_state())
        
        # 4. Decide actions
        actions = self.engine.decide(
            predicted_wh=predicted_wh,
            target_wh=self.target_wh,
            seconds_remaining=seconds_remaining,
            state=self.state,
            plugs=self.plug_ctrl.plugs,
            tesla=tesla_state,
        )
        
        # 5. Execute actions (async wrapped)
        results = []
        for action in actions:
            success = asyncio.run(self._execute_action(action))
            if success:
                self.state.pending_effects.append(action)
                results.append({"device": action.device_name, "action": action.action})
        
        return {
            "status": "ok",
            "qh": qh_name,
            "predicted_wh": predicted_wh,
            "target_wh": self.target_wh,
            "actions": results,
        }
```

---

## Phase-by-Phase Task Breakdown

### Phase 1: Core Logic (no async deps) — ~4 hours

**Files:** `load_manager.py` (new), `tests/test_load_manager.py` (new)

| # | Task | Details | Est. Lines |
|---|------|---------|------------|
| 1.1 | Define all data classes | `PlugConfig`, `TeslaConfig`, `DeviceState`, `PendingEffect`, `TeslaState` — full type hints, docstrings, defaults | ~60 |
| 1.2 | Implement `NBCCache` | TTL-based cache with QH-change detection. `get_or_fetch()` returns cached or triggers fresh fetch. Mock-friendly interface. | ~50 |
| 1.3 | Implement `NBCReader` | Parse metrics → find device by name → extract incomplete QH → return `(qh_name, predicted_wh, seconds_remaining)`. Handle edge cases: all complete, none started, device not found. | ~60 |
| 1.4 | Implement `StateTracker` | Full implementation: `is_stale()`, `has_pending_effect_since()`, `estimated_current_wh()`, `can_toggle()`, `get_load_capacity_wh()`. | ~80 |
| 1.5 | Implement `TetrisEngine.decide()` — excess solar path | Gap calculation, hysteresis check, build candidate list (fixed + flexible OFF plugs), sort by priority, bin-pack loads into gap. Tesla amp adjustment for residual gap. | ~120 |
| 1.6 | Implement `TetrisEngine.decide()` — over-target path | Build removable candidates (flexible ON plugs only), sort by priority, remove loads to fill gap. Tesla amp reduction or stop. | ~80 |
| 1.7 | Config loading functions | `load_plugs_from_env()`: parse `LOAD_PLUG_<NAME>=id:watts:role[:priority]` pattern from decouple config. `load_tesla_config_from_env()`: return `TeslaConfig` or `None`. Validate roles, handle missing optional fields. | ~60 |
| 1.8 | **Tests**: NBCReader unit tests | Test with MetricsMock at minute=10 (QH1 incomplete), 25 (QH2), 42 (QH3), 60 (all complete). Test device name filtering. Test None returns. | ~100 |
| 1.9 | **Tests**: StateTracker unit tests | Stale detection boundaries, pending effect tracking with multiple effects, estimated Wh accuracy, debounce edge cases (< 60s, exactly 60s, > 60s). | ~80 |
| 1.10 | **Tests**: TetrisEngine — excess solar | Various gap sizes vs load capacities. Priority ordering. Hysteresis boundary (no action at ±999 Wh, action at ±1001 Wh). Tesla amp calculation for residual gap. | ~120 |
| 1.11 | **Tests**: TetrisEngine — over target | Turn off flexible only (not fixed). Priority ordering for removal. Partial removal with Tesla amp reduction. | ~80 |
| 1.12 | **Tests**: Config loading | Valid/invalid ENV formats, missing optional priority defaults to 0, role validation, TeslaConfig None when no keys set. | ~60 |

**Done when:** All Phase 1 tests pass. `uv run pylint load_manager.py` clean. `uv run mypy load_manager.py` clean.

---

### Phase 2: Controller Stubs & Interfaces — ~1 hour

**Files:** `load_manager.py` (extend)

| # | Task | Details | Est. Lines |
|---|------|---------|------------|
| 2.1 | Define abstract controller interfaces | `AbstractPlugController`, `AbstractTeslaController` with async method signatures matching final API. Enables testing without real libraries. | ~40 |
| 2.2 | Implement `PlugController` stub | Inherits abstract interface. All methods are no-ops that log the action and return success/default values. Stores state in-memory for test verification. | ~40 |
| 2.3 | Implement `TeslaController` stub | Inherits abstract interface. Returns configurable defaults (`is_at_home=True`, `is_plugged_in=False`, etc.). Supports test scenario setup via `set_mock_state()`. | ~50 |
| 2.4 | **Tests**: Stub controller verification | Verify stubs return expected defaults. Verify state changes are trackable for test assertions. | ~30 |

**Done when:** Controllers import without aiohomekit/tesla-fleet-api installed. Stubs pass interface checks.

---

### Phase 3: Integration Tests (with stubs) — ~2 hours

**Files:** `load_manager.py` (extend with LoadManager class), `tests/test_load_manager.py` (extend)

| # | Task | Details | Est. Lines |
|---|------|---------|------------|
| 3.1 | Implement `LoadManager.__init__()` and `run_cycle()` | Wire together NBCReader+Cache, StateTracker, stub controllers, TetrisEngine. Use `asyncio.run()` for async calls. Return status dict. | ~80 |
| 3.2 | **Integration test**: Excess solar scenario | MetricsMock minute=42 (QH3 incomplete, negative predicted). Stub plugs + Tesla available. Verify correct "turn on" actions in priority order. | ~60 |
| 3.3 | **Integration test**: Over-target scenario | Positive predicted > target. Verify "turn off" only for flexible plugs. Fixed plugs untouched. | ~50 |
| 3.4 | **Integration test**: Tesla safety gates | `is_at_home=False` → no Tesla actions. `is_plugged_in=False` → no start_charging. `at_charge_limit=True` → Tesla skipped as candidate. | ~60 |
| 3.5 | **Integration test**: Stale data skip | Manipulate StateTracker.last_nbc_fetch >120s old. Verify cycle returns "stale_data" status, no actions. | ~40 |
| 3.6 | **Integration test**: Pending effect wait | Insert PendingEffect after last NBC fetch timestamp. Verify engine waits for fresh data. | ~40 |
| 3.7 | **Integration test**: Cache behavior | First call triggers fetch, second call within TTL + same QH uses cache. QH boundary change forces refetch. | ~50 |
| 3.8 | **Integration test**: Tesla amp adjustment | After turning on plugs, residual gap → TeslaController.set_charge_amps() called with correct value. Verify clamp to min/max range. | ~50 |

**Done when:** Full cycle runs end-to-end against MetricsMock + stub controllers. All integration tests pass.

---

### Phase 4: Real Controller Integration — ~3 hours

**Files:** `load_manager.py` (extend), `pyproject.toml` (modify)

| # | Task | Details | Est. Lines |
|---|------|---------|------------|
| 4.1 | Add dependencies to pyproject.toml | `aiohomekit`, `tesla-fleet-api`, `aiohttp`. Run `uv sync`. | ~3 |
| 4.2 | Implement real `PlugController` with aiohomekit | Research Home Assistant's `homekit_controller` for pairing/accessory patterns. Handle: accessory connection, on/off switch service (characteristic 0x14 = On), error recovery on connection drop/aiohomekit exceptions. Store pairing data persistently. | ~120 |
| 4.3 | Implement real `TeslaController` with tesla-fleet-api | OAuth via `TeslaFleetApi.authenticate()`. Token caching (library handles). Haversine distance for `is_at_home()`. Parse `vehicle_data()` response for charge state, SOC, plugged-in status. `charge_start()`, `charge_stop()`, `set_charge_amps()`. Wrap all in try/except for `TeslaFleetError`. | ~150 |
| 4.4 | Error handling/resilience | Per-device error isolation: one plug failing doesn't block others. Log errors, mark device `actual_state=None`, retry next cycle. Tesla auth expiry → attempt refresh → log and skip if failed. | ~40 |

**Done when:** Controllers compile/type-check against real library types. Manual smoke test possible (requires actual devices).

---

### Phase 5: App Integration — ~1 hour

**Files:** `app.py` (modify)

| # | Task | Details | Est. Lines |
|---|------|---------|------------|
| 5.1 | Add background thread to app.py | Daemon thread running `_load_management_loop()` every 30s. Start in `if __name__ == "__main__"` block. Graceful: don't block app startup if LoadManager fails to init (log warning, continue). Thread-safe access to LoadManager state. | ~40 |
| 5.2 | Add `POST /api/v1/load/manage` endpoint | Manual trigger. Accept optional `?force=true` to bypass stale-data check (debug only). Return JSON with status, current NBC prediction, pending effects, device states. Use `camelize()` for response keys. | ~30 |
| 5.3 | Add `GET /api/v1/load/status` endpoint | Read-only: return current StateTracker state, last cycle result timestamp, enabled/disabled flag, cache status. | ~25 |
| 5.4 | **Tests**: Flask endpoint tests | POST returns 200 with status. Disabled mode returns "skipped". With MOCK=True exercises full cycle. GET /status returns state dict. Force param bypasses stale check. | ~60 |

**Done when:** App starts with background thread. Manual endpoint triggers cycle. Status endpoint returns state. Existing tests still pass.

---

### Phase 6: Verification & Polish — ~30 min

| # | Task | Details |
|---|------|---------|
| 6.1 | `uv run pylint` | Fix any style issues in new code |
| 6.2 | `uv run mypy` | Fix type errors, especially async return types and Optional handling |
| 6.3 | `uv run pytest` | Full suite including existing tests (no regressions) + all new load manager tests |
| 6.4 | Document ENV variables | Add `env.example` with all load management vars documented |

---

## Tetris Algorithm Detail

The core challenge: fit discrete loads into a continuous Wh gap, where some loads can be partially controlled (Tesla amps) and others are binary (plugs).

```
gap = target_wh - predicted_wh   # positive = need to add this much load

# Phase A: Binary loads (plugs) — greedy by priority
candidates = sorted([p for p in plugs if eligible], key=lambda p: p.priority)
for plug in candidates:
    capacity = plug.power_watts * seconds_remaining / 1000
    if capacity <= gap:
        # Fits entirely → turn on
        actions.append(turn_on(plug))
        gap -= capacity
    elif plug supports partial control:
        # Partial fit → adjust (Tesla amps)
        needed_watts = gap * 1000 / seconds_remaining
        actions.append(set_amps(needed_watts))
        gap = 0
        break
    else:
        # Would overshoot and no partial control → skip

# Phase B: Tesla amp fine-tuning (if still charging and gap remains)
if tesla.is_charging and abs(gap) > small_threshold:
    target_amps = clamp(current_amps + delta, min_amps, max_amps)
    if abs(target_amps - current_amps) >= 2:  # avoid micro-adjustments
        actions.append(set_amps(target_amps))
```

## Open Questions / Risks

1. **aiohomekit pairing**: Requires initial pairing dance per accessory. Need to document setup step. Pairing data (pairing ID, LTPK cert) needs persistent storage — suggest `.homekit-pairings.json` file.
2. **Tesla OAuth flow**: First-time setup requires interactive browser redirect. Need `/api/v1/tesla/callback` endpoint or separate CLI tool for initial auth.
3. **Tesla `set_charge_amps` availability**: Not all Tesla models support per-amp control via Fleet API. May need to fall back to start/stop only if amp control fails.
4. **Thread safety**: LoadManager state accessed from both background thread and request handlers (manual trigger endpoint). Need simple lock or accept eventual consistency?
5. **Priority direction for removal**: When over target and removing loads, should we remove lowest-priority first (preserve important loads) or highest-priority first (remove the ones we turned on first)?

## Questions and Answers

Question 1:
aiohomekit requires pairing data (pairing ID, LTPK cert) per accessory. Where should this be stored?

Answer:
* .homekit-pairings.json: JSON file in project root with pairing data per accessory

Question 2:
Tesla OAuth requires an initial interactive browser flow. How should first-time auth be handled?

Answer:
* CLI command (Recommended): Add uv run python app.py --tesla-auth CLI command for initial setup

Question 3:
When over target and removing loads, which order should flexible plugs be turned off?

Answer:
* Lowest priority first (Recommended): Remove least important loads first, preserving high-priority ones

Question 4:
Should I replace the stub controllers entirely with real implementations, or keep both and select via env var?

Answer:
* Both + env selector (Recommended): Keep stubs as fallback; LOAD_PLUG_CONTROLLER=real|stub selects which to use
