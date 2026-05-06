# Plan: Cache pyemvue API Responses to Avoid Hammering

## Problem Statement

The Emporia VUE API (via pyemvue) is called too frequently, risking rate-limiting
and unnecessary load. Currently:

1. **The `/` index endpoint** creates a fresh `HourlyProjection` on every HTTP
   request, which calls `vue_init()`, `get_device_info()`, and `populate()` —
   all hitting the pyemvue API.

2. **The load management background loop** runs every 30 seconds and calls
   `metrics_fetch()` (which creates another fresh `HourlyProjection`), making
   independent API calls that don't share data with the index endpoint.

3. **The existing `NBCCache`** only caches the *parsed NBC result* (a small dict
   with `qh_name`, `predicted_wh`, `seconds_remaining`). It does NOT prevent the
   underlying API calls — `metrics_fetch()` still creates a full `HourlyProjection`
   on every invocation, even when the cache returns a hit.

4. **Double-fetch bug**: In `NBCReader.get_current_qh()`, when the cache is
   invalid, `fetch()` is called once to probe for the current QH name, then
   `_fetch_and_parse` (which calls `fetch()` again) is passed to `get_or_fetch()`.
   On cache miss, two full API round-trips happen instead of one.

## Goal

Reduce pyemvue API calls to the minimum necessary while ensuring the load manager
never acts on stale data. The load manager is especially sensitive to stale data
because it makes real-time decisions about turning plugs on/off based on solar
surplus predictions.

## Design

### 1. New `MetricsCache` class in `metrics.py`

A module-level cache that sits between callers and `HourlyProjection`, caching the
full `metrics` dict (the complete output of `HourlyProjection.metrics`).

```python
class MetricsCache:
    """Cache for HourlyProjection metrics data.

    Caches the full metrics dict for a configurable TTL. The cache key is
    implicit (there's only one "current metrics" snapshot). Callers get either
    a fresh fetch or a cached copy depending on TTL expiry.
    """

    def __init__(self, ttl_seconds: int = 30) -> None:
        self._data: dict[str, Any] | None = None
        self._fetched_at: datetime | None = None
        self._ttl = timedelta(seconds=ttl_seconds)

    def get_or_fetch(
        self,
        fetch_func: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Return (metrics_data, was_fresh).

        Returns cached data if not expired. Otherwise calls fetch_func and
        caches the result.

        Args:
            fetch_func: Callable that returns a fresh metrics dict.

        Returns:
            Tuple of (metrics_data, was_fresh). was_fresh is True when
            fetch_func was actually called.
        """
        now = datetime.now(timezone.utc)
        if (
            self._data is not None
            and self._fetched_at is not None
            and (now - self._fetched_at) < self._ttl
        ):
            return self._data, False

        fresh = fetch_func()
        self._data = fresh
        self._fetched_at = now
        return fresh, True

    def invalidate(self) -> None:
        """Clear the cache."""
        self._data = None
        self._fetched_at = None
```

**TTL choice: 30 seconds** — matches the load management cycle interval
(`LOAD_MANAGE_INTERVAL_SECS`). The Emporia API data is per-second granularity
for the current hour, so 30 seconds of staleness means at most 30 new seconds
of data are not reflected. For NBC quarter-hour predictions, 30 seconds is
negligible (each QH is 900 seconds). The load manager's stale-data threshold
(`STALE_THRESHOLD_SECS = 120`) provides an additional safety net.

**Not thread-safe by design** — the load manager already uses its own lock
(`self._lock`) during `run_cycle()`. The index endpoint is single-threaded
(Flask dev server / gunicorn per-worker). If needed, a `threading.Lock` can
be added later.

### 2. Fix the double-fetch bug in `NBCReader.get_current_qh()`

Currently, on cache miss:
```python
# Line ~316: probe calls fetch() -> full API round-trip
probe = fetch()
current_qh = self._find_incomplete_qh(probe, device_name)

# Line ~320: get_or_fetch calls _fetch_and_parse -> another full API round-trip
cached, _ = self.cache.get_or_fetch(device_name, current_qh, _fetch_and_parse)
```

**Fix**: Refactor so the probe result is reused as the fetch result. The
`get_or_fetch` method will accept an optional "already-fetched" parameter:

```python
def get_or_fetch(
    self,
    device_name: str,
    current_qh: str | None,
    fetch_func: Callable[[], dict[str, Any]],
    pre_fetched: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Return (data, was_fresh).

    If pre_fetched is provided and cache is invalid, uses it directly
    instead of calling fetch_func again.
    """
    now = datetime.now(timezone.utc)

    if (
        self._cache is not None
        and self._cached_at is not None
        and self._cached_qh == current_qh
        and now - self._cached_at < self._ttl
    ):
        return self._cache, False

    if pre_fetched is not None:
        fresh_data = pre_fetched
    else:
        fresh_data = fetch_func()

    self._cache = fresh_data
    self._cached_at = now
    self._cached_qh = current_qh
    return fresh_data, True
```

Then in `get_current_qh()`:
```python
probe = fetch()
if probe is not None:
    current_qh = self._find_incomplete_qh(probe, device_name)

cached, _ = self.cache.get_or_fetch(
    device_name, current_qh, _fetch_and_parse,
    pre_fetched=self._parse_metrics(device_name, probe) if probe is not None else None,
)
```

### 3. Wire `MetricsCache` into the index endpoint (`app.py`)

Create a module-level `MetricsCache` instance and use it in the `metrics_fetch`
callable passed to `LoadManager`, AND in the index endpoint:

```python
# Module level
_metrics_cache = MetricsCache(ttl_seconds=30)

def _get_load_manager():
    global _load_manager
    with _load_manager_lock:
        if _load_manager is None:
            from load_manager import LoadManager

            def metrics_fetch():
                return _metrics_cache.get_or_fetch(
                    lambda: HourlyProjection(logger).metrics
                )[0]

            _load_manager = LoadManager(metrics_fetch=metrics_fetch)
            # Share the NBCCache too
            _load_manager.nbc_reader.cache = _nbc_cache
        return _load_manager
```

The index endpoint (`/` route) will also use `_metrics_cache`:
```python
@app.route("/")
def index():
    # ...
    metrics_data, was_fresh = _metrics_cache.get_or_fetch(
        lambda: HourlyProjection(logger).metrics
    )
    # Use metrics_data instead of creating new HourlyProjection
```

### 4. Share `NBCCache` between LoadManager and index endpoint

Create a module-level `NBCCache` instance in `app.py` so the load manager and
any future consumers share the same NBC prediction cache:

```python
_nbc_cache = NBCCache(ttl_seconds=60)
```

Pass it during `LoadManager` construction.

### 5. Handle cache invalidation on pending effects

When the load manager executes an action (turns a plug on/off), the NBC prediction
will become inaccurate until the next API fetch reflects the change. The current
code already handles this via the `has_pending_effect_since()` check in
`run_cycle()` — it returns `"waiting_for_fresh_data"` and skips action decisions
until fresh data arrives.

The `MetricsCache` naturally supports this: after pending effects exist, the next
cycle will find the cache expired (30s TTL) and fetch fresh data. No explicit
invalidation is needed, but we can add a log message when this happens for
observability.

### 6. `StateTracker.last_nbc_fetch` tracking

The `StateTracker.is_stale()` check uses `last_nbc_fetch` timestamp with a
120-second threshold. When using cached data, we need to ensure `last_nbc_fetch`
is set to the **actual fetch time** (when data came from the API), not the cache
hit time.

The `MetricsCache.get_or_fetch()` returns `(data, was_fresh)`. In `run_cycle()`,
only update `self.state.last_nbc_fetch = now` when `was_fresh` is True, or track
the original fetch timestamp separately.

**Simpler approach**: Store the fetch timestamp in the cached data itself:
```python
fresh = fetch_func()
fresh["_fetched_at"] = datetime.now(timezone.utc)
```

Then in `run_cycle()`:
```python
metrics_data, was_fresh = ...
self.state.last_nbc_fetch = metrics_data.get("_fetched_at") or now
```

This ensures the stale-data check uses the real API fetch time, not the cache
hit time.

## Files Affected

| File | Change |
|---|---|
| `metrics.py` | Add `MetricsCache` class |
| `load_manager.py` | Fix double-fetch in `NBCReader.get_current_qh()`, update `NBCCache.get_or_fetch()` to accept `pre_fetched`, update `run_cycle()` to track real fetch timestamp |
| `app.py` | Add module-level `MetricsCache` and `NBCCache`, wire them into index endpoint and `LoadManager` |
| `tests/test_load_manager.py` | Add tests for `MetricsCache`, update existing tests if needed |
| `tests/test_metrics.py` | Add tests for `MetricsCache` |
| `AGENTS.md` | Update project structure if new file is created (not needed since `MetricsCache` goes in `metrics.py`) |

## Implementation Order

1. **Add `MetricsCache` class to `metrics.py`** — standalone, testable
2. **Fix double-fetch bug in `NBCReader` / `NBCCache`** — refactor `get_or_fetch()`
3. **Wire caches into `app.py`** — share between index and load manager
4. **Fix `last_nbc_fetch` tracking** — use real fetch timestamp
5. **Write tests** — cover cache hit/miss, TTL expiry, double-fetch fix
6. **Run full verification gate**: `pylint` → `mypy` → `pytest`

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Cached data is stale when load manager acts | 30s TTL + 120s stale threshold + pending-effects check provide 3 layers of protection |
| Thread-safety issues with shared cache | Load manager already uses `_lock`; Flask is single-threaded per worker; add lock if issues arise |
| Cache holds memory for large metrics dict | The metrics dict is ~KB-scale (per-second data for current hour). Insignificant. |
| Tests break because they expect fresh data | Tests already use `NBCCache(ttl_seconds=60)` injection; extend pattern to `MetricsCache` |

## What This Does NOT Change

- **Auth caching** (`vue_init()`, 24h token window) — already works correctly
- **Device info caching** (`get_device_info()`, 24h window) — already works correctly
- **TOU data fetching** — historical data, on-demand, not performance-critical
- **Load manager decision logic** — unchanged, just gets fresher data more efficiently
