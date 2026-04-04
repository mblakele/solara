# 📄 Implementation Plan: Time-of-Use (TOU) Energy Report Endpoint

## Goal
Implement a new API endpoint, `/api/v1/tou`, capable of reporting simulated or historical energy consumption broken down into four Time-of-Use (TOU) buckets: **Total, Peak, Part-Peak, and Off-Peak**. This endpoint must support both HTML and JSON output formats.

## Requirements & Constraints
1.  **Inputs:**
    *   `start_date`: Mandatory.
    *   `end_date`: Optional, defaults to the current date.
2.  **Date Validation:** The difference between `start_date` and `end_date` must be $\le 366$ days.
3.  **Data Source:** Must consume time-series data from the `pyemvue` API, similar to the existing hourly report. Input data is UTC, and must be adjusted to match local timezone.
4.  **Output Buckets (Wh):**
    *   **Total:** Sum of all power used in the period.
    *   **Peak:** Sum of power used daily between **16:00 and 21:00** (not inclusive: 21:00 is part-peak).
    *   **Off-Peak:** Sum of power used daily between **00:00 and 15:00** (not inclusive: 15:00 is part-peak).
    *   **Part-Peak:** Sum of power used daily in two windows: **15:00 to 16:00** and **21:00 to 00:00** (not inclusive: 00:00 is off-peak).
5.  **Output Formats:** Must provide both a structured JSON response and an equivalent HTML view.
6.  **Testing:** New unit and integration tests must be added.
    * Tests must convert input data from UTC to local timezone.
    * Is 21:00:00 peak? No, 21:00:00 is part-peak
    * Is 00:00:00 part-peak? No, 00:00:00 is off-peak
    * Is 15:00:00 off-peak? No, 15:00:00 is part-peak
    * Is 16:00:00 part-peak? No, 16:00:00 is peak

---

## Proposed Work Phases

### **Phase 1: Architectural Refactoring & Abstraction (Refactoring)**
**Objective:** Decouple the complex time-series aggregation logic from the API fetching mechanism.

1.  **Create `energy_aggregator.py`:** Develop `EnergyDataAggregator`. This class will house the core logic to calculate the four buckets from raw data, based on time-of-day rules.
2.  **Update `metrics.py`:** Modify the class to utilize the aggregator. The process will shift from "Current Hour Prediction" to "Historical Data Collection & Aggregation."
3.  **Key Focus:** Creating pure functions that accept raw historical datasets as input, removing deep coupling with the `pyemvue` API calls.

### **Phase 2: API Endpoint Implementation**
**Objective:** Expose the TOU functionality via the new endpoint.

1.  **Date Validation:** Implement the mandatory date difference check ($\Delta \le 366$ days).
2.  **New Handler in `app.py`:** Create the handler for `/api/v1/tou` which orchestrates the plan: Validate $\rightarrow$ Gather Data $\rightarrow$ Calculate $\rightarrow$ Format Output.
3.  **Output Formatting:** Ensure the HTML and JSON serialization correctly present the four distinct buckets.

### **Phase 3: Verification and Testing**
**Objective:** Guarantee correctness and prevent regressions.

1.  **Unit Tests:** Write standalone tests for `EnergyDataAggregator` using static, mocked data to validate the bucket calculation logic in isolation.
2.  **Integration Tests:** Update tests in `test_app.py` to test the entire workflow, ensuring data flow works correctly from the data source to the final response.

---
