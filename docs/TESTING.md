# Tests: what each file pins

Reference list for the Solara pytest suite. Each entry names the behaviour the
file protects, so an agent can pick the right file to extend. Binding testing
rules stay in `AGENTS.md`.

`bugs/` and `corpus/` are untracked. Any test that needs log data carries the
data inline as a checked-in fixture; real-log assertions are `skipif`-guarded
drift checks.

- **test_forecast_log_scoring.py**
  the offline tool (parser quarter identity, actual resolution, the error==-R*shortfall
  identity, sigma_rate/coverage, the historical churn band — rebuilt locally from anchors,
  offset/sign invariance, reset across quarters, churn_coverage — guard/cycle-result parsing
  incl. the segment-scoping traps (action data_point_at before, candidate reason after
  diagnostics), target/miss/guard joins, the estimator variants (prior rate from the same
  fetch's completed neighbours incl. the missing-total and no-prior fallbacks, blend weights,
  trend gate trust/ fallback/quarter isolation, shared anchor set, bucket sigma matrix, CLI
  table + default-output parity and unknown-flag rejection), and the turn-off path collection
  (decision entry ↔ same-second shed attachment incl. the orphan-shed skip, per-action
  overshoot/residual roll-ups, Tesla stop reasons without savings, the reversal join's time
  window bounding quarter scope, per-quarter miss join, empty-summary zeros, CLI section +
  default-output parity, multi-word device names, the B1 hedge attribution: quarter sigma/churn
  roll-up with thin-quarter None, bucket edges and labels, overshoot/reversal fractions with
  their separate denominators, unassigned sheds still landing on the gap axis, summary carries
  the attribution only when handed the map, report + CLI section), the suppression census (guard-decline
  parsing scoped to the diagnostics segment, candidate dedup and the off+toggleable filter,
  banked overrides, defer horizons, ramp fires and the band arithmetic pinned against
  load_nbc/load_manager's own constants, the settle trace's commits, the guarded/unguarded
  cohort split, the availability caveat, empty-census text, and CLI section + default-output
  parity), and that the tool runs standalone; real-log assertions skip because bugs/ is
  gitignored)
- **test_estimator_replay.py**
  is the spec for tools/estimator_replay.py, Subtask 2 of the variance-reduction plan: a
  closed loop over the two plug-only quarters (anchors + plug events as CHECKED-IN fixtures —
  the verdict must not depend on gitignored bugs/ — with a skipif-guarded drift test
  re-parsing the logs through the tool's own ingest_log), a step-function meter (opening plug
  state per quarter via Quarter.initial_on, defaulting to the all-on the two fixtures happen
  to open with), history mode landing on the logged actual, the status-quo rate reproducing
  production's own opening shed, the replay's rates equal to the scorer's estimators, the
  reversal window matching the scorer's, and the measured verdict — the flat prior cuts churn
  in both quarters but over-sheds the regime-change quarter, so its mean |miss| is worse
  (19.87 vs 15.19) and it must not ship. Its Tesla leg is pinned the same way: one amp over R
  is 240*(R−lag)/3600 Wh (the synthetic car quarter), the 2026-10-17 ceil overshoot reproduced
  (2 A past a 76.5 Wh gap), an unplugged car never commanded, history mode pinning the car
  too, the plug-only fixtures never commanding one, and --regime splitting car-active from
  plug-only. The policy axis (Policy: margins + lumpy_first, built from GapMinder's
  constructor) is spec'd on synthetic quarters where a coarse quantum makes the effect
  legible: the default policy reproduces the shipped engine (flag-off parity against the
  pinned verdicts), a turn-off margin as wide as the 260 W plug's own 43.3 Wh declines the
  shed, the turn-on margin leaves the surplus unspent, replay_all forwards the knobs, a
  supplied banked_wh is what arms the lumpy-first gate (without it the flag is a silent
  no-op), the shipped whole-window fit declines a plug it cannot hold, and the CLI header
  names the policy it ran; and the derived-signal arm (GUARD_MODES inert/live through
  --guards) is spec'd on a synthetic RAMPING quarter — the live arm feeds gap_trend_wh_per_s
  and the inert arm leaves it dark, a spy on GapMinder.decide pins what each arm handed the
  ctx, the two arms are bit-identical without --ramp-aware, the trusted-trend count reaches
  the aggregate, and the report names the signal, the inert control and the removed guard,
  plus the arm vocabulary and CLI parity (default output == --guards inert, block only when
  non-inert). Ingestion is covered on SYNTHETIC log text (no bugs/ needed): the built
  quarter's anchors/actual/lag/events/opening state, the amp history in time order, the dedup
  of a repeated data point, the fetch-line-derived lag, and each drop reason (never-known
  Tesla state, too few anchors, implausible lag, no candidate dump), plus the reporting layer
  (summaries, comparisons, regression ranking, regime selection), the freshness of the variant
  map, and the CLI (table, --date, --regime, --turn-off-margin/ --lumpy-first header, unknown
  variant, no quarters), plus the defer quantum lookahead on synthetic two-anchor quarters
  (the incident cycle stops with one window and defers with zero, scaling with each quarter's
  own spacing, inert without a window, the quarter carries the spacing the ingest measured,
  and the label names the knob only when it is on)
- **test_tou_page.py**
  inclusive date ranges, DST days, detail rows/defaults and picker dates
- **test_app.py**
  endpoint validation and range limits
- **test_gap_trend.py**
  the gap-slope tracker and its trust/rollover rules, and asserts the removed churn EWMA and
  churn_ready gate are gone,
- **test_tesla_decider.py**
  the ramp-aware stop rule, the reduction amp step (a ceil that covers the gap, the shipped
  rounding pinned), the clamped-increase actual amp delta (5 → 10 reports 5 A, not the
  unclamped request), and the defer quantum lookahead (TestDeferQuantumLookahead: the
  2026-10-18 incident cycle stops at one window, defers at zero, is inert without a window,
  scales in quanta, and the log line reports it),
- **test_tesla_priority.py**
  the explicit `tesla.priority` order against plug on/off decisions (a high-priority Tesla
  keeps amps while a low-priority plug sheds; a high-priority Tesla increases amps before a
  low-priority plug starts), plus the telemetry-only path where `devices.json` settings
  reach the engine with no REST credentials and `load_tesla_config()` returns None,
- **test_tesla_ramp_replay.py**
  the 2026-09-26 incident sequence (ramp_aware=True) plus a skipif-guarded drift check that
  the inline INCIDENT_POINTS gaps still match the log,
- **test_ramp_aware.py**
  the LOAD_MANAGER_RAMP_AWARE default-off stance (default defers on the c54 cycle, flag-on
  stops) and its LoadManagerConfig/env plumbing,
- **test_defer_lookahead_quanta.py**
  the shipped one-window defer lookahead (the 2026-10-19 rule): the default, the incident
  cycle on both settings, "unknown window stays inert", and the LOAD_MANAGER_DEFER_LOOKAHEAD_
  QUANTA config/env plumbing incl. the malformed-value fallback,
- **test_tesla_overshoot_replay.py**
  the 2026-10-01 overshoot (status quo characterized; the replay reproduces the log's two
  Tesla increases),
- **test_banked_wh_forwarding.py**
  the retained banked_wh plumbing from the cache and fetch stage to the engine's DecideContext
  (the lumpy-first gate still reads it)
- **test_qh_boundary_forecast.py**
  the 2026-10-14 QH-boundary glitch (complete QH1 keeps the waiting-for-data period label
  beside the placeholder instead of a blank)
- **test_prune_orphaned_tesla_command.py**
  the 2026-10-08 orphaned 12A command (pruning the last set_amps clears last_commanded_amps)
- **test_commit_tesla_direction.py**
  the 2026-10-08 7->5 cut (commit classifies direction from live amps when the command
  expired)
- **test_decision_lag_adjustment.py**
  the 2026-10-14 water-heater execution lag (fetch-start remainder shrunk to decision time
  before decide books plug/Tesla savings)
- **test_sheddable_residual.py**
  the controllable-load residual prototype (net-of-lumpy counterfactual + replayed c658 / c817
  small-plug prescriptions, production unwired)
- **test_sync_external_effect.py**
  the external-flip reconcile (an actual change records the synthetic effect, debounce bump and
  runtime book; an unchanged reading past the observation window is adopted as reality — the
  2026-10-26 water heater held desired=on for 33 min against a plug that never reported on)
- **test_residual_feedback_replay.py**
  the meter-feedback harness (event-driven counterfactual meter + closed-loop residual policy
  over both 2026-10-14 quarters; symmetric margin pinned harmful in B)
- **test_divergence_audit.py**
  the status-quo vs residual audit (logged priorities, watts-compared; 9/1015 corpus quarters
  diverge, always sheds less)
- **test_settle_window_counterfactual.py**
  the Subtask-6 counterfactual (cycle/effect repr parsing + UTC conversion, the short-window
  wait the pinned window takes, the adaptation's protection case — a 60 s committed window
  defers where the pinned 30 s decides — agreement when the effect is far, tolerance of the
  retired suppress_action token, the window histogram / non-default count, report totals +
  gate counts, default constant, standalone CLI)
- **test_lumpy_first.py**
  the Subtask-4 wiring (flag-off parity with history, flag-on c658/c817 prescriptions,
  config/env plumbing, and the 4a gapminder_lumpy_first census log when the lead is taken)

## Removed

- **test_dispatch_quantum.py**
  retired 2026-10-18 with the dispatch_secs knob (its verdict is above; the shipped
  whole-window fit is covered by test_gap_minder)
