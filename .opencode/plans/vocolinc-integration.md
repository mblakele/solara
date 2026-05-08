# VOCOlinc Smart Plug Integration — Design Document

## 1. Purpose

Extend the solar self-consumption load manager to control VOCOlinc smart plugs
alongside existing HomeKit (aiohomekit) plugs. The system supports mixing both
plug types in the same configuration through a composite controller that routes
each plug operation to the correct backend.

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                  LoadManager                         │
│  (load_manager.py — orchestrates 30s cycles)         │
│                                                     │
│  self.plug_ctrl: AbstractPlugController ─────────┐   │
└───────────────────────────────────────────────────┼───┘
                                                    │
                    ┌───────────────────────────────┘
                    │
        ┌───────────┴───────────┐
        │  CompositePlugCtrl    │  (when both plug types configured)
        │  routes by            │
        │  controller_type      │
        └───┬──────────────┬────┘
            │              │
     ┌──────▼──────┐  ┌───▼────────────┐
     │  HomeKit    │  │  VOCOlinc      │
     │  Controller │  │  Controller    │
     │  (aiohomekit)│  │  (vocolinc.py) │
     └─────────────┘  └────────────────┘
```

When only one plug type is configured, `LoadManager` uses that backend
directly without a composite layer.

### 2.1 Component Relationships

| Component | File | Role |
|---|---|---|
| `AbstractPlugController` | `load_manager.py` | Interface contract for all plug backends |
| `RealPlugController` | `load_manager.py` | HomeKit backend via aiohomekit |
| `PlugController` | `load_manager.py` | Stub/in-memory mock for HomeKit plugs |
| `VocolincPlugController` | `load_manager.py` | VOCOlinc backend via local vocolinc.py library |
| `CompositePlugController` | `load_manager.py` | Routes operations to correct backend |
| `PlugConfig` | `load_manager.py` | Per-plug configuration with `controller_type` tag |
| `vocolinc.VOCOlinc` | `vocolinc.py` | Unofficial VOCOlinc API client (AWS Cognito + IoT) |

## 3. Design Decisions & Rationale

### 3.1 Separate Environment Variable Prefixes

HomeKit plugs use `LOAD_PLUG_<NAME>=...` while VOCOlinc plugs use
`LOAD_VOCOLINC_PLUG_<NAME>=...`. This separation lets the loader distinguish
plug types at parse time and assign the correct `controller_type` without
requiring an extra field in the env var value.

**Rationale:** Adding a type tag to the colon-delimited value format would
break backward compatibility with existing `LOAD_PLUG_*` configurations.
Separate prefixes are self-documenting and require zero migration.

### 3.2 Auto-Detection of Controller Type

When `LoadManager.__init__` runs with `plug_ctrl=None`, it inspects the
environment for both `LOAD_PLUG_*` and `LOAD_VOCOLINC_PLUG_*` variables:

| HomeKit plugs | VOCOlinc plugs | Controller selected |
|---|---|---|
| Yes | Yes | `CompositePlugController` |
| No | Yes | `VocolincPlugController` |
| Yes | No | `RealPlugController` or `PlugController` per `LOAD_PLUG_CONTROLLER` |
| No | No | `PlugController` (stub) |

**Rationale:** Most deployments will have only one plug type. Auto-detection
avoids requiring users to set `LOAD_PLUG_CONTROLLER` explicitly when there is
no ambiguity. The env var remains available for overriding the HomeKit
backend type (real vs. stub) in mixed configurations.

### 3.3 Lazy Initialization for VOCOlinc

`VocolincPlugController` defers the expensive `VOCOlinc.login()` call until
the first `get_state()` or `set_state()` operation. The login involves AWS
Cognito authentication, credential refresh, and IoT device shadow discovery.

**Rationale:** VOCOlinc plugs may be configured but unavailable (e.g., during
testing or when credentials are missing). Lazy initialization lets the system
start without failing, and errors surface at the point of first use where they
are actionable.

### 3.4 Threading for Synchronous Calls

The vocolinc.py library uses boto3 and requests, both synchronous. All calls
are wrapped with `asyncio.to_thread()` to avoid blocking the event loop.

**Rationale:** `LoadManager` runs in a background thread and uses an async
event loop for plug operations. Blocking that loop would delay all other
plugs and the NBC prediction fetch. The `asyncio.to_thread()` wrapper is the
minimal change needed to make a synchronous library compatible with an async
interface.

### 3.5 Composite Controller Merges Plug Configurations

`CompositePlugController.__init__` merges `plugs` dicts from both backends
using `getattr(ctrl, "plugs", {})`. The merged dictionary is exposed as
`self.plugs` so the rest of `LoadManager` sees a unified view.

**Rationale:** The `GapMinder`, `StateTracker`, and action execution code
all iterate over `LoadManager.plugs`. Keeping a single merged dict avoids
duplicating iteration logic throughout the codebase.

## 4. Component Design

### 4.1 `PlugConfig`

```python
@dataclass
class PlugConfig:
    name: str
    accessory_id: str          # HomeKit: IP address; VOCOlinc: friendly name
    power_watts: float
    role: Literal["flexible", "fixed"]
    priority: int = 0
    controller_type: Literal["homekit", "vocolinc"] = "homekit"
```

The `controller_type` field is the routing key. It defaults to `"homekit"`
for backward compatibility with existing configurations that do not specify
it. `load_vocolinc_plugs_from_env()` explicitly sets it to `"vocolinc"`.

### 4.2 `VocolincPlugController`

```python
class VocolincPlugController(AbstractPlugController):
    def __init__(self, plugs, username=None, password=None) -> None
    async def get_state(self, name: str) -> bool | None
    async def set_state(self, name: str, on: bool) -> bool
```

Key behaviors:

- **Lazy init:** `_ensure_initialized()` creates the `VOCOlinc` client and
  calls `login()` on first operation. Subsequent calls return immediately.
- **Credentials fallback:** If `username`/`password` are not passed to
  `__init__`, the constructor falls back to `VOCOLINC_USERNAME` /
  `VOCOLINC_PASSWORD` environment variables.
- **Error handling:** Both `get_state` and `set_state` catch `RuntimeError`
  (from failed initialization) and general `Exception`, logging the error
  and returning `None` / `False` respectively. Unknown plug names are
  logged as warnings.

### 4.3 `CompositePlugController`

```python
class CompositePlugController(AbstractPlugController):
    def __init__(self, homekit_ctrl, vocolinc_ctrl) -> None
    async def get_state(self, name: str) -> bool | None
    async def set_state(self, name: str, on: bool) -> bool
```

Key behaviors:

- **Routing:** Each operation looks up the plug in the merged `self.plugs`
  dict, checks `controller_type`, and delegates to the corresponding
  backend.
- **Merge strategy:** `__init__` calls `getattr(homekit_ctrl, "plugs", {})`
  and `getattr(vocolinc_ctrl, "plugs", {})`, then updates into a single
  dict. This works because plug names are unique across backends
  (enforced by the env var prefix separation).
- **Unknown plugs:** Returns `None` for `get_state` and `False` for
  `set_state`, logging a warning.

### 4.4 `LoadManager.__init__` Controller Selection

The controller selection logic (lines 2156-2195 of `load_manager.py`)
follows this decision tree:

1. If `plug_ctrl` is passed explicitly, use it directly (supports testing).
2. Otherwise, load both `plugs_from_env` and `vocolinc_plugs`.
3. If both are non-empty, create a `CompositePlugController`.
4. If only VOCOlinc plugs exist, create a `VocolincPlugController`.
5. Otherwise, use `LOAD_PLUG_CONTROLLER` to choose between
   `RealPlugController` and `PlugController`.

## 5. Configuration

### 5.1 Environment Variables

| Variable | Required | Description |
|---|---|---|
| `VOCOLINC_USERNAME` | Conditional | VOCOlinc account email. Required if VOCOlinc plugs are configured. |
| `VOCOLINC_PASSWORD` | Conditional | VOCOlinc account password. Required if VOCOlinc plugs are configured. |
| `LOAD_VOCOLINC_PLUG_<NAME>` | Optional | Format: `<device_name>:<power_watts>:<role>[:<priority>]` |
| `LOAD_PLUG_CONTROLLER` | Optional | `"real"` or `"stub"`. Controls the HomeKit backend type. Auto-detected when unset and only HomeKit plugs exist. |

### 5.2 Example Configuration

```env
# HomeKit plug
LOAD_PLUG_WATER_HEATER=192.168.1.50:4500:flexible:10

# VOCOlinc plug
LOAD_VOCOLINC_PLUG_FLOOR_LAMP=floor_lamp:60:flexible:5

# VOCOlinc credentials
VOCOLINC_USERNAME=user@example.com
VOCOLINC_PASSWORD=secret

# With both types configured, a CompositePlugController is created
# automatically. LOAD_PLUG_CONTROLLER controls the HomeKit backend:
LOAD_PLUG_CONTROLLER=real
```

## 6. Integration with vocolinc.py

The local `vocolinc.py` library provides the `VOCOlinc` class with:

- **`VOCOlinc(username, password)`** — Constructor storing credentials.
- **`client.login()`** — Authenticates via AWS Cognito, refreshes AWS
  credentials, discovers devices through VOCOlinc's cloud API, and populates
  `client.devices`.
- **`client.get_plug(device_name)`** — Queries the AWS IoT Device Shadow
  for the current on/off state. Returns `bool`.
- **`client.set_plug(device_name, on)`** — Updates the Device Shadow to
  turn the plug on or off.

The library handles AWS credential rotation internally (3600s token TTL,
300s refresh margin). `mypy` is configured to ignore errors from the
`vocolinc` module since it is an unofficial library without type stubs.

## 7. Testing Strategy

Tests in `tests/test_load_manager.py` cover:

| Test Class | Coverage |
|---|---|
| `TestVocolincCredentials` | `load_vocolinc_credentials()` presence, absence, and whitespace handling |
| `TestVocolincPlugEnvLoading` | `load_vocolinc_plugs_from_env()` parsing, validation, and `controller_type` assignment |
| `TestVocolincPlugController` | `VocolincPlugController` get/set state with mocked vocolinc client, error paths |
| `TestCompositePlugController` | Routing to correct backend, merged plug dict, unknown plug handling |
| `TestLoadManagerControllerSelection` | Auto-detection with both types, HomeKit-only, VOCOlinc-only, and no-plug fallback |
| `TestCompositeIntegration` | End-to-end composite controller with mixed plug types in a real `LoadManager` instance |

The vocolinc client is mocked via `unittest.mock.patch` targeting
`vocolinc.VOCOlinc` to avoid actual network calls during testing.

## 8. Operational Considerations

### 8.1 Failure Modes

| Scenario | Behavior |
|---|---|
| VOCOlinc login fails | `VocolincPlugController` logs the error; operations return `None`/`False`; load management cycle continues with other plugs |
| VOCOlinc credentials missing | Lazy init raises `RuntimeError` on first operation; error is caught and logged |
| VOCOlinc device not found | `get_plug`/`set_plug` raise an exception; caught and logged; operation returns failure |
| boto3/pyjwt unavailable | ImportError at module load time; caught by mypy `ignore_missing_imports` |

### 8.2 Dependencies

Two additional dependencies were added to `pyproject.toml`:

- `boto3>=1.34.0` — AWS SDK for IoT Device Shadow operations
- `pyjwt>=2.8.0` — JWT token parsing for VOCOlinc auth

### 8.3 Rollout Path

1. Verify `boto3` and `pyjwt` are available in the deployment environment.
2. Set `VOCOLINC_USERNAME` and `VOCOLINC_PASSWORD` in `.env`.
3. Add `LOAD_VOCOLINC_PLUG_<NAME>` variables for each VOCOlinc device.
4. If HomeKit plugs also exist, the composite controller activates automatically.
5. Validate with dry-run mode (`LOAD_MANAGE_DRY_RUN=True`) before enabling.
