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

### 3. Bounded synchronous poll, not a background job

Verification happens inline, right after `_approval_gate()` returns
`"approved_applied"` and before `investigate()` returns — poll `get_workload_status`
every `verify_poll_interval_s` (default 5s) up to `verify_timeout_s` (default 90s),
stop early the moment the workload reports healthy. This fits the existing synchronous
CLI flow (which already blocks for dry-run + human confirmation) without new
scheduling infrastructure. The cost is real: up to 90 extra seconds on an `-x` run.
That's a deliberate, disclosed tradeoff — print `"Verifying recovery… (up to 90s)"` so
it never looks like a hang. New `Settings` fields make all three numbers
(`verify_after_apply: bool = True`, `verify_timeout_s: int = 90`,
`verify_poll_interval_s: int = 5`) overridable, including turning verification off
entirely if a user wants the old fire-and-forget behavior back.

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
| `approved_applied` | `confirmed_healthy` | "applied and approved by a human, and confirmed to have resolved the issue" |
| `approved_applied` | `still_unhealthy` | "applied and approved by a human, but verification found the issue did NOT resolve" |
| `approved_applied` | `not_checked` / `NULL` | "applied and approved by a human (not independently verified whether it resolved the issue)" |
| `rejected` / `blocked` / `dry_run_failed` / `apply_failed` | — | unchanged from today |
| (propose mode) | — | unchanged: "proposed only — outcome unknown, never applied" |

The `still_unhealthy` row is the new, important one — it turns "a human approved this"
from an unconditionally positive signal into what it should have been all along: a
fact whose *polarity* depends on what was actually observed afterward.

### 7. `render_memory_digest()`'s instruction needs a revision pass, not just new data

The current carve-out language (Phase 10, second fix) says an "applied and approved by
a human" entry is *always* "a confirmed real-world outcome." That's no longer quite
right once there are three shades of "applied." The instruction needs to distinguish:
- an entry confirmed to have worked → cite it, let it strengthen the remediation and
  (if the cause matches) confidence — exactly like today's carve-out.
- an entry applied-but-unverified → weaker than a confirmed one, still worth citing as
  "a human already tried this," but not with the same certainty.
- an entry confirmed to have **failed** → explicitly flag it as a reason to be
  *more* cautious about proposing the same fix again, not neutral information.

Exact wording is an implementation-time decision (follow the same sentence-boundary
formatting convention as the rest of `prompts.py` — see the `\n`-per-sentence style
already used throughout), but the three-way distinction above must survive into the
prompt text, or this phase only fixes the data and not the behavior it's meant to fix.

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
2. `history_store` columns + migration + `save_run()` params + tests (mirroring the
   existing `triggered_by`/`prior_incidents_json` test patterns). Pure, free.
3. Wire the poll-and-check step into `cli.py`'s execute branch; update the `resolved`
   computation. Print the "verifying recovery…" progress message.
4. Extend `_OUTCOME_LABELS` in `graph.py` for the four-row taxonomy; update
   `render_memory_digest()`'s instruction text for the third tier.
5. **Free verification of the negative path — don't wait for the model to produce a
   bad fix live (unpredictable, wasteful).** Stage a real incident, then call the new
   poll-and-check function directly against the still-broken workload (bypassing
   propose/apply entirely) to confirm it correctly reports `"still_unhealthy"` after
   timing out. This proves the negative path deterministically and for free.
6. **One live end-to-end verification of the positive path**: `investigate -x`
   (auto-approved) on a real crash_loop incident, confirm `verification_status`
   ends up `"confirmed_healthy"`, `resolved` is `True`, and the elapsed time reflects
   the poll (a few extra seconds beyond a normal investigation, not the full 90s
   timeout, since recovery should be fast for this incident class). Budget: ~$0.15,
   same as any other single investigation.
7. **Confirm the memory digest actually changes** for a fresh investigation that
   recalls this newly-verified row — check the RCA text reflects the stronger
   "confirmed to have resolved" framing rather than the older, flatter "applied and
   approved by a human" wording. One more live run, ~$0.15.
8. Re-run the full test suite; confirm the dashboard's "Eval pass rate" tile is
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

- **90s timeout, 5s poll interval.** Reasonable given every rollout observed live this
  session stabilized well within that window; revisit if a real incident class turns
  out to need longer (e.g. a slow-starting workload with a long readiness probe).
- **Should a `still_unhealthy` verification result change anything about the CLI's own
  console output at the time** (e.g. print a red warning instead of the current
  unconditional "✓ Applied")? Not decided — leans yes, since printing "✓ Applied" when
  verification then immediately fails would itself be a small honesty gap of the same
  shape as the one this phase fixes.
- **Exact wording of the three-tier memory instruction** (decision 7) — left to
  implementation time; the shape of the distinction is fixed, the sentences aren't.
