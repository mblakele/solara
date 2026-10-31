---
name: forecast-analysis
description: Use when analyzing solara load-management forecast quality or guard/Tesla behaviour from logs — scoring open-loop forecast error, auditing what the guards declined, comparing estimator variants or policies closed-loop, or deciding whether to remove or ship a guard, estimator, or policy knob. Also use when a task names a file under bugs/ or corpus/, or asks whether a change moves the per-quarter target.
---

# Forecast & Guard Analysis

Judgment workflow for solara's offline analysis tools. **The tools measure; they
do not authorize.** Only a closed-loop replay can justify changing production
behaviour, and never without a failing test, a characterized failure mode, and
owner sign-off.

## The one-line rule

**Lead with the tool's raw output. Never hand-derive a number the tool
computes.** Hand numbers are for *explaining* the tool's numbers, not for
replacing them. (This is the AGENTS rule for `bugs/*.log`; it applies to every
tool here.)

## Which tool answers which question

| Question | Tool | What it is |
|---|---|---|
| How wrong is the forecast, independent of any decision? | `tools/forecast_log_scoring.py` | **open-loop** forecast error vs completed-quarter actuals |
| Which alternative rate estimator is more accurate? | `forecast_log_scoring.py --estimator` | open-loop ranking over one shared anchor set |
| What did the guards *decline*, and what did the turn-off path shed? | `forecast_log_scoring.py --guards` / `--turn-offs` | read-only **census** (no verdict) |
| Does a policy/estimator change improve the *actual* quarter? | `tools/estimator_replay.py` | **closed-loop arbiter** — counterfactual meter through production `GapMinder.decide()` |
| Did the adaptive prediction/settle window change any decision? | `tools/settle_window_counterfactual.py` | window arm vs a pinned window (gate only) |
| Does lumpy-first / the residual differ from status quo? | `tools/divergence_audit.py` | shed-watts audit |

Read the full contract of each in `AGENTS.md` under `tools/`. The table above is
the routing decision, not the spec.

**The arbiter distinction matters.** Open-loop error tells you what the loop is
*fed*; it does not tell you what the loop *does*. The two rankings can invert —
a variant can be far more accurate open-loop and worse closed-loop. Never ship on
an open-loop result. The closed-loop replay is the only tool whose output can
authorize a production change.

## The order: score → census → replay → counterfactual

Run them in this order, and stop as soon as the question is answered.

1. **Score** (`--estimator`) — is the forecast itself the problem, or the
   decisions? Explains the input to everything downstream.
2. **Census** (`--guards`, `--turn-offs`) — *describe* what the machinery did
   (what was declined, shed, overshot, reversed). A census is evidence about
   behaviour, never a verdict on its own; every mechanism that only *declines*
   work is invisible to a loop that watches actions, which is exactly why the
   census exists.
3. **Replay** (`estimator_replay.py`) — the decision. If a policy/estimator/knob
   question is on the table, this is what answers it. Report every regime —
   the default `all` report already splits plug-only from car-active, and
   `--regime {tesla,plug-only}` isolates each — because a change can help one
   and hurt the other.
4. **Counterfactual** (`settle_window_counterfactual.py`) — price a machinery
   arm against a pinned alternative when the question is "does this default
   configuration even matter?", not "is the policy better?".

## Run recipes

From the repo root (all read-only; none write back):

```bash
# 1. score — ALWAYS first for a named bugs/*.log
uv run python tools/forecast_log_scoring.py bugs/<file>.log
uv run python tools/forecast_log_scoring.py corpus/*.log
uv run python tools/forecast_log_scoring.py corpus/*.log --estimator
uv run python tools/forecast_log_scoring.py corpus/*.log --turn-offs
uv run python tools/forecast_log_scoring.py corpus/*.log --guards

# 3. replay — the arbiter
uv run python tools/estimator_replay.py corpus/*.log
uv run python tools/estimator_replay.py corpus/*.log --variant prior
uv run python tools/estimator_replay.py corpus/*.log --regime tesla
uv run python tools/estimator_replay.py corpus/*.log --turn-off-margin 40
uv run python tools/estimator_replay.py corpus/*.log --guards live
uv run python tools/estimator_replay.py corpus/*.log --rate-window-quanta 2,3,4,all
uv run python tools/estimator_replay.py corpus/*.log --defer-lookahead-quanta 0

# 4. counterfactual
uv run python tools/settle_window_counterfactual.py corpus/*.log
uv run python tools/settle_window_counterfactual.py corpus/*.log --pinned-secs 30
```

Prefer `corpus/*.log` for anything that will be quoted: it is the broadest
sample. A single `bugs/<file>.log` is a case study, not a population.

## Evidence discipline

- **`bugs/` and `corpus/` are gitignored and measurement-only.** They are
  untracked, so a fresh clone has none of them. Anything that reads them
  *silently skips* there.
- **A verdict must rest on checked-in data.** Verdict-bearing tests carry the
  needed log data inline — as a checked-in fixture or as synthetic log text.
  Precedents: `A_ANCHORS` in `tests/test_residual_feedback_replay.py`,
  `QUARTER_A_ANCHORS` in `tests/test_estimator_replay.py`. A real-log assertion
  may exist only as an optional, `skipif`-guarded **drift check** that fails if
  the fixture goes stale.
- **The corpus is measurement-only: it can inform a decision, never gate one.**
  Never write a test whose pass/fail depends on `corpus/` or `bugs/`.
- **Guards are never removed without all three:** (a) a closed-loop verdict,
  (b) a characterized failure mode (what breaks, and how often), and (c)
  explicit owner sign-off. A plan or a specification is not a substitute for
  any of the three.
- **Deletions keep the knowledge.** When an instrument is retired, its verdict
  paragraph stays in `AGENTS.md` (and the plan), and a test pins the removal so
  it cannot silently come back.
- **Test-first for any production change.** RED (a failing test that
  reproduces the bug or defines the behaviour, with its failure trace shown) →
  GREEN (minimal code) → REFACTOR. Then run the gate:
  `uv run pylint *.py tools/*.py`, `uv run mypy`, `uv run pytest`.

## Traps

- **Dry-run logs are not production logs.** Under `LOAD_MANAGE_DRY_RUN`, actions
  are logged but not executed and state is not updated, so `actions=` is empty
  and `pending_effects_count=0`. The replay's effect store and opening ON/OFF
  state come from that dump; a dry-run log yields quarters the ingestion drops.
  Confirm the logging mode before trusting a log as evidence.
- **"0 fires" is not "unreachable".** Zero occurrences in the corpus is a
  measurement, not a proof; the corpus is finite and refreshed. Structural
  unreachability is a separate, *argued* claim — e.g. "the fresh-data gate is
  true whenever an active settle exists (both compare the same window and
  `now >= data_point_at`), so production exits before the suppression is
  consulted." State which kind of claim you are making, and do not delete on the
  measurement kind alone.
- **`mean |miss|` is diluted by night quarters, and it is symmetric.** On the
  2026-10-19 corpus ~45% of replayable quarters (515 of 1,152) never had a
  cycle with a surplus (`min(anchor.predicted_wh) > 0`) — 514 of them in the
  18:00–07:59 local window, since `qh_start` is UTC and the site is UTC−7 — so
  their miss is the house's baseline load against an unreachable target.
  Restricting to the leverable quarters moves the *level* (plug-only 23.36 →
  31.63 Wh) and no *ranking*; car-active was never diluted (117 of 117
  leverable). And the miss has two sides that trade: plug-only error is ~89%
  export-side and that side is invariant across every candidate measured,
  while import-side improvements come from absorbing less. State which
  population and which axis a figure is on before ranking candidates with it.
- **Corpus brittleness.** The corpus grows and logs get re-fetched; counts,
  per-log rows, and even the newest quarter's inclusion drift between runs. Any
  figure is a *snapshot of a moment*. Do not treat a figure as a baseline, and
  re-run before citing.
- **The one-log trap.** A single incident log can invert a corpus ranking (a
  change that wins on the corpus can lose on one dramatic day, and vice versa).
  Report the one log *separately*; never decide *on* it. And when the incident's
  own log *regresses* under the fix designed for it, say so plainly: the incident
  is then **characterized, not replayed** — the replay's meter did not reproduce
  the production trace, so no quarter in the model can confirm the story.
- **Nameplate vs measured draw.** Plug power uses configured nameplate watts, so
  replay *absolute landings are the model's*; the *deltas* are the result.
- **Replay fidelity.** The replay's meter is a counterfactual step-function and
  its gap reflects that meter, not production's pending-effect-adjusted
  prediction. Signs and relative deltas are the result; absolute landings are
  the model's. The same caveat applies to the guard arm (`--guards live` vs
  `churn` vs `inert`).
- **The window is two things at once.** The quantization-derived prediction
  window is both the `EffectStore` freshness window (the gate) *and* the
  forecast's trailing-rate length (`util.compute_nbc_quarter`). The
  settle-window counterfactual prices the **gate only** — it holds the logged
  forecast fixed. Do not read its "0 changed decisions" as "the window changes
  nothing". The natural unit of *both* is the **quantum, not seconds**: the meter
  reports a power at the end of each quantum and holds it across the quantum, so a
  window equal to the quantum makes the trailing rate the latest reading (a
  zero-order hold) and a sub-quantum window is degenerate. Frame any window or
  estimator-horizon sweep in quanta — the replay's `--rate-window-quanta` is the
  closed-loop form of that sweep (it re-averages the rate only; `raw_wh` and the
  cadence stay fixed). The defer deadline is the quantum's second consumer: a
  *time* rule that must leave room for the next observation should buy whole
  windows, sized from the resolved window (`DecideContext.observability_secs`,
  filled from `state.effective_settle_secs` in production and from each quarter's
  median data-point spacing in the replay), never from a literal 30 s — and it
  must fall back to shipped behaviour when the window is unknown rather than
  guess. That rule **shipped at K=1 on 2026-10-19**
  (`LOAD_MANAGER_DEFER_LOOKAHEAD_QUANTA`) as a soundness fix priced as a wash
  (car-active 17.99 → 17.91, plug-only bit-identical); `--defer-lookahead-quanta 0`
  reproduces the pre-ship baseline the older verdicts were measured against.
- **Stopping earlier cannot raise consumption — but |miss| is not monotone in
  consumption.** A change that only removes car draw lowers the landing, so a
  "regression" in |miss| means the quarter *overshot* the target, not that more
  energy was used. Check the signed miss and the landing before calling such a
  change harmful (the 2026-10-18 verdict was misread this way once: consumption
  fell −5.81 → −27.13 Wh while |miss| rose, and the log's own trace favours the
  rule). Also check that the replay's counterfactual actually reproduces the
  incident (`cf.raw_wh` vs the log's `raw_wh`, the decisive cycle's presence in
  the anchor set) before quoting its verdict as evidence about that incident.
- **Constant metadata.** `ParsedMetricsQH.qh_name` is the literal `"QH1"` on both
  sides of comparisons, so any QH-boundary clause keyed on it is dead in
  production. Verify a "QH expiry" is live before crediting it.

## Where the verdicts live

Verdict prose — the actual numbers, the ship/no-ship calls, the failure modes —
belongs in **`AGENTS.md`** (the `tools/` tree entries) and the **plan** under
`.opencode/plans/`. Those get refreshed when the corpus is re-measured, and the
`AGENTS.md` tools header states plainly that every `Corpus:` figure is a
snapshot.

This skill deliberately carries **no verdict numbers**. It is the workflow, and
it is written to survive a corpus change. When you need a number, run the tool
and cite its output; when you need the standing decision, read `AGENTS.md`.

## A worked shape

For "can we change X?":

1. Run the scorer first; lead your reply with its raw output.
2. Run the census if the question is about what the machinery declines/sheds.
3. Run the replay across both regimes. This is the verdict.
4. If it wins: write the failing test (synthetic text or a checked-in fixture),
   implement, re-run the replay, run the gate, and only then propose shipping —
   with the owner's sign-off for guard removals.
5. If it loses: record the verdict in `AGENTS.md` and the plan. A documented
   negative result is a successful outcome.
