# Phase 11: applied-fix verification — build plan

**Status: scoped, not yet implemented.** Written so a fresh session (no prior
conversation context) can pick this up and build it directly. Read this file top to
bottom before writing any code.

## What this is

Today, `approval_status == "approved_applied"` and `resolved == True` mean exactly one
thing: **the mutating `kubectl` command the agent ran returned exit code 0.** Nothing
checks that the underlying incident actually went away. Traced precisely in
`cli.py`'s `_approval_gate()`:

```python
for args in decision.readonly:
    rc, out, err = run_kubectl(args)
    ...
    # rc is captured and printed for a human to read — never checked
console.print("\n[green]✓ Applied.[/] Re-run `sre-agent status` to confirm recovery.")
return "approved_applied"
```

The function's own closing message — *"re-run `sre-agent status` to confirm
recovery"* — is an admission that the system doesn't do this itself. And in every
remediation command generated live in this project so far, `decision.readonly` was
empty anyway (it only fills up if the model's own command string bundles a `&&`
verification step, which none have), so that loop usually runs nothing at all.

This was a known, deliberately out-of-scope gap when Phase 10 (memory) shipped — see
`docs/memory-plan.md`'s "Explicitly out of scope" list. It stopped being a merely
theoretical gap the moment Phase 10's second calibration fix told the model to treat
`"applied and approved by a human"` as **legitimate strengthening evidence**. If that
label can be true even when the fix silently didn't work, the memory feature now has a
path to manufacture false confidence one level up from the bug it was built to prevent
— "a human approved this" standing in for "this is known to work," when today it only
ever meant "the command didn't error."

## Why this is worth getting right, not just adding a check mark

The fix has to answer three questions honestly, not just one:
1. **Did the apply command succeed?** (already answered today, correctly)
2. **Did the specific workload that was broken become healthy?** (the actual gap)
3. **How should a false positive here — verification says healthy but isn't — be
   avoided?** (the design has to be conservative: verify_resolved should default to
   "don't know" rather than guess, whenever the check is ambiguous or inapplicable)

## Design decisions

### 1. What "healthy" means — reuse `get_workload_status`, don't invent new parsing

The existing read-only tool already returns exactly the fields needed
(`tools/schemas.py`):

```python
class DeploymentStatus(BaseModel):
    name: str
    namespace: str
    desired: int
    ready: int
    available: int
    updated: int
    unavailable: int
    progressing: bool | None = None
    available_condition: bool | None = None
```

A pure, testable check:

```python
def _is_workload_healthy(status: NamespaceWorkloadStatus, workload: str) -> bool | None:
    dep = next((d for d in status.deployments if d.name == workload), None)
    if dep is None:
        return None  # workload not found in this namespace -- can't verify
    return dep.ready == dep.desired and dep.unavailable == 0
```

`ready == desired and unavailable == 0` is exactly the signal this session's own live
tests already relied on by hand — every crash_loop incident tested this session showed
`unavailable: 1` while broken (an old healthy pod masking a new crash-looping one) and
`unavailable: 0` once genuinely fixed. This is not a new heuristic; it's what a human
running `kubectl get deploy` already looks at.

**Deliberately not attempted in v1**: verifying downstream/dependent services also
recovered (e.g. after fixing `redis-cart`, also checking `cartservice`/`frontend`).
That needs the correlation's `dependency_chain` and multiplies the surface area. Scope
to the single named `workload` first.

### 2. When workload is `None` (e.g. a cascade incident with no specific hint) —
skip, don't guess

If `IncidentContext.workload` is unset, there's no single deployment name to check
against. Recording a guess here (e.g. "check the whole namespace looks fine") would be
exactly the kind of unearned confidence this fix exists to remove. Result:
`verification_status = "not_checked"`, with a clear reason in `verification_detail`.

### 3. Bounded synchronous poll, not a background job — and a STABILITY WINDOW, not a
single snapshot

Verification happens inline, right after `_approval_gate()` returns
`"approved_applied"` and before `investigate()` returns — poll `get_workload_status`
every `verify_poll_interval_s` (default 5s) up to `verify_timeout_s` (default 90s).

**Revised after review: do not stop at the first healthy reading.** A pod can pass a
single check and then genuinely fail again seconds later — a slow memory leak, a
probe that passes once and then starts failing, a dependency that's flaky rather than
fully down. Kubernetes' own probes don't trust one success either (`successThreshold`
exists for exactly this reason), and this check shouldn't either. Require
`verify_stability_checks` (default 3) **consecutive** healthy polls — i.e. the
workload must stay healthy across at least `verify_stability_checks *
verify_poll_interval_s` (≈15s by default) before declaring `"confirmed_healthy"`. Any
unhealthy reading during that window resets the consecutive-success counter to zero,
not just to "still checking."

This fits the existing synchronous CLI flow (which already blocks for dry-run + human
confirmation) without new scheduling infrastructure. The cost is real: up to 90 extra
seconds on an `-x` run. That's a deliberate, disclosed tradeoff — print
`"Verifying recovery… (up to 90s)"` so it never looks like a hang. New `Settings`
fields make these numbers (`verify_after_apply: bool = True`, `verify_timeout_s: int =
90`, `verify_poll_interval_s: int = 5`, `verify_stability_checks: int = 3`)
overridable, including turning verification off entirely if a user wants the old
fire-and-forget behavior back.

**Equally important: what this does NOT prove.** Even with a stability window,
verification is a point-in-time observation, not a permanent guarantee — a node
eviction an hour later is a different failure with nothing to do with whether this fix
worked. The outcome label (decision 6) must say so explicitly rather than imply
certainty it doesn't have: *"verified healthy immediately after applying"*, not
*"confirmed to have resolved the issue"* — the second phrasing reads as more
permanent than any bounded check can honestly claim.

Verification uses the agent's own **read-only** kubeconfig/tool machinery (the same
`get_workload_status()` call already used during investigation), not the operator's
kubeconfig used for the apply itself — verification is pure observation, so it belongs
on the safe side of the identity boundary the whole project is built around.

### 4. A three-way outcome, not a boolean — and the negative case must be loud

`verification_status` (new column, `TEXT`, nullable):
- `"confirmed_healthy"` — ready == desired and unavailable == 0 within the timeout.
- `"still_unhealthy"` — timeout elapsed without recovery. **This is important
  negative evidence** — a fix that was tried and demonstrably did not resolve the
  symptom. Per the same "never hide a bad outcome" principle already applied to
  rejected/blocked remediations (Phase 10 decision 5), this must be surfaced plainly,
  not softened.
- `"not_checked"` — no workload to check, or verification disabled via Settings.
- `NULL` — pre-existing rows from before this phase shipped (migration default).

`verification_detail` (new column, `TEXT`) — one line, e.g. `"emailservice: ready=1/1,
unavailable=0 after 12s"` or `"emailservice: still ready=0/1, unavailable=1 after 90s
(timed out)"`.

### 5. `resolved` changes meaning for execute-mode rows — a real, deliberate fix

Today: `resolved = True if approval_status == "approved_applied" else None` — this
conflates "the command didn't error" with "the problem is gone." After this phase,
for `mode == "execute"` rows: `resolved = (verification_status == "confirmed_healthy")`
if verification ran, else `None` (unchanged, honest "we don't know"). **`eval`-mode
rows are untouched** — their `resolved` field means something entirely different
already (`incident_passed(checks)`, i.e. "the RCA was scored correct" — see
`evals.py`), computed by a separate code path this phase doesn't touch. Confirm this
doesn't shift the dashboard's "Eval pass rate" tile, which already filters to
`mode == "eval"` only.

### 6. The memory outcome-label taxonomy grows from two tiers to four

`graph.py`'s `_OUTCOME_LABELS` / `_row_to_prior_incident()` currently only look at
`mode`/`approval_status`. They need to also look at `verification_status` when
`approval_status == "approved_applied"`:

| approval_status | verification_status | outcome_label |
|---|---|---|
| `approved_applied` | `confirmed_healthy` | "applied and approved by a human, verified healthy immediately after applying" — deliberately NOT "confirmed to have resolved the issue" (permanent-sounding); see decision 3's stability-window note on what a bounded check can and can't promise |
| `approved_applied` | `still_unhealthy` | "applied and approved by a human, but verification found the issue did NOT resolve — do not propose this same fix again without new evidence" |
| `approved_applied` | `not_checked` / `NULL` | "applied and approved by a human (not independently verified whether it resolved the issue)" |
| `rejected` / `blocked` / `dry_run_failed` / `apply_failed` | — | unchanged from today |
| (propose mode) | — | unchanged: "proposed only — outcome unknown, never applied" |

The `still_unhealthy` row is the new, important one — it turns "a human approved this"
from an unconditionally positive signal into what it should have been all along: a
fact whose *polarity* depends on what was actually observed afterward.

### 7. `render_memory_digest()`'s instruction needs a revision pass — and it must
DIRECT behavior for a failed fix, not just describe it

The current carve-out language (Phase 10, second fix) says an "applied and approved by
a human" entry is *always* "a confirmed real-world outcome." That's no longer quite
right once there are three shades of "applied." The instruction needs to distinguish:
- an entry confirmed to have worked → cite it, let it strengthen the remediation and
  (if the cause matches) confidence — exactly like today's carve-out, with the
  point-in-time honesty from decision 3 folded in.
- an entry applied-but-unverified → weaker than a confirmed one, still worth citing as
  "a human already tried this," but not with the same certainty.
- an entry confirmed to have **failed** → this is the case a review of this plan
  caught as under-specified. A label alone is not a guarantee the model won't just
  propose the identical command again — that has to be a direct instruction, not
  descriptive flavor text. The digest must explicitly say: **if today's evidence
  points to the same root cause as an entry whose outcome was `still_unhealthy`, do
  not propose that same fix again without explaining why this time is different.**
  Either propose a genuinely different remediation, or say plainly in the rationale
  that the obvious fix was already tried and failed, and flag that deeper
  investigation (or human escalation) is warranted rather than repeating it — and let
  that push confidence in the OLD hypothesis down, not up.

Exact wording is an implementation-time decision (follow the same sentence-boundary
formatting convention as the rest of `prompts.py` — see the `\n`-per-sentence style
already used throughout), but the three-way distinction — and specifically the
DIRECTIVE (not just descriptive) language for the failed case — must survive into the
prompt text, or this phase only fixes the data and not the behavior it's meant to fix.
This needs its own unit test in `tests/test_memory.py`, mirroring
`test_mixed_digest_keeps_both_instructions_and_labels_entries_correctly`: build a
digest containing a `still_unhealthy` entry and assert the "do not propose this fix
again" instruction is present in the rendered text.

## Failure containment — why a failed fix can't spiral into a retry storm

This plan doesn't add any new autonomy, so the failure modes that would make a
verification failure dangerous (the agent repeatedly re-applying a broken fix, or
alert-triggered runs firing in a tight loop) are already structurally prevented by
mechanisms built in earlier phases — worth stating explicitly, since it's easy to read
"the agent detects its fix failed" and worry it implies some kind of automatic retry:

- **No auto-retry exists, full stop.** Every `execute` run — success or failure —
  requires a fresh `typer.confirm()` from a human. A `still_unhealthy` result ends the
  current `investigate()` call; nothing loops back and tries again automatically.
- **Alert-triggered re-investigation is already throttled independently of this
  phase.** Phase 9's per-`(namespace, alertname)` 30-minute cooldown and daily run cap
  (`alerts.py`) govern how soon the listener can investigate the same alert again,
  regardless of whether the previous attempt's fix worked. A still-firing alert after
  a failed fix will eventually trigger a fresh investigation once the cooldown
  expires — not sooner, and not in a loop.
- **When that fresh investigation happens, decision 7's directive language is what
  prevents it from being a blind repeat** of the same failed action — this is the
  actual point of recording `still_unhealthy` at all, not just an audit trail.
- **The human stays in the loop for the outcome, too.** `_approval_gate()`'s console
  output should not print the current unconditional `"✓ Applied."` when verification
  is still running or comes back negative — see the open question below, now resolved:
  print `"✓ Applied, verifying recovery…"` first, then either `"✓ Recovery confirmed."`
  or a clearly-marked `"⚠ Applied, but recovery was NOT confirmed — investigate
  again."` This closes the loop for the human operator, not just for the agent's own
  memory.

## Components

1. **`tools/schemas.py`** — no changes; `DeploymentStatus`/`NamespaceWorkloadStatus`
   already have everything needed.
2. **`remediation.py`** — new pure function `_is_workload_healthy()` (shown above).
   Pure, unit-testable with synthetic `NamespaceWorkloadStatus` objects, no cluster.
3. **`config.py`** — `verify_after_apply: bool = True`, `verify_timeout_s: int = 90`,
   `verify_poll_interval_s: int = 5`.
4. **`history_store.py`** — `verification_status TEXT` + `verification_detail TEXT`
   columns (idempotent `ALTER` migration, same pattern as `triggered_by` /
   `prior_incidents_json`). `save_run()` gains both as params.
5. **`cli.py`** — in `investigate()`'s execute branch, after `_approval_gate()` returns
   `"approved_applied"`: call a new poll-and-check step (using the read-only tool
   client already loaded for the investigation, not the operator's kubeconfig), print
   progress, compute `verification_status`/`verification_detail`, and use them to set
   `resolved` per decision 5 above.
6. **`agent/graph.py`** — extend `_OUTCOME_LABELS`/`_row_to_prior_incident()` per the
   four-row table in decision 6.
7. **`agent/prompts.py`** — revise `render_memory_digest()`'s instruction per decision
   7. Keep the existing anti-corroboration and confirmed-outcome carve-out language
   intact; add the third tier alongside them, don't replace what's already working.

## Order of work

1. `_is_workload_healthy()` in `remediation.py` + unit tests (synthetic
   `DeploymentStatus` combinations: ready==desired/unavailable==0 → True; ready<desired
   → False; unavailable>0 → False; workload name not found → None). Pure, free.
2. The poll/stability-window state machine as its own pure, injectable-clock function
   (e.g. takes a `check_fn` + `sleep_fn` so tests don't need to sleep for real) +
   tests covering: healthy immediately and stays healthy → `confirmed_healthy` after
   `verify_stability_checks` calls, not 1; healthy once then unhealthy again before
   the window completes → counter resets, does NOT short-circuit to
   `confirmed_healthy` (this is the direct regression test for the flapping scenario
   this plan was revised for); never healthy → `still_unhealthy` after the timeout.
   Pure, free.
3. `history_store` columns + migration + `save_run()` params + tests (mirroring the
   existing `triggered_by`/`prior_incidents_json` test patterns). Pure, free.
4. Wire the poll-and-check step into `cli.py`'s execute branch; update the `resolved`
   computation; replace the unconditional "✓ Applied." with the three outcome-specific
   messages from "Failure containment" above.
5. Extend `_OUTCOME_LABELS` in `graph.py` for the four-row taxonomy (point-in-time
   wording per decision 3); update `render_memory_digest()`'s instruction text for the
   third tier, including the directive "don't repeat a failed fix" language (decision
   7) + its unit test.
6. **Free verification of the negative path — don't wait for the model to produce a
   bad fix live (unpredictable, wasteful).** Stage a real incident, then call the new
   poll-and-check function directly against the still-broken workload (bypassing
   propose/apply entirely) to confirm it correctly reports `"still_unhealthy"` after
   timing out. This proves the negative path deterministically and for free.
7. **One live end-to-end verification of the positive path**: `investigate -x`
   (auto-approved) on a real crash_loop incident, confirm `verification_status`
   ends up `"confirmed_healthy"` only after multiple consecutive healthy polls (check
   the actual poll count/timing in the console output, not just the final result),
   `resolved` is `True`. Budget: ~$0.15, same as any other single investigation.
8. **Confirm the memory digest actually changes and actively steers** for a fresh
   investigation that recalls this newly-verified row — check the RCA text reflects
   the point-in-time-honest framing (not "permanently confirmed"), and separately, do
   the same check against a `still_unhealthy` prior to confirm the model doesn't
   simply re-propose the identical failed command. Two more live runs, ~$0.15 each.
9. Re-run the full test suite; confirm the dashboard's "Eval pass rate" tile is
   unaffected (decision 5) and the Analytics "approval funnel" continues to render
   correctly with the new column present but unused there for now (surfacing
   verification status in the funnel UI is optional polish, not required for this
   phase — see below).

## Explicitly out of scope (this phase)

- Verifying downstream/dependent services in the correlation's `dependency_chain` —
  scoped to the single named `workload` only (decision 1).
- A background/async verification job that re-checks minutes or hours later — the
  bounded synchronous poll is the v1 design (decision 3).
- Retroactively backfilling `verification_status` for existing `approved_applied` rows
  (e.g. `a1df1c7fb105` from the Phase 10 testing) — those stay honestly labeled as
  "not independently verified," which is what they actually were.
- Dashboard/CLI UI to surface `verification_status` prominently (e.g. a chip on the
  incident card, or folding it into the Analytics approval funnel) — the data will
  exist; a display pass can follow later the same way Phase 10's `prior_incidents`
  display was deferred and then added afterward.
- Verifying `execute`-mode runs that used a *rollback* remediation differently from a
  *scale* or other remediation type — the health check is generic (ready == desired,
  unavailable == 0) and doesn't need to know which kind of fix was applied.

## Open questions — defaults chosen, confirm before/while building

- **90s timeout, 5s poll interval, 3 consecutive stability checks.** Reasonable given
  every rollout observed live this session stabilized well within that window;
  revisit if a real incident class turns out to need longer (e.g. a slow-starting
  workload with a long readiness probe) or a flappier one needs more than 3 checks to
  trust.
- ~~Should a `still_unhealthy` result change the CLI's console output~~ — **resolved**,
  see "Failure containment" above: yes, replace the unconditional "✓ Applied." with an
  outcome-specific message.
- **Exact wording of the three-tier memory instruction** (decision 7) — left to
  implementation time; the shape of the distinction — including the directive
  "don't repeat a fix already marked failed" language — is fixed, the sentences aren't.
