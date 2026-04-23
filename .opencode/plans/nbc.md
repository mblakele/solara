# Design Document: NBC (Non-Bypassable Charge) Quarter-Hour Computation

## Overview

PG&E bills Non-Bypassable Charges based on **net consumption** over each 15-minute interval. This feature computes per-device NBC values for each quarter hour within the current clock hour and exposes them through the `/` endpoint as both JSON and HTML.

## Quarter Hour Mapping

| Label | Time Range (local) | Seconds Into Hour |
|-------|-------------------|-------------------|
| `QH1` | :00 – :14         | 0–899             |
| `QH2` | :15 – :29         | 900–1799          |
| `QH3` | :30 – :44         | 1800–2699         |
| `QH4` | :45 – :59         | 2700–3599         |

## Algorithm

### Complete Quarter
Sums all per-second kWh values in the 15-minute interval (including negative generation), converts to Wh, and clamps at zero:
```
wh = max(0, sum(kWh_per_second) * 1000)
```

### Incomplete Quarter
Uses an absolute 60-second lookback window from `now` (not clipped to quarter boundaries). Computes average rate per second, then extrapolates for remaining seconds:
```
rate     = sum(lookback_values) / len(lookback_values)
raw_wh   = sum(observed_in_quarter) * 1000
predicted_wh = raw_wh + rate * remaining_seconds * 1000
wh       = max(0, predicted_wh)
```

The lookback intentionally crosses quarter boundaries to maximize available data. If fewer than 60 seconds are available, all available data is used.

### Not Started
Returns `null` for quarters that have not yet begun.

## Data Flow

1. `HourlyProjection.populate()` fetches per-second kWh data from the VUE API and stores it on each device info object as `dig.nbc_seconds` and `dig.nbc_data_start`.
2. `HourlyProjection.__init__()` calls `_compute_nbc()` for each device and attaches the result to the device metrics dict under `"nbc"`.
3. The `/` endpoint returns the enriched device metrics as JSON (camelCased) or renders them in HTML via `templates/index.html`.

## JSON Schema

Each device object in `metrics.devices[]` includes an `nbc` field:

```json
{
  "devices": [
    {
      "name": "device",
      "nbc": {
        "QH1": {
          "wh": 45.2,
          "complete": true,
          "rawWh": 45.2
        },
        "QH2": {
          "wh": 38.7,
          "complete": false,
          "rawWh": 19.4,
          "predictedWh": 38.7,
          "samplesUsed": 60
        },
        "QH3": null,
        "QH4": null
      }
    }
  ]
}
```

### Field Definitions

| Field | Type | Present When | Description |
|-------|------|-------------|-------------|
| `wh` | number | always (when not null) | NBC value clamped at zero; predicted for incomplete quarters |
| `complete` | bool | always (when not null) | Whether the full 15-minute interval has elapsed |
| `rawWh` | number | always (when not null) | Actual sum of observed seconds before clamping |
| `predictedWh` | number | `complete == false` | Extrapolated total for remaining seconds |
| `samplesUsed` | int | `complete == false` | Number of seconds used in the prediction |

## HTML Rendering

The template (`templates/index.html`) renders NBC rows below the data age row, ordered QH4 through QH1 (most recent first). For incomplete quarters, shows `predicted / raw`. For complete quarters, shows `raw Wh`. Not-yet-started quarters are omitted.

## Timezone Handling

Uses the device's configured timezone via `pytz.timezone(device_info["time_zone"])`, falling back to the global `TIMEZONE` constant. Handles both naive and tz-aware datetimes by localizing or converting as needed, following the pattern in `EnergyDataAggregator._get_local_hour()`.

## Error Handling

If `_fetch_channel_data()` raises an exception during `populate()`, the error propagates fatally — the entire request fails rather than returning partial data. For incomplete quarters with no available data, `_compute_nbc()` raises `RetryableMetricsException("No data for period")`.

## Files Modified

| File | Change |
|------|--------|
| `metrics.py` | Added `_compute_nbc()` to `HourlyProjection`; attached NBC data per device in `__init__`; stored per-second data on device info during `populate()` |
| `mockdata.py` | Parameterized `MetricsMock` with `instant_minute`; added second device (`SOLAR+LOAD`); generated deterministic synthetic per-second data (~3600 floats/device) |
| `templates/index.html` | Added NBC section with QH1–QH4 rows showing predicted/raw Wh values |
| `tests/test_nbc.py` (new) | 26 tests across three test classes covering unit, mock, and integration scenarios |

## Test Coverage

### Unit Tests (`TestComputeNBCUnit`) — 13 tests
- All quarters complete with positive consumption
- All quarters complete with negative values (clamped to zero)
- Mixed complete/incomplete quarters
- Quarter not yet started → `None`
- Prediction accuracy for incomplete quarters
- Cross-quarter lookback behavior
- Empty data handling
- Wh clamping at zero for incomplete quarters
- Raw wh preservation for transparency
- Incomplete raw vs. predicted comparison
- Predicted wh near quarter end
- Device timezone fallback to global TIMEZONE

### Mock Tests (`TestMetricsMockNBC`) — 6 tests
- All complete positive (Device B)
- All complete negative clamped to zero (Device A)
- Boundary at minute 14 (QH1 incomplete)
- Boundary at minute 30 (QH1/QH2 complete, QH3 not started)
- Boundary at minute 45 (QH1–QH3 complete, QH4 not started)
- NBC structure field verification

### Integration Tests (`TestNBCIntegration`) — 7 tests
- JSON response includes `nbc` field per device
- Default minute=42 structure (QH1/QH2 complete, QH3 incomplete, QH4 null)
- Minute=10 structure (only QH1 incomplete)
- Full hour (minute=60) all quarters complete
- Two devices with different signs (negative clamped, positive)
- Prediction positivity for load-only device
- `samplesUsed` field presence and validity

## Known Limitations

- **Prediction accuracy**: A 60-second lookback may not capture rapid load changes (e.g., HVAC cycling). This matches PG&E's methodology for partial intervals.
- **No historical NBC**: Only the current hour is computed. The `/api/v1/tou` endpoint does not include NBC data.
