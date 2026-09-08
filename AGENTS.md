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

Never try to work around permission errors. Stop and ask for help.

### Communication Style

Use simple, everyday language. Avoid unnecessary jargon unless the context
requires technical precision.

---

## 🧪 Pytest-Driven Development Protocol

To ensure code quality and prevent regressions, all development must follow a strict **Test-First (Red-Green-Refactor)** workflow. You are prohibited from modifying production code until a failing test has been established. You are prohibited from attempting to diagnose, fix, or design a fix for any bug until a failing test has been written. Test first!

### Layout Experiments (Exemption)

Iterating on **presentation** — template markup, CSS, and visual structure —
is exploratory by nature: you often don't know what "correct" looks like until
you render it and look, so there is no meaningful failing test to write up
front. For those changes you may **skip the Red-Green ceremony**:

- No failing test is required before touching `templates/`, `static/style.css`,
  or presentation-only changes to markup.
- Still run the full verification gate (`uv run pylint *.py`, `uv run mypy`,
  `uv run pytest`) after the change, and update any existing tests that assert
  on the changed markup so the suite stays green.
- The exemption covers **presentation only**. Any change that alters behavior,
  logic, data shape, or API contracts must still follow the full Red-Green-
  Refactor protocol below.

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
| `requests` | HTTP client used internally by `pyemvue` for API calls |

### Key Environment Variables

- `DEBUG` — Enables debug logging when set to `True`
- `TIMEZONE` — Device timezone for display and load management time ranges (default: `America/Los_Angeles`)
- `LOAD_MANAGE_ENABLED` — Enables load management; accepts `True`, `False`, or `HH:MM-HH:MM` time range
- `LOAD_TARGET_WH` — Target Wh per quarter-hour for load decisions (default: `-50`)
- `LOAD_NBC_DEVICE` — Device name for NBC predictions
- `LOAD_MANAGE_INTERVAL_SECS` — Seconds between load management cycles (default: 30)
- Telegram device whitelist comes from the `telegram.devices` section of `devices.json`
  (there is no env-var override; the old `LOAD_TELEGRAM_DEVICES` env var is gone).
  Example: `{"pool_pump": ["turn_on", "turn_off"], "jackery": ["turn_on"]}`
- Emporia VUE credentials are stored in `.vue-keys.json` rather than environment variables

---

## Project Layout

This is a flat-layout Python project. All source files live at the project root — there is no src/ directory, no packages, and no nested module hierarchy.

---

## Project Structure

```
project-root
├── app.py                 # Flask app factory (create_app()), route definitions (/, /health,
                           # /api/v1/tou, /api/v1/load/status, /api/tesla/callback),
                           # _AppState runtime singletons, start_background_services()
├── wsgi.py                # Gunicorn entry point: app = create_app(); start_background_services()
├── gunicorn.conf.py       # Gunicorn hooks: post_worker_init chains cooperative-shutdown
                           # signal handlers; worker_int/worker_exit call app.request_shutdown();
                           # timeout = 60 pins the arbiter watchdog (2x the 30s fetch bound)
├── clock.py               # Clock protocol (now()) with FakeClock for tests
├── chart.py               # Server-side SVG generation for the per-second energy
                           # sparkline (per_second_sparkline): bar height = magnitude,
                           # color = sign (green generation, blue consumption); flush
                           # bars tile slots edge-to-edge (no gap). Samples are
                           # bucketed by the quantization window (quantization_seconds,
                           # typically 30 s) so each bar is many viewBox units wide —
                           # a sub-pixel per-second grating has nowhere to alias
                           # against the device pixel grid when CSS scales the SVG
                           # (moire); bucketed bars overhang the next by 1 s so
                           # anti-aliased seams at shared edges are repainted solid,
                           # and the svg root carries shape-rendering="crispEdges"
                           # so every edge snaps onto whole device pixels (no AA
                           # hairlines even above shorter neighbors); always laid
                           # out for a full 300-second window (partial windows render
                           # left-aligned at real time positions, blank right)
├── config.py              # TeslaConfig/PlugConfig/VocolincConfig dataclasses,
                           # load_tesla_config(), load_plug_configs(), Config.log_file, etc.
├── config_loader.py       # Config loading helpers (load_tesla_config,
                           # load_plugs_from_file, load_vocolinc_*) reading
                           # env via the Config class + devices.json
├── conftest.py            # Pytest shared fixtures & configuration
├── constants.py           # Named constants for magic numbers (STALE_DATA_THRESHOLD_SECS,
                           # Tesla charging constants TESLA_HARD_MAX_AMPS, etc.)
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
 ├── load_models.py        # Shared data models (CycleContext, CycleResult, AsyncPhaseResult,
                            # PendingEffect,
                            # TeslaChargeState, TeslaDriveState, TeslaLocation, TeslaCallbackPayload,
                            # TeslaEvent, TeslaEventKind, TeslaVehicleTelemetry,
                            # parse_tesla_event_payload, update_tesla_telemetry,
                            # get_active_tesla_telemetry, FleetTelemetryProvisionConfig) plus
                            # shared fleet-telemetry parsing helpers (unwrap_telemetry_value,
                            # parse_charge_amps) used by mqtt_telemetry and load_controllers
├── load_nbc.py            # NBCReader, EffectStore, TeslaSettleTracker, StateTracker, GapMinder bin-packing + TeslaDecider, PendingEffect factories
 ├── logfmt.py              # Structured log formatters: render extra= fields as
 │                          #   [key=value ...] suffixes (default) or JSON lines
 │                          #   (LOG_FORMAT=json); wired into app.py handlers
 ├── mockdata.py            # Test data generation utilities
 ├── mqtt_telemetry.py      # Tesla MQTT message parsing (on_message, tesla_state_from_snapshot);
 │                          # idempotent start_mqtt_subscriber() (lock + live-thread check);
 │                          # generation token isolates superseded sessions after restart
  ├── quantization.py        # Detect N-second constant-value windows (quantization) in per-second data
  ├── sse_event.py            # SSEBroadcaster thread-safe pub/sub + event_stream generator for Flask
                              # (close_all() wakes blocked streams on shutdown; sentinel never yielded)
├── telegram.py            # TelegramSender, NotificationEvent, config loading helpers
├── telegram_client.py     # Async Telegram Bot API client using aiohttp
├── util.py                # Shared utilities (JSON helpers, timezone handling)
├── pyproject.toml         # Project metadata, dependencies & script entrypoints
├── render.yaml            # Render.com deployment configuration
├── env.example            # Template for required environment variables
├── tests/                 # All pytest tests
├── templates/             # Jinja2 HTML templates (index, TOU, error pages);
                           # _metrics.html/_load_management.html are SSE-swappable fragments
                           # (the metrics fragment carries the #data-freshness strip; the
                           # sparkline filter receives quantization_seconds to feed
                           # chart.per_second_sparkline's bucket_secs downsampling)
├── static/                # Mobile-first design system (style.css) and the SSE dashboard
                           # client (app.js) wiring EventSource → fragment swaps + freshness ticker
├── docs/                  # Supplementary documentation (e.g., LOADMANAGER.md, SSE_STREAMING.md, architecture.md)
├── devices.json           # Local device configuration — never commit
├── .env                   # Local secrets — never commit
├── .tesla-callback-config # Tesla callback registration config (client_id, registration_url)
```

### Key entry points

- **Guard functions** `metrics.py`: `cap_chart_start()`,
  `_chart_start_for()` — cap over-fetching when cache is stale and keep the
  fetch window anchored to a stale QH-aligned `data_start` so a completed QH
  is not lost at a boundary; pure functions, independently tested
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
- `_is_vehicle_offline_error()` in `load_controllers.py` — detects VehicleOffline
  exceptions from Tesla fleet API; used to downgrade ERROR to WARNING and trigger
  short sleep hint for faster retries
- OAuth in `load_manager.py`
- Tesla callback config dotfile: `.tesla-callback-config` (auto-created, auto-updated)
- Pipeline orchestration in `load_manager.py` (`_stage_enabled_check`, `_stage_nbc_fetch`,
  `_stage_pending_check`, `_stage_compute_gap`, `_stage_async_phase`, `_stage_commit`,
  `_stage_build_result`) — each independently testable. All early exits go through
  the `_early_exit()` builder (shared hysteresis/plugs/tesla/quantization defaults);
  shared queries live in `_local_time()` (device-tz conversion), `_eligible_plugs()`
  (engine-eligible vs out-of-range split), `is_sentinel_on()`, `_is_tesla_in_range()`,
  and `_waiting_sleep_hint()` (prediction-window-capped re-check delay). The async
  phase (`_cycle_async_phase` → `_cycle_async_phase_body`) returns an
  `AsyncPhaseResult` dataclass (named fields, not a tuple) and is split into
  `_async_sync_and_check_sentinel()`, `_record_tesla_auth_error()`,
  `_correct_gap_with_inflight()`, `_suppressed_by_settle()`,
  `_eligible_for_decision()`, `_decide_actions()`, `_run_actions()`; Tesla
  execute paths share `_mark_vehicle_offline()` + `_handle_tesla_auth_error()`;
  `run_cycle()` stage timings go through `_timed()`
- `_stage_nbc_fetch` in `load_manager.py` — always fetches fresh NBC data
  (`get_current_qh(force=True, ...)` regardless of `ctx.force`). The NBC reader shares the
  app-level `EnergyCache` (wired in `app._get_load_manager()`), whose TTL outlives the
  30 s cycle; a TTL-paced read would cache-hit and skip the fetch, letting data age toward
  the stale-data threshold. The reader's fast path (`get_current_qh` with `force=False`)
  remains for other callers (e.g. `metrics.py`)
- `_resolve_prediction_window()` / `StateTracker.apply_prediction_window()` — the
  prediction/settle window is adaptive: it resolves lazily from shared-cache
  quantization (quantization data only exists after the first fetch of a cycle) and
  tracks sustained meter-behavior changes. A new window is committed only after two
  consecutive `_stage_compute_gap` calls, and detections within
  `SETTLE_WINDOW_DEADBAND_SECS` (5 s) of the committed window are treated as detector
  jitter (e.g. 29/30/31 s around a true 30 s period). Periods below
  `MIN_QUANTIZATION_WINDOW_SECS` (15 s) — e.g. the flat-data N=2 artifact — are
  rejected by `_resolve_prediction_window` and fall back to the default
- `_quantization_diagnostics()` in `load_manager.py` — snapshots the current
  quantization state (from the shared `EnergyCache`, refreshed every fetch) plus the
  derived settle window (`StateTracker.effective_settle_secs`); every
  `CycleDiagnostics` construction site folds this in, so each cycle result exposes
  `quantization_seconds`/`quantization_offset`/`quantization_confidence`/
  `settle_window_secs` in the DEBUG cycle-result log (default dataclass repr at
  `app.py` `_load_management_loop`) and the JSON/SSE payloads
  (`/api/v1/load/status`, index, SSE `load_cycle`). None-guarded for stub
  `nbc_reader`s that lack `energy_cache`
- `_fetch_tesla_state_async()` in `load_manager.py` — fetches Tesla state from MQTT
  telemetry with a fast path; returns telemetry state as long as `ChargeAmts` is present
  (does NOT require `Location`). Preserves `at_home` from `_last_tesla_at_home` when
  `Location` is absent in the snapshot. When live telemetry is present but parses to
  `None` (no `DetailedChargeState` and no positive `ChargeAmps`), the vehicle is treated
  as idle/disconnected — a not-charging `TeslaState` is returned (`current_amps=0`,
  `plugged_in=False`, `at_home` from live `Location` or `_last_tesla_at_home`) and the
  controller's stale cached `_init_state` is NOT used as an answer (ghost-guard;
  bugs/2026-08-31-ghost-tesla-amps.log). Delegates to controller's `init_tesla_state()`
  (which waits up to 60 s for telemetry, then REST) when telemetry is not yet available
  or when `at_home` is unseeded and `Location` is absent (to seed location)
- Data models in `load_models.py`
- `nominal_voltage()` in `load_nbc.py` — deferred-config resolver for the
  `TESLA_NOMINAL_VOLTAGE` env (default 240; invalid/non-positive falls back
  with a WARNING). All StateTracker amp↔watt conversions and
  `GapMinder.car_power_watts_5a` read it per-call. Do not reintroduce
  import-time voltage constants into conversion math.
- `StateTracker.record_tesla_amp_command()` / `tesla_inflight_wh()` zero-amp
  confirmation (plan 3.5): a single reported 0 A frame does NOT clear
  `last_commanded_amps` — only `TESLA_ZERO_AMPS_CLEAR_SAMPLES` (2)
  consecutive zeros do, and until then the full commanded delta stays
  accounted. The 1 A ramp-up gate credits the unconfirmed portion
  (`commanded − reported`) instead of returning 0. Tests asserting an
  instant clear or a ramp-up zero encode the old bug — do not restore them.
- Shared fleet-telemetry parsing helpers in `load_models.py` (`unwrap_telemetry_value`,
  `parse_charge_amps`) — single home for the `{"value": ...}` envelope unwrap + amps
  rounding, used by `mqtt_telemetry.py` (`on_message`, `tesla_state_from_snapshot`,
  `_compute_at_home_from_location`) and `load_controllers.py` (`_init_from_rest`)
- `_reset_runtime_state()` / `_apply_persisted_tokens()` in `load_controllers.py` —
  shared init paths for `RealTeslaController` (`__init__`/`reset_session` and the
  `_ensure_api` create/update branches respectively)
- `create_app()` factory + routes in `app.py` (module-level `app` singleton; no
  background threads start at import time)
- Cooperative shutdown (single-interrupt exit): `_stop_event` + idempotent
  `request_shutdown(reason)` in `app.py` — sets the stop event every background
  loop waits on (`_load_management_loop` sleeps via `_stop_event.wait()`, not
  `time.sleep()`), wakes SSE streams via `SSEBroadcaster.close_all()`, stops the
  MQTT subscriber (`mqtt_telemetry.stop_mqtt_subscriber()`), and closes the
  LoadManager exactly once. `install_shutdown_signal_hooks()` chains those calls
  over existing SIGINT/SIGQUIT/SIGTERM handlers; wired by `gunicorn.conf.py`
  hooks (`post_worker_init`/`worker_int`/`worker_exit`) and the `__main__`
  block. Rationale: concurrent.futures joins all executor threads (daemon or
  not) at interpreter exit, so a blocked SSE stream used to hang shutdown until
  a second Ctrl-C.
- `start_background_services()` in `app.py` — explicitly starts the MQTT subscriber
  and load-management thread; called from `wsgi.py` and the `__main__` block, never
  at import time (so tests can import the module safely)
- `_setup_file_logging()` in `app.py` — creates `RotatingFileHandler` when `LOG_FILE` is configured
- `_metrics_freshness_payload()` in `app.py` — computes the dashboard
  data-freshness strip (worst device lag + next-update cadence) rendered into
  `_metrics.html`; returns None when no devices. The `live` flag tells the
  client whether SSE is a continuous driver (load management enabled) or a
  one-shot snapshot (keep the server auto-refresh / JS reload timer alive);
  it also selects the mode-aware warn/stale thresholds emitted as
  `data-warn`/`data-stale`.
  Cadence resolution: load-management sleep hint → quantization refresh
  window → `METRICS_UPDATE_FALLBACK_SECS` (120 s)
- Test data generation in `mockdata.py`
- Quantization detection in `quantization.py`
- Timezones in `util.py`
- `CompletedNBCPeriod` in `util.py` — immutable record of a compacted QH period
- `inject_completed_qh()` in `util.py` — fills QH2-QH4 from completed periods
- `compact()` in `energy_cache.py` — compacts completed QH periods into `CompletedNBCPeriod` objects, called after every fetch in `get_or_fetch()`
- `_merge_samples_replace()` in `energy_cache.py` — always-replace fetch path (no overlap merge)
- `EnergyCacheAlignmentError` in `energy_cache.py` — raised by `get_current_qh()` when `data_start`
  is missing or not QH-aligned; a plain `Exception` (asserts are stripped under `python -O`), kept as a
  safety net since the fetch-site drift checks below reject misaligned data before it is stored
- Deferred config in `config.py` (`Config.get()`, `Config.set()`, `_lookup` chain:
  overrides -> os.environ -> .env); config loading helpers in `config_loader.py`
  (`load_tesla_config`, `load_plugs_from_file`, `load_vocolinc_*`)
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
- CycleContext usage is covered by the pipeline stage tests (`tests/test_pipeline_stages.py`)
- Tesla callback config tests in `tests/test_tesla_callback_config.py`
- Tesla init state tests (telemetry-first, REST fallback) in `tests/test_tesla_init_state.py`
- Tesla command VehicleOffline handling in `tests/test_vehicle_offline_command.py`
- File logging with rotation tests in `tests/test_file_logging.py` (`_setup_file_logging`)
- Compaction tests in `tests/test_compaction.py` (`CompletedNBCPeriod`, `compact()`, `inject_completed_qh()`, replace-not-merge behavior)

### Actions Generation Flow
- GapMinder.decide() generates actions as a list of PendingEffect objects
- All PendingEffect construction goes through the module factories in load_nbc.py:
  `make_plug_effect()` (applies the signed-power invariant), `make_tesla_set_amps()`,
  `make_tesla_stop()` — no other PendingEffect() call sites remain in load_nbc.py
- Tesla amp logic lives in TeslaDecider (owned by GapMinder as `tesla_decider`):
  `decide_increase()` / `decide_reduce()` / `supports()` / `safe_defer_secs()`.
  GapMinder keeps thin `_decide_tesla_*` delegates so existing call sites and tests
  keep working. Plug candidate collection is unified in
  `GapMinder._eligible_plugs(ctx, want_on)` (priority order baked in).
- In run_cycle(), raw NBC predictions are adjusted with pending effect deltas via
  `estimated_current_wh()` before being passed to decide() — this accounts for
  actions already taken this quarter-hour without waiting for fresh API data
- Actions are determined by comparing adjusted predicted_wh against target_wh (default -50 Wh)
- Three action types: "turn_on", "turn_off", "set_amps"
- Algorithm uses bin-packing to fit eligible loads into the surplus gap
- Turn-off shedding is deliberately unguarded (design decision, not an
  oversight): no MIN_SECONDS_TO_ACT floor and no overshoot cap —
  `_decide_turn_off` turns plugs off until `remaining_reduction <= 0`, even
  for negligible in-quarter savings near the QH boundary. Asserted by
  tests/test_gap_minder.py; do not add turn-off guards without revisiting
  this decision.

### Dry-Run Mode
- Controlled by LOAD_MANAGE_DRY_RUN env var (currently True in .env line 10)
- In LoadManager.run_cycle():
  - When dry_run=True: actions are logged but NOT executed, state is NOT updated
  - When dry_run=False: actions are executed via _execute_action(), and successful actions are appended to self.state.pending_effects
- Returns "status": "dry-run" vs "status": "ok" depending on mode

### Index Endpoint
- `index()` in app.py serves HTML or JSON based on Accept header
- Static assets (`static/style.css`, `static/app.js`) are referenced with
  **relative paths** (not `url_for` absolute paths) so the dashboard works
  behind a transparent nginx proxy that mounts the app under a path prefix
  (e.g. `/solara/`); the SSE `?partial=` fetches in `app.js` are likewise
  relative
- Returns model.metrics which includes:
  - devices: list with gid, lag, name, prediction, nbc (clock-boundary quarter-hour data),
    prev_hour_data
  - api_response: timing info
  - instant: timestamp
- Template templates/index.html displays NBC QH1-QH4 values with dynamic time-range labels,
  minute/hour usage, predictions
- The metrics section (`_metrics.html`) opens with a data-freshness strip
  (`#data-freshness`): worst device lag + next-update cadence, ticked by
  `static/app.js`; status colors fresh → aging → stale against mode-aware
  thresholds in `constants.py` — `FRESHNESS_RELOAD_WARN/STALE_SECS` (300/420,
  load management disabled) and `FRESHNESS_LIVE_WARN/STALE_SECS` (180/300,
  load management enabled), emitted as `data-warn`/`data-stale` so the client
  ticks with the same values. Its `data-live` flag ("1" when load management
  is enabled) tells the SSE client whether to cancel page auto-refresh
  (continuous driver) or keep the meta refresh / JS reload timer (one-shot
  snapshot)
- The legacy "Load Management" section (`_load_management.html`,
  `#load-management-section`) is kept but only **displayed when debug mode is
  enabled** (`metrics.debug` in the full page; `metrics_data["debug"]` gates
  the `/?partial=load` fragment). Load management itself keeps running and the
  freshness strip still drives live SSE updates regardless — the section is a
  diagnostics view, not the live driver

### Device State Tracking
 - EffectStore class (load_nbc.py) owns the pending-effects list with its own
   RLock: `add()`, `snapshot()`, `has_since()` / `count_since()` (window-buffered
   freshness checks), `prune()` (dual wall-clock + data-point age), `latest_for()`,
   `clear_tesla_set_amps()`. StateTracker keeps a `pending_effects` property for
   backward compat plus thin delegating methods.
 - TeslaSettleTracker class (load_nbc.py) owns the in-flight Tesla amp command
   (`last_commanded_amps`, zero-sample confirmation): `record_command()`,
   `inflight_wh()` (with `_inflight_zero()` / `_inflight_one_amp()` helpers),
   `get_active()` / `is_settling()` sharing one `_find_active()` lookup
   (dual wall-clock + data-point age, QH-boundary expiry). StateTracker keeps
   a `last_commanded_amps` property plus delegating `tesla_inflight_wh()`,
   `record_tesla_amp_command()`, `get_active_tesla_settle()`, `is_settling()`,
   and syncs the adaptive window into both stores via `apply_prediction_window`.
 - StateTracker class (load_nbc.py) maintains:
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
  - `apply_prediction_window()`: resolves the prediction/settle window from shared-cache
    quantization; commits a new window only after two consecutive cycles and ignores
    dead-band jitter (see `_resolve_prediction_window`)
- DeviceState dataclass tracks per-device runtime state
- Stale detection uses **data-point age** (not fetch time): `data_point_at = fetched_at - timedelta(seconds=data_lag_secs)`.
  The threshold is `STALE_DATA_THRESHOLD_SECS` (80 seconds, constants.py) from the most
  recent per-second data point, accounting for Emporia API lag. Min toggle interval: 60 seconds.

### Telegram Notifications
- `TelegramConfig` (telegram_client.py) — frozen config dataclass with bot_token and chat_id
- `TelegramClient` (telegram_client.py) — async aiohttp client, fire-and-forget, returns `bool`, never raises
- `TelegramSender` (telegram.py) — high-level sender with config loading from env vars and devices.json
- `NotificationEvent` (telegram.py) — frozen dataclass for structured notifications with `format_message()`
- `load_telegram_config()` (telegram.py) — loads config via the Config lookup chain (env vars → `.env` file) first, then devices.json
- Telegram device whitelist: loaded from the `telegram.devices` section of devices.json in `LoadManager.__init__` / `reload_config()` (there is no env-var override; the old `LOAD_TELEGRAM_DEVICES` env var is gone)
- `validate_telegram_devices()` (device_config.py) — validates telegram.devices keys match plug names after every `_load()`; "tesla" is accepted as a special device name for Tesla stop-charging alerts
- Whitelist gate: Telegram notifications are only sent when a telegram.devices whitelist is explicitly configured AND at least one action matches it. Without a whitelist, notifications are blocked to prevent unintended messages to unconfigured devices.
- Plug notifications use emoji format: `🟢 device → ON` / `🔘 device → OFF`
- Tesla notifications use device-specific format: `🔌 Tesla charging stopped` / `⚡ Tesla charging started` / `🔋 Tesla charge amps → N A`

### EnergyCache & Incremental Fetch

**⚠️ Contiguity axiom (deliberate design decision):** per-second samples are
strictly contiguous at 1 s from `data_start` — sample `i` occurred at exactly
`data_start + timedelta(seconds=i)`, and every bucket is present, non-null,
and finite. The upstream Emporia API is trusted to return complete windows;
interior gaps / null buckets are ruled out by design and **no count-vs-span,
interior-gap, or null-bucket validation exists anywhere in the pipeline — do
not add defensive gap detection without revisiting this axiom first.**
Everything below depends on it: pruning maps index → time arithmetically,
compaction chunks purely by count (`samples[offset:offset+900]`), QH splitting
is count-based (`values_len % 900`), and `_data_lag_secs` is *derived* from the
count (`instant − (data_start + len(samples))`, metrics.py), so lag measures
tail freshness only and cannot reveal interior gaps. Only the window *head* is
validated (`firstUsageInstant != chart_start` drift check).

- `EnergyCache` (energy_cache.py) stores per-second energy samples with metadata in a
  frozen `EnergyCacheData` dataclass:
  - `samples`: list[float] — per-second Wh values
  - `data_start`: datetime — start time of the sample window
  - `sample_count`, `last_sample_at`: metadata for diagnostics
  - `full_metrics_dict`: dict[str, Any] | None — metrics dict refreshed on every fetch (not just the first),
    returned on cache hits to preserve keys like `devices`, `nbc`, `instant`
- `_fetch_channel_data()` in `metrics.py` (HourlyProjection) — rejects a drifted API
  `data_start` (`firstUsageInstant != requested chart_start`) by raising
  `RetryableMetricsException` before any misaligned data is stored; `_run_fetch_with_timeout`
  logs it as transient ("will retry"), `get_or_fetch` serves stale cache, and the next cycle
  refetches (quiet self-heal).  Persistent drift — the same `(channel_num, chart_start)`
  key rejected `DRIFT_REJECTION_ALERT_AFTER` (5) consecutive fetches — escalates: an
  ERROR-level log plus a one-time `DriftAlert` event per QH key, drained by
  `LoadManager._drain_drift_alerts()` (called by `run_cycle` after the NBC fetch stage)
  which queues a Telegram error notification via `_queue_drift_error_notification()`
  (bypasses the devices whitelist; fires whenever a `TelegramSender` is configured).
  `metrics.drain_drift_alerts()` returns/clears the pending alert events.
- `_merge_samples_replace(existing, new_data)`: replaces existing samples with new
  data (always-replace, no overlap merge), updating metadata.
- `_prune_old_samples()`: removes samples older than 3600 seconds from `now` to prevent
  unbounded memory growth. Called automatically by `get_or_fetch()`.
- `get_or_fetch(fetcher, force=False)`: returns cached data if valid (within TTL), otherwise
  calls the fetcher. Fetch results replace the cached samples (no overlap merge). On cache
  hits, returns the full metrics dict (including `devices`) if stored from a prior fetch.
  Always updates `full_metrics_dict` on every fetch to keep predictions current.

### Key Architecture
- LoadManager orchestrates cycles every 30 seconds via background thread, calling `run_cycle(force=False)` by default.
- EnergyCache stores per-second samples in a sliding window; NBCReader reads QH predictions from it with `get_current_qh(force=False)`. After compaction, completed QH periods are stored as immutable `CompletedNBCPeriod` objects and per-second data only covers the current incomplete QH.
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


### Plan Implementation

When implementing a pre-existing plan (written by you or another agent), follow this order:
1. **Read the plan** — understand what needs to change.
2. **Write failing tests first** — the Red phase comes first, no exemptions.
3. **Make them pass** — implement production code to satisfy the tests.
4. **Refactor** — clean up while keeping all tests green.

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
uv run pytest                          # 3. Full test suite (fast, no coverage)
uv run pytest --cov=.                  # 4. Coverage check (opt-in)
```

Coverage is opt-in: `uv run pytest` runs without instrumentation for speed.
Run `uv run pytest --cov=.` (or add `--cov=<module>`) when coverage validation
is required (e.g. CI, or after changing test-relevant code).

### Individual Commands

| Purpose | Command |
|---|---|
| Run full test suite | `uv run pytest` |
| Run a single test | `uv run pytest tests/test_app.py::test_function_name` |
| Lint | `uv run pylint *.py` |
| Type check | `uv run mypy` |
| Dev server | `uv run python app.py` |
| Production-like server | `gunicorn --reload -c gunicorn.conf.py --worker-class=gthread --threads=4 --bind 127.0.0.1:8000 wsgi:app` |

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
  Use Google-style format (`Args:` / `Returns:` / `Raises:` sections).

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
  variables via the `config` module (`Config`/`Config.set`). If you find
  hardcoded secrets, fix them immediately.
- **Validate all user input** (URL params, form fields, query strings) before
  use or storage.

---

## 🧩 Specific Guidelines

### Date / Time

- Always use timezone-aware `datetime` objects
- Use `pytz` for timezone handling; default to local system timezone unless
  storing to a database, in which case use UTC
- Never compare naive and aware datetimes — this will raise a `TypeError` at runtime

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
  tests is incomplete (see the Test-First protocol above).
- **Always guard against pollution from `devices.json` and `.env` files** The local `.env` (read lazily by the `config` module) may conflict with your test. Consider that and guard against it. Use deferred config in app code, and monkeypatch.setenv in pytest fixtures.
```
# ❌ Evaluated at import — hard to mock
DATABASE_URL = config('DATABASE_URL')

# ✅ Deferred — evaluated when called, so we can patch config's lookup before the values are ever resolved, giving tests full control.
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
  `FakeClock` (same `Clock` protocol as `RealClock`) into the object under test.
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
