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

### Analyzing a log in `bugs/`

**When a task mentions a path under `bugs/` (e.g. "analyze
`bugs/2026-10-01-tesla-overshoot.log`"), your FIRST tool call is the scorer, and
your FIRST output block is its raw output:**

```bash
uv run python tools/forecast_log_scoring.py bugs/<file>.log
```

It extracts every forecast anchor plus the completed-quarter actual in one pass
and prints scored-anchor counts, signed/absolute error, `sigma_rate`, per-`R`
buckets, and the quarter's realized total. Lead the reply with that output, then
explain it with a hand reconstruction of the `nbc_set` lines. Do not derive
anchor tables, errors, or the quarter's actual Wh by hand when the tool already
computes them — hand numbers are for *explaining* the tool's numbers, not for
*replacing* them.

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
- Still run the full verification gate (`uv run pylint *.py tools/*.py`, `uv run mypy`,
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
- `LOAD_MANAGE_LUMPY_FIRST` — Enables lumpy-first turn-off ordering (default: `False`)
- `LOAD_MANAGER_RAMP_AWARE` — Enables the ramp-aware Tesla stop hastener (default: `False`).
  Off, the static defer rule alone applies; on, a trusted rising deficit pulls
  the stop one cycle earlier (`bugs/2026-09-26-tesla-stop-charging.log`). The
  default is off because the hastener fired 0 times across the corpus and only
  that incident benefits; the defer rule itself stays (see docs/LOADMANAGER.md)
- `LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA` — Observation windows the Tesla stop's
  safe defer deadline must still clear before a defer is taken (default: `1`).
  A deferral whose deadline sits inside the next window cannot be observed
  through, so the stop is taken now
  (`bugs/2026-10-18-tesla-stop-overshoot.log`). Counted in windows, never
  seconds: the window is the resolved prediction/settle window
  (`DecideContext.observability_secs`). `0` restores the pre-2026-10-19 static
  rule; a malformed value falls back to the default rather than refusing to
  start (see docs/LOADMANAGER.md)
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
├── tools/                 # Offline-only analysis tools. No production module
│                          # imports anything in here; they read log text and report.
│                          # (estimator_replay.py additionally re-runs production
│                          # decision classes — it is still never imported by
│                          # production, and still writes nothing back.)
│                          # Linted and type-checked (`uv run pylint *.py tools/*.py`,
│                          # mypy `files` includes `tools/*.py`), but not shipped in
│                          # the gunicorn/Flask runtime path.
│                          # Every `Corpus`/`corpus/` figure quoted below is a
│                          # SNAPSHOT of the corpus at measurement time — the
│                          # corpus is refreshed over time, so treat the numbers
│                          # as a measurement of a moment, not a fixed baseline.
│   └── forecast_log_scoring.py  # FIRST STEP for any task that names a `bugs/*.log`
│                          # file — run it and lead with its output (see
│                          # "Analyzing a log in `bugs/`" under General Advice).
│                          # Scores NBC forecast quality against production logs:
│                          # parses forecast anchors + completed-quarter actuals and
│                          # reports forecast uncertainty. Despite the name it does NOT
│                          # replay decisions (contrast tests/test_tesla_ramp_replay.py,
│                          # which re-runs production classes); it measures the forecast.
│                          # Identity: a realized error is *identically*
│                          # -remaining_secs * rate_error (ForecastAnchor.error vs
│                          # rate_shortfall), so U(R) = k * sigma_rate * R is complete,
│                          # not an approximation. It also reports a churn band
│                          # (k * churn * R, production's GapTrendTracker fed from the
│                          # anchor series — raw predicted_wh stands in for the gap;
│                          # constant offsets/signs cancel in |Δslope|) beside the
│                          # sigma band, and reads — never re-executes — what
│                          # production logged about its own decisions: CycleResult
│                          # reprs (per-quarter target/miss, reason, production churn,
│                          # scoped to the diagnostics=CycleDiagnostics(...) segment)
│                          # and gapminder_jitter_guard INFO events (wall-time quarter,
│                          # exact because previous_qh exits before decide()). Report
│                          # output: churn-band coverage line, jitter-guard summary
│                          # line, target/miss/guard columns in the quarter table.
│                          # It also attributes controllable watts per cycle from
│                          # the candidates lists (prior-cycle join) and reports
│                          # the residual series (net-of-ON rate error,
│                          # sigma_residual, 260 W survival — residual never
│                          # beat raw sigma on corpus/, so no margin ships).
│                          # `--estimator[=NAME,...]` additionally *scores*
│                          # alternative forecast rates over the same anchors
│                          # (status-quo trailing window; prior = median rate of
│                          # the completed qh2..qh4 the same fetch reports, with
│                          # the shipped forecast as fallback so every row shares
│                          # one anchor set; prior-blend@M = (n*w + M*p)/(n+M);
│                          # trend-gated = the window only while GapTrendTracker
│                          # trusts a trend, mirroring _stage_compute_gap's
│                          # HYSTERESIS_WH/R floor, override with --trend-floor).
│                          # Report: per-variant mean|e|/p80/max/optimistic/
│                          # sigma_rate plus a sigma_rate-by-R-bucket matrix.
│                          # Corpus verdict (760 anchors, floor 3): prior 12.60
│                          # mean|e| vs status-quo 29.71, and at R 360-900 s
│                          # sigma_rate 0.0270 vs 0.1268 — but ramp quarters
│                          # regress (2026-10-01: 22.7 -> 57.5), so this measures,
│                          # it does not ship (ForecastAnchor.prior_rate).
│                          # Default output is unchanged without the flag.
│                          # `--turn-offs` adds the turn-off path collection:
│                          # every entry into the unguarded `_decide_turn_off`
│                          # (horizon R, gap, each shed booked: plug
│                          # `action=turn_off`, Tesla amp reduce, Tesla stop; the
│                          # raw logfmt values, joined to the decision by its
│                          # line-prefix second), reported as shed totals, median
│                          # horizon, short-horizon count
│                          # (TURN_OFF_SHORT_HORIZON_SECS=60), gap coverage with
│                          # the per-action overshoot (savings − remaining gap),
│                          # reversals (a plug turned back on within
│                          # TURN_OFF_REVERSAL_WINDOW_SECS=900 s of the shed —
│                          # time-based, not quarter-scoped, because the clearest
│                          # case crosses the QH boundary), and a per-quarter table
│                          # joining the completed actual/target/miss. Data
│                          # collection only: the unguarded path is a documented
│                          # design decision and is not re-executed or changed.
│                          # Plug device names can contain spaces ("water heater"),
│                          # so the name pattern is `(.+?)` — a `\S+` silently drops
│                          # every shed by a multi-word plug (caught by
│                          # tests/test_estimator_replay.py, which needs the whole
│                          # event set; the dropped heater sheds moved the corpus by
│                          # 5 sheds / 4.9 kWh / 150 Wh of overshoot).
│                          # Corpus: 405 decisions/65 quarters, 34 under 60 s,
│                          # 109/165 sheds overshot (max 222 Wh), 50/84 plug sheds
│                          # reversed (median 136 s).
│                          # `--guards` adds the suppression census — every
│                          # mechanism that only *declines* work is invisible to
│                          # a loop that watches actions, so this counts what each
│                          # one withheld: jitter-guard declines
│                          # (`reason=excessive_jitter` cycles, read from the
│                          # diagnostics segment so a candidate's own reason
│                          # cannot stand in) and the off+toggleable candidates in
│                          # the same repr, banked-cover rescues, stop deferrals
│                          # with their R distribution, ramp-rule fires and
│                          # arithmetical reach (`R <= gap/P + cycle*(1+clamp)`;
│                          # P built from constants.TESLA_CHARGE_AMPS_MIN_DEFAULT *
│                          # TESLA_NOMINAL_VOLTAGE / 3600 and the clamp/cadence
│                          # pinned by test against TeslaDecider
│                          # .RAMP_TREND_CLAMP_FRACTION / LoadManagerConfig
│                          # .config_interval_secs), and the committed settle
│                          # window per cycle (commits = the change production
│                          # never logs). A guarded-vs-unguarded quarter split
│                          # rides alongside with the confound stated in the
│                          # output: the guard fires *in* the churning-forecast
│                          # quarters, so the split is correlation, not effect.
│                          # Corpus: 1242 declines over 1989 declined cycles (all
│                          # 1989 offering a load — availability is universal, so
│                          # the report says so and leaves "did the decline
│                          # withhold an action" to the replay), 767 rescues, 172
│                          # deferrals (median R 382 s), 0 ramp fires / 14
│                          # eligible, settle 30 s x 55299 / 15 s x 103 / 21 s x 4
│                          # / 6 commits. Default output stays byte-identical.
│                          # Run from the repo root:
│                          #   uv run python tools/forecast_log_scoring.py bugs/*.log
│                          #   uv run python tools/forecast_log_scoring.py bugs/*.log --estimator
│                          #   uv run python tools/forecast_log_scoring.py bugs/*.log --turn-offs
│                          #   uv run python tools/forecast_log_scoring.py corpus/*.log --guards
│   └── estimator_replay.py  # Closed-loop replay: drives an estimator through
│                          # production GapMinder.decide() against a counterfactual
│                          # step-function meter, so the verdict is the quarter's
│                          # miss + churn, not the open-loop forecast error the
│                          # scorer reports. The model (Quarter/Replay/meter/
│                          # rate_* variants/Policy) lives here;
│                          # tests/test_estimator_replay.py is its spec. Two
│                          # independent axes: the rate estimator (only `rate_fn`
│                          # differs between runs, pinned equal to the scorer's
│                          # estimate_prior_only/estimate_prior_blend) and the
│                          # decision Policy, expressed *only* through production
│                          # GapMinder constructor surface — `--turn-on-margin WH`
│                          # `--turn-off-margin WH` `--lumpy-first`
│                          # `--defer {on,off}` `--max-defer-secs SECS` `--ramp-aware`
│                          # (the Tesla-stop reluctance trio: the defer rule itself,
│                          # its safe-buffer cap, and the trend hastener; all default
│                          # to the shipped values — defer on, cap 120 s, ramp off)
│                          # The meter has two actuator populations: plugs (ON/OFF,
│                          # nameplate watts) and the car (TeslaSample steps of
│                          # REPORTED amps, replaced by a policy set_amps/Tesla
│                          # turn_off, priced at nominal_voltage() W/amp so one amp
│                          # over R is 240*R/3600 Wh — the quantum behind the
│                          # 2026-10-17 overshoots). plugged_in/at_home ride along
│                          # from the log because production gates both charging
│                          # directions on them, so an absent car is never charged.
│                          # Ingestion stays conservative: quarters where no cycle
│                          # ever reported a Tesla state are dropped
│                          # (tesla_state=None is an early exit, not an idle car;
│                          # production now records the reason on
│                          # CycleDiagnostics.tesla_state_missing_reason, so newer logs
│                          # need no inference — older ones still do); so are quarters under
│                          # MIN_ANCHORS_PER_QUARTER, with an implausible fetch-line lag
│                          # (LAG_MIN/MAX_SECS), or with no candidate dump to read the
│                          # opening ON/OFF state from; repeated samples_used are
│                          # deduped (production's tracker rejects repeats). Command
│                          # times come from each anchor's own `wall_at` (per-quarter
│                          # median lag), not a hand constant. `--variant NAME` (repeatable)
│                          # `--date YYYY-MM-DD` `--worst N` `--trend-floor WH`
│                          # `--regime {all,tesla,plug-only}` (default all; splits the
│                          # report by which actuators were in play, since the plug-only
│                          # half is the pre-car baseline reproduced exactly — the
│                          # policy never commands a car that is not drawing). A Policy
│                          # run reports itself: the header carries the margins and the
│                          # lumpy-first / defer / ramp flags. DecideContext
│                          # always carries banked_wh (the
│                          # quarter's accumulated raw energy, qh1.raw_wh); whether the
│                          # derived gap signals ride along is the guard arm
│                          # (`--guards {inert,churn,live}`, default inert). inert —
│                          # every pinned verdict below — leaves
│                          # gap_jitter_wh_per_s / gap_trend_wh_per_s dark, so the
│                          # turn-on jitter guard, its banked-cover override and the
│                          # ramp-aware Tesla stop never fire; both sides of a comparison
│                          # are equally affected, but a Policy that suppresses actions
│                          # (a margin) overlaps with the guard's job, so its numbers
│                          # read as "outside the guard". live feeds one
│                          # GapTrendTracker per quarter in production's own order
│                          # (noise floor HYSTERESIS_WH/R, churn withheld past
│                          # JITTER_MAX_REMAINING_SECS, churn to gap_jitter_wh_per_s
│                          # unconditionally, the slope to gap_trend_wh_per_s when
│                          # trusted); churn feeds only the jitter signal, which
│                          # isolates the ramp rule (the trend field's only reader is
│                          # _ramp_stop_now). Stated deviation: production's tracker sees
│                          # the pending-effect-adjusted gap of the published
│                          # prediction while the replay's gap reflects its own
│                          # counterfactual meter, so signs and relative deltas are the
│                          # result and absolute landings are the model's. Inert parity
│                          # verified to the printed digit: plug-only 23.41, car-active
│                          # 17.99 mean|miss|.
│                          # Baseline note (2026-10-19): the corpus verdicts
│                          # below are snapshots measured with the pre-ship
│                          # static defer rule (--defer-lookahead-quanta 0). The
│                          # shipped policy is now 1: car-active 17.99 -> 17.91,
│                          # plug-only bit-identical (23.36). Re-measured on one
│                          # current snapshot, no ordering below reverses
│                          # (estimator variants still lose, the zero-margin
│                          # policy still wins or ties, the defer rule is still
│                          # load-bearing 17.91 vs 25.90 off, no rate window
│                          # still helps both regimes). The guard-arm reading
│                          # is the one that moved, and that is the corpus
│                          # refresh, not this change: `live == churn` exactly
│                          # at both K (0 ramp fires) and the bundle is net
│                          # better car-active (16.96 vs 17.91 inert) while
│                          # costing plug-only +0.21.
│                          # Scorecard shape (2026-10-19): ~45% of the
│                          # replayable quarters are night quarters the loop
│                          # has nothing to do in — no cycle ever saw a
│                          # surplus (`min(anchor.predicted_wh) > 0`), so the
│                          # -9 Wh target is unreachable without a battery and
│                          # the miss is the house's baseline load. 514 of the
│                          # 515 fall in the 18:00-07:59 local window
│                          # (`qh_start` is UTC). Restricting to the leverable
│                          # 637 raises every candidate's mean |miss| by 6-14
│                          # Wh (plug-only 23.36 -> 31.63) and reverses no
│                          # ordering; car-active was never diluted (117/117
│                          # leverable — the car only ran on surplus). Two-axis
│                          # split over leverable quarters: plug-only error is
│                          # ~89% export-side (-28.10 Wh/q against +3.53) and
│                          # that axis is invariant across every candidate
│                          # (-26.94..-28.21); improving imports means
│                          # absorbing less (car-active guard arm 8.74 -> 4.13
│                          # Wh/q imports at -9.17 -> -12.83 exports), so each
│                          # knob trades one axis for the other and a symmetric
│                          # |miss| scores each trade a wash — why the margins
│                          # and the guard-prune never cleared their bars. The
│                          # exchange rate is a price ratio (imported vs
│                          # exported Wh); no price/TOU/credit term exists in
│                          # the decision path, and none is planned from this.
│                          # Corpus verdict (13 logs, 1146 replayable of 1294 scored;
│                          # 117 car-active carrying 1269 Tesla commands): status-quo
│                          # beats every variant in BOTH regimes — plug-only 23.41
│                          # mean|miss| / 14.27 median / -4.87 mean / 2.07 actions/q /
│                          # 0.90 reversals/q vs prior 27.71, prior-blend@60 26.26,
│                          # trend-gated 24.11; car-active 17.99 / 5.00 / +0.30 vs
│                          # prior 31.99, prior-blend@60 19.31, trend-gated 24.15
│                          # (churn 15.73 -> 9.65-11.97) — the open-loop ranking
│                          # INVERTS closed-loop (the shipped window is ~2x worse
│                          # open-loop). The 2026-10-17 counter-example
│                          # (prior-blend@60 6.88 vs status-quo 10.54 on its four
│                          # car-active quarters) does not survive the corpus.
│                          # No estimator variant ships; Subtask 3 stays blocked.
│                          # Policy sweep verdict (closed loop, both regimes): the
│                          # shipped zero-margin policy is best or tied on mean|miss|
│                          # everywhere, so no margin and no ordering ships —
│                          # plug-only 23.41 at 0 / 23.41 at 10 / 24.80 at 40 / 26.85
│                          # at 60 (turn-on margin worse still: 25.64 at 10, 31.98 at
│                          # 40), car-active 17.99 vs 19.62 at 10 and 39.48 at 40.
│                          # The plug regime's -4.87 mean miss is real bias (a 60 Wh
│                          # margin zeroes it) yet mean|miss| gets WORSE, i.e. the
│                          # over-shedding hedges forecast variance and |miss| punishes
│                          # removing it. lumpy_first fires but nets out (23.51 /
│                          # 17.88). 2026-10-17 disagrees with the corpus again
│                          # (turn-on margin 20: 4.78 vs 10.54 there, 23.40 vs 17.99 on
│                          # the car corpus) — the one-log trap. The granularity cure
│                          # must win without shedding less on average.
│                          # Amp-floor verdict (code + flag removed 2026-10-18;
│                          # car-active only; the
│                          # plug-only regime is bit-identical as it must be with no
│                          # car commanded): it LOSES — car-active 19.34 mean|miss| vs
│                          # 17.99 shipped, churn 19.30 vs 15.73 actions/q and
│                          # reversals 5.67 vs 2.09; 2026-10-17 itself 11.55 vs 10.54.
│                          # The floor's premise (close the residue on a later, finer
│                          # step) is false: the quantum is R-scaled and R shrinks
│                          # ~30 s/cycle, so an amp is 40.0 Wh at R=600 s and 38.0 Wh
│                          # one cycle later, only falling below a 20 Wh gap at
│                          # R < 300 s (two-thirds through the quarter). Declining the
│                          # step just routes the deficit to the plugs (+3.5
│                          # actions/q, +3.6 reversals/q ≈ one extra duty-cycle per
│                          # declined step) and the worst quarter (287 Wh, not
│                          # amp-quantum-driven) is untouched. The DURATION form is
│                          # closed too: the meter quantizes the MINIMUM DISPATCH to
│                          # the reporting window (~30 s, also the settle window), so
│                          # a minimum-length dispatch is one full observation step and
│                          # a scheduled restore would be open-loop exactly where the
│                          # closed loop is blind — strictly worse than today, where the
│                          # loop's re-decide IS the restore (2.09 reversals/quarter,
│                          # median 136 s ≈ 4-5 dispatch windows). The dispatchable unit
│                          # is power * quantization / 3600: 2.2 Wh per 260 W plug,
│                          # 2.0 Wh per amp, 40.5 Wh per heater run — not the R-scaled
│                          # 43.3 Wh production books for that plug at R=600 s. The
│                          # booking is a belief about a duration the actuator is never
│                          # asked to hold; whether that feeds the over-shedding is the
│                          # remaining measurable question (plan §5.3).
│                          # Dispatch-quantum verdict (code + flag removed
│                          # 2026-10-18; that §5.3 question measured): the
│                          # admissibility horizon is the one place the R-scaled
│                          # belief forbids a real action — a starting load must fit
│                          # the surplus with its whole-window
│                          # cost (43.3 Wh for a 260 W plug at R=600 s), production's
│                          # `loads_too_large` no-action reason (12-17% of worst-decile
│                          # cycles). Admitting a load whose FIRST dispatch fits (the
│                          # running debit and the pending-effect estimate stay
│                          # window-priced) is a VARIANCE TRADE, not the resolution gain
│                          # it looked like: plug-only 23.41 -> 19.29 at 60 s but 238
│                          # quarters improve / 160 worsen (median delta 0.00, 61%
│                          # untouched; the mean is five rescues of 100-268 Wh, the 400
│                          # Wh surpluses the whole-window fit declines outright), while
│                          # car-active regresses the other way (17.99 -> 24.66 at 60 s;
│                          # 30 improve / 73 worsen, median +2.66; 2026-10-17 10.54 ->
│                          # 23.84). All-regime only improves because 90% of the corpus
│                          # is plug-only. Best value 60 s (the toggle debounce, not the
│                          # meter's 30 s): 10 s is worst in both regimes (31.41 / 45.72),
│                          # 300 s the coarsest that still helps the car side (17.10).
│                          # So the whole-window fit test is a conservative prior about
│                          # whether a small predicted surplus materializes — the same
│                          # hedge, on the surplus side, the turn-off margin sweep found
│                          # on the shed side. No ship.
│                          # Nameplate watts stand in for measured draw, so deltas
│                          # are the result, not absolute landings.
│                          # Guard-arm verdict (`--guards live`, corpus, status-quo
│                          # rate, churn-only and inert controls): the arm is live, not
│                          # silent — 291 declines (0.28/q) + 809 rescues plug-only,
│                          # 115 (0.98/q) + 645 car-active, vs the scorer census's 1242
│                          # declines / 1294 q (0.96/q) and 767 rescues (0.59/q), the
│                          # replay's churn 0.256 mean (max 31.57) Wh/s against the
│                          # census's 0.13 (12.60), all same-order. live was worse
│                          # than inert in both regimes on that snapshot
│                          # (plug-only 23.63 vs 23.41, +0.21
│                          # mean; car-active 20.42 vs 17.99, +2.42) but the bundle is
│                          # what the attribution convicts: churn-only (the jitter guard
│                          # and its override alone) is +0.21 plug-only and -0.88
│                          # car-active — a wash, so the pre-registered deletion rule is
│                          # not met and NOTHING is deleted; live minus churn-only is the
│                          # ramp rule alone at +3.31 car-active, 4 changed quarters of
│                          # 117 (8.6/corpus-month, above the ~1/month threshold), so the
│                          # ramp rule's deletion condition does NOT fire either —
│                          # contrary to the plan's expectation, and with the pre-emption
│                          # sometimes hundreds of seconds (two quarters lost 183 and 222
│                          # Wh to an early stop the deferral would not have taken).
│                          # Re-measured under the shipped policy the car-active leg
│                          # flips to net BETTER (live 16.96 vs inert 17.91) with
│                          # live == churn exactly and 0 ramp fires — the earlier
│                          # +2.42 was a corpus-refresh artifact, so the bundle's
│                          # deletion rule still does not fire, from the other side.
│                          # 2026-10-17 separately: 12.06 live vs 12.23 inert overall,
│                          # 13.22 vs 14.37 on its car-active quarters, ramp 0 changed.
│                          # Rate-window verdict (`--rate-window-quanta`,
│                          # closed loop, both regimes; snapshot): the shipped
│                          # 1-quantum window is best or tied. A 2-quantum
│                          # average HELPS plug-only (23.36 -> 22.99 mean|miss|)
│                          # but regresses car-active hard (17.99 -> 23.53), far
│                          # past the +/-0.2 Wh noise bar, and worsens more
│                          # plug-only quarters than it fixes (142 worse / 108
│                          # better of 1035, median delta 0.00 — the mean is a
│                          # handful of 30-115 Wh rescues). Longer windows are
│                          # monotone worse, and 2026-10-17 shows no window win
│                          # either (2q 16.23 vs 14.37 on its 15 car-active
│                          # quarters). The rate axis is not where the residual
│                          # miss lives: the loop pays for the level's freshness,
│                          # not its smoothness. No ship; status-quo stays.
│                          # Composed only when `--rate-window-quanta K|all` is
│                          # given (K=1 is the shipped status-quo, always
│                          # reported), so default output is byte-identical.
│                          # Defer-lookahead verdict (`--defer-lookahead-quanta
│                          # K`, closed loop, corpus, status-quo rate; snapshot):
│                          # **SHIPPED at K=1 on 2026-10-19** as a SOUNDNESS fix,
│                          # priced as a wash. The knob withholds K observation
│                          # windows from the Tesla stop's safe defer window,
│                          # sized from each quarter's OWN window
│                          # (`Quarter.observability_secs`, the median data-point
│                          # spacing — never a literal 30 s), so a defer never
│                          # leaves less room than the next observation needs: a
│                          # deferral the engine cannot observe through is not a
│                          # deferral, so it takes the stop now. Corpus: car-active
│                          # (117 quarters) 17.99 -> 17.91 mean |miss|, 3 quarters
│                          # better / 4 worse, signed miss +0.30 -> -0.42;
│                          # plug-only BIT-IDENTICAL (23.36, the knob is
│                          # Tesla-only). At K=2 the mean is better still (17.68,
│                          # 7 better / 5 worse, median delta 0.00) but the sweep is
│                          # non-monotone (18.21 at K=3) and 2 is not the stated
│                          # rule, so 1 ships. The direction is legible in the
│                          # changed quarters: all improvements came from
│                          # under-shed quarters, all regressions from near-target
│                          # or already-over-shed ones — the rule converts
│                          # under-shedding into over-shedding, which |miss| also
│                          # punishes, so it is not a mean win.
│                          # Stopping earlier strictly LOWERS consumption (checked,
│                          # not assumed: the replay's regressing quarter lands
│                          # -5.81 -> -27.13 Wh); the |miss| rise is overshoot, and
│                          # overshoot against the *claimed* gap is bounded at one
│                          # quantum (`R <= gap/P + Q` <=> `P*R <= gap + P*Q`) while
│                          # overshoot against the *realized* quarter is the
│                          # forecast's own churn, which no stop rule bounds.
│                          # The incident is characterized, not replayed: the earlier
│                          # "the log regresses under its own fix" (5.91 -> 10.89)
│                          # was a replay-model artifact — the replay derives its
│                          # prediction from its own counterfactual meter, which for
│                          # that quarter diverges from the log's raw energy all
│                          # quarter (up to 8 Wh), has NO anchor at the decisive
│                          # R=60 s cycle (19 anchors, median spacing 45 s), and
│                          # lands 7.62 Wh from the logged actual. On the log's own
│                          # trace the rule helps: the deferral series reports
│                          # R=119 / 35.8 Wh / safe_defer=107 s (slack 12 s, under
│                          # one 30 s window), so the stop is taken there and sheds
│                          # 39.7 Wh instead of the 20.0 Wh the R=60 s stop removed,
│                          # landing the quarter at -8.86 Wh against the -9 Wh target
│                          # instead of +10.81. `0` restores the pre-2026-10-19
│                          # static rule (used for every older verdict in this tree).
│                          # `DecideContext.observability_secs`
│                          # (production: the resolved prediction/settle window)
│                          # is the window plumbing this added, and it is the
│                          # sanctioned way for any decision rule to size itself
│                          # from the quantum instead of a hardcoded 30 s.
│                          # Run from the repo root:
│                          #   uv run python tools/estimator_replay.py bugs/*.log
│                          #   uv run python tools/estimator_replay.py corpus/*.log --variant prior
│                          #   uv run python tools/estimator_replay.py corpus/*.log --regime tesla
│                          #   uv run python tools/estimator_replay.py corpus/*.log --turn-off-margin 40
│                          #   uv run python tools/estimator_replay.py corpus/*.log --guards live
│                          #   uv run python tools/estimator_replay.py corpus/*.log --rate-window-quanta 2,3,4,all
│                          #   uv run python tools/estimator_replay.py corpus/*.log --defer-lookahead-quanta 0
│   ├── settle_window_counterfactual.py  # Prices the *adaptive* prediction/
│                          # settle window against a pinned one (Subtask 6 of
│                          # the guard-observability plan). The window is the
│                          # EffectStore freshness window — the fresh-data gate
│                          # (`EffectStore.has_since`), the one consumer priced
│                          # here — and separately the forecast's trailing-rate
│                          # length, which this tool does NOT price (it holds the
│                          # logged forecast fixed; see the estimator-
│                          # responsiveness plan). So the tool rebuilds the
│                          # effect store from the logged
│                          # `actions=[PendingEffect(...)]` reprs and replays
│                          # production's own EffectStore once per cycle under
│                          # two arms: the window the log committed, and
│                          # `--pinned-secs` (default
│                          # DEFAULT_PREDICTION_WINDOW_SECS, 30 s). Reports per
│                          # log the histogram of non-default windows and the
│                          # cycles where the arms disagree. Snapshot (corpus/,
│                          # 2026-10-18, 14 logs, 58,390 cycles): 111 non-default
│                          # (15 s/21 s/24 s), 0 gate flips -> no decision
│                          # changed, no quarter affected, gate fires 2672 under
│                          # both arms. The corpus is refreshed over time, so
│                          # these figures are a snapshot, not a fixed baseline.
│                          # The post-amp-change suppression
│                          # `_suppressed_by_settle` — a would-be settle-window
│                          # consumer that never decided a cycle — was retired
│                          # 2026-10-18 as unreachable: whenever an active settle
│                          # exists the gate is already true (both compare the
│                          # effect's wall and data-point ages against the same
│                          # window, and the settle's `now` is at or after
│                          # `data_point_at`), so production exits on the gate
│                          # first; it fired in no recorded cycle. Its accessors
│                          # (`StateTracker.get_active_tesla_settle`,
│                          # `TeslaSettleTracker.get_active`), the
│                          # `GapMinder.TESLA_SETTLE_SUPPRESS_WH` threshold and
│                          # the vestigial `PendingEffect.suppress_action` went
│                          # with it. The adaptation itself stays: it guards a
│                          # failure mode (a sustained meter change to a slower
│                          # quantum) the corpus never exercises, and the tool's
│                          # 60 s committed-window fixture shows the flip that
│                          # would restore. Spec: tests/test_settle_window_counterfactual.py.
│                          # Run from the repo root:
│                          #   uv run python tools/settle_window_counterfactual.py corpus/*.log
│                          #   uv run python tools/settle_window_counterfactual.py corpus/*.log --pinned-secs 30
│   └── divergence_audit.py  # Status-quo vs residual shed-watts audit over logs:
│                          # per-anchor ON states (deduped candidates) + logged
│                          # priorities, lumpy-first iff implicated and beyond
│                          # smalls capacity; corpus/ verdict 9/1015 quarters,
│                          # always sheds less, max one 260 W plug.
├── app.py                 # Flask app factory (create_app()), route definitions (/, /health,
                           # /api/v1/tou, /api/v1/load/status, /api/tesla/callback),
                           # _AppState runtime singletons, start_background_services();
                           # TOU date-only end dates include the full local day (DST-aware),
                           # while explicit timestamps retain their exact meaning;
                           # picker/details defaults use the selected day, not the fetch boundary
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
                           # left-aligned at real time positions, blank right);
                           # slot aggregation lives in _indexed_bars() (extracted
                           # from per_second_sparkline to stay under pylint's local
                           # budget; behavior unchanged, covered by the chart tests)
├── config.py              # TeslaConfig/PlugConfig/VocolincConfig dataclasses,
                           # load_tesla_config(), load_plug_configs(), Config.log_file,
                           # Config.defer_lookahead_quanta (read leniently — a
                           # malformed LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA falls
                           # back to the shipped default, never crashes startup), etc.
├── config_loader.py       # Config loading helpers (load_tesla_config,
                           # load_plugs_from_file, load_vocolinc_*) reading
                           # env via the Config class + devices.json
├── conftest.py            # Pytest shared fixtures & configuration
├── constants.py           # Named constants for magic numbers (STALE_DATA_THRESHOLD_SECS,
                           # DATA_STALE_ALERT_THRESHOLD_SECS=300 for Telegram data-health
                           # alerts, Tesla charging constants TESLA_HARD_MAX_AMPS, etc.,
                           # DEFAULT_HYSTERESIS_WH=20 residential fallback,
                           # JITTER_GUARD_FRACTION=2.0 turn-on jitter guard,
                           # JITTER_FLOOR_WH_PER_S=0.2 churn noise floor,
                           # JITTER_HORIZON_SECS=150 swing projection cap,
                           # JITTER_MAX_REMAINING_SECS=600 churn readiness gate,
                           # DEFER_LOOKAHEAD_QUANTA_DEFAULT=1 the shipped
                           # Tesla defer-lookahead rule)
├── device_config.py       # devices.json loader and typed accessors (get_telegram_config,
                           # get_tesla_config, get_homekit_plugs, etc.)
├── energy_aggregator.py   # TOU (time-of-use) energy aggregation logic
├── energy_cache.py        # EnergyCache with per-second sample storage, incremental
                           # fetch merging, pruning, and last_fetch_error surfacing
                           # (stored fetch exception for LoadManager data-health alerts)
 ├── load_controllers.py   # Load manager controllers: PlugController/RealPlugController,
                            # TeslaController/RealTeslaController, VocolincController/RealVocolincController,
                            # and factory functions (load_controller_from_env,
                            # fleet_telemetry_config_create)
 ├── load_manager.py       # OAuth handling, pipeline stages (_stage_*), load-shedding management,
                             # _last_tesla_at_home preserves at_home across telemetry snapshots,
                             # _tesla_state_missing_reason() explains tesla_state=None
                             # (post-async sites in _stage_commit/_stage_build_result pass
                             # tesla_fetched=True; _early_exit defaults to "not_reached"),
                             # data-health Telegram alerts (_check_data_health_alerts:
                             # fatal fetch errors + 300 s stale/no-data, once per QH each,
                             # bypassing the devices whitelist)
 ├── load_models.py        # Shared data models (CycleContext (stage-4 gap_jitter_wh_per_s),
                            # CycleResult, CycleDiagnostics (gap_jitter_wh_per_s,
                            # tesla_state_missing_reason + to_dict),
                            # AsyncPhaseResult, PendingEffect,
                            # TeslaChargeState, TeslaDriveState, TeslaLocation, TeslaCallbackPayload,
                            # TeslaEvent, TeslaEventKind, TeslaVehicleTelemetry,
                            # parse_tesla_event_payload, update_tesla_telemetry,
                            # get_active_tesla_telemetry, FleetTelemetryProvisionConfig) plus
                            # shared fleet-telemetry parsing helpers (unwrap_telemetry_value,
                            # parse_charge_amps) used by mqtt_telemetry and load_controllers
 ├── load_nbc.py            # NBCReader, EffectStore, TeslaSettleTracker, StateTracker, GapMinder bin-packing + TeslaDecider (incl. turn_on_jitter_guard_fires — JITTER_FLOOR_WH_PER_S noise floor and jitter_swing_wh()'s JITTER_HORIZON_SECS-capped swing, the one implementation shared by the predicate and every jitter log line; banked_cover_cap_wh banked-cover override letting covered plug turn-ons proceed despite jitter, Tesla never; the Tesla-stop trio defer_stop / max_defer_secs / ramp_aware — the safe-defer branch is skipped when defer_stop is False, max_defer_secs tunes the buffer cap, and the ramp hastener `_ramp_stop_now` is consulted only when ramp_aware, so the shipped default leaves the defer rule intact with no trend hastening; defer_lookahead_quanta (shipped 1) adds that many DecideContext.observability_secs windows to the defer deadline so a defer never leaves less room than the next observation needs — the window is the resolved prediction/settle window (the quantisation quantum), never a literal 30 s; defer/ramp decisions log at INFO with safe_defer/seconds_remaining/gap/trend/defer_lookahead), PendingEffect factories
├── gap_trend.py           # GapTrendTracker: EWMA slope of the adjusted gap across cycles
│                          # (keyed on data_point_at; quarter-hour identity derived via
│                          # floor_to_qh because ParsedMetricsQH.qh_name is the constant
│                          # "QH1"; history clears on QH rollover or a data gap over
│                          # GAP_TREND_MAX_SPAN_SECS; plateau-neutral; churn_wh_per_s
│                          # reports the |Δslope| jitter EWMA (0.0 until measurable)
│                          # behind the turn-on jitter guard, accumulated over a
│                          # SEPARATE churn_ready window — update(churn_ready=False)
│                          # still records the sample for the slope/trust rule but
│                          # withholds it from churn, which is how a quarter-opening
│                          # prediction_w sign flip no longer seeds a five-minute
│                          # false "excessive jitter"). Feeds the
│                          # ramp-aware Tesla stop in TeslaDecider.decide_reduce
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
├── telegram.py            # TelegramSender, NotificationEvent (turn_off plug lines
                           # carry "(MM:SS today)" via format_runtime_today +
                           # runtime_today_secs, fed by LoadManager._runtime_for_actions),
                           # action_device_and_type shared unwrapping helper,
                           # config loading helpers
├── telegram_client.py     # Async Telegram Bot API client using aiohttp
├── util.py                # Shared utilities (JSON helpers, timezone handling)
├── pyproject.toml         # Project metadata, dependencies & script entrypoints
├── render.yaml            # Render.com deployment configuration
├── env.example            # Template for required environment variables
├── tests/                 # All pytest tests; test_forecast_log_scoring.py covers the
                           # offline tool (parser quarter identity, actual resolution,
                           # the error==-R*shortfall identity, sigma_rate/coverage,
                           # the churn band — GapTrendTracker fed from anchors,
                           # offset/sign invariance, reset across quarters,
                           # churn_coverage — guard/cycle-result parsing incl. the
                           # segment-scoping traps (action data_point_at before,
                           # candidate reason after diagnostics), target/miss/guard
                           # joins, the estimator variants (prior rate from the same
                           # fetch's completed neighbours incl. the missing-total and
                           # no-prior fallbacks, blend weights, trend gate trust/
                           # fallback/quarter isolation, shared anchor set, bucket
                           # sigma matrix, CLI table + default-output parity and
                           # unknown-flag rejection), and the turn-off path collection
                           # (decision entry ↔ same-second shed attachment incl. the
                           # orphan-shed skip, per-action overshoot/residual roll-ups,
                           # Tesla stop reasons without savings, the reversal join's
                           # time window bounding quarter scope, per-quarter miss
                           # join, empty-summary zeros, CLI section + default-output
                           # parity, multi-word device names), the suppression
                           # census (guard-decline parsing scoped to the
                           # diagnostics segment, candidate dedup and the
                           # off+toggleable filter, banked overrides, defer
                           # horizons, ramp fires and the band arithmetic pinned
                           # against load_nbc/load_manager's own constants, the
                           # settle trace's commits, the guarded/unguarded cohort
                           # split, the availability caveat, empty-census text,
                           # and CLI section + default-output parity), and that the
                           # tool runs standalone; real-log
                           # assertions skip because bugs/ is gitignored);
                           # test_estimator_replay.py is the spec for
                           # tools/estimator_replay.py, Subtask 2 of the variance-reduction plan:
                           # a closed loop over the two plug-only quarters (anchors + plug events
                           # as CHECKED-IN fixtures — the verdict must not depend on gitignored
                           # bugs/ — with a skipif-guarded drift test re-parsing the logs through
                           # the tool's own ingest_log), a step-function meter (opening plug state
                           # per quarter via Quarter.initial_on, defaulting to the all-on the two
                           # fixtures happen to open with), history mode landing on the logged
                           # actual, the status-quo rate reproducing production's own opening shed,
                           # the replay's rates equal to the scorer's estimators, the reversal
                           # window matching the scorer's, and the measured verdict — the flat
                           # prior cuts churn in both quarters but over-sheds the regime-change
                           # quarter, so its mean |miss| is worse (19.87 vs 15.19) and it must not
                           # ship. Its Tesla leg is pinned the same way: one amp over R is
                           # 240*(R−lag)/3600 Wh (the synthetic car quarter), the 2026-10-17 ceil
                           # overshoot reproduced (2 A past a 76.5 Wh gap), an unplugged car never
                           # commanded, history mode pinning the car too, the plug-only fixtures
                           # never commanding one, and --regime splitting car-active from
                           # plug-only. The policy axis (Policy: margins + lumpy_first,
                           # built from GapMinder's constructor) is spec'd on synthetic
                           # quarters where a coarse quantum makes the effect legible: the
                           # default policy reproduces the shipped engine (flag-off parity
                           # against the pinned verdicts), a turn-off margin as wide as the
                           # 260 W plug's own 43.3 Wh declines the shed, the turn-on margin
                           # leaves the surplus unspent, replay_all forwards the knobs, a
                           # supplied banked_wh is what arms the lumpy-first gate (without
                           # it the flag is a silent no-op), the shipped whole-window fit
                           # declines a plug it cannot hold, and the CLI header names the
                           # policy it ran; and the guard arm (GUARD_MODES inert/churn/live
                           # through --guards) is spec'd on a synthetic CHURNING quarter —
                           # its gap alternating 40/5 Wh makes churn ~2.33 Wh/s, so the
                           # guard declines on both cycles that reach the turn-on side and
                           # the arm suppresses two commands the inert control issues —
                           # with a banked-cover twin (banked_wh -49 → one rescue, steps
                           # and landing identical to inert) and a spy on
                           # GapMinder.decide pinning what the tracker handed each arm's
                           # ctx (inert both fields None, churn the jitter signal and
                           # still no trend, live both), plus the arm vocabulary, the
                           # aggregate forwarding, the three-way report text and the CLI
                           # parity (default output == --guards inert, block only when
                           # non-inert). Ingestion is covered on SYNTHETIC log text (no bugs/ needed):
                           # the built quarter's anchors/actual/lag/events/opening state, the amp
                           # history in time order, the dedup
                           # of a repeated data point, the fetch-line-derived lag, and each drop
                           # reason (never-known Tesla state, too few anchors,
                           # implausible lag, no candidate dump), plus the reporting layer
                           # (summaries, comparisons, regression ranking, regime selection),
                           # the freshness of the
                           # variant map, and the CLI (table, --date, --regime, --turn-off-margin/
                           # --lumpy-first header, unknown variant, no
                           # quarters), plus the defer quantum lookahead on synthetic
                           # two-anchor quarters (the incident cycle stops with one
                           # window and defers with zero, scaling with each quarter's
                           # own spacing, inert without a window, the quarter carries
                           # the spacing the ingest measured, and the label names the
                           # knob only when it is on); test_tou_page.py covers inclusive date
                           # ranges, DST days, detail rows/defaults and picker dates;
                           # test_app.py covers endpoint validation and range limits;
                           # test_gap_trend.py covers the gap-slope tracker (+ the churn /
                           # jitter EWMA, incl. the churn_ready readiness gate that
                           # withholds the quarter-opening extrapolation),
                           # test_tesla_decider.py the ramp-aware stop rule, the
                           # reduction amp step (a ceil that covers the gap, the shipped
                           # rounding pinned), and the defer quantum lookahead
                           # (TestDeferQuantumLookahead: the 2026-10-18 incident cycle
                           # stops at one window, defers at zero, is inert without a
                           # window, scales in quanta, and the log line reports it),
                           # test_tesla_ramp_replay.py the 2026-09-26 incident sequence
                           # (ramp_aware=True) plus a skipif-guarded drift check that
                           # the inline INCIDENT_POINTS gaps still match the log,
                           # test_ramp_aware.py the LOAD_MANAGER_RAMP_AWARE default-off
                           # stance (default defers on the c54 cycle, flag-on stops)
                           # and its LoadManagerConfig/env plumbing,
                           # test_defer_lookahead_quanta.py the shipped one-window
                           # defer lookahead (the 2026-10-19 rule): the default,
                           # the incident cycle on both settings, "unknown window
                           # stays inert", and the LOAD_MANAGER_DEFER_LOOKAHEAD_
                           # QUANTA config/env plumbing incl. the malformed-value
                           # fallback,
                           # test_tesla_overshoot_replay.py the 2026-10-01 overshoot
                           # (status quo characterized; jitter-guard gate: |miss| < 10.7
                           # toward target −9),
                           # test_marine_layer_jitter.py the 2026-10-02 calm-morning log
                           # (all 35 logged jitter-guard triples replayed through the real
                           # tracker: cluster A's quantization churn sits under
                           # JITTER_FLOOR_WH_PER_S, cluster B's quarter-opening flip is
                           # gated by JITTER_MAX_REMAINING_SECS, incident still fires);
                           # test_undershoot_jitter_retune.py the 2026-10-09 undershoot
                           # (fraction 2.0 / horizon 150: 4 of 5 sampled guard triples
                           # stay quiet, highest-churn one still fires, c579 still fires);
                           # test_banked_cover.py the banked-cover override (covered plug
                           # acts despite jitter, uncovered stays blocked, Tesla never
                           # takes the cover path, banked=None behaves as today);
                           # test_qh_boundary_forecast.py the 2026-10-14 QH-boundary
                           # glitch (complete QH1 keeps the waiting-for-data period
                           # label beside the placeholder instead of a blank);
                           # test_prune_orphaned_tesla_command.py the 2026-10-08 orphaned
                           # 12A command (pruning the last set_amps clears last_commanded_amps);
                           # test_commit_tesla_direction.py the 2026-10-08 7->5 cut
                           # (commit classifies direction from live amps when the command expired);
                           # test_decision_lag_adjustment.py the 2026-10-14 water-heater
                           # execution lag (fetch-start remainder shrunk to decision
                           # time before decide books plug/Tesla savings);
                           # test_sheddable_residual.py the controllable-load residual
                           # prototype (net-of-lumpy counterfactual + replayed c658 /
                           # c817 small-plug prescriptions, production unwired);
                           # test_residual_feedback_replay.py the meter-feedback
                           # harness (event-driven counterfactual meter + closed-loop
                           # residual policy over both 2026-10-14 quarters; symmetric
                           # margin pinned harmful in B);
                           # test_divergence_audit.py the status-quo vs residual
                           # audit (logged priorities, watts-compared; 9/1015
                           # corpus quarters diverge, always sheds less);
                           # test_settle_window_counterfactual.py the Subtask-6
                           # counterfactual (cycle/effect repr parsing + UTC
                           # conversion, the short-window wait the pinned window
                           # takes, the adaptation's protection case — a 60 s
                           # committed window defers where the pinned 30 s decides
                           # — agreement when the effect is far, tolerance of the
                           # retired suppress_action token, the window histogram /
                           # non-default count, report totals + gate counts,
                           # default constant, standalone CLI);
                           # test_lumpy_first.py the Subtask-4 wiring (flag-off
                           # parity with history, flag-on c658/c817
                           # prescriptions, config/env plumbing, and the 4a
                           # gapminder_lumpy_first census log when the lead is taken);
                           # test_dispatch_quantum.py retired 2026-10-18 with the
                           # dispatch_secs knob (its verdict is above; the shipped
                           # whole-window fit is covered by test_gap_minder)
├── templates/             # Jinja2 HTML templates (index, TOU, error pages);
                           # tou.html shares the index design system (app-bar,
                           # card, kv, data-table) with a single-change
                           # date-range picker calendar (collapsed by default:
                           # the range shows as text on a toggle button and
                           # the grid opens only when the date text is
                           # selected; grid clicks/Today stage a pending
                           # selection, Go hides the widget and submits once,
                           # Cancel/Esc/toggle-collapse revert to the
                           # committed range while outside-click only hides
                           # (pending kept for Go/Refresh); outside-click
                           # guard runs in the capture phase since render()
                           # detaches the clicked day mid-dispatch;
                           # hidden start_date/end_date fields,
                           # <noscript> date-input fallback)
                           # (default: today from midnight) that syncs the
                           # details checkbox then auto-submits on commit, a
                           # details checkbox (auto on for single days, off for
                           # multi-day) that auto-submits on change via
                           # requestSubmit(); headers lay out title, status
                           # control (connection dot on index, Refresh ⟳
                           # submit button with form=tou-form on TOU),
                           # app-bar__spacer, then the cross link
                           # (relative hrefs for subpath-proxy compat);
                           # _metrics.html/_load_management.html are SSE-swappable fragments
                           # (the metrics fragment carries the hidden #data-freshness state
                           # strip mirrored onto the header #connection button by syncConnection()
                           # (dot-only when live+fresh; tap toggles data-show-text to reveal/hide
                           # the age text) plus the forecast-period
                           # label, which renders abnormal states
                           # ("⚠ waiting for data" / "⚠ stale data" /
                           # "⚠ low confidence" when
                           # diag.reason == "excessive_jitter") in red
                           # (forecast__period--warn); the
                           # sparkline filter receives quantization_seconds to feed
                           # chart.per_second_sparkline's bucket_secs downsampling)
├── static/                # Mobile-first design system (style.css) and the SSE dashboard
                           # client (app.js) wiring EventSource → fragment swaps + freshness ticker
                           # + silence watchdog (re-arms reload when a live stream goes quiet)
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
  `_stage_pending_check` refreshes the Tesla display plus a pre-gate plug sync
  (`_sync_plugs_for_dashboard` reusing `_sync_plug_states`, so early exits still
  show fresh dashboard pills; carried on `CycleContext` for the async phase to
  reuse — one controller round-trip per plug per cycle); shared queries live in `_local_time()` (device-tz conversion), `_eligible_plugs()`
  (engine-eligible vs out-of-range split), `is_sentinel_on()`, `_is_tesla_in_range()`,
  and `_waiting_sleep_hint()` (prediction-window-capped re-check delay). The async
  phase (`_cycle_async_phase` → `_cycle_async_phase_body`) returns an
  `AsyncPhaseResult` dataclass (named fields, not a tuple) and is split into
  `_async_sync_and_check_sentinel()`, `_record_tesla_auth_error()`,
  `_correct_gap_with_inflight()`,
  `_eligible_for_decision()`, `_decide_actions()`, `_run_actions()`; Tesla
  execute paths share `_mark_vehicle_offline()` + `_handle_tesla_auth_error()`;
  `run_cycle()` stage timings go through `_timed()`
- `_stage_nbc_fetch` in `load_manager.py` — always fetches fresh NBC data
  (`get_current_qh(force=True, ...)` regardless of `ctx.force`). The NBC reader shares the
  app-level `EnergyCache` (wired in `app._get_load_manager()`), whose TTL outlives the
  30 s cycle; a TTL-paced read would cache-hit and skip the fetch, letting data age toward
  the stale-data threshold. The reader's fast path (`get_current_qh` with `force=False`)
  remains for other callers (e.g. `metrics.py`)
- `_stage_compute_gap` in `load_manager.py` feeds `gap_trend.update(...)` with
  `churn_ready=seconds_remaining <= JITTER_MAX_REMAINING_SECS`, so churn accumulates
  only once at least a third of the quarter has observed data. Every sample still
  reaches the trend window (slope, trust rule and `gap_trend_wh_per_s` unchanged);
  only the churn accumulator skips the extrapolation-dominated opening. Its DEBUG
  `gap_jitter ... swing_wh=` line goes through `load_nbc.jitter_swing_wh()`, the same
  capped swing `turn_on_jitter_guard_fires` judges, so the log and the reason agree
- `_resolve_prediction_window()` / `StateTracker.apply_prediction_window()` — the
  prediction/settle window is adaptive: it resolves lazily from shared-cache
  quantization (quantization data only exists after the first fetch of a cycle) and
  tracks sustained meter-behavior changes. A new window is committed only after two
  consecutive `_stage_compute_gap` calls, and detections within
  `SETTLE_WINDOW_DEADBAND_SECS` (5 s) of the committed window are treated as detector
  jitter (e.g. 29/30/31 s around a true 30 s period). Periods below
  `MIN_QUANTIZATION_WINDOW_SECS` (15 s) — e.g. the flat-data N=2 artifact — are
  rejected by `_resolve_prediction_window` and fall back to the default. The window
  tracks the *quantum* because the meter reports a per-quantum power held constant
  across the quantum (a quantum is too short to measure a real Wh delta), so a window
  equal to the quantum makes the forecast's trailing rate the latest end-of-quantum
  reading — a zero-order hold — and a sub-quantum window is degenerate (identical
  inside one quantum, an aliasing mix across a boundary). Rationale and the
  gate-vs-rate consequence: docs/LOADMANAGER.md
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
  telemetry with a fast path; `tesla_state_from_snapshot()` requires charging-state
  corroboration (`DetailedChargeState`/`ChargeState`, see `telemetry_indicates_charging()`
  in `load_models.py`) because `ChargeAmps` alone is the pilot setting, not measured
  draw (bugs/2026-09-09-tesla-ghost.log). Preserves `at_home` from `_last_tesla_at_home`
  when `Location` is absent in the snapshot. Two fallbacks before the idle verdict:
  `_commanded_charge_echo()` trusts positive amps under an active command (the car
  answering us; bugs/2026-09-11-tesla-ghost-c.log), and `_arbitrate_tesla_state_from_rest()`
  polls REST `charge_state` for ambiguous (positive, uncorroborated, uncommanded) amps —
  load manager never starts charging itself, so every session begins this way. Polls are
  spaced `TESLA_ARBITRATION_COOLDOWN_SECS` (300 s) apart (definitive answers only — transport failures such as 408 offline leave the timestamp untouched so the next cycle retries instead of latching idle; bugs/2026-10-08-tesla-ghost.log); the verdict is sustained while
  amps stay positive and cleared on amps-zero, corroborated idle, or our own stop command
  (prevents a trim-restart loop). When live telemetry parses to `None` with no echo and
  no arbitration answer, the vehicle is treated as idle/disconnected — a not-charging
  `TeslaState` is returned (`current_amps=0`, `plugged_in=False`, `at_home` from live
  `Location` or `_last_tesla_at_home`) and the controller's stale cached `_init_state`
  is NOT used as an answer (ghost-guard; bugs/2026-08-31-ghost-tesla-amps.log). Delegates
  to controller's `init_tesla_state()` (which waits up to 60 s for telemetry, then REST)
  when telemetry is not yet available or when `at_home` is unseeded and `Location` is
  absent (to seed location). Location-fetch failures preserve the last resolved at_home instead of latching False (bugs/2026-10-08-tesla-ghost.log). `_init_from_rest()` reports `current_amps=0` (not the stale
  pilot) whenever REST `charging_state != "Charging"` (bugs/2026-09-09-tesla-ghost-b.log);
  same zeroing applies to the MQTT `DetailedChargeState`-present path
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
- Data-health Telegram alerts (fatal fetch + 300 s meter-stale/no-data +
  prediction-stall with a fresh meter, per-QH throttle each) in
  `tests/test_data_health_alerts.py`
- File logging with rotation tests in `tests/test_file_logging.py` (`_setup_file_logging`)
- Compaction tests in `tests/test_compaction.py` (`CompletedNBCPeriod`, `compact()`, `inject_completed_qh()`, replace-not-merge behavior)

### Actions Generation Flow
- GapMinder.decide() generates actions as a list of PendingEffect objects
- All PendingEffect construction goes through the module factories in load_nbc.py:
  `make_plug_effect()` (applies the signed-power invariant), `make_tesla_set_amps()`,
  `make_tesla_stop()` — no other PendingEffect() call sites remain in load_nbc.py
- Tesla amp logic lives in TeslaDecider (owned by GapMinder as `tesla_decider`):
  `decide_increase()` / `decide_reduce()` / `supports()` / `safe_defer_secs()`.
  The reduction's amp step comes from `TeslaDecider.reduce_amps_for()`, which
  ceils (covers the gap, may overshoot by up to one quantum:
  `volts * R / 3600` Wh). The floor alternative was measured negative and
  removed 2026-10-18 (the residue cannot be closed later because the quantum
  only shrinks with R; verdict above). The increase direction truncates via
  `int`.
  GapMinder keeps thin `_decide_tesla_*` delegates so existing call sites and tests
  keep working. Plug candidate collection is unified in
  `GapMinder._eligible_plugs(ctx, want_on)` (priority order baked in).
- The turn-on fit test prices a *starting* load over the whole remaining window
  (`GapMinder._decide_turn_on`): a load may only start if its held cost fits the
  surplus, and the running debit and `estimated_current_wh` are window-priced. The
  dispatch-quantum alternative (one meter dispatch — the minimum dispatch time,
  bounded by the remaining window) was measured a variance trade and removed
  2026-10-18 (verdict above); the whole-window fit stands as a conservative prior
  about whether a small predicted surplus materializes.
- The constructor's opt-in policy knobs (`lumpy_first`, `defer_stop`,
  `max_defer_secs`, `ramp_aware`, `defer_lookahead_quanta`) are keyword-only on
  `GapMinder.__init__`
  (pylint's positional ceiling); the margin and hysteresis knobs stay positional.
  Pinned by tests/test_gap_minder.py. `defer_stop`/`max_defer_secs` are diagnostic-only
  (measured by `tools/estimator_replay.py --defer` / `--max-defer-secs`); `ramp_aware`
  is the production flag `LOAD_MANAGER_RAMP_AWARE` (default off), wired through
  `LoadManagerConfig.ramp_aware`. The defer rule is load-bearing (car-active closed-loop
  mean |miss| 17.91 on vs 25.90 off); the buffer value is not (30–180 s spans 17.96–17.89);
  the trend-fold into `safe_defer_secs` is not equivalent to the `t*` hastener (the cap
  swallows it), so no second trend-aware form ships. Cover: tests/test_ramp_aware.py,
  tests/test_tesla_decider.py, tests/test_tesla_ramp_replay.py.
- The defer deadline is sized in observation windows, not seconds:
  `defer_lookahead_quanta` (keyword-only, **default 1 = shipped 2026-10-19**) adds
  that many
  `DecideContext.observability_secs` to `safe_defer_secs`, so a defer never
  leaves less room than the next observation needs. `observability_secs` is the
  quantisation window — production passes the resolved prediction/settle window
  (`state.effective_settle_secs`) and the replay passes each quarter's median
  data-point spacing — and `None` (unknown) leaves the lookahead inert rather
  than guessing. Shipped as a soundness fix priced as a wash (car-active
  17.99 → 17.91, 3 better / 4 worse, plug-only bit-identical; K=2's 17.68 is
  better but non-monotone — 18.21 at K=3 — and is not the stated rule), wired
  through `LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA` and
  `LoadManagerConfig.defer_lookahead_quanta`; verdict in the tool tree above.
  `0` restores the static rule and reproduces the older verdicts.
  Cover: tests/test_defer_lookahead_quanta.py (the shipped rule, the env
  plumbing, the malformed-value fallback), tests/test_tesla_decider.py
  (`TestDeferQuantumLookahead`), tests/test_estimator_replay.py,
  tests/test_pipeline_stages.py.
- The other R-floor is `constants.MIN_SECONDS_TO_ACT` (21 s — itself a
  hardcoded ≈0.7 × 30, the last quantum-shaped literal in the decision path).
  It gates only the *energizing* directions (plug turn-on
  `GapMinder._decide_turn_on`, Tesla amp increase
  `TeslaDecider.decide_increase`) — never turn-off or the last-resort Tesla
  stop, which are deliberately unguarded — so the defer lookahead does not
  need to cover it: the two floors are independent consumers of "is there
  enough room left". Whether `MIN_SECONDS_TO_ACT` should also be derived from
  the quantum (it is too small for a 60 s meter and over-strict for a 15 s
  one) is an unmeasured question, not a shipped change.
- In run_cycle(), raw NBC predictions are adjusted with pending effect deltas via
  `estimated_current_wh()` before being passed to decide() — this accounts for
  actions already taken this quarter-hour without waiting for fresh API data
- Actions are determined by comparing adjusted predicted_wh against target_wh (default -50 Wh)
- Three action types: "turn_on", "turn_off", "set_amps"
- Algorithm uses bin-packing to fit eligible loads into the surplus gap
- Both directions aim at the target: `decide()` passes `gap - TURN_ON_MARGIN_WH`
  (default 0) to `_decide_turn_on` via `_turn_on_budget()` and
  `abs_gap - TURN_OFF_MARGIN_WH` (default 0) to `_decide_turn_off` via
  `_turn_off_budget()`; pass `turn_on_margin_wh=hysteresis_wh` or
  `turn_off_margin_wh=hysteresis_wh` to restore the old deadband-edge
  behavior. Covered by tests/test_turn_on_aim.py and tests/test_turn_off_aim.py;
  the edge-mode plug case lives in tests/test_gap_minder.py as
  `test_edge_gap_reduces_surplus_turn_on` and the edge-mode Tesla case as
  `test_edge_gap_reduces_tesla_amps_reduction`.
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
  - `prune_old_effects()`: removes effects older than cutoff to prevent unbounded growth; pruning the last Tesla `set_amps` effect also clears the orphaned `last_commanded_amps` (bugs/2026-10-08-weird-behavior.log)
  - `apply_prediction_window()`: resolves the prediction/settle window from shared-cache
    quantization; commits a new window only after two consecutive cycles and ignores
    dead-band jitter (see `_resolve_prediction_window`)
- DeviceState dataclass tracks per-device runtime state, including daily
  ON-time fields (`on_since`, `runtime_today_secs`, `runtime_day`) credited by
  `StateTracker.note_desired_transition()` (single funnel for GapMinder
  decisions and `_sync_plug_states` reconciliation; meter-local midnight
  resets/clips) and read via `runtime_today_for()` for alerts only
- Dashboard pills (`templates/_metrics.html`) show `pending-on/off` (faded)
  while a pending effect exists, else fall back to unconfirmed `desired_state`
  mismatch (desired=True/actual=False → pending-on, desired=False/actual=True
  → pending-off) so pruning before controller confirmation never flaps to
  off/on; `_sync_plug_states` (`load_manager.py`) only reconciles `desired`
  when the reported `actual` actually changed, preserving our own unconfirmed
  command. Covered by `tests/test_desired_pending.py`.
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
- Plug `turn_off` lines append today's ON-time since meter-local midnight: `🔘 water heater (06:12 today)` (`MM:SS` under an hour, `H:MM:SS` above; plugs only, never Tesla/`turn_on`)
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
uv run pylint *.py tools/*.py          # 1. Style and bug checks
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
| Lint | `uv run pylint *.py tools/*.py` |
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
- **Never depend on `bugs/` or `corpus/`**: neither is tracked by git, so a
  fresh clone has no logs and anything that reads them silently skips. A test
  whose *verdict* needs log data must carry that data inline as a checked-in
  fixture (the precedent: `A_ANCHORS` in `tests/test_residual_feedback_replay.py`,
  `QUARTER_A_ANCHORS` in `tests/test_estimator_replay.py`) and treat the log as
  an optional `skipif`-guarded drift check that fails if the fixture goes stale.
  Behavioural tests should be driven by synthetic log text. The corpus is
  measurement-only: it can inform a decision, never gate one.
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
