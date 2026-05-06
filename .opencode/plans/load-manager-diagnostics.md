# Load Manager Diagnostics — Design Document

## Overview

The Load Manager runs a background cycle every 30 seconds, comparing NBC
(Net Billing Cycle) quarter-hour predictions against a target Wh value,
then deciding whether to turn flexible loads on or off to absorb excess
solar or reduce grid draw. Before this work, when no actions were taken
the system provided no explanation, and the index page showed only raw
NBC metrics with no visibility into Load Manager internals.

This document describes the design for surfacing diagnostic information
and Load Manager operational state on the index endpoint.

---

## Problem Statement

1. **Silent no-op cycles** — When `actions` is empty in dry-run mode (or
   production), there is no diagnostic output explaining why: disabled?
   hysteresis? no candidates? Tesla threshold not met? stale data?

2. **No operational visibility** — The index endpoint showed only NBC
   metrics. Load Manager enabled/dry-run state, device states, recent
   actions, and cycle diagnostics were invisible to the operator.

### Root Causes

- `run_cycle()` returns early with minimal status on: disabled, no
  incomplete QH, stale data, waiting for fresh data.
- `TetrisEngine.decide()` returns `[]` silently when the gap is within
  hysteresis (1000 Wh), no candidates are available, or the Tesla amp-
  change threshold is not met.
- The index endpoint did not read `_last_cycle_result` or
  `LoadManager.state`.

---

## Architecture

### Components Involved

| Component | File | Role |
|---|---|---|
| `LoadManager.run_cycle()` | `load_manager.py` ~L2194 | Executes one cycle, returns status dict |
| `TetrisEngine.decide()` | `load_manager.py` ~L485 | Bin-packs flexible loads into surplus gap |
| `StateTracker` | `load_manager.py` ~L433 | In-memory device state & pending effects |
| `_build_load_management_payload()` | `app.py` ~L264 | Serializes Load Manager state for index |
| Index endpoint | `app.py` ~L171 | Serves HTML/JSON with metrics + load mgmt |
| Index template | `templates/index.html` | Renders Load Management section |

### Data Flow

```
LoadManager.run_cycle()  [every 30s, background thread]
    │
    ├─ early exit → {"status": "disabled" | "stale_data" | ...}
    │
    └─ normal path
         ├─ NBCReader.get_current_qh() → predicted_wh, seconds_remaining
         ├─ TeslaController.get_charging_state() → tesla_state (optional)
         ├─ TetrisEngine.decide() → actions[]
         ├─ execute actions (or dry-run log only)
         └─ returns {status, qh, predicted_wh, target_wh, actions, diagnostics}
              │
              └─ stored in _last_cycle_result (protected by _load_manager_lock)
                   │
                   └─ index endpoint reads via _build_load_management_payload()
                        │
                        └─ template rendering  /  JSON response
```

---

## Interface Contracts

### `run_cycle()` Return Dict (Designed)

The return dict is enriched with a `diagnostics` key on every code path:

```python
{
    "status": str,           # "ok" | "dry-run" | "disabled" |
                            # "no_incomplete_qh" | "stale_data" |
                            # "waiting_for_fresh_data"
    "qh": str | None,       # quarter-hour label, e.g. "QH2"
    "predicted_wh": float | None,
    "target_wh": float | None,
    "actions": list[dict],  # [{"device": str, "action": str}, ...]
    "diagnostics": {        # ← NEW
        "gap_wh": float | None,           # target_wh - predicted_wh
        "hysteresis_wh": 1000,            # threshold for reference
        "seconds_remaining": int | None,
        "reason": str,                    # explanation when actions is empty
        "tesla_state": dict | None,       # charging state if Tesla configured
        "plugs_configured": list[str],    # names of configured plugs
    },
}
```

The `reason` field distinguishes why no actions were generated:

| Value | Meaning |
|---|---|
| `"ok"` | Actions were generated or cycle completed normally |
| `"disabled"` | Load Manager is disabled |
| `"no_incomplete_qh"` | No incomplete quarter-hour found |
| `"stale_data"` | NBC data older than 120 seconds |
| `"waiting_for_fresh_data"` | Pending effects not yet reflected in NBC |
| `"hysteresis"` | Gap within ±1000 Wh hysteresis band |
| `"no_candidates"` | Gap exceeds hysteresis but no toggleable devices |

### `StateTracker.to_dict()` (Designed)

A serialization helper on `StateTracker` for clean API/template consumption:

```python
def to_dict(self) -> dict[str, Any]:
    """Serialize state for API/template consumption."""
    return {
        "devices": {name: {
            "desired_state": ds.desired_state,
            "actual_state": ds.actual_state,
            "current_amps": ds.current_amps,
            "last_toggle": ds.last_toggle.isoformat() if ds.last_toggle else None,
        } for name, ds in self.devices.items()},
        "pending_effects": [{
            "device_name": eff.device_name,
            "action": eff.action,
            "timestamp": eff.timestamp.isoformat(),
            "power_delta_wh": eff.power_delta_wh,
        } for eff in self.pending_effects],
        "last_nbc_fetch": (self.last_nbc_fetch.isoformat()
                          if self.last_nbc_fetch else None),
    }
```

### Index Endpoint Payload

The HTML template receives `load_management` and the JSON response includes
`loadManagement` (camelized via `camelize()`):

```python
{
    "enabled": bool,
    "dry_run": bool,
    "target_wh": float,
    "nbc_device": str,
    "devices": dict[str, DeviceStateDict],
    "pending_effects": list[PendingEffectDict],
    "last_cycle_result": dict,  # from run_cycle() return
}
```

### Template Sections

The `templates/index.html` Load Management section renders:

1. **Status line** — enabled/disabled, dry-run flag, target Wh
2. **Last Cycle** — status, predicted Wh, actions taken
3. **Devices** — table of configured devices with desired/actual state, amps
4. **Pending Effects** — recent actions (or would-be actions in dry-run)

---

## Implementation Status

| # | Change | Status | Details |
|---|---|---|---|
| 3 | `_build_load_management_payload()` in `app.py` | ✅ Done | L264-310, serializes state inline |
| 3 | Index endpoint passes load management | ✅ Done | L188, L192-201 |
| 4 | `templates/index.html` Load Management section | ✅ Done | L184-265 |
| 2 | `StateTracker.to_dict()` | ⏳ Pending | Serialization currently inline in `app.py` |
| 1 | `diagnostics` dict in `run_cycle()` | ⏳ Pending | Early exits and normal path not enriched |
| 1 | `reason` logic for empty actions | ⏳ Pending | Requires gap computation before `decide()` call |

Items marked **⏳ Pending** remain as future work. The index endpoint already
provides meaningful visibility into Load Manager state; the pending items
add deeper diagnostic detail for troubleshooting no-op cycles.

---

## Remaining Work (Pending Items)

### A. Enrich `run_cycle()` with Diagnostics

**File**: `load_manager.py`, `run_cycle()` ~L2194-2285

On every return path (early exits and normal execution), populate a
`diagnostics` dict. For early exits, populate what is available (e.g.,
`"disabled"` reason with no gap computation needed). For normal execution,
compute `gap_wh = target_wh - predicted_wh` before calling `decide()`, and
set `reason` based on whether actions were generated:

1. If `abs(gap_wh) <= HYSTERESIS_WH (1000)`, set reason to `"hysteresis"`
   and skip calling `decide()` entirely.
2. If `decide()` returns `[]`, set reason to `"no_candidates"`.
3. If actions are returned, set reason to `"ok"`.

### B. Extract `StateTracker.to_dict()`

**File**: `load_manager.py`, class `StateTracker` ~L433

Add the `to_dict()` method described above. Then refactor
`_build_load_management_payload()` in `app.py` to call `lm.state.to_dict()`
instead of the current inline serialization. This centralizes the
serialization contract on the state object itself.

---

## Thread Safety

`_build_load_management_payload()` reads `_last_cycle_result` under
`_load_manager_lock`, following the same pattern used by `load_status()`.
The `run_cycle()` method acquires `self._lock` for its entire execution.
These are separate locks; the payload builder only needs the module-level
lock to safely copy the last result.

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Mock mode: `_get_load_manager()` returns `None` | Template uses `{% if load_management %}` guard |
| `to_dict()` adds overhead on hot path | Method is only called from index endpoint (user-requested, not periodic) |
| Diagnostics dict grows large | Tesla state only included when Tesla configured; plugs list is names only |
| Stale diagnostics displayed | Index auto-reloads every 2 min; cycle runs every 30s |

---

## Files Modified

| File | Lines | Change |
|---|---|---|
| `load_manager.py` | ~2194-2285 | Enrich `run_cycle()` return with diagnostics |
| `load_manager.py` | ~433-483 | Add `StateTracker.to_dict()` |
| `app.py` | ~264-310 | Use `to_dict()` instead of inline serialization |
| `templates/index.html` | ~184-265 | Diagnostics display in Load Management section |
| `AGENTS.md` | structure | Update Key Architecture section |
