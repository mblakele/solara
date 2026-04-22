# Plan: Rewrite `MetricsMock` for NBC, TOU, and Test Coverage

## Overview

Rewrite `MetricsMock` to support all current and planned features:
- **Existing**: Hourly prediction with scales (1H, 1MIN–10MIN) and smoothing
- **NBC** (`nbc.md`): Variable-length per-second arrays (up to 3600 floats) for quarter-hour NBC computation
- **TOU** (`TOU.md`): Realistic non-zero TOU bucket values

The rewritten mock will be parameterized by `instant_minute` so it always represents a current timestamp, enabling tests to verify invariant properties rather than hardcoded dates.

---

## Current State Analysis

### What exists today

| Feature | Status in MetricsMock |
|---------|----------------------|
| Single device (gid=12345, name="MOCK") | ✅ Present |
| Scales dict (1H, 1MIN–10MIN) with `data`, `usage` | ✅ Present but truncated data arrays |
| Smoothing dict (1MIN–10MIN) | ✅ Present with exact values |
| Hardcoded UTC timestamp (`2026-02-27T18:42:34`) | ❌ Static, not relative to "now" |
| Per-second kWh arrays (variable length, up to 3600) | ❌ Only 3 sample floats per scale |
| NBC field (QH1–QH4) | ❌ Missing entirely |
| TOU bucket data | ❌ Returns zeroed dict in `_get_tou_model()` |
| Second device with positive consumption | ❌ Not needed (user decision) |

### Existing tests that must work

**`test_metrics.py::TestMetrics`:**
- `test_mock_device_data`: Checks `gid=12345`, `name="MOCK"`, `timezone`, presence of `prediction`, `scales`, `smoothing`
- `test_mock_scales`: Checks scales keys, `hour_data["seconds"] == 2552`, exact `usage == 415.917...`, `instant` is datetime
- `test_mock_smoothing`: Checks smoothing keys, exact `smoothing["1MIN"] == -52.516...`

**`test_metrics.py::TestTOUReporterAggregate`:**
- Uses a partial `TOUReporter` instance (not MetricsMock) — no changes needed

**`test_app.py::TestApp`:**
- `test_index_json_mock`: Checks JSON has `devices` array with `gid`, `name`, `prediction`
- `test_index_html_mock`: Checks HTML contains "Wh, predicted total"
- `test_tou_endpoint_valid_dates`: Uses MOCK=True, expects 200 response

---

## Design Decisions

### 1. Parameterize by `instant_minute` instead of hardcoded timestamp

```python
class MetricsMock:
    def __init__(self, instant_minute: int = 42) -> None:
        self.instant = datetime.now(timezone.utc).replace(minute=instant_minute % 60, second=0, microsecond=0)
```

This makes the mock always look current. Tests must verify **invariant properties** (NBC values, prediction accuracy, TOU bucket structure) rather than absolute timestamps.

### 2. Generate deterministic variable-length per-second data for NBC

Each device needs a list of observed kWh/second values covering however far into the current hour has been observed. Use a seeded helper function to generate consistent data:

```python
def _generate_hour_seconds(device_seed: int, minute_of_hour: int) -> List[float]:
    """Generate deterministic kWh/second values for an hour.
    
    The list contains exactly `minute_of_hour * 60` floats (capped at 3600).
    This is a variable-length list — only observed seconds are included.
    len(per_second_data) directly indicates how far into the hour we are,
    so NBC can determine quarter states by index comparison without needing
    minute_of_hour passed separately.
    
    Args:
        device_seed: Seed value for reproducibility (different per device).
        minute_of_hour: Current minute (0-59), determines list length.
    
    Returns:
        List of `minute_of_hour * 60` floats (kWh/second). All elements contain
        real values — no padding or nulls. At minute=42, len(data)==2520.
    """
```

The generated data should produce realistic scale aggregates that match the existing mock's `usage` values for backward compatibility.

### 3. Maintain backward-compatible scale/smoothing values

The existing tests assert exact values:
- `scales["1H"]["seconds"] == 2552`, `usage == 415.917...`
- `smoothing["1MIN"] == -52.516...`

These must remain unchanged. The per-second data generation should be designed so that when aggregated to minute/hour scales, the results match these exact values. This means:
- The 1H scale's `usage` (415.917 Wh) comes from summing all ~2552 seconds of real data
- The smoothing values come from minute-scale rates extrapolated to hourly predictions

### 4. Add NBC field with quarter-hour computation

The mock should include pre-computed NBC data that matches what `_compute_nbc()` would produce given the synthetic per-second data:

```python
device["nbc"] = {
    "QH1": {"wh": <computed>, "complete": True, "raw_wh": <value>},
    "QH2": {"wh": <computed>, "complete": False, "predicted_wh": <value>, "samples_used": N},
    "QH3": None,  # not started yet at minute=42
    "QH4": None,
}
```

### 5. Add realistic TOU bucket data

Replace the zeroed dict with realistic values:

```python
self.tou_result = {
    "total": 12847.3,   # Wh for the period
    "peak": 3200.5,     # 16:00–21:00 (exclusive)
    "part_peak": 4100.2, # 15:00–16:00 and 21:00–00:00
    "off_peak": 5546.6,  # 00:00–15:00 (exclusive)
}
```

### 6. Keep monolithic structure

Per the metrics-refactor plan decision: MetricsMock stays as a single class with no hierarchy. All data is assembled in `__init__()`.

---

## Implementation Plan

### Step 1: Add per-second data generation helper

**File**: `metrics.py`, add before `MetricsMock` class (~line 506):

```python
def _generate_hour_seconds(device_seed: int, minute_of_hour: int) -> List[float]:
    """Generate deterministic kWh/second values for an hour.

    Returns a variable-length list of exactly `minute_of_hour * 60` floats (capped at 3600).
    All elements contain real consumption data — no padding or nulls.
    len(data) directly indicates how far into the hour we are, enabling NBC to determine
    quarter states by index comparison without needing minute_of_hour passed separately.
    Data is negative to represent solar generation exceeding consumption, matching the
    existing mock's sign convention.

    Args:
        device_seed: Seed for reproducibility.
        minute_of_hour: Current minute (0–59), determines list length.

    Returns:
        List of `minute_of_hour * 60` floats (kWh/second). At minute=42, len(data)==2520.
    """
```

**Algorithm**:
- Use `random.Random(device_seed)` for deterministic generation
- Generate values in the range [-0.001, -0.0004] kWh/s (negative = solar export)
- Return exactly `minute_of_hour * 60` floats — no padding, no zeros appended
- Scale the values so that when aggregated to minute scales, they produce the existing mock's exact usage numbers

### Step 2: Rewrite `MetricsMock.__init__()` with parameterized timestamp

**File**: `metrics.py`, rewrite `MetricsMock`:

```python
class MetricsMock:
    """
    Mock metrics data for testing. Supports NBC quarter-hour computation,
    TOU bucket reporting, and all existing hourly prediction features.
    
    The mock is parameterized by instant_minute so it always represents a
    current timestamp. Tests should verify invariant properties rather than
    absolute timestamps.
    """

    metrics: Dict[str, Any]
    tou_result: Dict[str, float]

    def __init__(self, instant_minute: int = 42) -> None:
        now = datetime.now(timezone.utc)
        self.instant = now.replace(minute=instant_minute % 60, second=0, microsecond=0)
        
        # Generate per-second data for the current hour
        minute_of_hour = instant_minute % 60
        per_second_data = _generate_hour_seconds(12345, minute_of_hour)
        
        self.metrics = {
            "api_response": {...},  # Keep existing structure
            "debug": True,
            "devices": [self._build_device(per_second_data, minute_of_hour)],
            "instant": self.instant + timedelta(seconds=2),
        }
        
        self.tou_result = {
            "total": 12847.3,
            "peak": 3200.5,
            "part_peak": 4100.2,
            "off_peak": 5546.6,
        }
```

### Step 3: Add `_build_device()` helper method

**File**: `metrics.py`, add as method of `MetricsMock`:

```python
def _build_device(self, per_second_data: List[float], minute_of_hour: int) -> Dict[str, Any]:
    """Build a single device dict with all fields."""
    # Compute scales from per-second data to match existing exact values
    # Compute smoothing from minute-scale rates
    # Compute NBC from per-second data using quarter-hour logic
    ...
```

This method assembles:
- `gid`, `name`, `timezone` (unchanged)
- `lag`, `minutes_remaining` (computed from instant)
- `prediction`, `prediction_min`, `prediction_max` (unchanged exact values)
- `scales`: 1H and 1MIN–10MIN with full data arrays, matching existing usage/seconds
- `smoothing`: unchanged exact values
- `nbc`: Quarter-hour NBC computation result

### Step 4: Add NBC computation to MetricsMock

**File**: `metrics.py`, add `_compute_nbc()` method to `MetricsMock`:

```python
def _compute_nbc(self, per_second_data: List[float], minute_of_hour: int) -> Dict[str, Any]:
    """Compute NBC values for each quarter hour.
    
    Mirrors the logic that HourlyProjection._compute_nbc() will implement.
    """
```

**Algorithm**:
- For each QH (indices 0–899 seconds within the hour):
  - If QH hasn't started (based on `minute_of_hour`): return `None`
  - If QH is complete: sum all kWh values, multiply by 1000 → Wh, clamp at zero
  - If QH is incomplete: use last 60 seconds of available data for extrapolation

### Step 5: Ensure backward-compatible scale/smoothing values

The tricky part: the existing tests assert exact numbers. The per-second data must be generated so that:

1. `scales["1H"]["seconds"] == 2552` — this is the count of non-zero seconds at minute=42
2. `scales["1H"]["usage"] == 415.917...` — sum of all kWh values × 1000
3. `smoothing["1MIN"] == -52.516...` — derived from the last minute's rate

**Approach**: Generate per-second data that, when aggregated, produces these exact numbers. This may require:
- Starting with a base value and adjusting to hit target sums
- Using the existing scale `usage` values as targets for the synthetic data generation
- The 1H scale has 2552 seconds of data (42 min 32 sec), so generate exactly that many non-zero values

### Step 6: Update `_get_tou_model()` in app.py

**File**: `app.py`, modify `_get_tou_model()`:

```python
def _get_tou_model(start_date, end_date, force_mock=False):
    is_mock = ...
    if is_mock:
        return MetricsMock().tou_result  # Now returns realistic values instead of zeros
    model = TOUReporter(start_date, end_date, logger)
    return model.tou_result
```

### Step 7: Update tests

**File**: `test_metrics.py`:

- **`TestMetrics` class**: Keep all existing assertions. The rewritten mock must produce the same exact values for backward compatibility. Add new test methods:
  - `test_mock_nbc_structure`: Verify `nbc` field exists with QH1–QH4 keys
  - `test_mock_nbc_complete_quarters`: At minute=42, QH1 and QH2 should be complete
  - `test_mock_nbc_incomplete_quarter`: QH2 at minute=42 should have `predicted_wh` field
  - `test_mock_nbc_not_started`: QH3 and QH4 at minute=42 should be `None`
  - `test_mock_tou_result`: Verify `MetricsMock().tou_result` has non-zero values with all four keys

- **`TestTOUReporterAggregate`**: No changes needed (doesn't use MetricsMock)

**File**: `test_app.py`:

- All existing tests should pass without modification
- Add new test: `test_tou_endpoint_mock_realistic_values` — verify TOU endpoint returns non-zero buckets in mock mode
- Consider adding parameterized NBC JSON response test using different `instant_minute` values

---

## Files Modified

| File | Change |
|------|--------|
| `metrics.py` | Add `_generate_hour_seconds()` helper (variable-length, up to 3600 floats); rewrite `MetricsMock.__init__()` with `instant_minute` param, `_build_device()`, and `_compute_nbc()` methods; maintain exact backward-compatible values for scales/smoothing/prediction |
| `app.py` | Update `_get_tou_model()` to return `MetricsMock().tou_result` instead of zeroed dict |
| `test_metrics.py` | Keep all existing assertions unchanged; add NBC structure tests and TOU result test |
| `test_app.py` | Keep all existing assertions unchanged; add TOU realistic values test |

---

## Risks & Trade-offs

### Risk 1: Exact value compatibility with synthetic data
**Mitigation**: Generate per-second data that is specifically calibrated to produce the exact scale usage and smoothing values. Use a two-pass approach: generate candidate data, compute aggregates, then adjust scaling factor to hit targets.

### Risk 2: NBC computation in mock must match HourlyProjection._compute_nbc()
**Mitigation**: The `_compute_nbc()` method will be implemented identically in both `MetricsMock` and `HourlyProjection`. This ensures consistency between test data and production logic.

### Risk 3: Tests that check absolute timestamps
**Mitigation**: Existing tests don't check absolute timestamp values (they only check types like `isinstance(instant, datetime)`). The new parameterized approach is safe for all existing assertions.

### Trade-off: Mock complexity increases significantly
The mock grows from ~200 lines to potentially 400+ lines with data generation, NBC computation, and device building logic. However, this is justified by the need to exercise both NBC and TOU code paths in tests. The helper methods (`_generate_hour_seconds`, `_build_device`, `_compute_nbc`) keep the main `__init__` readable.

---

## Testing Strategy

1. **Run existing test suite**: `uv run pytest` — all existing tests must pass without modification
2. **Verify NBC structure**: New tests confirm QH1–QH4 presence, completeness flags, and null handling
3. **Verify TOU values**: Confirm non-zero realistic buckets in both mock and endpoint responses
4. **Parameterized scenarios**: Test with different `instant_minute` values (e.g., 7, 25, 48) to cover different QH states

---

## Task Breakdown

Each task is scoped to 1–2 hours of work or ~32k–48k tokens of context, whichever comes first. Tasks are ordered by dependency — later tasks depend on earlier ones being complete.

---

### Task 1: Foundation — Per-second data generation + parameterized mock (~1 hr)

**Goal**: Add `_generate_hour_seconds()` helper and rewrite `MetricsMock.__init__()` to accept `instant_minute` parameter, generating per-second data and TOU result dict.

**Context needed (files to read)**:
- `metrics.py` lines 506–729 — current MetricsMock structure (~224 lines)
- `metrics.py` top-level imports (lines 1–30) — existing type hints, datetime usage (~30 lines)

**What to do**:
1. Add `_generate_hour_seconds(device_seed: int, minute_of_hour: int) -> List[float]` helper function before the MetricsMock class
    - Uses `random.Random(device_seed)` for deterministic generation
    - Returns a variable-length list of exactly `minute_of_hour * 60` floats (kWh/second), capped at 3600
    - All elements contain real values — no padding or nulls. At minute=42, len(data)==2520.
   - Values in range [-0.0004, -0.001] kWh/s (negative = solar export)
2. Rewrite `MetricsMock.__init__(self, instant_minute: int = 42)` to:
   - Compute `self.instant` relative to `datetime.now(timezone.utc)` with the given minute
   - Call `_generate_hour_seconds(12345, instant_minute % 60)` for per-second data
   - Build device dict inline (keep it simple for now — full refactoring in Task 2)
   - Set `self.tou_result` with realistic non-zero values: `{"total": 12847.3, "peak": 3200.5, "part_peak": 4100.2, "off_peak": 5546.6}`
   - Keep all existing exact values for scales (1H usage=415.917..., seconds=2552), smoothing (1MIN=-52.516...), prediction (-52.516...)
3. The `data` arrays in each scale can remain as truncated sample floats (3 elements) — the full per-second data is stored separately and used for NBC computation

**Deliverable**: A working MetricsMock that produces identical scale/smoothing/prediction values but with parameterized timestamp, per-second data, and TOU result.

---

### Task 2: `_build_device()` method — backward-compatible device assembly (~1–2 hrs)

**Goal**: Extract device-building logic into a clean `_build_device()` method that assembles all fields from the per-second data while maintaining exact backward compatibility with existing test assertions.

**Context needed (files to read)**:
- `metrics.py` lines 506–729 — current MetricsMock device dict structure (~224 lines)
- `test_metrics.py` full file — all exact value assertions (~105 lines)
- `metrics.py` lines ~380–505 — HourlyProjection class for context on how scales/smoothing are computed in production (~125 lines)

**What to do**:
1. Create `_build_device(self, per_second_data: List[float], minute_of_hour: int) -> Dict[str, Any]` method
2. The method must produce a device dict with these exact fields and values (matching current mock):
   - `gid=12345`, `name="MOCK"`, `timezone="America/Los_Angeles"`
   - `lag` — computed from instant, keep existing timedelta structure
   - `minutes_remaining = 17.466...` — derived from (60 - minute_of_hour) / ... formula
   - `minute_predicted = -468.434...`
   - `prediction = -52.516...`, `prediction_min = -52.516...`, `prediction_max = -38.242...`
   - `scales` — 1H through 10MIN with exact usage/seconds values from current mock
   - `smoothing` — 1MIN through 10MIN with exact values from current mock
3. The scales' `data` arrays remain truncated (3 sample floats each) — full per-second data is stored in a separate field for NBC use
4. Add `per_second_data` field to device dict containing the full 900-float array

**Key challenge**: The existing tests assert exact floating-point values. The `_build_device()` method must produce identical numbers. This means:
- Don't recompute scale usage from per-second data — use hardcoded exact values for backward compatibility
- Per-second data is generated but not used to derive scale/smoothing values (it's stored separately for NBC)
- Only `lag`, `minutes_remaining`, and timestamp-related fields are computed dynamically

**Deliverable**: Clean `_build_device()` method that produces byte-identical device dicts for the default `instant_minute=42` case.

---

### Task 3a: `_compute_nbc()` method in MetricsMock (~45 min)

**Goal**: Add NBC quarter-hour computation to MetricsMock, implementing the state machine described in `nbc.md`.

**Context needed (files to read)**:
- `.opencode/plans/nbc.md` full file — NBC algorithm specification (~240 lines)
- `metrics.py` lines 506–729 — current MetricsMock with per-second data from Task 1/2 (~224 lines)

**What to do**:
1. Add `_compute_nbc(self, per_second_data: List[float]) -> Dict[str, Any]` method
   - **No `minute_of_hour` parameter needed** — `len(per_second_data)` directly indicates how far into the hour we are
2. Implement quarter logic with explicit index ranges:

```python
def _compute_nbc(self, per_second_data: List[float]) -> Dict[str, Any]:
    """Compute NBC values for each quarter hour.
    
    Args:
        per_second_data: Variable-length list of observed kWh/second values.
                        len(data) = how many seconds have been observed so far.
    
    Returns:
        Dict with keys QH1–QH4, each containing NBC metrics or None.
    """
    n = len(per_second_data)  # number of observed seconds
    
    quarters = [
        ("QH1", 0, 899),       # minutes 0–14 (first 15 min)
        ("QH2", 900, 1799),    # minutes 15–29
        ("QH3", 1800, 2699),   # minutes 30–44
        ("QH4", 2700, 3599),   # minutes 45–59
    ]
    
    result = {}
    for qh_name, start_idx, end_idx in quarters:
        # Not started: quarter's first second hasn't been observed yet
        if n <= start_idx:
            result[qh_name] = None
            continue
        
        # Determine which indices have data in this quarter
        obs_start = max(start_idx, 0)
        obs_end = min(n, end_idx + 1)  # exclusive upper bound
        
        if n > end_idx:
            # Complete: all seconds in this quarter have been observed
            values = per_second_data[start_idx:end_idx + 1]
            raw_wh = sum(values) * 1000
            result[qh_name] = {
                "wh": max(0, raw_wh),
                "complete": True,
                "raw_wh": raw_wh,
            }
        else:
            # Incomplete: look back up to 60 seconds from current position
            # (absolute window — may include data from previous quarter)
            lookback_start = max(n - 60, start_idx)
            values = per_second_data[lookback_start:n]
            rate = sum(values) / len(values) if values else 0.0
            predicted_wh = max(0, rate * 900)
            
            # raw_wh = actual observed data in this quarter only (not lookback)
            raw_values = per_second_data[obs_start:obs_end]
            raw_wh = sum(raw_values) * 1000
            
            result[qh_name] = {
                "wh": predicted_wh,
                "complete": False,
                "raw_wh": raw_wh,
                "predicted_wh": predicted_wh,
                "samples_used": len(values),
            }
    
    return result
```

3. Key behaviors:
   - **Not started**: Return `None` if the quarter's first index hasn't been reached yet (e.g., QH4 at minute=42 since 2700 > 2520)
   - **Complete**: Sum all kWh values in the quarter × 1000 → Wh, clamp at zero (`max(0, raw_wh)`)
   - **Incomplete**: Look back up to 60 seconds from current position as an absolute window (not clipped to QH boundaries). Compute `rate = sum / count`. Extrapolate: `predicted_wh = max(0, rate * 900)`. Clamp at zero. Include `samples_used` field.
   - **raw_wh for incomplete quarters**: Uses only data within the quarter's index range (not the lookback window). The lookback is purely for extrapolation.

4. **Note on timezones**: The mock's `_compute_nbc()` operates on UTC-only data with no timezone conversion. This is simpler than the production `HourlyProjection._compute_nbc()` which converts `self.instant` to device-local time before determining quarter boundaries. The mock and production need only match algorithmically (same state machine, same extrapolation formula), not byte-for-byte in their intermediate values.

**NBC state at default instant_minute=42** (`len(data)==2520`):
- QH1 (indices 0–899): complete → `{"wh": <computed>, "complete": True, "raw_wh": <value>}`
- QH2 (indices 900–1799): complete → same structure
- QH3 (indices 1800–2699): incomplete at index 2520 → `{"wh": <predicted>, "complete": False, "raw_wh": <actual>, "predicted_wh": <extrapolated>, "samples_used": N}`
- QH4 (indices 2700–3599): not started → `None`

**Deliverable**: MetricsMock produces NBC data with correct quarter states at minute=42.

---

### Task 3b: Wire NBC into `_build_device()` (~15 min)

**Goal**: Attach the NBC result to each device dict assembled by `_build_device()`.

**Context needed (files to read)**:
- `metrics.py` lines ~580–729 — current `_build_device()` method (~150 lines)

**What to do**:
1. In `_build_device()`, after assembling the other device fields, add:
   ```python
   "nbc": self._compute_nbc(per_second_data),
   ```
2. No changes needed to existing fields — this is a pure addition.

**Deliverable**: Device dicts include the `nbc` field with correct quarter states.

---

### Task 3c: NBC tests (~45 min)

**Goal**: Comprehensive test coverage for new NBC functionality.

**Context needed (files to read)**:
- `test_metrics.py` full file — existing test patterns (~105 lines)

**What to do**:

Add these methods to the `TestMetrics` class in `test_metrics.py`:

```python
def test_mock_nbc_structure(self):
    """Verify nbc field exists with QH1–QH4 keys."""
    device = self.metrics_data["devices"][0]
    self.assertIn("nbc", device)
    nbc = device["nbc"]
    self.assertIn("QH1", nbc)
    self.assertIn("QH2", nbc)
    self.assertIn("QH3", nbc)
    self.assertIn("QH4", nbc)

def test_mock_nbc_complete_quarters(self):
    """At minute=42, QH1 and QH2 should be complete."""
    device = self.metrics_data["devices"][0]
    self.assertTrue(device["nbc"]["QH1"]["complete"])
    self.assertTrue(device["nbc"]["QH2"]["complete"])

def test_mock_nbc_incomplete_quarter(self):
    """At minute=42, QH3 should be incomplete with predicted_wh."""
    device = self.metrics_data["devices"][0]
    qh3 = device["nbc"]["QH3"]
    self.assertFalse(qh3["complete"])
    self.assertIn("predicted_wh", qh3)
    self.assertIn("samples_used", qh3)

def test_mock_nbc_not_started(self):
    """At minute=42, QH4 should be None."""
    device = self.metrics_data["devices"][0]
    self.assertIsNone(device["nbc"]["QH4"])

def test_mock_nbc_parameterized_minute(self):
    """Test NBC at different instant_minute values."""
    # minute=10: QH1 incomplete, QH2–QH4 not started
    mock_10 = MetricsMock(instant_minute=10)
    nbc_10 = mock_10.metrics["devices"][0]["nbc"]
    self.assertFalse(nbc_10["QH1"]["complete"])
    self.assertIsNone(nbc_10["QH2"])
    
    # minute=37: QH1–QH2 complete, QH3 incomplete, QH4 not started
    mock_37 = MetricsMock(instant_minute=37)
    nbc_37 = mock_37.metrics["devices"][0]["nbc"]
    self.assertTrue(nbc_37["QH1"]["complete"])
    self.assertTrue(nbc_37["QH2"]["complete"])
    self.assertFalse(nbc_37["QH3"]["complete"])
    self.assertIsNone(nbc_37["QH4"])

def test_mock_nbc_wh_clamped_at_zero(self):
    """Verify NBC wh values are never negative (clamped at zero)."""
    device = self.metrics_data["devices"][0]
    for qh in ["QH1", "QH2", "QH3"]:
        if device["nbc"][qh] is not None:
            self.assertGreaterEqual(device["nbc"][qh]["wh"], 0)
```

**Deliverable**: All new NBC tests pass alongside existing ones.

---

### Task 4: Update `_get_tou_model()` in app.py (~30 min)

**Goal**: Replace the zeroed TOU dict in mock mode with realistic values from `MetricsMock.tou_result`.

**Context needed (files to read)**:
- `app.py` lines 91–120 — current `_get_tou_model()` implementation (~30 lines)

**What to do**:
1. Modify the mock branch of `_get_tou_model()`:
   ```python
   if is_mock or force_mock:
       return MetricsMock().tou_result
   ```
2. Remove the existing zeroed dict fallback code

**Deliverable**: `/api/v1/tou` endpoint returns non-zero TOU buckets in mock mode.

---

### Task 5: Run existing tests + fix backward compatibility (~30 min)

**Goal**: Verify all existing tests pass with the rewritten MetricsMock.

**What to do**:
1. Run `uv run pytest test_metrics.py -v` — verify TestMetrics class passes
2. Run `uv run pytest test_app.py -v` — verify integration tests pass
3. If any failures:
   - Check if it's a value mismatch (scale usage, smoothing) → adjust hardcoded values in `_build_device()`
   - Check if it's a type issue (datetime vs string) → ensure types match exactly
   - Check if it's a structure issue (missing keys) → add missing fields

**Deliverable**: All existing tests pass without modification.

---

### Task 6: Add new TOU test cases (~30 min)

**Goal**: Test coverage for the new TOU functionality.

**Context needed (files to read)**:
- `test_metrics.py` full file — existing test patterns (~105 lines)
- `test_app.py` full file — existing integration test patterns (~140 lines)

**What to do**:

**In `test_metrics.py`, add new method to `TestMetrics` class**:
```python
def test_mock_tou_result(self):
    """Verify MetricsMock().tou_result has non-zero values with all four keys."""
    mock = MetricsMock()
    tou = mock.tou_result
    self.assertIn("total", tou)
    self.assertIn("peak", tou)
    self.assertIn("part_peak", tou)
    self.assertIn("off_peak", tou)
    self.assertGreater(tou["total"], 0)
    self.assertGreater(tou["peak"], 0)
```

**In `test_app.py`, add new test**:
```python
def test_tou_endpoint_mock_realistic_values(self):
    """Verify TOU endpoint returns non-zero buckets in mock mode."""
    response = self.app.get("/api/v1/tou?start_date=2026-01-01&end_date=2026-01-01T04:00:00")
    data = json.loads(response.data)
    self.assertEqual(response.status_code, 200)
    self.assertGreater(data["total"], 0)
    self.assertGreater(data["peak"], 0)
```

**Deliverable**: All new tests pass alongside existing ones.

---

## Execution Order (Updated)

| # | Task | Est. Time | Token Budget | Dependencies |
|---|------|-----------|-------------|--------------|
| 1 | Foundation: per-second data + parameterized mock | ~1 hr | ~32k | None |
| 2 | `_build_device()` method — backward-compatible assembly | ~1–2 hrs | ~32k | Task 1 |
| 3a | `_compute_nbc()` method in MetricsMock | ~45 min | ~32k | Tasks 1, 2 |
| 3b | Wire NBC into `_build_device()` | ~15 min | ~16k | Task 3a |
| 3c | NBC tests | ~45 min | ~32k | Task 3b |
| 4 | Update `_get_tou_model()` in app.py | ~30 min | ~16k | Task 1 |
| 5 | Run existing tests + fix backward compatibility | ~30 min | ~16k | Tasks 1–4, 3a–c |
| 6 | Add new TOU test cases | ~30 min | ~16k | Tasks 1–5 |

**Total estimated effort**: 5–8 hours of implementation work.
