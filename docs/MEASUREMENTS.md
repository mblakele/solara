# Measurements and verdicts

Append-only ledger of load-management measurements and the decisions they
justified. Moved from `AGENTS.md` on 2026-10-21. Each figure is a snapshot of
the corpus at measurement time; the corpus is refreshed, so treat a number as
a measurement of a moment, not a fixed baseline.

This is reference material. Agents do not load it unless a task needs the
history behind a shipped rule. Binding rules stay in `AGENTS.md` and
`docs/LOADMANAGER.md`.

## Offline analysis tools

Offline-only analysis tools. No production module imports anything in here;
they read log text and report. `estimator_replay.py` additionally re-runs
production decision classes — it is never imported by production, and it
writes nothing back. Linted and type-checked (`uv run pylint *.py tools/*.py`,
mypy `files` includes `tools/*.py`), but not shipped in the gunicorn/Flask
runtime path.

## `tools/forecast_log_scoring.py`

FIRST STEP for any task that names a `bugs/*.log` file — run it and lead with its output (see
"Analyzing a log in `bugs/`" under General Advice). Scores NBC forecast quality against
production logs: parses forecast anchors + completed-quarter actuals and reports forecast
uncertainty. Despite the name it does NOT replay decisions (contrast
tests/test_tesla_ramp_replay.py, which re-runs production classes); it measures the forecast.
Identity: a realized error is *identically* -remaining_secs * rate_error (ForecastAnchor.error
vs rate_shortfall), so U(R) = k * sigma_rate * R is complete, not an approximation. It also
reports a historical churn band (k * churn * R, rebuilt locally from the anchor series because
production removed the churn EWMA on 2026-10-21 — raw predicted_wh stands in for the gap;
constant offsets/signs cancel in |Δslope|) beside the sigma band, and reads — never re-executes
— what production logged about its own decisions: CycleResult reprs (per-quarter target/miss,
reason, historical churn, scoped to the diagnostics=CycleDiagnostics(...) segment) and
gapminder_jitter_guard INFO events in old logs (wall-time quarter, exact because previous_qh
exits before decide()). Report output: churn-band coverage line, historical jitter-guard census
line, target/miss/guard columns in the quarter table. It also attributes controllable watts per
cycle from the candidates lists (prior-cycle join) and reports the residual series (net-of-ON
rate error, sigma_residual, 260 W survival — residual never beat raw sigma on corpus/, so no
margin ships). `--estimator[=NAME,...]` additionally *scores* alternative forecast rates over
the same anchors (status-quo trailing window; prior = median rate of the completed qh2..qh4 the
same fetch reports, with the shipped forecast as fallback so every row shares one anchor set;
prior-blend@M = (n*w + M*p)/(n+M); trend-gated = the window only while GapTrendTracker trusts a
trend, mirroring _stage_compute_gap's HYSTERESIS_WH/R floor, override with --trend-floor).
Report: per-variant mean|e|/p80/max/optimistic/sigma_rate plus a sigma_rate-by-R-bucket matrix.

Corpus verdict (760 anchors, floor 3): prior 12.60 mean|e| vs status-quo 29.71, and at R 360-900
s sigma_rate 0.0270 vs 0.1268 — but ramp quarters regress (2026-10-01: 22.7 -> 57.5), so this
measures, it does not ship (ForecastAnchor.prior_rate). Default output is unchanged without the
flag. `--turn-offs` adds the turn-off path collection: every entry into the unguarded
`_decide_turn_off` (horizon R, gap, each shed booked: plug `action=turn_off`, Tesla amp reduce,
Tesla stop; the raw logfmt values, joined to the decision by its line-prefix second), reported
as shed totals, median horizon, short-horizon count (TURN_OFF_SHORT_HORIZON_SECS=60), gap
coverage with the per-action overshoot (savings − remaining gap), reversals (a plug turned back
on within TURN_OFF_REVERSAL_WINDOW_SECS=900 s of the shed — time-based, not quarter-scoped,
because the clearest case crosses the QH boundary), and a per-quarter table joining the
completed actual/target/miss. Data collection only: the unguarded path is a documented design
decision and is not re-executed or changed. Plug device names can contain spaces ("water
heater"), so the name pattern is `(.+?)` — a `\S+` silently drops every shed by a multi-word
plug (caught by tests/test_estimator_replay.py, which needs the whole event set; the dropped
heater sheds moved the corpus by 5 sheds / 4.9 kWh / 150 Wh of overshoot). Corpus: 405
decisions/65 quarters, 34 under 60 s, 109/165 sheds overshot (max 222 Wh), 50/84 plug sheds
reversed (median 136 s). `--guards` adds the historical suppression census — every mechanism
that only *declines* work is invisible to a loop that watches actions, so this counts what each
one withheld in the OLD logs: jitter-guard declines (`reason=excessive_jitter` cycles, read from
the diagnostics segment so a candidate's own reason cannot stand in) and the off+toggleable
candidates in the same repr, banked-cover rescues, stop deferrals with their R distribution,
ramp-rule fires and arithmetical reach (`R <= gap/P + cycle*(1+clamp)`; P built from
constants.TESLA_CHARGE_AMPS_MIN_DEFAULT * TESLA_NOMINAL_VOLTAGE / 3600 and the clamp/cadence
pinned by test against TeslaDecider .RAMP_TREND_CLAMP_FRACTION / LoadManagerConfig
.config_interval_secs), and the committed settle window per cycle (commits = the change
production never logs). A guarded-vs-unguarded quarter split rides alongside with the confound
stated in the output: the guard fires *in* the churning-forecast quarters, so the split is
correlation, not effect. Corpus: 1242 declines over 1989 declined cycles (all 1989 offering a
load — availability is universal, so the report says so and leaves "did the decline withhold an
action" to the replay), 767 rescues, 172 deferrals (median R 382 s), 0 ramp fires / 14 eligible,
settle 30 s x 55299 / 15 s x 103 / 21 s x 4 / 6 commits. Default output stays byte-identical.

Hedge attribution (B1, 2026-10-22, 17-log corpus). `--turn-offs` also buckets every shed by the
quarter's rate sigma (`TURN_OFF_SIGMA_EDGES` 0.10/0.20 Wh/s), the quarter's median rebuilt churn
(`TURN_OFF_CHURN_EDGES` 0.10/0.30) and the shed's own pre-shed `|gap|` (`TURN_OFF_GAP_EDGES`
20/60 Wh), reporting overshoot and reversal rate per bucket plus a per-axis direction line
(`quarter_uncertainty` rolls the two quarter signals from the same anchors). Snapshot: 28617
decisions over 1291 quarters, 1034 sheds with a logged saving, 817 plug legs, 616 reversed
(median 330 s), 733 overshoots (max 452.30 Wh). Verdict: the SIGMA axis has no signal — overshoot
471/604 (<0.10) then 80/135 (0.10-0.20) then 175/286 (>=0.20), reversal 365/472, 64/80, 180/256,
so a sigma-scaled hedge has nothing to scale on. Churn reversal rises with the signal (8/23 ->
126/176 -> 482/618) but its overshoot does not (16/24 -> 153/201 -> 564/809). The gap axis
dominates and is the only one available at the shed: `|gap| < 60 Wh` overshoots ~90% with a
POSITIVE median overshoot (19.2 / 18.4 Wh) while `|gap| >= 60 Wh` overshoots 163/406 and the
median shed there falls 17.1 Wh SHORT of the gap — the path over-sheds small gaps and under-sheds
large ones. Caveat: sigma and churn are whole-quarter (retrospective) values, so they bound what a
decision-time rule could see rather than reproducing it. Any revisit of the unguarded path should
key on the gap it still has, not on forecast uncertainty.

Run from the repo root:

```bash
uv run python tools/forecast_log_scoring.py bugs/*.log
uv run python tools/forecast_log_scoring.py bugs/*.log --estimator
uv run python tools/forecast_log_scoring.py bugs/*.log --turn-offs
uv run python tools/forecast_log_scoring.py corpus/*.log --guards
```

## `tools/estimator_replay.py`

Closed-loop replay: drives an estimator through production GapMinder.decide() against a
counterfactual step-function meter, so the verdict is the quarter's miss + churn, not the
open-loop forecast error the scorer reports. The model (Quarter/Replay/meter/rate_*
variants/Policy) lives here; tests/test_estimator_replay.py is its spec. Two independent axes:
the rate estimator (only `rate_fn` differs between runs, pinned equal to the scorer's
estimate_prior_only/estimate_prior_blend) and the decision Policy, expressed *only* through
production GapMinder constructor surface — `--turn-on-margin WH` `--turn-off-margin WH`
`--lumpy-first` `--defer {on,off}` `--max-defer-secs SECS` `--ramp-aware` `--tesla-priority N`.
The defer trio is `--defer` / `--max-defer-secs` / `--ramp-aware` (the defer rule itself, its
safe-buffer cap, and the trend hastener; all default to the shipped values — defer on, cap
900 s, ramp off); `--tesla-priority` is the car's rank against the plugs (production's
`tesla.priority`, shipped 0, which keeps the legacy order — plugs first on surplus, car first
on deficit). The meter has two actuator populations:
plugs (ON/OFF, nameplate watts) and the car (TeslaSample steps of REPORTED amps, replaced by a
policy set_amps/Tesla turn_off, priced at nominal_voltage() W/amp so one amp over R is
240*R/3600 Wh — the quantum behind the 2026-10-17 overshoots). plugged_in/at_home ride along
from the log because production gates both charging directions on them, so an absent car is
never charged. Ingestion stays conservative: quarters where no cycle ever reported a Tesla state
are dropped (tesla_state=None is an early exit, not an idle car; production now records the
reason on CycleDiagnostics.tesla_state_missing_reason, so newer logs need no inference — older
ones still do); so are quarters under MIN_ANCHORS_PER_QUARTER, with an implausible fetch-line
lag (LAG_MIN/MAX_SECS), or with no candidate dump to read the opening ON/OFF state from;
repeated samples_used are deduped (production's tracker rejects repeats). Command times come
from each anchor's own `wall_at` (per-quarter median lag), not a hand constant. `--variant NAME`
(repeatable) `--date YYYY-MM-DD` `--worst N` `--trend-floor WH` `--regime {all,tesla,plug-only}`
(default all; splits the report by which actuators were in play, since the plug-only half is the
pre-car baseline reproduced exactly — the policy never commands a car that is not drawing). A
Policy run reports itself: the header carries the margins and the lumpy-first / defer / ramp
flags. DecideContext always carries banked_wh (the quarter's accumulated raw energy,
qh1.raw_wh); whether the derived gap signal rides along is the derived-signal arm (`--guards
{inert,live}`, default inert). inert — every pinned verdict below — leaves gap_trend_wh_per_s
dark, so the ramp-aware Tesla stop never fires; both sides of a comparison are equally affected.
live feeds one GapTrendTracker per quarter in production's own order (noise floor
HYSTERESIS_WH/R, then the slope to gap_trend_wh_per_s when trusted). Without --ramp-aware the
two arms are bit-identical; the turn-on jitter guard and its banked-cover override were removed
2026-10-21. Stated deviation: production's tracker sees the pending-effect-adjusted gap of the
published prediction while the replay's gap reflects its own counterfactual meter, so signs and
relative deltas are the result and absolute landings are the model's. Inert parity verified to
the printed digit on the fixed car model (2026-10-21): plug-only 24.87, car-active 10.34
mean|miss|. Car-model correction (2026-10-21): the replay's counterfactual car used to hold the
quarter's opening amps until the policy commanded, ignoring every later logged transition. It
now follows the logged transitions and overlays policy commands (`_tesla_series`), so a manual
stop or a charge-complete event is not booked as draw. Every car-active figure below this block
is a PRE-FIX snapshot (2026-10-19 or earlier) unless it says otherwise. Re-measured on the
current corpus (141 car-active / 1199 plug-only), status-quo rate, shipped policy: car-active
mean|miss| 10.34 (median 3.69, mean +2.43, 14.92 actions/q); plug-only 24.87 (median 14.32, mean
-6.12, 2.09 actions/q). Estimators: prior 19.15, prior-blend@60 14.71, trend-gated 13.29, so
status-quo still wins. Margins (car-active): turn-on 10/20/40 = 11.68/13.29/18.04, turn-off
10/20/40/60 = 12.26/17.18/31.02/50.15, so zero still wins. Lumpy-first 10.38 vs 10.34 — a wash,
no longer a loss. Defer off 18.12 vs 10.34 on, so the defer rule stays load-bearing. Guards:
churn == live 12.28 vs inert 10.34 (44 better / 62 worse), 0 ramp fires; this REVERSES the
pre-fix "net better" reading, and the bundle's deletion is left to the owner (nothing deleted).
Rate windows car-active: 2q 13.13, 3q 14.16, 4q 12.48, all 23.27, so 1q stays. Plug-only moves
only where a plug knob moves (margins, lumpy-first, rate windows); the Tesla-only knobs are
bit-identical.

Scorecard shape (2026-10-19): ~45% of the replayable quarters are night quarters the loop has
nothing to do in — no cycle ever saw a surplus (`min(anchor.predicted_wh) > 0`), so the -9 Wh
target is unreachable without a battery and the miss is the house's baseline load. 514 of the
515 fall in the 18:00-07:59 local window (`qh_start` is UTC). Restricting to the leverable 637
raises every candidate's mean |miss| by 6-14 Wh (plug-only 23.36 -> 31.63) and reverses no
ordering; car-active was never diluted (117/117 leverable — the car only ran on surplus).
Two-axis split over leverable quarters: plug-only error is ~89% export-side (-28.10 Wh/q against
+3.53) and that axis is invariant across every candidate (-26.94..-28.21); improving imports
means absorbing less (car-active guard arm 8.74 -> 4.13 Wh/q imports at -9.17 -> -12.83
exports), so each knob trades one axis for the other and a symmetric |miss| scores each trade a
wash — why the margins and the guard-prune never cleared their bars. The exchange rate is a
price ratio (imported vs exported Wh); no price/TOU/credit term exists in the decision path, and
none is planned from this.

Corpus verdict (13 logs, 1146 replayable of 1294 scored; 117 car-active carrying 1269 Tesla
commands): status-quo beats every variant in BOTH regimes — plug-only 23.41 mean|miss| / 14.27
median / -4.87 mean / 2.07 actions/q / 0.90 reversals/q vs prior 27.71, prior-blend@60 26.26,
trend-gated 24.11; car-active 17.99 / 5.00 / +0.30 vs prior 31.99, prior-blend@60 19.31,
trend-gated 24.15 (churn 15.73 -> 9.65-11.97) — the open-loop ranking INVERTS closed-loop (the
shipped window is ~2x worse open-loop). The 2026-10-17 counter-example (prior-blend@60 6.88 vs
status-quo 10.54 on its four car-active quarters) does not survive the corpus. No estimator
variant ships; Subtask 3 stays blocked.

Policy sweep verdict (closed loop, both regimes): the shipped zero-margin policy is best or tied
on mean|miss| everywhere, so no margin and no ordering ships — plug-only 23.41 at 0 / 23.41 at
10 / 24.80 at 40 / 26.85 at 60 (turn-on margin worse still: 25.64 at 10, 31.98 at 40),
car-active 17.99 vs 19.62 at 10 and 39.48 at 40. The plug regime's -4.87 mean miss is real bias
(a 60 Wh margin zeroes it) yet mean|miss| gets WORSE, i.e. the over-shedding hedges forecast
variance and |miss| punishes removing it. lumpy_first fires but nets out (23.51 / 17.88).
2026-10-17 disagrees with the corpus again (turn-on margin 20: 4.78 vs 10.54 there, 23.40 vs
17.99 on the car corpus) — the one-log trap. The granularity cure must win without shedding less
on average.

Reversal-cost axis (2026-10-22, refreshed 17-log corpus; 141 car-active / 1287 plug-only
quarters). The report already prints `reversals/q` beside `actions/q`, so the turn-off margin was
re-run at 0/5/10 Wh on that second verdict column (status-quo rate, inert guards): all-regime
22.79 / 22.72 / 22.99 mean|miss| at 1.05 / 0.94 / 0.84 reversals/q; plug-only 24.15 / 24.06 /
24.17 at 0.93 / 0.83 / 0.75; car-active 10.34 / 10.51 / 12.26 at 2.14 / 1.95 / 1.72 (medians
3.69 / 4.07 / 6.60). A 5 Wh margin removes ~10% of the reversal churn for a 0.09 Wh plug-only gain
but loses in the car regime; 10 Wh removes ~20% and loses in both. Reversal churn is cheap to buy
and the car-active regime pays for it disproportionately, so no margin ships — the actuator-cost
axis does not overturn the `|miss|` verdict.

Amp-floor verdict (code + flag removed 2026-10-18; car-active only; the plug-only regime is
bit-identical as it must be with no car commanded): it LOSES — car-active 19.34 mean|miss| vs
17.99 shipped, churn 19.30 vs 15.73 actions/q and reversals 5.67 vs 2.09; 2026-10-17 itself
11.55 vs 10.54. The floor's premise (close the residue on a later, finer step) is false: the
quantum is R-scaled and R shrinks ~30 s/cycle, so an amp is 40.0 Wh at R=600 s and 38.0 Wh one
cycle later, only falling below a 20 Wh gap at R < 300 s (two-thirds through the quarter).
Declining the step just routes the deficit to the plugs (+3.5 actions/q, +3.6 reversals/q ≈ one
extra duty-cycle per declined step) and the worst quarter (287 Wh, not amp-quantum-driven) is
untouched. The DURATION form is closed too: the meter quantizes the MINIMUM DISPATCH to the
reporting window (~30 s, also the settle window), so a minimum-length dispatch is one full
observation step and a scheduled restore would be open-loop exactly where the closed loop is
blind — strictly worse than today, where the loop's re-decide IS the restore (2.09
reversals/quarter, median 136 s ≈ 4-5 dispatch windows). The dispatchable unit is power *
quantization / 3600: 2.2 Wh per 260 W plug, 2.0 Wh per amp, 40.5 Wh per heater run — not the
R-scaled 43.3 Wh production books for that plug at R=600 s. The booking is a belief about a
duration the actuator is never asked to hold; whether that feeds the over-shedding is the
remaining measurable question (plan §5.3).

Dispatch-quantum verdict (code + flag removed 2026-10-18; that §5.3 question measured): the
admissibility horizon is the one place the R-scaled belief forbids a real action — a starting
load must fit the surplus with its whole-window cost (43.3 Wh for a 260 W plug at R=600 s),
production's `loads_too_large` no-action reason (12-17% of worst-decile cycles). Admitting a
load whose FIRST dispatch fits (the running debit and the pending-effect estimate stay
window-priced) is a VARIANCE TRADE, not the resolution gain it looked like: plug-only 23.41 ->
19.29 at 60 s but 238 quarters improve / 160 worsen (median delta 0.00, 61% untouched; the mean
is five rescues of 100-268 Wh, the 400 Wh surpluses the whole-window fit declines outright),
while car-active regresses the other way (17.99 -> 24.66 at 60 s; 30 improve / 73 worsen, median
+2.66; 2026-10-17 10.54 -> 23.84). All-regime only improves because 90% of the corpus is
plug-only. Best value 60 s (the toggle debounce, not the meter's 30 s): 10 s is worst in both
regimes (31.41 / 45.72), 300 s the coarsest that still helps the car side (17.10). So the
whole-window fit test is a conservative prior about whether a small predicted surplus
materializes — the same hedge, on the surplus side, the turn-off margin sweep found on the shed
side. No ship.

Nameplate watts stand in for measured draw, so deltas are the result, not absolute landings.

Pre-removal guard-arm verdict (`--guards live`, corpus, status-quo rate, churn-only and inert
controls): the arm is live, not silent — 291 declines (0.28/q) + 809 rescues plug-only, 115
(0.98/q) + 645 car-active, vs the scorer census's 1242 declines / 1294 q (0.96/q) and 767
rescues (0.59/q), the replay's churn 0.256 mean (max 31.57) Wh/s against the census's 0.13
(12.60), all same-order. live was worse than inert in both regimes on that snapshot (plug-only
23.63 vs 23.41, +0.21 mean; car-active 20.42 vs 17.99, +2.42) but the bundle is what the
attribution convicts: churn-only (the jitter guard and its override alone) is +0.21 plug-only
and -0.88 car-active — a wash, so the pre-registered deletion rule is not met and NOTHING is
deleted; live minus churn-only is the ramp rule alone at +3.31 car-active, 4 changed quarters of
117 (8.6/corpus-month, above the ~1/month threshold), so the ramp rule's deletion condition does
NOT fire either — contrary to the plan's expectation, and with the pre-emption sometimes
hundreds of seconds (two quarters lost 183 and 222 Wh to an early stop the deferral would not
have taken). Re-measured under the shipped policy the car-active leg flips to net BETTER on the
pre-fix car model (live 16.96 vs inert 17.91) with live == churn exactly and 0 ramp fires — the
earlier +2.42 was a corpus-refresh artifact, so the bundle's deletion rule still does not fire,
from the other side. 2026-10-17 separately: 12.06 live vs 12.23 inert overall, 13.22 vs 14.37 on
its car-active quarters, ramp 0 changed. Superseded 2026-10-21: the fixed-car corpus measured
the guard net worse (10.34 vs 12.28 car-active, 24.87 vs 25.18 plug-only), so the guard,
override and churn arm were removed; only the ramp-aware stop remains.

Rate-window verdict (`--rate-window-quanta`, closed loop, both regimes; snapshot): the shipped
1-quantum window is best or tied. A 2-quantum average HELPS plug-only (23.36 -> 22.99
mean|miss|) but regresses car-active hard (17.99 -> 23.53), far past the +/-0.2 Wh noise bar,
and worsens more plug-only quarters than it fixes (142 worse / 108 better of 1035, median delta
0.00 — the mean is a handful of 30-115 Wh rescues). Longer windows are monotone worse, and
2026-10-17 shows no window win either (2q 16.23 vs 14.37 on its 15 car-active quarters). The
rate axis is not where the residual miss lives: the loop pays for the level's freshness, not its
smoothness. No ship; status-quo stays. Composed only when `--rate-window-quanta K|all` is given
(K=1 is the shipped status-quo, always reported), so default output is byte-identical.

Defer-lookahead verdict (`--defer-lookahead-quanta K`, closed loop, corpus, status-quo rate;
snapshot): **SHIPPED at K=1 on 2026-10-19** as a SOUNDNESS fix, priced as a wash. The knob
withholds K observation windows from the Tesla stop's safe defer window, sized from each
quarter's OWN window (`Quarter.observability_secs`, the median data-point spacing — never a
literal 30 s), so a defer never leaves less room than the next observation needs: a deferral the
engine cannot observe through is not a deferral, so it takes the stop now. Corpus: car-active
(117 quarters, pre-fix car model) 17.99 -> 17.91 mean |miss|, 3 quarters better / 4 worse,
signed miss +0.30 -> -0.42; plug-only BIT-IDENTICAL (23.36, the knob is Tesla-only). At K=2 the
mean is better still (17.68, 7 better / 5 worse, median delta 0.00) but the sweep is
non-monotone (18.21 at K=3) and 2 is not the stated rule, so 1 ships. The direction is legible
in the changed quarters: all improvements came from under-shed quarters, all regressions from
near-target or already-over-shed ones — the rule converts under-shedding into over-shedding,
which |miss| also punishes, so it is not a mean win. Stopping earlier strictly LOWERS
consumption (checked, not assumed: the replay's regressing quarter lands -5.81 -> -27.13 Wh);
the |miss| rise is overshoot, and overshoot against the *claimed* gap is bounded at one quantum
(`R <= gap/P + Q` <=> `P*R <= gap + P*Q`) while overshoot against the *realized* quarter is the
forecast's own churn, which no stop rule bounds. The incident is characterized, not replayed:
the earlier "the log regresses under its own fix" (5.91 -> 10.89) was a replay-model artifact —
the replay derives its prediction from its own counterfactual meter, which for that quarter
diverges from the log's raw energy all quarter (up to 8 Wh), has NO anchor at the decisive R=60
s cycle (19 anchors, median spacing 45 s), and lands 7.62 Wh from the logged actual. On the
log's own trace the rule helps: the deferral series reports R=119 / 35.8 Wh / safe_defer=107 s
(slack 12 s, under one 30 s window), so the stop is taken there and sheds 39.7 Wh instead of the
20.0 Wh the R=60 s stop removed, landing the quarter at -8.86 Wh against the -9 Wh target
instead of +10.81. `0` restores the pre-2026-10-19 static rule (used for every older verdict in
this tree). `DecideContext.observability_secs` (production: the resolved prediction/settle
window) is the window plumbing this added, and it is the sanctioned way for any decision rule to
size itself from the quantum instead of a hardcoded 30 s. `--trace-quarter YYYY-MM-DDTHH:MM`
prints one quarter's per-cycle rows (data time, R, predicted Wh, gap, counterfactual car amps,
actions) and its landing/actual/miss, then exits; a naive value is UTC and a time inside a
quarter snaps down to it. `--defer-cap-sweep SECS,SECS` runs the status-quo rate at each cap and
prints one row per cap paired against the first. Both leave default output byte-identical when
absent.

Run from the repo root:

```bash
uv run python tools/estimator_replay.py bugs/*.log
uv run python tools/estimator_replay.py corpus/*.log --variant prior
uv run python tools/estimator_replay.py corpus/*.log --regime tesla
uv run python tools/estimator_replay.py corpus/*.log --turn-off-margin 40
uv run python tools/estimator_replay.py corpus/*.log --guards live
uv run python tools/estimator_replay.py corpus/*.log --rate-window-quanta 2,3,4,all
uv run python tools/estimator_replay.py corpus/*.log --defer-lookahead-quanta 0
uv run python tools/estimator_replay.py corpus/*.log --defer-cap-sweep 120,300,900
uv run python tools/estimator_replay.py corpus/*.log --tesla-priority 100 --regime tesla
uv run python tools/estimator_replay.py corpus/*.log --trace-quarter 2026-10-20T22:15
```

## `tools/settle_window_counterfactual.py`

Prices the *adaptive* prediction/settle window against a pinned one (Subtask 6 of the
guard-observability plan). The window is the EffectStore freshness window — the fresh-data gate
(`EffectStore.has_since`), the one consumer priced here — and separately the forecast's
trailing-rate length, which this tool does NOT price (it holds the logged forecast fixed; see
the estimator-responsiveness plan). So the tool rebuilds the effect store from the logged
`actions=[PendingEffect(...)]` reprs and replays production's own EffectStore once per cycle
under two arms: the window the log committed, and `--pinned-secs` (default
DEFAULT_PREDICTION_WINDOW_SECS, 30 s). Reports per log the histogram of non-default windows and
the cycles where the arms disagree. Snapshot (corpus/, 2026-10-18, 14 logs, 58,390 cycles): 111
non-default (15 s/21 s/24 s), 0 gate flips -> no decision changed, no quarter affected, gate
fires 2672 under both arms. The corpus is refreshed over time, so these figures are a snapshot,
not a fixed baseline. The post-amp-change suppression `_suppressed_by_settle` — a would-be
settle-window consumer that never decided a cycle — was retired 2026-10-18 as unreachable:
whenever an active settle exists the gate is already true (both compare the effect's wall and
data-point ages against the same window, and the settle's `now` is at or after `data_point_at`),
so production exits on the gate first; it fired in no recorded cycle. Its accessors
(`StateTracker.get_active_tesla_settle`, `TeslaSettleTracker.get_active`), the
`GapMinder.TESLA_SETTLE_SUPPRESS_WH` threshold and the vestigial `PendingEffect.suppress_action`
went with it. The adaptation itself stays: it guards a failure mode (a sustained meter change to
a slower quantum) the corpus never exercises, and the tool's 60 s committed-window fixture shows
the flip that would restore. Spec: tests/test_settle_window_counterfactual.py.

Run from the repo root:

```bash
uv run python tools/settle_window_counterfactual.py corpus/*.log
uv run python tools/settle_window_counterfactual.py corpus/*.log --pinned-secs 30
```

## `tools/divergence_audit.py`

Status-quo vs residual shed-watts audit over logs: per-anchor ON states (deduped candidates) +
logged priorities, lumpy-first iff implicated and beyond smalls capacity; corpus/ verdict 9/1015
quarters (a snapshot — the corpus is refreshed, so re-run before quoting). The charging car is
out of scope by default; `--tesla-priority N` puts it in both arms at that priority (production's
`tesla.priority`), where it gives up amps down to the 5 A floor before the plugs that rank below
it. `tesla_state=None` is unknown, never an idle car.

```bash
uv run python tools/divergence_audit.py corpus/*.log
uv run python tools/divergence_audit.py corpus/*.log --tesla-priority 100
```
