# Solara Architecture Diagrams

Mermaid diagrams describing the Solara system: the Flask web app, the energy
prediction pipeline, and the load management subsystem that controls smart
plugs and Tesla charging to maximize solar self-consumption.

## 1. System Overview

External services on the left (Emporia VUE, Tesla, Telegram) feed data in;
the app's core modules in the middle process it; clients on the right consume
it.

```mermaid
flowchart LR
    subgraph External["External Services"]
        VUE["Emporia VUE API<br/>(via pyemvue)"]
        MQTT["Tesla Fleet Telemetry<br/>MQTT broker"]
        FLEET["Tesla Fleet API"]
        HOMEKIT["HomeKit smart plugs<br/>(aiohomekit)"]
        VOCOLINC["Vocolinc plugs"]
        TG["Telegram Bot API"]
    end

    subgraph App["Solara App"]
        subgraph Web["Flask App (app.py)"]
            ROUTES["Routes<br/>/ · /health · /api/v1/tou<br/>/api/v1/load/status · /stream/status<br/>/tesla/oauth"]
            TEMPLATES["Jinja2 templates<br/>index.html · tou.html"]
            SSE["SSEBroadcaster<br/>(sse_event.py)"]
        end

        subgraph Data["Energy Data Path"]
            METRICS["metrics.py<br/>HourlyProjection / TOUReporter"]
            CACHE["EnergyCache<br/>(energy_cache.py)"]
            AGGR["energy_aggregator.py<br/>TOU buckets"]
            QUANT["quantization.py<br/>window detection"]
        end

        subgraph LoadMgmt["Load Management"]
            LM["LoadManager<br/>(load_manager.py)"]
            NBC["load_nbc.py<br/>NBCReader / StateTracker / GapMinder"]
            CTRL["load_controllers.py<br/>Plug / Tesla / Vocolinc controllers"]
            TELEM["mqtt_telemetry.py<br/>MQTT subscriber"]
            MODELS["load_models.py<br/>data models + parsing"]
            NOTIF["telegram.py / telegram_client.py<br/>TelegramSender"]
        end
    end

    subgraph Clients["Clients"]
        BROWSER["Browser dashboard"]
        JSONAPI["Home automation<br/>(JSON endpoints)"]
        SSECLIENT["SSE client<br/>(/stream/status)"]
    end

    VUE --> METRICS
    VUE --> AGGR
    METRICS --> CACHE
    CACHE --> QUANT
    CACHE --> NBC
    CACHE --> ROUTES
    METRICS --> ROUTES

    MQTT --> TELEM
    TELEM --> MODELS
    MODELS --> CTRL
    FLEET --> CTRL
    HOMEKIT --> CTRL
    VOCOLINC --> CTRL

    TELEM --> LM
    CTRL --> LM
    NBC --> LM
    CACHE --> LM
    LM --> NOTIF
    NOTIF --> TG

    LM --> ROUTES
    LM --> SSE
    ROUTES --> TEMPLATES
    SSE --> SSECLIENT
    ROUTES --> BROWSER
    ROUTES --> JSONAPI
```

## 2. Metrics / Data Flow (index, TOU, SSE)

The `EnergyCache` is the shared per-second sample store: the web layer, the
NBC reader, and the load manager all read from it. Completed quarter-hours are
compacted into `CompletedNBCPeriod` objects and injected back into
quarter-hour windows (`util.inject_completed_qh`).

```mermaid
flowchart TD
    VUE["Emporia VUE API"] -->|"pyemvue channel fetch"| HP["HourlyProjection.populate()<br/>(metrics.py)"]
    HP -->|"per-second Wh samples"| CACHE["EnergyCache.get_or_fetch()<br/>TTL 60s, prune >3600s,<br/>quantization detect"]
    CACHE -->|"stale-cache serve on retryable errors"| INDEX["index()<br/>/ (HTML or JSON)"]
    HP -->|"drift rejection"| DRIFT["DriftAlert → Telegram<br/>(_drain_drift_alerts)"]

    VUE -->|"TOUReporter.fetch_usage_data()"| AGGR["energy_aggregator.py<br/>TOU buckets"]
    AGGR -->|"TOU buckets"| TOUROUTE["/api/v1/tou"]

    CACHE -->|"QH-aligned data_start"| NBC["NBCReader.get_current_qh()<br/>(load_nbc.py)"]
    NBC -->|"predicted_wh"| LM
    CACHE -->|"compacted periods"| INJECT["inject_completed_qh()<br/>fills QH2–QH4 (util.py)"]
    INJECT --> TEMPLATE["templates/index.html"]

    CACHE -->|"full_metrics_dict"| SSE["SSE broadcaster<br/>metrics_update events"]
    SSE --> STREAM["/stream/status<br/>(SSE)"]
```

## 3. Load Management Cycle

`LoadManager.run_cycle()` runs a seven-stage pipeline every ~30 seconds on a
background thread (or adaptively per `sleep_hint`). Each stage is an
independently testable method; any stage may early-exit with a `CycleResult`.

```mermaid
flowchart LR
    LOOP["_load_management_loop()<br/>background thread (app.py)"]
    LOOP --> CYCLE["run_cycle()<br/>guarded by self._lock"]

    CYCLE --> S0["0. _check_config_changes()<br/>reload .env / devices.json"]
    S0 --> S1["1. _stage_enabled_check()<br/>disabled / outside time window?"]
    S1 -->|"early exit"| RESULT["CycleResult"]

    S1 -->|"enabled"| S2["2. _stage_nbc_fetch()<br/>NBCReader.get_current_qh(force=True)<br/>→ drains DriftAlerts"]
    S2 -->|"stale / no data"| RESULT
    S2 -->|"predicted_wh"| S3["3. _stage_pending_check()<br/>data-point age vs STALE_DATA_THRESHOLD_SECS<br/>pending effects not yet reflected?"]

    S3 -->|"early exit"| RESULT
    S3 -->|"ok"| S4["4. _stage_compute_gap()<br/>prune old effects,<br/>StateTracker.estimated_current_wh(),<br/>gap = target_wh − adjusted_wh"]

    S4 --> S5["5. _stage_async_phase()<br/>single event loop:<br/>fetch Tesla state (MQTT fast path,<br/>REST fallback) → GapMinder.decide()<br/>→ _execute_action()"]

    S5 --> S6["6. _stage_commit()<br/>sentinel check, persist PendingEffect,<br/>Tesla amp-state tracking,<br/>hysteresis early exit"]
    S6 -->|"early exit"| RESULT
    S6 -->|"ok"| S7["7. _stage_build_result()<br/>candidate details, no-action reason,<br/>CycleResult + sleep_hint"]

    RESULT --> LOOP
    RESULT -->|"camelized payload"| SSE2["SSEBroadcaster.publish('load_cycle')"]
    RESULT -->|"notifications flushed"| NOTIF2["TelegramSender (whitelist-gated)"]
```

## 4. Load Cycle Sequence (detailed)

Shows the interactions between the load manager, the shared energy cache, and
the device controllers for one cycle.

```mermaid
sequenceDiagram
    autonumber
    participant THREAD as _load_management_loop
    participant LM as LoadManager
    participant CACHE as EnergyCache (shared)
    participant NBCR as NBCReader
    participant TRACK as StateTracker
    participant GM as GapMinder
    participant CTRL as Controllers
    participant SSE as SSEBroadcaster
    participant TG as TelegramSender

    THREAD->>LM: run_cycle()
    LM->>LM: _check_config_changes()
    LM->>LM: _stage_enabled_check()
    alt disabled / off-hours
        LM-->>THREAD: CycleResult(status=disabled)
        THREAD->>THREAD: sleep(interval)
    else enabled
        LM->>NBCR: get_current_qh(force=True)
        NBCR->>CACHE: get_or_fetch(force=True)
        CACHE-->>NBCR: per-second samples + quantization
        NBCR-->>LM: predicted_wh, data_point_at
        LM->>TRACK: estimated_current_wh(predicted_wh)
        TRACK-->>LM: adjusted_wh
        LM->>LM: _stage_compute_gap()
        LM->>GM: decide(gap, devices, tesla)
        GM-->>LM: list[PendingEffect]
        loop for each action
            LM->>CTRL: set_state / set_amps
            CTRL-->>LM: success?
            alt dry_run
                LM-->>LM: log only, no execute
            end
        end
        LM->>LM: _stage_commit()
        LM-->>SSE: publish load_cycle
        LM-->>TG: send pending notifications (sync flush)
        LM-->>THREAD: CycleResult + sleep_hint
        THREAD->>THREAD: sleep(sleep_hint, cache-adjusted)
    end
```

## 5. Tesla Telemetry & State Flow

Fleet telemetry arrives over MQTT; the load manager prefers this fast path
over the REST API and preserves `at_home` across snapshots when `Location` is
missing.

```mermaid
flowchart TD
    TESLA["Tesla vehicle"] -->|"fleet telemetry push<br/>(charge state, location, amps)"| MQTTB["MQTT broker"]
    MQTTB --> SUB["mqtt_telemetry.start_mqtt_subscriber()<br/>daemon thread (started by start_background_services)"]
    SUB --> ONMSG["on_message()<br/>parse + store snapshot"]
    ONMSG --> SNAP["get_telemetry_snapshot()<br/>in-memory store"]
    SNAP --> FETCH["_fetch_tesla_state_async()<br/>(load_manager.py)"]
    FETCH -->|"ChargeAmts present → telemetry state<br/>(Location optional; at_home preserved)"| DECIDE["GapMinder.decide()"]
    FETCH -->|"no telemetry → wait ≤60s then REST"| REST["RealTeslaController.init_tesla_state()<br/>(tesla-fleet-api)"]
    REST --> DECIDE

    DECIDE -->|"set_amps / stop-charge"| TESLA

    subgraph OAuth["Tesla OAuth (tesla_oauth.py)"]
        INIT["GET /tesla/oauth/initiate"] --> CALLBACK["GET /tesla/oauth/callback<br/>stores tokens → .tesla-tokens.json"]
        CALLBACK --> REST
    end

    %% unwrap_telemetry_value / parse_charge_amps (load_models.py) are shared
    %% between mqtt_telemetry and load_controllers
```

## 6. Entry Points & Background Services

```mermaid
flowchart TD
    WSGI["wsgi.py<br/>app = create_app()<br/>start_background_services()"] --> APP["app.py create_app()<br/>routes · JSON provider · error handlers"]
    MAIN["python app.py<br/>(dev server)"] --> APP
    CLI1["python app.py --pair-plug<br/>&lt;name> &lt;address> &lt;pin>"] --> PH["pair_homekit_accessory()"]
    CLI2["python app.py --tesla-auth"] --> TA["tesla_auth_cli()"]
    CLI3["python app.py --provision-fleet-telemetry<br/>&lt;host> &lt;ca> [port]"] --> PT["provision_fleet_telemetry()"]

    start_background_services["start_background_services()"] --> MQTTSTART["MQTT subscriber thread<br/>(if load_tesla_controller == 'real')"]
    start_background_services --> LMTHREAD["Load management thread<br/>(if load management enabled)"]
    LMTHREAD --> LOOP2["_load_management_loop()"]
    APP --> REG["atexit → _shutdown_load_manager()"]
```

## Data Model Axioms

### Per-second sample contiguity

The `EnergyCache` sample list is **strictly contiguous**: sample `i`
represents exactly the 1-second bucket beginning at
`data_start + timedelta(seconds=i)`, and every bucket is present, non-null,
and finite.

This is an axiom, not a validated invariant. The upstream Emporia API (via
pyemvue `get_chart_usage`) is trusted to return complete, contiguous windows;
interior gaps and null buckets are ruled out by design. Only the window head
is checked (`firstUsageInstant != chart_start` → drift rejection). No
count-vs-span, interior-gap, or null-bucket validation exists anywhere in the
pipeline — by deliberate decision, not oversight.

Code that would be wrong if this axiom were violated (and therefore depends
on it):

- Pruning maps index → time arithmetically (`_prune_old_samples`,
  `sample_time = data_start + i s`).
- Compaction chunks purely by count (`samples[offset:offset+900]`).
- Quarter-hour splitting is count-based (`values_len % 900` in
  `util.compute_nbc_quarters`).
- `_data_lag_secs` is *derived from the sample count*
  (`instant − (data_start + len(samples))` in `metrics.py`), so it measures
  tail freshness only — it cannot reveal interior gaps, and any hypothetical
  gap check built on it would be circular.

If upstream behavior ever changes such that gaps or nulls can appear, revisit
this axiom before adding defensive checks.

## File → Module Map

| Concern | Module |
|---|---|
| Flask app, routes, background loops | `app.py` |
| Gunicorn entry point | `wsgi.py` |
| Energy fetch & hourly prediction | `metrics.py` |
| Per-second sample cache | `energy_cache.py` |
| TOU aggregation | `energy_aggregator.py` |
| NBC reading, state tracking, bin-packing decisions | `load_nbc.py` |
| Load cycle orchestration, OAuth, notifications queue | `load_manager.py` |
| Device controllers (HomeKit, Tesla, Vocolinc), factories | `load_controllers.py` |
| Shared data models, telemetry parsing helpers | `load_models.py` |
| Tesla MQTT telemetry parsing | `mqtt_telemetry.py` |
| Quantization detection | `quantization.py` |
| Structured log formatting | `logfmt.py` |
| SSE broadcaster | `sse_event.py` |
| Telegram notifications | `telegram.py`, `telegram_client.py` |
| Deferred config, Tesla/Plug config dataclasses | `config.py`, `config_loader.py` |
| devices.json loader & integrity validation | `device_config.py` |
| Quarter-hour helpers, compaction records | `util.py` |
| Tesla OAuth routes | `tesla_oauth.py` |
| FakeClock / Clock protocol | `clock.py` |
| Test data generation | `mockdata.py` |
| Templates | `templates/` |
| Tests | `tests/` |
