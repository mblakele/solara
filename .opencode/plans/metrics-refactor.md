# metrics.py refactor for hourly and multi-day TOU output

Use this plan to refactor metrics.py so that it enables both the current hourly projection output and the new multi-day TOU output. Consider different ways to do this. We'll always want to initialize a PyEmVue connection and authenticate. 

## Current state:
- `Metrics` class does everything in `__init__`: auth, device discovery, fetches current hour of second-level data, computes hourly prediction
- `/api/v1/tou` endpoint (in `app.py`) needs **multi-day historical data** at **minute granularity** for TOU bucket aggregation
- `EnergyDataAggregator` already exists with static methods for aggregating to TOU buckets

---

## Design plan: Auth Base + Specialized Factories

```python
class MetricsBase:
    """Handles PyEmVue connection and authentication only."""
    def __init__(self):
        self.vue = PyEmVue()
        self.vue_init()

class HourlyProjection(MetricsBase):
    """Current hourly prediction behavior."""
    pass

class TOUReporter(MetricsBase):
    """Multi-day TOU aggregation."""  
    def __init__(self, start_date, end_date, scale='minute'):
        super().__init__()
        self.fetch_and_aggregate_tou(...)
```

**Pros:**
- Reuses auth logic cleanly through inheritance
- Each subclass has focused responsibility
- Natural extension of existing single-class design
- Follows Open/Closed Principle

**Cons:**
- Two classes instead of one (mild complexity increase)

**Rationale:**

1. **Preserves existing behavior** - `HourlyProjection` can mirror current `Metrics` behavior with minimal changes

2. **Clean separation** - TOU reporting has fundamentally different needs:
   - Different time ranges (multi-day vs current hour)
   - Different granularity (minute vs second)
   - Different output (buckets vs prediction)

3. **Shared auth** - Both need PyEmVue authentication; inheritance handles this elegantly

4. **Extensible** - If you add daily summaries or weekly reports later, new subclasses fit naturally

5. **Testable** - Each class can be mocked/tested independently

6. **Minimal disruption** - `app.py` changes are localized to which class it instantiates

---

### Proposed Structure

```
metrics.py
├── VueAuthenticationError
├── RetryableMetricsException
├── MetricsBase                    # NEW: auth + device discovery only
│   ├── vue_init()
│   ├── get_device_info()
│   └── vue, device_info, vue_auth
│
├── HourlyProjection(MetricsBase)  # RENAMED from Metrics
│   ├── populate()                 # fetch current hour at second granularity
│   ├── predict()                  # compute hourly forecast
│   └── metrics                    # existing output structure
│
└── TOUReporter(MetricsBase)       # NEW
    ├── __init__(start_date, end_date, scale='minute')
    ├── fetch_usage_data()         # fetch historical at specified scale
    ├── aggregate_tou()            # delegate to EnergyDataAggregator
    └── tou_result                 # {total, peak, part_peak, off_peak}
```

---

### Clarifying Questions and Answers

1. **Should `HourlyProjection` maintain exact backward compatibility** with current `Metrics` output, or are breaking changes acceptable?

HourlyProjection should maintain exact backward compatibility with current Metrics output (unless there's a good reason for changes: we control all the code).

2. **For TOU reporting:** Should it support both historical (completed days) and real-time (current partial day), or just historical for now?

TOU reporting should support both historical (completed days) and real-time (current partial day).

3. **Scale flexibility:** Should TOU support second/min/hour scales, or just minute as a fixed choice?

Due to limits in the source API, TOU will only use minute scale.

4. **Mock support:** Should `MetricsMock` also be refactored into a hierarchy, or stay as-is for simplicity in testing?

Let's keep MetricsMock monolithic until that becomes clumsy-- eventually we'll need different mock data.

---
