# Plan: Add NBC (Non-Bypassable Charge) Quarter-Hour Data to `/` Endpoint

## Overview

PG&E bills NBC based on **net consumption** over each 15-minute interval. This feature adds four new labels (`QH1`–`QH4`) to the default `/` endpoint, showing per-device NBC values for each quarter hour within the current clock hour.

### Quarter Hour Mapping

| Label | Time Range (local) | Minutes |
|-------|-------------------|---------|
| `QH1` | :00 – :14         | 0–14    |
| `QH2` | :15 – :29         | 15–29   |
| `QH3` | :30 – :44         | 30–44   |
| `QH4` | :45 – :59         | 45–59   |

### NBC Definition

- **Complete QH**: `nbc_wh = MAX(0, sum_of_all_seconds_in_quarter * 1000)` — sum all per-second kWh values in the interval (including negative generation), convert to Wh, clamp at zero.
- **Incomplete QH**: Look back from `now` by up to 60 seconds as an **absolute time window** (not clipped to quarter boundaries). This means if a QH is incomplete and we are e.g. 12 minutes into it, the lookback may include data from the previous quarter — that is intentional and correct. Use whatever data is available within that absolute window. Compute average rate per second, extrapolate over 900 seconds (15 min). If fewer than 60 seconds are available (e.g., right at the start of an hour), use all available data. Clamp prediction at zero.

### Data Structure (JSON)

Each device object in `metrics.devices[]` gains an `nbc` field:

```json
{
  "devices": [
    {
      "name": "fubar",
      "nbc": {
        "QH1": {
          "wh": 45.2,
          "complete": true,
          "raw_wh": 45.2
        },
        "QH2": {
          "wh": 38.7,
          "complete": false,
          "raw_wh": 19.4,
          "predicted_wh": 38.7,
          "samples_used": 60
        },
        "QH3": null,
        "QH4": null
      }
    }
  ]
}
```

- `wh`: The NBC value (clamped at zero). For incomplete quarters this is the predicted value.
- `complete`: Whether the full 15-minute interval has data.
- `raw_wh`: Actual sum of seconds in the quarter (before clamping), for transparency.
- `predicted_wh`: Only present when `complete` is false — the extrapolated total.
- `samples_used`: Number of seconds used in the prediction (for debugging).
- `null`: For quarters that haven't started yet.

### HTML Changes

Add an NBC section below the existing metrics table for each device, with rows like:

```
NBC (PG&E Non-Bypassable Charges):
  QH1 :00–:14   45 Wh    [done]
  QH2 :15–:29   39 Wh*   [estimating]
  QH3 :30–:44       —    [not started]
  QH4 :45–:59       —    [not started]
```

- `*` marks estimated values, rendered with a `<sup>` tag.
- Use a subtle color (e.g., muted gray) to visually distinguish from the primary metrics.
- Show all four quarters regardless of whether they have started; quarters that haven't started display `—`.

---

## Tasks

- [x] **Task 0: Enhance MetricsMock for TOU endpoint** — ~30 min
- [x] **Task 1: Add `_compute_nbc()` method to `HourlyProjection`** — ~60–90 min
- [x] **Task 2: Attach NBC data to each device in metrics dict** — ~30 min
- [x] **Task 3: Update MetricsMock with synthetic per-second data & second device** — ~60–90 min
- [x] **Task 4: Add NBC section to HTML template + CSS** — ~30–45 min
- [x] **Task 5: Write unit tests for `_compute_nbc()` and integration tests** — ~60–90 min

---

## Implementation Plan

### 0. Enhance `MetricsMock` for TOU endpoint (pre-requisite)

**File**: `metrics.py`, in `MetricsMock.__init__()`:

The existing mock lacks TOU data, which has been a testing gap. Add:
- A `tou_result` dict with realistic `total`, `peak`, `part_peak`, `off_peak` values
- Ensure the mock instant timestamp is consistent across all devices

**Note**: This is arguably scope creep relative to NBC, but it fills an existing testing gap and keeps the mock comprehensive. The TOU endpoint already handles historical aggregation separately (step 6 confirms no changes needed there).

### 1. Add NBC computation method in `metrics.py` (`HourlyProjection`)

**File**: `metrics.py`, add a new method to `HourlyProjection` class, before `TOUReporter`.

```python
def _compute_nbc(self, usage_data_local: List[float], 
                 usage_data_start_local: datetime) -> Dict[str, Any]:
    """Compute NBC values for each quarter hour in the current hour.

    Args:
        usage_data_local: Per-second kWh data for the current hour.
        usage_data_start_local: Start time of the first data point (hour boundary).

    Returns:
        Dict with keys QH1–QH4, each containing NBC metrics or None.
    """
```

**Algorithm**:
- For each QH (indices 0–899 seconds within the hour):
  - Convert `self.instant` (UTC) to the device's local timezone via `pytz.timezone(device_tz_string)` before determining quarter boundaries. Use the device's configured timezone wherever possible; fall back to the global `TIMEZONE` if unavailable. Handle both naive and tz-aware input consistently (follow the pattern in `EnergyDataAggregator._get_local_hour()`).
  - Determine if the quarter has started based on `self.instant` in local time
  - If not started: return `None`
  - Identify which seconds belong to this quarter from `usage_data_local`
  - **Complete**: Sum all kWh values, multiply by 1000 → Wh, clamp at zero
  - **Incomplete**: Look back from `now` by up to 60 seconds as an **absolute time window** (not clipped to quarter boundaries). Compute average rate per second: `rate = sum_of_available_seconds / count`. Extrapolate: `predicted_wh = MAX(0, rate * 900)`. Clamp at zero.

**Key considerations**:
- The per-second data is already fetched in `populate()` — reuse it rather than refetching.

### 2. Attach NBC data to each device in `metrics` dict

**File**: `metrics.py`, in `HourlyProjection.__init__()`:

After the existing device metrics are assembled, add:
```python
device_metrics["nbc"] = self._compute_nbc(usage_data_local, usage_data_start_local)
```

This requires passing `usage_data_local` and `usage_data_start_local` from `populate()` to the device metrics assembly. The cleanest approach is to store them on `dig` (the device info object) during `populate()`, since `dig` already carries per-device data like `scales`, `smoothing`, etc.

**Change**: In `populate()`, after `_process_offset_scales`:
```python
dig.nbc_data_start = usage_data_start_local
dig.nbc_seconds = usage_data_local
```

Then in `__init__()`, add:
```python
"nbc": self._compute_nbc(dig.nbc_seconds, dig.nbc_data_start) if hasattr(dig, "nbc_seconds") else None,
```

**Important**: If `_fetch_channel_data` raises an exception during `populate()`, the error is **fatal** — it should propagate up and be displayed on an error page. NBC data will not be calculated in that case; the entire request fails rather than returning a partial response with missing NBC values. The `hasattr` guard is only needed for defensive coding, not as an expected code path.

### 3. Update mock data (`MetricsMock`) — **requires synthetic per-second data**

**File**: `metrics.py`, in `MetricsMock`:

The existing mock data is insufficient for both the TOU endpoint and this new NBC feature. The current mock only has aggregated scales (`1H`, `1MIN`–`10MIN`) but **no per-second kWh arrays**, which are required to exercise `_compute_nbc()`. This step must generate synthetic per-second data.

**a) Add a second device** with positive consumption (solar + load scenario) to cover different NBC cases — one where some quarters have net generation (`raw_wh < 0`, `wh = 0`) and others have net consumption.

**b) Parameterize mock instantiation by time-of-hour.** The current `MetricsMock` is a single static dict with a hardcoded instant timestamp. Instead, parameterize it to accept an `instant_minute` argument (or similar) so the mock can represent different moments within the hour:
```python
class MetricsMock:
    def __init__(self, instant_minute: int = 42) -> None:
        ...
```

The mock's internal timestamp should be computed relative to "now" rather than hardcoded, so it always looks current. This means tests must verify **invariant** aspects (predictions, NBC values, TOU bucket structure) and never inspect absolute date/time fields like year or month.

**c) Generate synthetic per-second mock data for each device.** Each device needs ~900 floats representing kWh per second across the full hour. Use a deterministic helper function (seeded random or fixed pattern) to generate consistent test data:
   - Device A (`instant_minute=12`): QH1 complete, QH2 incomplete (~12 min in), QH3/QH4 not started
   - Device B (`instant_minute=37`): QH1–QH2 complete, QH3 incomplete (~7 min in), QH4 not started

**d) Add mock TOU data** to cover the `/api/v1/tou` endpoint — include `peak`, `part_peak`, and `off_peak` buckets with realistic values.

**e) Ensure NBC covers all cases**:
   - Positive consumption quarters (normal load)
   - Negative raw_wh quarters (net generation → `wh = 0`)
   - Mixed scenario within a single device

### 4. Update HTML template (`templates/index.html`)

**File**: `templates/index.html`, add NBC section after the existing metrics table, before the closing `</table>`:

Desired output per device:
```
NBC (PG&E Non-Bypassable Charges):
  QH1 :00–:14   45 Wh    [done]
  QH2 :15–:29   39 Wh*   [estimating]
  QH3 :30–:44       —    [not started]
  QH4 :45–:59       —    [not started]
```

- `*` marks estimated values, rendered with a `<sup>` tag.
- Use a subtle color (e.g., muted gray) to visually distinguish from the primary metrics.
- Show all four quarters regardless of whether they have started; quarters that haven't started display `—`.

Add a CSS rule for the `*` estimator marker:
```css
td.note sup {
    font-size: 0.5em;
    color: #999;
}
```

### 5. Add tests

**File**: `test_energy_aggregator.py` or new file `test_nbc.py`:

- Test `_compute_nbc()` with various scenarios using **synthetic per-second data**:
  - All quarters complete, all positive (pure consumption)
  - All quarters complete, some negative (net generation → NBC = 0)
  - Mixed complete/incomplete quarters
  - Quarter not yet started → `None`
  - Prediction accuracy: incomplete quarter extrapolation matches expected rate
  - **Cross-quarter lookback**: Verify that the absolute 60-second lookback for an incomplete QH can include data from the previous quarter (not clipped to boundaries)

- Update `test_app.py`: Verify JSON payload includes `nbc` field with correct structure for both real and mock data paths. Use multiple mock scenarios via parameterized instantiation (`MetricsMock(instant_minute=...)`) to cover different QH states. Tests should verify invariant properties (NBC values, prediction accuracy, TOU bucket structure) rather than absolute timestamps.

### 6. No changes needed to `/api/v1/tou` endpoint

The TOU endpoint already handles historical aggregation separately. Adding NBC data to TOU is out of scope for this plan.

---

## Files Modified

| File | Change |
|------|--------|
| `metrics.py` | Add `_compute_nbc()` method; attach NBC data per device in `__init__`; parameterize `MetricsMock` with `instant_minute` constructor arg and compute timestamp relative to "now"; add second device, deterministic synthetic per-second data (~900 floats/device), TOU mock data, and comprehensive NBC scenarios |
| `templates/index.html` | Add NBC section with QH1–QH4 rows; add `<sup>` tag for estimator marker; show all four quarters always |
| `test_energy_aggregator.py` or `test_nbc.py` (new) | Unit tests for `_compute_nbc()` with synthetic per-second inputs |
| `test_app.py` | Integration test: verify `nbc` in JSON response; use parameterized mock scenarios; test invariant properties not absolute timestamps |

## Risks & Trade-offs

- **Timezone correctness**: The NBC calculation depends on knowing which local minute we're at. Use the device's configured timezone via `pytz.timezone(device_tz_string)` wherever possible; fall back to global `TIMEZONE` if unavailable. Handle both naive and tz-aware input consistently (follow `EnergyDataAggregator._get_local_hour()`).
- **Prediction accuracy**: Using only the last 60 seconds for extrapolation may not capture rapid load changes (e.g., HVAC cycling). This is a known limitation of the approach and matches PG&E's own methodology for partial intervals. The lookback intentionally crosses quarter boundaries to maximize available data.
- **Mock data parameterization**: Parameterizing `MetricsMock` by time-of-hour means tests must verify invariant properties (NBC values, predictions, TOU structure) rather than absolute timestamps. This is a test design consideration but keeps the mock always-current without hardcoding dates.
- **Per-second mock data generation**: Generating ~900 synthetic floats per device is non-trivial but necessary for NBC testing. Use a deterministic helper function (seeded or fixed pattern) to ensure consistent, reproducible test data that aligns with existing mock scale aggregates.

## Testing Strategy

1. Run `uv run pytest` to verify all existing tests pass
2. New unit tests for `_compute_nbc()` with controlled synthetic per-second inputs
3. Verify JSON output structure via the mock endpoint (no real API needed)
4. Manual inspection of HTML rendering in a browser or curl
```

---

