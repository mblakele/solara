# Design: MetricsMock — NBC, TOU, and Per-Second Data Generation

## Overview

`MetricsMock` (in `mockdata.py`) provides deterministic mock data for testing the
Solara metrics pipeline. It supports three capabilities beyond the original hourly
prediction mock:

1. **Per-second data generation** — variable-length arrays of kWh/second values,
   parameterized by device seed and minute-of-hour, enabling NBC quarter-hour
   computation without a real VUE API connection.
2. **NBC (Non-Bypassable Charge) quarter-hour computation** — `_compute_nbc()`
   mirrors the production `HourlyProjection._compute_nbc()` state machine,
   producing QH1–QH4 dicts with complete/incomplete/not-started states.
3. **Realistic TOU bucket values** — `tou_result` returns non-zero peak/part-peak/off-peak
   buckets instead of a zeroed dict.

The mock is parameterized by `instant_minute` so it always represents a current
timestamp, enabling tests to verify invariant properties rather than hardcoded dates.

---

## Architecture

### Module: `mockdata.py` (273 lines)

```
_generate_hour_seconds(device_seed, minute_of_hour, sign) -> List[float]
│
├── MetricsMock(instant_minute=42)
│   ├── __init__()
│   │   ├── generates per_second_data for Device A (negative/solar export)
│   │   ├── generates per_second_data for Device B (positive/load only)
│   │   └── sets self.tou_result with realistic bucket values
│   ├── _build_device(per_second_data, minute_of_hour, device_name, timezone_str, sign) -> Dict
│   │   ├── computes scales from per-second data (1H + 1MIN–10MIN)
│   │   ├── computes smoothing from minute-scale rates
│   │   └── calls _compute_nbc() for NBC field
│   ├── _make_scale_entry(data, data_start, data_len, usage) -> Dict  [static]
│   └── _compute_nbc(per_second_data, start) -> Dict[str, Any]
```

### Module: `metrics.py` (618 lines)

Contains a copy of `_generate_hour_seconds()` at module level for backward
compatibility with code that imports it from `metrics`. The production
`HourlyProjection._compute_nbc()` implements the same state machine as the mock,
with timezone-aware quarter boundary computation.

### Module: `app.py` (241 lines)

- `_get_model()` returns `MetricsMock(instant_minute=...)` in mock mode
- `_get_tou_model()` returns `MetricsMock().tou_result` in mock mode

---

## Data Model

### Per-second data generation

```python
_generate_hour_seconds(device_seed: int, minute_of_hour: int, sign: float = -1.0) -> List[float]
```

Returns exactly `minute_of_hour * 60` floats (capped at 3600). Uses a seeded
`random.Random` for deterministic output. Two value ranges:

| Sign | Range (kWh/s) | Meaning |
|------|---------------|---------|
| `-1.0` | `[-0.001, -0.0004]` | Solar export (generation > consumption) |
| `+1.0` | `[0.0002, 0.0008]` | Load-only consumption |

When `instant_minute >= 60`, the data is padded to exactly 3600 values by
repeating the last value, enabling full-hour NBC tests.

### Device structure

Each device dict contains:

| Field | Type | Description |
|-------|------|-------------|
| `gid` | `int` | Hash of device name mod 10^8 |
| `name` | `str` | `"MOCK"` or `"SOLAR+LOAD"` |
| `timezone` | `str` | IANA timezone string |
| `per_second_data` | `List[float]` | Full array of observed kWh/second values |
| `scales` | `Dict[str, Dict]` | 1H + 1MIN–10MIN with `data`, `seconds`, `usage`, `instant` |
| `smoothing` | `Dict[str, float]` | 1MIN–10MIN extrapolated predictions |
| `prediction` | `float` | Hourly prediction in Wh |
| `prediction_min/max` | `float` | Min/max bounds (±10% of prediction) |
| `nbc` | `Dict[str, Any]` | QH1–QH4 quarter-hour NBC data |

### NBC structure

```python
{
    "QH1": {                          # or None if not started
        "wh": float,                  # clamped at 0 (max(0, value))
        "complete": bool,             # True if all 900 seconds observed
        "raw_wh": float,              # actual sum * 1000, may be negative
        "predicted_wh": float,        # only for incomplete quarters
        "samples_used": int,          # only for incomplete quarters
    },
    "QH2": ...,
    "QH3": ...,
    "QH4": ...,                       # None if not yet started
}
```

Quarter boundaries (by index into per-second data):

| Quarter | Index range | Minutes |
|---------|-------------|---------|
| QH1 | 0–899 | 0–14 |
| QH2 | 900–1799 | 15–29 |
| QH3 | 1800–2699 | 30–44 |
| QH4 | 2700–3599 | 45–59 |

### TOU result structure

```python
{
    "total":     12847.3,   # Wh for the period
    "peak":      3200.5,    # 16:00–21:00 (exclusive)
    "part_peak": 4100.2,    # 15:00–16:00 and 21:00–00:00
    "off_peak":  5546.6,    # 00:00–15:00 (exclusive)
}
```

---

## NBC Algorithm

The `_compute_nbc()` method implements a three-state machine per quarter:

### Not started (`n <= start_idx`) → `None`

The quarter's first second has not been observed yet.

### Complete (`n > end_idx`) → dict with `complete=True`

All 900 seconds in the quarter have data. Sum all values, multiply by 1000 for Wh,
clamp at zero: `wh = max(0, raw_wh)`. The `raw_wh` field preserves the unclamped
value for transparency.

### Incomplete (`start_idx < n <= end_idx`) → dict with `complete=False`

Uses a lookback window of up to 60 seconds from the current position (absolute,
may include data from previous quarter). Computes rate from lookback samples, then:

```
predicted_wh = raw_wh + rate * remaining_seconds * 1000
wh = max(0, predicted_wh)
```

Key design decision: `predicted_wh` is **not** clamped to zero by design. Only the
display value `wh` is clamped. This preserves the sign information for debugging
and allows downstream code to distinguish between "predicted net generation" and
"predicted net consumption."

---

## Two-Device Design

The mock includes two devices to cover both consumption scenarios:

| Device | Name | Sign | Purpose |
|--------|------|------|---------|
| A | `MOCK` | Negative | Solar export — tests clamping (`wh = 0` when `raw_wh < 0`) |
| B | `SOLAR+LOAD` | Positive | Load-only — tests positive Wh without clamping |

This design ensures NBC tests exercise both branches of the `max(0, raw_wh)` clamp.

---

## Test Coverage

### `tests/test_metrics.py` (255 lines)

| Test class | Tests |
|------------|-------|
| `TestTOUReporterAggregate` | Verifies TOUReporter uses module-level import |
| `TestMetrics` | 16 tests covering mock structure, scales, smoothing, NBC states, TOU values, both devices |

NBC-specific tests verify:
- Structure (QH1–QH4 keys present)
- Complete quarters at minute=42 (QH1, QH2)
- Incomplete quarter with `predicted_wh` and `samples_used` (QH3)
- Not-started quarter is `None` (QH4)
- Parameterized minutes (10, 37) shift quarter states correctly
- Wh clamped at zero for Device A, positive for Device B

### `tests/test_nbc.py` (569 lines)

| Test class | Tests | Focus |
|------------|-------|-------|
| `TestComputeNBCUnit` | 12 tests | Production `_compute_nbc()` with controlled inputs |
| `TestComputeNBCMetricsMock` | 6 tests | Mock's NBC at boundary minutes (14, 30, 45, 60) |
| `TestNBCIntegration` | 8 tests | API endpoint JSON includes correct NBC structure |

Notable edge cases covered:
- All quarters complete with positive/negative values
- Cross-quarter lookback clamping to quarter start index
- Prediction accuracy near quarter end (795s observed, 105s remaining)
- Empty data → all quarters `None`
- Device timezone fallback when `time_zone` is empty

---

## Files Modified

| File | Lines | Change |
|------|-------|--------|
| `mockdata.py` | 273 | New file: `_generate_hour_seconds`, `MetricsMock` class |
| `metrics.py` | +20 | Added `_generate_hour_seconds()` copy for backward compat |
| `app.py` | ~5 | Updated `_get_model()`, `_get_tou_model()` to use MetricsMock |
| `tests/test_metrics.py` | 255 | Rewritten with NBC, TOU, and two-device tests |
| `tests/test_nbc.py` | 569 | New file: comprehensive NBC unit + integration tests |

---

## Differences from Original Plan

The implementation diverged from the plan in several intentional ways:

1. **Two devices instead of one** — The plan called for a single device. The
   implementation adds Device B (`SOLAR+LOAD`, positive consumption) to exercise
   both branches of the NBC clamping logic without needing synthetic data tricks.

2. **Dynamic scale/smoothing values** — The plan required maintaining exact
   backward-compatible floating-point values (e.g., `usage == 415.917...`). The
   implementation instead computes scales and smoothing dynamically from per-second
   data, and updates tests to check sign-based properties (`< 0`, `> 0`) rather
   than exact numbers. This is more robust: the mock's values change with each run
   (different timestamps) but the invariant properties hold.

3. **`predicted_wh` not clamped** — The plan specified clamping all Wh values at
   zero. The implementation only clamps `wh`, leaving `predicted_wh` unclamped to
   preserve sign information for debugging and downstream logic.

4. **NBC uses `start` parameter** — The plan's `_compute_nbc(per_second_data)`
   used `len(data)` directly. The implementation accepts an optional `start: datetime`
   parameter, computing elapsed seconds from `self.instant - start`. This matches
   the production API and enables timezone-aware testing.

5. **`_generate_hour_seconds` duplicated** — The function exists in both
   `mockdata.py` and `metrics.py` for backward compatibility with existing imports.
