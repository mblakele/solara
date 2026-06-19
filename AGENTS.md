# 🛠️ AI Agent Guidelines for Solara Codebase

This document serves as a style guide and command reference for AI coding agents
operating within the `solara` repository. Adhering to these guidelines ensures
code consistency, maintainability, and adherence to project standards.

---

## General Advice

Don't ask whether a bug or error might be pre-existing or might pre-date your changes. Don't try to dig into git. Just fix it.

When something is ambiguous or two consecutive attempts have not resolved a
failing test, **stop and ask** rather than continuing to iterate blindly.

Write tests first, then diagnose and fix bugs.

### Tool Use

Always invoke tools using structured function-calling JSON (not inline XML or markdown text).

Never try to work around permission errors.

### Editing

#### ast-grep usage

ast-grep is installed. Use it instead of grep/ripgrep when the search or edit cares about CODE STRUCTURE, not text. Concretely:

- Finding all call sites of a function/method, regardless of formatting/whitespace
- Renaming a parameter only where it's a parameter (not in strings/comments)
- Finding a specific syntactic shape (e.g. all `try/except` blocks missing a `finally`)
- Structural rewrites across many files with consistent shape

Do NOT use ast-grep for: plain string/log/comment search, or one-off single-file edits where a normal Edit tool call is simpler.

##### Syntax reminders
- model is not reliable on these from memory — verify with `--help` or a dry run first.
- Search: `ast-grep run -p '<pattern>' -l <language> <path>`
- Pattern meta-variables: `$NAME` matches one node, `$$$NAME` matches zero-or-more (e.g. `$$$ARGS`)
- Rewrite: `ast-grep run -p '<pattern>' -r '<rewrite>' -l <language> --update-all <path>`
- ALWAYS run without `--update-all` first to preview matches before rewriting.
- If unsure of exact flag names, run `ast-grep --help` or `ast-grep run --help` rather than guessing.

#### batch_str_replace usage

Prefer `batch_str_replace` over repeated `str_replace` calls for multiple independent edits. Emit all edits in one call.

### Communication Style

Use simple, everyday language. Avoid unnecessary jargon unless the context
requires technical precision.

---

## 🧪 Pytest-Driven Development Protocol

To ensure code quality and prevent regressions, all development must follow a strict **Test-First (Red-Green-Refactor)** workflow. You are prohibited from modifying production code until a failing test has been established. You are prohibited from attempting to diagnose, fix, or design a fix for any bug until a failing test has been written. Test first!

### Mandatory Pre-Code Checklist
Before writing any production code, confirm:
1. A failing test exists that reproduces the bug or defines the new behavior.
2. You have run `pytest` and verified it fails with the correct error/trace.

If either is false, stop and write the test first. Pre-existing plans, designs, or specifications do not exempt you from this requirement — a plan is never a substitute for tests.

### 1. Phase: RED (The Failing Test)
Before any logic changes, you must demonstrate the need for the change. Pre-existing plans, designs, or specifications do not exempt you from this requirement — write failing tests first even when the plan describes exactly what to build.
- **For Bug Fixes:** Write a test case that reproduces the reported bug.
- **For New Features:** Write tests defining the new expected behavior.
- **Verification:** Run `pytest <test_path>` and confirm it fails. 
- **Requirement:** You must output the failure trace (e.g., `AssertionError` or `XFAIL`) to prove the test is valid.

### 2. Phase: GREEN (The Implementation)
Write only the code necessary to satisfy the failing test.
- **Action:** Implement the fix or feature in the project root directory.
- **Verification:** Run the specific test again to confirm it passes.
- **Regression Check:** Run the entire suite using `pytest` to ensure existing functionality remains intact.

### 3. Phase: REFACTOR (The Cleanup)
Clean up the implementation while maintaining passing status.
- **Action:** Improve naming, remove duplication, and utilize **pytest fixtures** for shared setup.
- **Verification:** Final run of `pytest` to ensure everything is still green.

---

## Project Overview

Solara is a Python/Flask web application that connects to the Emporia VUE Utility
Connect API to predict hourly solar energy usage. It helps homeowners with rooftop
solar and net energy metering (NEM) maximize self-consumption by predicting total
energy produced or consumed in the coming hour, based on per-second energy data from
smart meters.

Key capabilities:
- Fetches real-time energy metrics from the Emporia VUE API via `pyemvue`
- Predicts hourly energy usage/generation
- Provides a web UI showing current and predicted metrics
- Exposes a JSON HTTP endpoint for home automation integrations
- Automatically controls smart plugs and Tesla vehicle charging to absorb excess solar (load management)

### Key Dependencies

| Dependency | Purpose |
|---|---|
| `pyemvue` | Python client for the Emporia VUE API — fetches real-time smart meter data |
| `flask` | Web framework for routing, templating, and JSON responses |
| `pytz` | Timezone conversion for displaying timestamps in device-local time |
| `isodate` | Serializes Python `timedelta` objects to ISO 8601 duration strings |
| `humps` | Case conversion utilities (camelCase ↔ snake_case) for API data |
| `requests` | HTTP client used internally by `pyemvue` for API calls |

### Key Environment Variables

- `DEBUG` — Enables debug logging when set to `True`
- `TIMEZONE` — Device timezone for display and load management time ranges (default: `America/Los_Angeles`)
- `LOAD_MANAGE_ENABLED` — Enables load management; accepts `True`, `False`, or `HH:MM-HH:MM` time range
- `LOAD_TARGET_WH` — Target Wh per quarter-hour for load decisions (default: `-50`)
- `LOAD_NBC_DEVICE` — Device name for NBC predictions
- `LOAD_MANAGE_INTERVAL_SECS` — Seconds between load management cycles (default: 30)
- `LOAD_TELEGRAM_DEVICES` — JSON string of device→actions whitelist for Telegram notifications.
  Notifications are **only sent** when this whitelist is configured AND at least one
  action matches. If omitted or empty, no Telegram notifications are sent.
  Example: `{"pool_pump": ["turn_on", "turn_off"], "jackery": ["turn_on"]}`
- Emporia VUE credentials are stored in `.vue-keys.json` rather than environment variables

---

## Project Layout

This is a flat-layout Python project. All source files live at the project root — there is no src/ directory, no packages, and no nested module hierarchy.

---

## Project Structure

```
project-root
├── app.py                 # Flask application entrypoint & route definitions (/, /health,
                           # /api/v1/tou, /api/v1/load/status, /api/tesla/callback)
├── clock.py               # Clock protocol (now()) with FakeClock for tests
├── config.py              # TeslaConfig/PlugConfig/VocolincConfig dataclasses,
                           # load_tesla_config(), load_plug_configs(), etc.
├── config_loader.py       # LazyConfig deferred env loading (config.get(), config.set())
├── conftest.py            # Pytest shared fixtures & configuration
├── constants.py           # Named constants for magic numbers (STALE_DATA_THRESHOLD_SECS, etc.)
├── device_config.py       # devices.json loader and typed accessors (get_telegram_config,
                           # get_tesla_config, get_homekit_plugs, etc.)
├── energy_aggregator.py   # TOU (time-of-use) energy aggregation logic
├── energy_cache.py        # EnergyCache with per-second sample storage, incremental
                           # fetch merging, and pruning
 ├── load_controllers.py   # Load manager controllers: PlugController/RealPlugController,
                            # TeslaController/RealTeslaController, VocolincController/RealVocolincController,
                            # and factory functions (load_controller_from_env,
                            # fleet_telemetry_config_create)
 ├── load_manager.py       # OAuth handling, pipeline stages (_stage_*), load-shedding management,
                             # _last_tesla_at_home preserves at_home across telemetry snapshots
 ├── load_models.py        # Shared data models (CycleContext, CycleResult, PendingEffect,
                            # TeslaChargeState, TeslaDriveState, TeslaLocation, TeslaCallbackPayload,
                            # TeslaEvent, TeslaEventKind, TeslaVehicleTelemetry,
                            # parse_tesla_event_payload, update_tesla_telemetry,
                            # get_active_tesla_telemetry, FleetTelemetryProvisionConfig)
├── load_nbc.py            # NBCReader, StateTracker, GapMinder bin-packing, Tesla decisions
 ├── mockdata.py            # Test data generation utilities
 ├── mqtt_telemetry.py      # Tesla MQTT message parsing (on_message, tesla_state_from_snapshot)
  ├── quantization.py        # Detect N-second constant-value windows (quantization) in per-second data
  ├── sse_event.py            # SSEBroadcaster thread-safe pub/sub + event_stream generator for Flask
├── telegram.py            # TelegramSender, NotificationEvent, config loading helpers
├── telegram_client.py     # Async Telegram Bot API client using aiohttp
├── util.py                # Shared utilities (JSON helpers, timezone handling)
├── pyproject.toml         # Project metadata, dependencies & script entrypoints
├── render.yaml            # Render.com deployment configuration
├── env.example            # Template for required environment variables
├── tests/                 # All pytest tests
├── templates/             # Jinja2 HTML templates (index, TOU, error pages)
├── docs/                  # Supplementary documentation (e.g., LOADMANAGER.md, SSE_STREAMING.md)
├── devices.json           # Local device configuration — never commit
├── .env                   # Local secrets — never commit
├── .tesla-callback-config # Tesla callback registration config (client_id, registration_url)
```

### Key entry points

- **Guard functions** `metrics.py`: `cap_chart_start()`, `cap_fetch_window()` —
  prevent over-fetching when cache is stale; pure functions, independently tested
- `EnergyCache` in `energy_cache.py` with `get_or_fetch()`, `is_valid()`,
  `sleep_interval_adjust()`, and quantization detection
- NBC calculation in `metrics.py` (`get_current_qh()` helper)
- `HourlyProjection` in `metrics.py` with `populate()` (uses `cap_chart_start`
  guard), `predict()`, and per-device prediction via `_predict_device()`
- TOU in `energy_aggregator.py`
- Load controller factories in `load_controllers.py` (`load_controller_from_env`, etc.)
- `init_tesla_state()` / `_init_from_rest()` in `load_controllers.py` — initializes
  vehicle state from telemetry with REST fallback when initial telemetry is missing
  (waits up to 60 s for telemetry, then falls back to REST API with minimal calls)
- OAuth in `load_manager.py`
- Tesla callback config dotfile: `.tesla-callback-config` (auto-created, auto-updated)
- Pipeline orchestration in `load_manager.py` (`_stage_enabled_check`, `_stage_nbc_fetch`,
  `_stage_pending_check`, `_stage_compute_gap`, `_stage_async_phase`, `_stage_commit`,
  `_stage_build_result`) — each independently testable
- `_fetch_tesla_state_async()` in `load_manager.py` — fetches Tesla state from MQTT
  telemetry with a fast path; returns telemetry state as long as `ChargeAmts` is present
  (does NOT require `Location`). Preserves `at_home` from `_last_tesla_at_home` when
  `Location` is absent in the snapshot. Delegates to controller's `init_tesla_state()`
  (which waits up to 60 s for telemetry, then REST) when telemetry is not yet available
- Data models in `load_models.py`
- Routes in `app.py`
- Test data generation in `mockdata.py`
- Quantization detection in `quantization.py`
- Timezones in `util.py`
- Deferred config in `config_loader.py` (`LazyConfig`, `config.get()`, `config.set()`)
- Tesla config in `config.py` (`TeslaConfig` dataclass, `load_tesla_config()`)
- Tesla telemetry intervals in `config.py` (`tesla_telemetry_chargestate_interval`,
  `tesla_telemetry_location_interval`, `tesla_telemetry_chargeamps_interval`,
  `tesla_telemetry_detailedchargestate_interval`)
- FleetTelemetryProvisionConfig in `load_models.py` (all interval fields including
  `detailedchargestate_interval_sec`)
- Tesla fleet-telemetry provisioning in `load_controllers.py` (`fleet_telemetry_config_create`)
- DetailedChargeState parsing in `mqtt_telemetry.py` (`tesla_state_from_snapshot`)
- Device config accessors in `device_config.py` (`get_telegram_config`, `get_tesla_config`, etc.)
- Integrity validations in `device_config.py` (`validate_telegram_devices`, `_validate_integrity` — called after every `_load()` to ensure `telegram.devices` keys match plug names)
- Telegram client in `telegram_client.py` (`TelegramClient`, `TelegramConfig`)
- Telegram sender in `telegram.py` (`TelegramSender`, `NotificationEvent`)
- SSE broadcaster and endpoint tests in `tests/test_sse.py` (`SSEBroadcaster`, `event_stream`)
- Pipeline stage tests in `tests/test_pipeline_stages.py`
- Pipeline stage tests in `tests/test_pipeline_stages.py`
- CycleContext tests in `tests/test_cycle_context.py`
- Tesla callback config tests in `tests/test_tesla_callback_config.py`
- Tesla init state tests (telemetry-first, REST fallback) in `tests/test_tesla_init_state.py`

### Actions Generation Flow
- GapMinder.decide() generates actions as a list of PendingEffect objects
- In run_cycle(), raw NBC predictions are adjusted with pending effect deltas via
  `estimated_current_wh()` before being passed to decide() — this accounts for
  actions already taken this quarter-hour without waiting for fresh API data
- Actions are determined by comparing adjusted predicted_wh against target_wh (default -50 Wh)
- Three action types: "turn_on", "turn_off", "set_amps"
- Algorithm uses bin-packing to fit eligible loads into the surplus gap

### Dry-Run Mode
- Controlled by LOAD_MANAGE_DRY_RUN env var (currently True in .env line 10)
- In LoadManager.run_cycle():
  - When dry_run=True: actions are logged but NOT executed, state is NOT updated
  - When dry_run=False: actions are executed via _execute_action(), and successful actions are appended to self.state.pending_effects
- Returns "status": "dry-run" vs "status": "ok" depending on mode

### Index Endpoint
- app.py / route (lines 171-195) serves HTML or JSON based on Accept header
- Returns model.metrics which includes:
  - devices: list with gid, lag, name, prediction, nbc (clock-boundary quarter-hour data),
    prev_hour_data
  - api_response: timing info
  - instant: timestamp
- Template templates/index.html displays NBC QH1-QH4 values with dynamic time-range labels,
  minute/hour usage, predictions

### Device State Tracking
 - StateTracker class (load_nbc.py lines 315–578) maintains:
   - devices: dict[str, DeviceState] - desired/actual state, current_amps, last_toggle
   - pending_effects: list[PendingEffect] - actions taken since last NBC data point,
     pruned when fresh data arrives via `prune_old_effects()`
   - last_data_point_at, last_nbc_predicted_wh
   - registered: bool - whether Tesla callback is registered via dotfile
- Key methods:
  - `estimated_current_wh()`: adjusts raw NBC prediction with pending effect deltas
  - `has_pending_effect_since()`: checks if any action was taken after given timestamp
  - `pending_since_count()`: counts effects after a given timestamp (for diagnostics)
  - `prune_old_effects()`: removes effects older than cutoff to prevent unbounded growth
- DeviceState dataclass tracks per-device runtime state
- Stale detection uses **data-point age** (not fetch time): `data_point_at = fetched_at - timedelta(seconds=data_lag_secs)`.
  The threshold is 120 seconds from the most recent per-second data point, accounting for
  Emporia API lag. Min toggle interval: 60 seconds.

### Telegram Notifications
- `TelegramConfig` (telegram_client.py) — frozen config dataclass with bot_token and chat_id
- `TelegramClient` (telegram_client.py) — async aiohttp client, fire-and-forget, returns `bool`, never raises
- `TelegramSender` (telegram.py) — high-level sender with config loading from env vars and devices.json
- `NotificationEvent` (telegram.py) — frozen dataclass for structured notifications with `format_message()`
- `load_telegram_config()` (telegram.py) — loads config from env vars (priority) or devices.json
- `load_telegram_devices()` (telegram.py) — loads device whitelist dict from `LOAD_TELEGRAM_DEVICES` env var or devices.json
- `validate_telegram_devices()` (device_config.py) — validates telegram.devices keys match plug names after every `_load()`
- Whitelist gate: Telegram notifications are only sent when a telegram.devices whitelist is explicitly configured AND at least one action matches it. Without a whitelist, notifications are blocked to prevent unintended messages to unconfigured devices.
- Plug notifications use emoji format: `🔵 device → ON` / `🔴 device → OFF`

### EnergyCache & Incremental Fetch
- `EnergyCache` (energy_cache.py) stores per-second energy samples with metadata in a
  frozen `EnergyCacheData` dataclass:
  - `samples`: list[float] — per-second Wh values
  - `data_start`: datetime — start time of the sample window
  - `sample_count`, `last_sample_at`: metadata for diagnostics
  - `full_metrics_dict`: dict[str, Any] | None — metrics dict refreshed on every fetch (not just the first),
    returned on cache hits to preserve keys like `devices`, `nbc`, `instant`
- `_build_incremental_fetch(cache, vue_mock, gid, now)`: builds a fetcher that returns
  only new samples since the last data point. Returns `None` on API error.
- `_merge_samples(existing, new_data)`: merges new samples into existing cache, updating
  metadata. Handles overlapping and non-overlapping data ranges.
- `_prune_old_samples()`: removes samples older than 3600 seconds from `now` to prevent
  unbounded memory growth. Called automatically by `get_or_fetch()`.
- `get_or_fetch(fetcher, force=False)`: returns cached data if valid (within TTL), otherwise
  calls the fetcher. When an incremental fetcher is available, it merges new samples into
  existing cache instead of replacing them entirely. On cache hits, returns the full metrics
  dict (including `devices`) if stored from a prior fetch. Always updates `full_metrics_dict`
  on every fetch to keep predictions current.

### Key Architecture
- LoadManager orchestrates cycles every 30 seconds via background thread, calling `run_cycle(force=False)` by default.
  The optional `force=True` parameter bypasses the stale-data check and always fetches fresh NBC data from API.
- EnergyCache stores per-second samples in a sliding window; NBCReader reads QH predictions from it with `get_current_qh(force=False)`
- Controllers: PlugController (stub) / RealPlugController (aiohomekit), TeslaController (stub) / RealTeslaController (tesla-fleet-api)
- Plugs configured via LOAD_PLUG_<NAME>=<accessory_id>:<power_watts>[:<priority>] env vars

### Authentication & Error Handling
- **Emporia VUE API**: Auth tokens (access, id, refresh) are stored in `.vue-keys.json` at the project root. The `Metrics` class reads this file to authenticate via `pyemvue`. This file contains sensitive credentials and must never be committed.
- **Retryable Errors**: A custom `RetryableMetricsException` triggers an auto-refreshing error page (5-second refresh) when the Emporia API returns server errors.

## Maintenance

When you add, delete, or significantly change a file, update this tree — 
including the description — before finishing the task.

---

## Planning

**During planning, operate in strict read-only mode.** This means:

- Always write your plan to a new file in your agent's plan directory (`.opencode/plans/` or `.mimocode/plans/`).
- No file writes anywhere in the repo except the agent's plan directory
- No shell commands that mutate state: no `pip install`, no `git commit`,
  no `git add`, no file edits, no database migrations
- Allowed read operations: `cat`, `ls`, `grep`

When asked to plan changes, break tasks into subtasks that each fit within a
**32k–48k token budget per subtask**. If a task requires touching more than 3
files or ~200 lines of code, split it into sequential subtasks and plan them
separately. Document each subtask in the overall plan file in your agent's plan directory.

## Plan files
When writing or updating plan files in your agent's plan directory (`.opencode/plans/*.md` or `.mimocode/plans/*.md`), always use bash (e.g. `cat > .opencode/plans/foo.md << 'EOF'...` or `cat > .mimocode/plans/foo.md << 'EOF'...`)
rather than the Write or Edit tools, which fail due to path matching issues.

### Plan Implementation

When implementing a pre-existing plan (written by you or another agent), follow this order:
1. **Read the plan** — understand what needs to change.
2. **Write failing tests first** — even if the plan is detailed, a plan is not a substitute for tests.
3. **Make them pass** — implement production code to satisfy the tests.
4. **Refactor** — clean up while keeping all tests green.

Pre-existing plans, designs, or specifications do not exempt you from the test-first requirement. The Red phase must always come first — before any production code changes, even if the plan was written by a human or another agent.

For changes larger than ~20 lines, summarize what will change (files affected,
functions modified, any data migrations or schema changes) before writing any code.

When a plan file is still in `.opencode/plans/` or `.mimocode/plans/`, treat it as potentially active work unless told otherwise. A plan file in that directory is a signal that work may still be in progress.

Before any destructive action (deleting files, removing test classes, truncating files), **stop and ask**.

---

## 🚀 Build, Lint, & Test Commands

### Mandatory Post-Edit Verification Gate

After **any** code change, always run these commands in order. Do not proceed
to the next step if a prior step fails.

```bash
uv run pylint *.py                     # 1. Style and bug checks
uv run mypy                            # 2. Type correctness
uv run pytest                          # 3. Full test suite
```

### Individual Commands

| Purpose | Command |
|---|---|
| Run full test suite | `uv run pytest` |
| Run a single test | `uv run pytest tests/test_app.py::test_function_name` |
| Lint | `uv run pylint *.py` |
| Type check | `uv run mypy` |
| Dev server | `uv run python app.py` |
| Production-like server | `gunicorn --reload --worker-class=gthread --threads=4 --timeout=0 --bind 127.0.0.1:8000 app:app` |

The dev server reads credentials from `.env` (`VUE_USERNAME`, `VUE_PASSWORD`).
Ensure that file is present and sourced before running.

---

## 📐 Code Style Guidelines

### 1. General Formatting (PEP 8)

- **Indentation:** 4 spaces — no tabs
- **Line length:** 100 characters maximum
- **Imports:** Grouped in this order, each on its own line:
  1. Standard library (`os`, `json`, `datetime`)
  2. Third-party packages (`flask`, `requests`, `pytz`)
  3. Local project imports (`from load_manager import ...`, `import metrics`)
- All code must pass `pylint` clean with no suppressions unless explicitly justified
  in a comment

### 2. Naming Conventions

| Construct | Convention | Example |
|---|---|---|
| Modules / files | `snake_case` | `energy_utils.py` |
| Classes | `PascalCase` | `EmporiaClient` |
| Functions / methods | `snake_case` | `fetch_daily_usage()` |
| Constants | `ALL_CAPS` | `DEFAULT_TIMEOUT_SECS` |
| Variables | `snake_case` | `kwh_total` |

### 3. Documentation & Typing

- **Docstrings:** Required on all modules, classes, public methods, and functions.
  Use Google-style format:

  ```python
  def fetch_usage(start: datetime, end: datetime) -> list[float]:
      """Fetch energy usage between two timestamps.

      Args:
          start: Start of the query window, timezone-aware.
          end: End of the query window, timezone-aware.

      Returns:
          List of kWh readings, one per hour.

      Raises:
          EmporiaAPIError: If the upstream API returns a non-200 status.
      """
  ```

- **Type hints:** Mandatory on all function arguments, return values, and instance
  attributes. Use `from __future__ import annotations` at the top of modules to
  support forward references. Prefer built-in generics (`list[str]`, `dict[str, int]`)
  over `typing.List`, `typing.Dict` in Python 3.9+.

- **Codebase map:** Read the "Project Structure" section in this document and
  keep it up to date.

### 4. Error Handling

- Never use bare `except:` — always catch specific exceptions
- Use `with` statements for file handles, DB connections, and any resource
  requiring cleanup
- Wrap all Emporia API calls in `try/except` blocks handling at minimum:
  `requests.RequestException`, `requests.Timeout`, and any custom `APIError`
- On auth failures (HTTP 401/403), log the error and raise — do not silently retry

### 5. Security

- **No hardcoded secrets.** Read all credentials and API keys from environment
  variables via the local `decouple.py` library. If you find hardcoded secrets,
  fix them immediately.
- **Validate all user input** (URL params, form fields, query strings) before
  use or storage.

---

## 🧩 Specific Guidelines

### Date / Time

- Always use timezone-aware `datetime` objects
- Use `pytz` for timezone handling; default to local system timezone unless
  storing to a database, in which case use UTC
- Never compare naive and aware datetimes — this will raise a `TypeError` at runtime

### HTTP Requests

- Use the `requests` library
- Parse JSON responses with `.json()` — never `json.loads(response.text)`
- Set explicit timeouts on all outbound requests (e.g., `timeout=30`)

### Emporia API

- Rate limits: respect any `Retry-After` headers
- Auth tokens expire; implement token refresh before retrying a failed request
- Wrap all calls in the standard error handling pattern described above

---

### 💡 Agent Guidelines for Pytest
- **Assertions:** Use descriptive assertions. For expected errors, use `with pytest.raises(Exception):`.
- **Parametrization:** Use `@pytest.mark.parametrize` for testing multiple edge cases efficiently.
- **Isolation:** Ensure tests do not depend on local environment state; use mocks or temporary directories (`tmp_path` fixture) where necessary.
- **Stop Condition:** If you cannot create a failing test that reproduces a bug, **STOP** and request clarification. Do not attempt a "blind fix."

---

## 🧪 Testing Guidelines

**Write tests for all new functionality.** A PR with new behavior but no new
  tests is incomplete. This includes changes driven by pre-existing plans — a plan file is never a substitute for tests.
- **Always guard against pollution from `devices.json` and `.env` files** The local `.env` (loaded by `decouple`) may conflict with your test. Consider that and guard against it. Use deferred config in app code, and monkeypatch.setenv in pytest fixtures.
```
# ❌ Evaluated at import — hard to mock
DATABASE_URL = config('DATABASE_URL')

# ✅ Deferred — evaluated when called, so we can patch decouple's config before the values are ever resolved, giving tests full control.
def get_database_url():
    return config('DATABASE_URL')

# ✅ In test code:
@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    config.set('DATABASE_URL', 'sqlite:///:memory:')
    config.set('DEBUG', 'True')
```
- **Use `FakeClock` for time-based tests.** Never patch `datetime.now` directly
  — it is fragile and often patches the wrong module namespace. Always inject a
  `FakeClock` into the object under test. Example:
  ```python
  from clock import FakeClock
  fake_now = FakeClock(datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc))
  mgr._clock = fake_now
  ```
  The `FakeClock` implements the same `Clock` protocol as `RealClock` so it works
  anywhere a real clock is used (load management time-range checks, NBC quarter-hour
  boundaries, stale-data detection, sleep-hint calculations, etc.).
- **Never add special-case code solely to make tests pass.** For example, do not
  add `if os.getenv("TESTING"):` branches in production code paths.
- **Updating test data is allowed and expected** when modernizing hardcoded dates
  or stale fixture values. Example of what's allowed:
  ```python
  # Before (stale fixture date causes false failure)
  SAMPLE_DATE = datetime(2021, 1, 1)
  # After (updated to a current reference date)
  SAMPLE_DATE = datetime(2025, 1, 1)
  ```
  Example of what's **not** allowed:
  ```python
  # Not allowed — production logic changed to accommodate a test
  if date.year < 2022:
      return []  # silence legacy test failure
  ```

---

## ⛔ Stop and Ask Policy

Pause and explicitly ask the user before proceeding when:

- Requirements are ambiguous and the choice between interpretations would affect
  more than one file
- A change involves destructive operations: file deletion, schema migration,
  bulk data modification
- Two consecutive attempts to fix a failing test have not resolved it
- A dependency needs to be added or upgraded (`pyproject.toml` / `requirements`)
- You are about to make a change that touches the auth flow or secrets handling
