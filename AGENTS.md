# 🛠️ AI Agent Guidelines for Solara Codebase

This file is the rulebook for AI coding agents in the `solara` repository. It
holds binding rules only. Reference material lives in `docs/`.

Write all responses in Simplified Technical English (ASD-STE100 style).

Invoke tools with structured function-calling JSON, not inline XML or Markdown.

## Workflow

1. Write a failing test that reproduces the bug or defines the new behavior.
2. Run it and show the failure trace. Do not touch production code before this.
3. Write only enough code to make the test pass.
4. Run the full gate. Refactor while it stays green.

A plan, design, or specification is not a test. It never exempts you from
step 1.

**Layout exemption.** Presentation-only changes may skip the Red-Green cycle:
template markup, `static/style.css`, and visual structure. Run the full gate
after the change. Any change to behavior, logic, data shape, or API contract
still needs a failing test first.

Do not ask whether a bug pre-dates your change. Do not dig through git. Fix it.

## Stop and ask

Stop and ask the user when:

- Requirements are ambiguous, and the reading changes more than one file.
- The change is destructive: file deletion, schema migration, bulk data edit.
- Two consecutive attempts did not fix a failing test.
- A dependency must be added or upgraded.
- The change touches authentication or secret handling.
- You cannot write a failing test that reproduces the bug.
- A permission error blocks you. Never work around it.

## Analyzing a log in `bugs/`

When a task names a path under `bugs/`, your FIRST tool call is the scorer:

```bash
uv run python tools/forecast_log_scoring.py bugs/<file>.log
```

Lead your reply with its raw output. Then explain that output from the
`nbc_set` lines. Do not compute anchors, errors, or the quarter's actual Wh by
hand. Hand numbers explain the tool's numbers; they never replace them.

Figures in `docs/MEASUREMENTS.md` are snapshots of the corpus at measurement
time. The corpus is refreshed. Treat the numbers as history, not a baseline.

Flags, report sections, and dated corpus figures for every tool:
`docs/MEASUREMENTS.md`.

## Planning

Planning is read-only. Write the plan to `.opencode/plans/` or
`.mimocode/plans/`. Do not write elsewhere or run anything that mutates the
repository.

Split a plan into subtasks of 32k-48k tokens. Split any subtask that touches
more than 3 files or about 200 lines.

To implement a plan: read it, write the failing tests, make them pass, then
refactor.

A plan file left in a plans directory marks possible active work. Treat it as
active unless told otherwise.

## Commands

Run these after every code change, in this order. Stop at the first failure.

```bash
uv run pylint *.py tools/*.py      # style and bug checks
uv run mypy                        # type correctness
uv run pytest                      # full suite, no coverage
uv run pytest --cov=.              # opt-in coverage check
```

| Purpose | Command |
|---|---|
| Single test | `uv run pytest tests/test_app.py::test_name` |
| Dev server | `uv run python app.py` |
| Production-like | `gunicorn --reload -c gunicorn.conf.py --bind 127.0.0.1:8000 wsgi:app` |

The dev server reads credentials from `.env`.

## Code style

- 4 spaces, no tabs. Maximum line length 100 characters.
- Imports in three groups, one per line: standard library, third party, local.
- `snake_case` modules and functions, `PascalCase` classes, `UPPER_CASE`
  constants.
- Type hints on every argument, return value, and instance attribute. Use
  `from __future__ import annotations` and built-in generics.
- Google-style docstrings on modules, classes, and public functions.
- Pylint clean. Justify any suppression in a comment.
- Catch specific exceptions. Never write a bare `except:`.
- Use `with` for files and other resources.
- Wrap Emporia API calls. Handle `RequestException`, `Timeout`, and `APIError`.
- On auth failure (HTTP 401/403), log and raise. Do not retry silently.
- Emporia tokens expire. Refresh them before you retry a failed request.
- No hardcoded secrets. Read them through `config.Config`. Validate all user
  input before use.
- Use timezone-aware datetimes only. Never compare naive and aware values. Keep
  storage in UTC and convert with `pytz` for display.
- Respect a `Retry-After` header from the Emporia API.

## Testing notes

- Use `FakeClock`. Never patch `datetime.now`.
- Use `pytest.mark.parametrize` for edge cases. Use `tmp_path` for files.
- Never depend on `bugs/` or `corpus/`. Both are untracked, so a fresh clone
  has no logs. Put the needed log data in the test as a checked-in fixture,
  and guard any real-log check with `skipif`.
- Drive behavior tests with synthetic log text.
- Guard tests against the local `.env` and `devices.json`. Read config lazily
  and set values with `monkeypatch.setenv`.
- Never add a production branch only to satisfy a test.
- Updating stale test data is allowed. Changing production logic to fit a test
  is not.

## Project overview

Solara predicts hourly energy use and generation for a home with rooftop solar
and net energy metering. It reads per-second smart-meter data from the Emporia
VUE API through `pyemvue`, serves a Flask dashboard and a JSON endpoint, and
controls smart plugs and Tesla charging to absorb excess solar.

Key dependencies: `pyemvue`, `flask`, `pytz`, `isodate`, `requests`.

Key settings:

- `DEBUG` — enables debug logging and the diagnostics section.
- `TIMEZONE` — device timezone (default `America/Los_Angeles`).
- `LOAD_MANAGE_ENABLED` — `True`, `False`, or an `HH:MM-HH:MM` range.
- `LOAD_TARGET_WH` — target Wh per quarter-hour (default `-50`).
- Local files: `.env`, `devices.json`, and `.vue-keys.json` hold secrets.
  Never commit them.

All other load manager settings: `docs/LOADMANAGER.md`.

## Invariants

These are design decisions. Do not change them without the owner's approval.

- **Contiguity axiom.** Per-second samples are contiguous at 1 s from
  `data_start`. The upstream API is trusted to return complete windows. No
  count-vs-span, interior-gap, or null-bucket check exists anywhere. Do not add
  one without revisiting the axiom first.
- **Turn-off is unguarded.** `_decide_turn_off` has no minimum-time floor and
  no overshoot cap. Do not add either one.
- **Defer deadline sizing.** `defer_lookahead_quanta` counts observation
  windows, never seconds. Size any new decision rule from
  `DecideContext.observability_secs`, not from a literal 30 s.
- **Churn EWMA is removed.** Do not reintroduce it.

## Maintenance

When you add, delete, or rename a file, update the module map below in the
same change. `tests/test_agents_md.py` fails when the map drifts, when this
file passes 250 lines, or when a line passes 100 characters.

Put new reference text in `docs/`, not here:

- `docs/architecture.md` — module internals, data flow, state ownership.
- `docs/LOADMANAGER.md` — load manager settings, decision rules, Tesla setup.
- `docs/MEASUREMENTS.md` — dated measurements and shipped verdicts.
- `docs/TESTING.md` — what each test file pins.
- `docs/SSE_STREAMING.md` — dashboard streaming and the event stream.

## Module map

Flat layout: all source files live at the project root. One line per file.

```text
app.py                 # Flask factory, routes, background services, shutdown
gunicorn.conf.py       # Gunicorn hooks and arbiter watchdog timeout
wsgi.py                # Gunicorn entry point
chart.py               # Server-side SVG sparkline
clock.py               # Clock protocol and FakeClock
config.py              # Deferred config lookup, Tesla/plug/vocolinc config
config_loader.py       # Config loading helpers
conftest.py            # Pytest fixtures and configuration
constants.py           # Named constants: thresholds, Tesla limits, defaults
device_config.py       # devices.json loader, typed accessors, integrity checks
energy_aggregator.py   # TOU aggregation
energy_cache.py        # Per-second sample store, compaction, replace-not-merge
gap_trend.py           # Adjusted-gap slope tracker for the ramp-aware stop
load_controllers.py    # Plug/Tesla/Vocolinc controllers and factories
load_manager.py        # Cycle pipeline stages, controllers, Telegram, alerts
load_models.py         # Cycle and telemetry dataclasses, parsing helpers
load_nbc.py            # Cycle engine: GapMinder, TeslaDecider, effect stores
logfmt.py              # Structured log formatting: logfmt or JSON lines
metrics.py             # NBC fetch, HourlyProjection, cache guards, drift alerts
mockdata.py            # Test data generation
mqtt_telemetry.py      # Tesla MQTT parsing and subscriber lifecycle
quantization.py        # N-second constant-window detection
sse_event.py           # SSE broadcaster and event stream
telegram.py            # TelegramSender, NotificationEvent
telegram_client.py     # Async Telegram Bot API client
tesla_oauth.py         # Tesla OAuth blueprint and token flow
util.py                # Timezone, JSON, and quarter-hour helpers
vocolinc.py            # VOCOlinc cloud API client
static/                # Design-system CSS and the SSE dashboard client
templates/             # Jinja2 pages and SSE-swappable fragments
tests/                 # Pytest suite; what each file pins: docs/TESTING.md
tools/                 # Offline analysis only; never imported by production
tools/divergence_audit.py            # Status-quo vs residual shed-watts audit
tools/estimator_replay.py            # Closed-loop estimator and policy replay
tools/forecast_log_scoring.py        # Score forecast anchors against actuals
tools/settle_window_counterfactual.py  # Price adaptive vs pinned settle window
```
