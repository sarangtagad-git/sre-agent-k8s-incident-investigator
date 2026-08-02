# Phase 10: incident memory — build plan

**Status: scoped, not yet implemented.** Written so a fresh session (no prior
conversation context) can pick this up and build it directly. Read this file top to
bottom before writing any code.

## What this is

Today the agent investigates every incident cold. `history_store.py` (Phase 7) writes
every run to SQLite, but nothing ever reads it back *into* the agent — `agent/graph.py`
never imports `history_store`. The dashboard, the CLI, and the listener all read
history; the agent itself has amnesia. This phase gives the agent access to its own
past: before it commits to a diagnosis, it gets to see what it (or a human) concluded
last time something like this happened in this cluster.

This is **not** RAG in the classic sense (no embeddings, no vector store) and it is
**not** the agent "learning" in the ML sense (no weights change, no fine-tuning). It's
closer to a doctor pulling a patient's chart before the exam: a short, honestly-labeled
summary of prior visits, offered as context for the human (here, the LLM) to reason
over — never as a fact to cite in place of the current exam's own findings.

## Why this is riskier than it sounds — the design has to earn trust, not assume it

Memory is the one feature in this project that can make the agent *worse* if done
carelessly:
- **Echo chambers.** If a wrong diagnosis gets recalled and reinforced, the same wrong
  answer can repeat indefinitely with rising apparent confidence, even though nothing
  about the evidence changed.
- **Stale answers presented as current facts.** A workload that broke one way in June
  can break a completely different way in July. Memory that isn't clearly time-stamped
  and clearly hedged reads as certainty it hasn't earned.
- **Silent contamination of the eval harness.** `sre-agent eval` exists specifically to
  test the agent's *cold* reasoning against ground truth — that's how it caught the
  `scheduling` vs `dependency` bug (see README). If eval runs could recall other eval
  runs (including earlier runs of the exact same staged incident), passing would start
  to mean "recognized a rerun," not "diagnosed correctly." That must not happen.

So the guiding rule for every design choice below: **memory augments the LLM's
reasoning at exactly one step (weighing hypotheses), is never allowed to touch a
confidence score mechanically, and is invisible to two things that must stay
memory-blind: evidence-gathering, and the eval harness.**

## Design decisions

### 1. What gets recalled — plain SQL, not a vector store

Match on **namespace + exact workload name** only, for v1. Both are known from
`IncidentContext` before a single tool runs — no need to wait for evidence or for a
category (`category` isn't known until *after* `hypothesize`, which is the very step
memory is meant to inform, so it can't be a match key). A vector/embedding-based
semantic retrieval is explicitly not worth it yet: the whole `runs` table is a handful
of rows. Revisit if incident volume ever grows into the hundreds — note it here as
future work, don't build it now.

**Excluded from the recall pool: `mode == "eval"`.** Eval incidents are synthetic
fixtures staged by the harness, not organic incident history, and including them
would let an eval run "recognize" an earlier eval run of the same scenario. Excluding
them is also what keeps the harness meaningful as a cold-reasoning regression test —
see decision 4.

### 2. Where it plugs into the pipeline — a new deterministic `recall` node

```
Alertmanager ─▶ listener ─▶ ...
      CLI ──────────────────┴▶ gather ─▶ recall ─▶ correlate ─▶ hypothesize ─▶ rank ─▶ propose
                                  ▲            │                     ▲            ▲
                          fresh evidence   plain SQL,           sees prior     sees prior
                          (memory-blind)   no LLM call          incidents      incidents
```

`recall` sits **after `gather`, before `correlate`** — deliberately after fresh
evidence is already in hand, so the transcript reads "investigate first, consult
memory second," never the other way around. Like `rank`, it's plain Python (one
`history_store` query + a formatting function), no LLM call, its own OTel span — same
pattern as the existing deterministic step, not a new kind of thing.

**`gather` never sees it.** Evidence-gathering stays exactly as memory-blind as it is
today — the system prompt's "ground every claim in a tool result; never invent cluster
state" would be directly undermined if the model could jump straight to "last time it
was X" instead of checking. This is a deliberate v1 boundary, not an oversight — revisit
only if it's proven safe in practice.

**`hypothesize` and `propose` both see it.** `hypothesize` is where competing causes
get weighed — the natural place for "have we seen this before" to matter.  `propose`
also needs it: a remediation that was already applied-and-approved by a human before is
worth citing in the rationale. Both `analyze()` calls build their `ask` text fresh per
call (see `_analyze_call` in `graph.py` — only `system`/`tools` are cached; the ask is
appended per call), so threading a data-dependent memory digest into both is cheap and
does **not** break the Phase-4 cache-sharing that keeps cost down.

### 3. Never a confidence modifier — text in, text out

The recall step produces **text for the model to read**, never a number the code
adds to `confidence_score`. No "if a similar past incident had the same root cause,
+0.1 confidence." That kind of mechanical reinforcement is exactly the echo-chamber
risk called out above. The model is free to weigh a strong precedent heavily in its own
reasoning — visibly, in its stated rationale — but the code never does that weighing
for it.

### 4. Eval mode stays fully memory-blind — both directions

Two separate rules, both required:
- Eval runs are **excluded from the recall pool** (decision 1) — they can't be recalled.
- Eval runs **never call `recall` in the first place** — the `recall` node checks
  `state["incident"]` mode/origin (threaded through from `investigate()`) and returns
  an empty digest immediately when the caller is `eval_cmd`. Belt and suspenders: even
  if a future bug let an eval row into the pool, eval investigations still wouldn't
  look at it.

This keeps `sre-agent eval` meaning exactly what it means today: does the agent
diagnose this correctly *from evidence alone*. Memory must never become a way to pass
a regression test without actually reasoning.

### 5. Honest outcome labels, not a bare list of past guesses

Each recalled incident is shown with a provenance label so the model — and a human
reading `-v` output — can judge how much to trust it, not just what it said:

| mode / approval_status | Label shown |
|---|---|
| `execute` + `approved_applied` | "applied and approved by a human — this fix was actually used" |
| `execute` + `rejected` | "proposed, but a human rejected this fix" |
| `execute` + `blocked` / `dry_run_failed` / `apply_failed` | "proposed, but this fix failed the safety gate" |
| `propose` | "proposed only — outcome unknown, never applied" |

No row is hidden for having a bad outcome — a rejected or failed fix is *useful*
memory ("don't propose that again without new evidence"), maybe more useful than a
quiet success. Hiding it would be its own kind of dishonesty.

## Components

### 1. `history_store.py` — new query function

```python
def find_related_runs(
    namespace: str, workload: str | None, *, limit: int = 3, exclude_run_id: str | None = None
) -> list[sqlite3.Row]:
    """Most recent non-eval runs for this namespace(+workload), for the agent's own
    recall step. Excludes mode='eval' — see docs/memory-plan.md decision 1."""
```
Returns most-recent-first, `mode != 'eval'`, matching `namespace` and (if `workload`
is not None) `workload`. If `workload` is None, match `namespace` alone — matches the
existing `to_incident_context()` behavior of leaving `workload` unset for pod-level
alerts.

### 2. `agent/schemas.py` — new model + state/result fields

```python
class PriorIncident(BaseModel):
    """One past run surfaced to the agent by the recall step — see memory-plan.md."""
    run_id: str
    when: str  # started_at
    category: str | None
    confidence_score: float | None
    root_cause: str
    remediation_command: str
    outcome_label: str  # one of the table in memory-plan.md decision 5
```
- `AgentState` gains `prior_incidents: list[PriorIncident]`.
- `RunResult` gains `prior_incidents: list[PriorIncident] = Field(default_factory=list)`
  — so what memory a run actually saw is itself persisted, auditable later, not just
  used-and-discarded. Mirrors how `correlation`/`hypotheses` already round-trip.

### 3. `agent/graph.py` — the `recall` node

```python
def recall(state: AgentState) -> dict:
    with _tracer.start_as_current_span("agent.recall"):
        if state["incident"].skip_recall:  # eval runs set this — decision 4
            return {"prior_incidents": []}
        rows = history_store.find_related_runs(
            state["incident"].namespace, state["incident"].workload, limit=3
        )
        prior = [_row_to_prior_incident(r) for r in rows]
        if console and prior:
            say(f"\n[bold cyan]▶ recall[/] — {len(prior)} related past incident(s)")
            for p in prior:
                say(f"  [dim]{p.when}[/] {p.category}: {p.root_cause[:80]} [dim]({p.outcome_label})[/]")
        return {"prior_incidents": prior}
```
Add to the graph: `g.add_edge("gather", "recall")`, `g.add_edge("recall", "correlate")`
(replacing the old direct `gather -> correlate` edge).

`_render_memory_digest(prior: list[PriorIncident]) -> str` — a pure formatting
function (easy to unit test without a DB or an LLM) that turns the list into a short
block, or `""` when empty (no filler text like "no prior incidents found" — silence
when there's nothing relevant). Append it to both `HYPOTHESIZE_INSTRUCTION` and the
`analysis` text built in `propose()`, the same way `CATEGORY_GUIDE` is already
appended to instruction strings.

### 4. `agent/schemas.py` — `IncidentContext.skip_recall`

Add `skip_recall: bool = False` to `IncidentContext`. `evals.py`'s `run(inc.context)`
call constructs eval incidents already — no plumbing change needed there beyond
setting this flag true when building eval `IncidentContext`s (or simpler: derive it
from a `mode` the eval runner already knows and passes down — read `evals.py` first to
pick whichever is less invasive to the existing call shape).

### 5. `agent/prompts.py`

New `_render_memory_digest()` output gets appended (not baked into the static
`HYPOTHESIZE_INSTRUCTION`/`REPORT_INSTRUCTION` constants, since it's per-run data) at
the call sites in `graph.py`. No change to `SYSTEM_PROMPT` — memory is not a standing
instruction, it's per-run context, exactly like the evidence transcript itself.

### 6. `history_store.py` — persist what memory a run saw

Add a `prior_incidents_json` column (same `ALTER ... ADD COLUMN` idempotent-migration
pattern used for `triggered_by` in Phase 9). `save_run()` gains the param, dumps
`[p.model_dump() for p in result.prior_incidents]`. This is what makes the feature
auditable later — "why did it say this?" can include "here's what it remembered."

## Order of work

1. `history_store.find_related_runs()` + `tests/test_history_store.py` additions
   (matching, eval exclusion, limit, ordering, workload=None case). Pure, free.
2. `PriorIncident` schema + `_render_memory_digest()` pure formatter +
   `tests/test_agent.py` (or a new `tests/test_memory.py`) additions — feed it
   synthetic `PriorIncident` lists, assert the rendered text and the empty-string case.
   Pure, free.
3. `IncidentContext.skip_recall`, wire `evals.py` to set it, confirm with a test that an
   eval-mode incident produces an empty `prior_incidents` list even if matching rows
   exist in a seeded test DB.
4. Wire the `recall` node into `graph.py`'s `StateGraph`; thread the digest into
   `hypothesize`'s and `propose`'s `analyze()` calls.
5. `prior_incidents_json` column + `save_run()` param (idempotent migration, same
   pattern as Phase 9's `triggered_by`).
6. **One live verification, not a full eval run:** stage the same incident twice in a
   row (e.g. crash_loop) with `investigate -v` between them. First run: confirm the
   console shows no `recall` hits (nothing in history yet for that workload). Second
   run: confirm `recall` surfaces the first run, confirm the hypothesize-stage
   reasoning visibly references it, confirm the eval-mode exclusion by also running
   `sre-agent eval -i crash_loop -y` in between and checking neither of the two manual
   runs recalls the eval run, nor does the eval run recall anything. Budget: ~3 paid
   runs (~$0.45).
7. **Confirm the eval harness is unaffected:** re-run the full `sre-agent eval` suite
   (all 3 incidents) and confirm scores/behavior match the pre-memory baseline in
   README's "Incidents proven live" table — this is the regression check that proves
   decision 4 actually holds.
8. *(Optional, last, only if it's cheap once the above works):* surface
   `prior_incidents` on the dashboard detail page as a "Similar past incidents" card,
   reusing the existing `_scard()` pattern in `dashboard.py`. Not required for
   correctness — the data will already be sitting in `prior_incidents_json` waiting for
   it, so this can always be a fast follow-up later instead of blocking the phase.

## Live calibration finding (2026-07-26) — echo-chamber stress test

Built and shipped, then stress-tested before being trusted: staged one incident once
and ran `investigate -v` against it **6 times in a row without reverting** (the
realistic risk case — a persisting issue investigated repeatedly, memory accumulating
echoes of its own past conclusions). Full sequence:

| # | confidence | category | prior shown |
|---|---|---|---|
| 1 | 0.85 | workload | 0 |
| 2 | 0.90 | workload | 1 |
| 3 | 0.90 | workload | 2 |
| 4 | 0.90 | workload | 3 |
| 5 | 0.92 | rollout  | 3 |
| 6 | 0.85 | workload | 3 |

**No runaway confidence drift.** It plateaued around 0.90 and dropped back to 0.85 on
the last run — not the monotonic climb toward 1.0 that unbounded reinforcement would
produce. Evidence-gathering also stayed rigorous every time (3-4 fresh tool calls each
run, no shrinkage) — `gather` never sees memory, and this confirms it behaves that way
in practice, not just in the code path.

**The category flip at run 5 (workload → rollout) is not a memory artifact.** All 3
priors shown to run 5 were labeled `workload`, yet the model chose `rollout` anyway —
proof it isn't just copying memory's label. This specific incident's own ground truth
in `evals.py` already accepts `{"workload", "config", "rollout"}` as correct — the
ambiguity is inherent to "a rollout that broke the pod spec," not something memory
introduced.

**The real finding: false corroboration in the model's own language, not the number.**
By runs 5-6, the RCA text described the 3 recalled priors as *"multiple independent
confirmations"* / *"three prior incident investigations independently pinned..."* —
but those 3 "prior incidents" are the same staged fault, re-investigated by me within
about an hour, not three separate real-world occurrences. The digest correctly
timestamped each entry, but nothing told the model that near-identical, closely-timed
entries are one event observed repeatedly, not independent evidence. This is exactly
the risk decision 3 (memory-plan.md, above) was written to guard against — it showed
up in the narrative more than in the confidence score, but it's real.

**Mitigation shipped the same session:** `render_memory_digest()` now explicitly
instructs the model that near-identical, closely-timed priors are ONE underlying event
observed repeatedly, not independent confirmation, and that the confidence score must
be justified by today's fresh evidence alone — repetition of a past conclusion is not
itself new evidence. Test added (`test_anti_false_corroboration_language_present` in
`tests/test_memory.py`) asserting this language is present in the digest.

**Re-verified live (2026-07-31) — the fix worked, more strongly than expected.**
Re-ran the same experiment (stage once, `investigate -v` repeatedly without reverting)
with the hardened digest, against a history that already had 3 near-duplicate priors
queued up from the round above:

| # | confidence | band | category | prior shown |
|---|---|---|---|---|
| 7 | 0.75 | medium | rollout | 3 |
| 8 | 0.78 | medium | rollout | 3 |
| 9 | 0.72 | medium | rollout | 3 |

Two changes, both in the right direction:
1. **The false-corroboration language is gone, not just softened.** Every RCA's
   Evidence and root-cause sections were checked across all 3 runs — none of them
   mention the recalled priors at all. Before the fix, the same 3-priors condition
   produced explicit citations like *"three prior incident investigations independently
   pinned..."*. After the fix, the model still received the digest every time (`recall`
   fired with 3 hits each run) but stopped treating those priors as citable evidence —
   every Evidence list is grounded only in that run's own fresh tool output.
2. **Confidence band dropped from high to medium** (0.85-0.92 before → 0.72-0.78
   after) with the identical 3 priors shown each time. This is the more telling
   result: before the fix, repeated priors correlated with confidence climbing into
   "high"; after, the model no longer lets repetition nudge it there. A quantitative
   confirmation, not just a linguistic one.

The category settling on `rollout` in all 3 hardened runs (vs. a workload/rollout mix
before) is not read as a memory artifact — this incident's own eval ground truth
already accepts both categories as correct, the digest itself still had a mix of
workload- and rollout-labeled priors, and nothing in the instruction targets category
choice. Treated as ordinary variance on a genuinely boundary-line incident.

**Optimum-strategy takeaway** (answers the "ignore memory vs. over-trust it" question
directly): the risk was never really in the score — a single number is easy to keep
bounded and this test showed it stayed bounded even before the fix. The risk is in the
*narrative epistemics*: whether the model correctly distinguishes "I've seen this
exact thing before" (weak evidence, especially when the priors are close together in
time and describe the identical fault) from "multiple different incidents converged on
this cause" (strong evidence). Getting that distinction right in the prompt is the
actual lever — not a numeric cap or a mechanical decay function in code, which would
be exactly the kind of code-side confidence modifier decision 3 rules out.

## Second calibration finding (2026-07-31) — the fix over-corrected

Round 1's fix was strong enough to raise a new question: had it swung too far, so the
model now ignores memory even when it *should* count? Tested directly by creating a
genuinely different kind of evidence — not another unverified guess, but a **confirmed
real-world outcome**.

Ran `investigate -x` (auto-approved) against a staged incident: the approval gate ran
for real (dry-run → confirm → apply → verify) and produced the project's first-ever
`execute`/`approved_applied` history row. Re-staged the identical fault and ran
`investigate` again. The digest that reached the model had this as its most recent,
most prominent entry:

> `outcome: "applied and approved by a human — this fix was actually used"`

**The model didn't use it.** Every Evidence bullet, every line of the remediation
rationale, was grounded only in that run's fresh tool output. The one prior incident
that should have mattered most — a human-confirmed fix for the exact same fault —
went completely uncited, indistinguishable from the unverified repeats the Round 1 fix
was designed to suppress. The instruction ("repetition of the same past conclusion is
not itself new evidence") had generalized further than intended: the model applied
"don't trust repetition" to *all* memory, not just repetition of its own unverified
guesses.

**Fix:** `render_memory_digest()` now explicitly carves this case out. Repetition of
an entry that was only ever *proposed* (never applied) still doesn't count as new
evidence. But an entry whose outcome says a human already approved and applied that
exact fix is named as categorically different — "a confirmed real-world outcome, not
a repeated guess" — and the model is told to use it: cite it, and let it strengthen
the remediation rationale (and confidence, if today's evidence genuinely matches).

**Re-verified live, same session.** With the same mixed digest (2 unverified priors +
1 approved_applied prior) reaching a fresh investigation, the RCA now reads:

> Evidence: *"Prior incident 2026-07-31T17:08:23 (rev 24, same ReplicaSet ..., same
> crash signature) had its rollback fix ... **applied and approved by a human**,
> confirming rollback is an effective, **previously-validated remedy** for this exact
> fault pattern"*
>
> Remediation rationale: *"This mirrors a prior incident with the same crash signature
> where an equivalent rollback was reviewed and approved by a human."*

Both citations point *only* at the approved_applied entry. Neither of the other two
unverified priors appears anywhere in the report — the false-corroboration suppression
from Round 1 held. Confidence came back up to 0.88 (high band), but this time with an
explicit, sound justification (a validated remedy plus fresh evidence) rather than bare
repetition count — the correct kind of confidence increase, not the kind Round 1 was
built to prevent.

**Revised optimum-strategy takeaway:** getting memory calibration right isn't one fix,
it's a balance that has to be checked from both directions. Round 1 alone would have
shipped a model that's *safe but wasteful* — it throws away the single most valuable
kind of memory this system can produce (a human-validated outcome) along with the
kind it should discard. The full picture only appeared by testing both failure modes
explicitly: stage the same fault repeatedly (tests over-trust), then stage it once
more against a validated prior (tests under-use). Shipping after only the first test
would have looked complete and been quietly wrong.

## Explicitly out of scope (this phase)

- Vector/embedding-based semantic retrieval — plain SQL match is enough at this data
  volume (see decision 1).
- Automatic confidence blending/boosting from memory — text only, never a score
  modifier (decision 3).
- Feeding memory into `gather` (evidence-gathering) or into eval-mode runs (decision 4).
- Cross-namespace or cross-cluster memory.
- An active feedback loop that re-checks whether an applied fix actually held over time
  (e.g., re-investigating a workload a day later to confirm resolution) — the outcome
  labels in decision 5 use only what's already recorded (`approval_status`), not a new
  monitoring mechanism. **UPDATE (2026-07-31): this stopped being purely theoretical
  once the second calibration fix told the model to trust `"applied and approved by a
  human"` as legitimate strengthening evidence — that label can be true even when the
  underlying fix silently didn't work.** Scoped and **implemented (2026-08-02)** as its
  own phase in `docs/verification-plan.md` (Phase 11): a bounded synchronous poll of
  `get_workload_status` right after apply, not the re-investigate-later feedback loop
  described above — narrower, but closes the actual gap this note was flagging. Live
  end-to-end: a real applied fix got correctly labeled "verified healthy... not a
  permanent guarantee," and when the same fault later recurred despite that label, the
  next investigation independently reasoned the earlier fix hadn't held — the honest
  wording did its job without even needing the directive steering language to fire.
- Dashboard "Similar past incidents" UI — data-ready, UI deferred (step 8 above).

## Open questions — defaults chosen, confirm before/while building

- **Recall limit of 3, no time-based lookback window.** Fine at current data volume;
  revisit if the table grows and old incidents start crowding out recent, more relevant
  ones.
- **Match key is exact workload name only** (no category, no fuzzy alert-text
  matching). Simple and safe, but means a `redis-cart` incident won't surface a prior
  `cartservice` incident even if they're part of the same cascade. Expand the match key
  later only if this proves too narrow in practice — don't over-build it up front.
- Should `gather` ever see memory (e.g., "last time, logs --previous found it fast," as
  a pure efficiency hint, never a diagnosis shortcut)? Current answer is no — flagged in
  decision 2 as a deliberate boundary, not a permanent one.
