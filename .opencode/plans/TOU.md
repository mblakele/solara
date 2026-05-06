# Time-of-Use (TOU) Energy Report — Design Document

## Overview

The TOU feature reports historical energy consumption broken into four PG&E Time-of-Use buckets: **Total**, **Peak**, **Part-Peak**, and **Off-Peak**. It exposes a `/api/v1/tou` endpoint supporting both JSON and HTML responses.

## Architecture

```
app.py (Flask route)
  └── _validate_dates()          # input validation, ≤366-day range check
        └── _get_tou_model()     # mock or real data selection
              ├── MetricsMock.tou_result   (no credentials / MOCK=True)
              └── TOUReporter             (real pyemvue API)
                    ├── fetch_usage_data()  # chunked minute-granularity API calls
                    └── aggregate_tou()     # delegates to EnergyDataAggregator
                          └── energy_aggregator.EnergyDataAggregator
                                └── classify_hour()   # hour → bucket mapping
```

## Components

### `energy_aggregator.py` — Pure Aggregation Logic

`EnergyDataAggregator` is a static-only class with no external dependencies beyond `pytz` and the shared `TIMEZONE` constant. It provides three entry points for different data granularities:

| Method | Input Granularity | Used By |
|---|---|---|
| `aggregate_from_minutes()` | 1-minute intervals | `TOUReporter` (production) |
| `aggregate_from_seconds()` | 1-second intervals | NBC tests, misalignment tests |
| `aggregate_from_hourly()` | Hour-aligned tuples | Future use, testing |

All methods return `{"total": float, "peak": float, "part_peak": float, "off_peak": float}` in **watt-hours (Wh)**. Negative values are preserved for net-metering solar export.

### TOU Bucket Classification

`classify_hour(hour)` maps local-time hours to buckets:

| Local Hour | Bucket |
|---|---|
| 00–14 | `off_peak` |
| 15 | `part_peak` |
| 16–20 | `peak` |
| 21–23 | `part_peak` |

Boundary rules: 15:00 is part-peak (not off-peak), 16:00 is peak, 21:00 is part-peak (not peak), 00:00 is off-peak.

### `metrics.py` — Data Fetching

`TOUReporter(MetricsBase)` handles API orchestration:

1. **Authentication** — inherited from `MetricsBase.vue_init()`
2. **Chunked fetching** — The pyemvue API limits 1-minute data to ~13 hours per call. `fetch_usage_data()` iterates in 12-hour chunks with a 1-minute gap between chunks to stay within the limit.
3. **Aggregation** — `aggregate_tou()` combines all chunk results by summing bucket values.

### `app.py` — API Endpoint

`GET /api/v1/tou?start_date=REQUIRED&end_date=OPTIONAL`

- `start_date`: Required. Accepts `YYYY-MM-DD` or ISO 8601 with time component.
- `end_date`: Optional. Defaults to current UTC time.
- Range validation: Rejects spans > 366 days with HTTP 400.
- Content negotiation: Returns HTML (`templates/tou.html`) or JSON based on `Accept` header.
- Error handling: Catches `HTTPError` and `IOError`, returns HTTP 500 with error details.

JSON response shape:
```json
{
  "startDate": "2026-01-01",
  "endDate": "2026-01-07",
  "buckets": {
    "total": 12345.6,
    "peak": 4000.0,
    "partPeak": 3000.0,
    "offPeak": 5345.6
  }
}
```

### Mock Mode

When `VUE_USERNAME` is unset or `MOCK=True`, `_get_tou_model()` returns `MetricsMock().tou_result` — a precomputed dictionary with realistic non-zero bucket values. This enables local development and testing without API credentials.

## Timezone Handling

All input timestamps are interpreted in the local timezone (`util.TIMEZONE`) then converted to UTC for API calls. The aggregator converts UTC data back to local time before classifying hours, ensuring correct bucket assignment across DST transitions.

## Test Coverage

| File | Scope |
|---|---|
| `tests/test_energy_aggregator.py` | Unit tests for all three aggregation methods and boundary classification |
| `tests/test_day_boundary.py` | Day-boundary edge cases in the aggregator |
| `tests/test_start_time_misalignment.py` | Handles misaligned start times with second-granularity data |
| `tests/test_app.py` | Integration tests: missing params, invalid dates, 366/367-day range, API failure, mock mode |
| `tests/test_metrics.py` | Verifies `TOUReporter.aggregate_tou()` uses module-level `EnergyDataAggregator`; validates mock result structure |
| `tests/test_nbc.py` | Confirms NBC data appears on TOU endpoint devices in mock mode |
