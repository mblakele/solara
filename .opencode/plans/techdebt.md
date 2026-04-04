# Revised Technical Debt Plan — April 2026
## Status: Items from original plan vs current code

### Completed items (~100% of original scope)

| # | Original Item | Notes |
|---|--------------|-------|
| 1.1 | Missing module docstrings | `app.py`, `util.py` both have docstrings |
| 1.2 | Import organization violations | All imports correctly ordered: stdlib → third-party → local in both files |
| 1.3 | Unnecessary else clauses | No more `elif` after return; no unnecessary pass statements |
| 1.4 | Global state usage (`global logger`) | Replaced with `self.logger = logger_next or logger` (metrics.py:56) |
| 1.5 | Unused variables/imports | `timezone` removed from app.py; unused vars cleaned up in metrics.py |
| 2.1 | Overly broad `except Exception` | Both locations now use specific exceptions (app.py:202, metrics.py:450) |
| 2.2 | Generic exception catching | Now uses `requests.exceptions.RequestException` at all 3 locations (metrics.py:86, 286, 450) |
| 2.3 | Missing exception chaining | `raise ... from ex` / `from inner_ex` in place |
| 2.4 | Incorrect exception attribute access | Uses `ex.response.status_code` pattern correctly |
| 3.1 | Too many returns/branches in `index()` | Refactored with `_get_model`, `_json_response`, `_validate_dates` helpers |
| 3.2 | Too many locals in `populate()` | Extracted `_fetch_channel_data` and `_process_offset_scales` helpers (metrics.py:203, 231) |
| 3.3 | Redundant conditional logic | `min()/max()` already used |
| 4.1 | Missing file encoding specification | `encoding="utf-8"` in all file opens; explicit encoding at metrics.py:76–77 |
| 4.2 | Missing type hints on `predict()` | Has `-> None` return annotation (metrics.py:333) |
| 5.1 | TOUReporter local import | Moved to module-level import (metrics.py:18) |
| 5.2 | MetricsMock too few methods | Not actionable — mock only needs `__init__` for current use |
| 6.1 | TODO left in code | No stale TODO comments found |
| 6.2 | Missing function docstrings | All functions including `predict()` have proper docstrings |
| 7.1 | Limited integration tests | **COMPLETED** — Error-path tests added: `test_tou_api_failure_http_error` (HTTPError handling), `test_index_mock_error_retryable` (MOCK_ERROR retry flow), `test_tou_date_range_367_days_rejected` / `test_tou_date_range_366_days_accepted` (date validation edge cases) |

---
## Execution Order & Verification
1. Run: **Task → verification**
2. After the task, run `uv run pytest` to ensure nothing breaks
3. Final verification: `uv run pylint app.py util.py metrics.py energy_aggregator.py && uv run pytest`
## Estimated Total Effort
- **Remaining task:** ~30 min (test additions)
- **Total remaining: ~30 minutes**
---
