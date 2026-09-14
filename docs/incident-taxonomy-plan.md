# Incident taxonomy — planning note

**Status: Batch 1 + incident #11 complete.** All 9 steps of the eval-expansion plan below
are done — 11 incidents are built and recorded, 9 of 11 passing the remediation gate (2
honest, documented gaps, not bugs — see "Step 5-9 — done"). See "Next steps" at the
bottom for what's actually still open (it's Batch 2, not anything in the numbered plan).

## The plan this feeds into

1. List the 10+ incidents on paper first — $0 *(this file)*
2. Check what's missing for each one (tools / RBAC) — $0
3. Build the missing tools + RBAC grants — $0
4. Write the `Incident()` entries in `evals.py` for all of them — $0
5. Record ONE real run per incident, on a cheap model (Haiku) — 💰 tiny (~$0.01–0.05 each)
6. Build a "replay" mode so checks can be tuned against the saved recording, not the live API — $0
7. Tune `must_include`/`expect_categories` checks against the saved recordings — $0, unlimited iterations
8. Check the guardrail (`validate_remediation`) against each recorded remediation command — $0
9. Final validation — run each incident once on the real target model — 💰 ~$2–4 total

The point: pay for the expensive model once per incident (step 5), reuse that recording
for free everywhere else, pay for the good model again only once at the very end (step 9).

## Batch 1 — uses services that already exist in `boutique`, no cluster setup needed

Existing incidents (already in `evals.py`): `image_pull`, `crash_loop`, `cascade`.

| # | Name | What breaks | How to break it | How to fix it |
|---|------|-------------|------------------|----------------|
| 4 | `oom_killed` | Pod gets killed for using too much memory | Set `recommendationservice`'s memory limit too low | Put the limit back |
| 5 | `cpu_throttled` | Service works but is very slow | Set `checkoutservice`'s CPU limit too low | Put the limit back |
| 6 | `unschedulable` | New pod never starts | Ask `paymentservice` for way more CPU than any node has | Undo the resource request change |
| 7 | `bad_config` | Pod won't even start | Point `cartservice` at a Secret that doesn't exist | Remove the bad reference |
| 8 | `network_blocked` | Two services can't talk, but both look "healthy" | Add a NetworkPolicy blocking `checkoutservice` → `paymentservice` | Delete that policy |
| 9 | `wrong_service_selector` | Traffic goes nowhere, pods look fine | Break `frontend`'s Service so it stops matching its own pods | Fix the selector back |
| 10 | `node_down` | Everything on one node breaks at once | Cordon and drain one of the 2 worker nodes | Uncordon it |

That's 10 total (3 existing + 7 new), all buildable against the current cluster with no
new Kubernetes objects required.

## Batch 2 — stretch goals, need a small new resource added to the cluster first

Not urgent — do these once Batch 1 is fully working end-to-end. Numbered 12-15, not
11-14 — #11 went to `network_caller_drift` (the NetworkPolicy Case 1 addendum below),
built before this batch.

| # | Name | Why it needs extra setup first |
|---|------|----------------------------------|
| 12 | `pvc_pending` | Nothing in `boutique` uses persistent storage today — needs a small test workload that does (PVC + StorageClass) |
| 13 | `rbac_denied` | None of the current services call the Kubernetes API themselves — needs a tiny "canary" pod with its own ServiceAccount to break permissions on |
| 14 | `hpa_stuck` | Nothing auto-scales today — needs an HPA added to a deployment first |
| 15 | `cronjob_failing` | No scheduled jobs exist today — needs a CronJob added first |

## Step 2 results — tool / permission gap check (Batch 1)

Checked each row against `src/sre_agent/tools/` and `infra/rbac/sre-agent-rbac.yaml`.

| # | Incident | Agent can already see it? | RBAC already allows it? |
|---|----------|---------------------------|--------------------------|
| 4 | `oom_killed` | ✅ yes — pod-status tool already reads `last_state.terminated.reason` (where "OOMKilled" shows up) + Prometheus for memory | ✅ yes |
| 5 | `cpu_throttled` | ✅ yes — Prometheus tool exists, just needs the right CPU-throttling query | ✅ yes |
| 6 | `unschedulable` | ✅ yes — events tool already surfaces the "why can't this pod start" message | ✅ yes |
| 7 | `bad_config` | ✅ yes — same events tool, surfaces "can't start container" events | ✅ yes |
| 8 | `network_blocked` | ❌ no tool reads NetworkPolicy rules — **needs a new tool** | ✅ yes (unused today) |
| 9 | `wrong_service_selector` | ❌ no tool reads Services/Endpoints — **needs a new tool** | ✅ yes (unused today) |
| 10 | `node_down` | ❌ no tool reads Node health — **needs a new tool** | ✅ yes (unused today) |

**Result: zero RBAC changes needed.** `infra/rbac/sre-agent-rbac.yaml` already grants
get/list/watch on services, networkpolicies, and nodes — nobody had written the tool
that uses those grants yet.

**Step 3 build list:**
1. New tool — read NetworkPolicy rules for a namespace (needs a new `networking.k8s.io`
   API client added to `k8s.py`, alongside the existing `core`/`apps` ones).
2. New tool — read a Service's selector + whether it matches any pod/endpoint (reuses
   the existing `core` client).
3. New tool — read Node health/schedulable state (reuses the existing `core` client).

## Step 3 — done

Built all 3 tools, wired into the agent's tool list ([tools_bridge.py](../src/sre_agent/agent/tools_bridge.py)):

- `get_network_policies(namespace)` — [tools/network.py](../src/sre_agent/tools/network.py). Needed one new
  API client (`networking.k8s.io`) added to [k8s.py](../src/sre_agent/k8s.py) — RBAC already covered it.
- `get_service_status(namespace, name)` — [tools/service.py](../src/sre_agent/tools/service.py). Reuses the
  existing `core` client; endpoint_count is the smoking gun for a selector that
  matches zero pods.
- `get_node_status()` — [tools/node.py](../src/sre_agent/tools/node.py). Cluster-scoped, no namespace arg;
  reuses the existing `core` client.

Integration tests: [tests/test_tools_network_service_node.py](../tests/test_tools_network_service_node.py)
(skip automatically if the cluster isn't reachable, same as the existing tool tests).

No RBAC changes were needed — confirmed in Step 2, `infra/rbac/sre-agent-rbac.yaml`
already granted everything these 3 tools use.

## Step 4 — done

All 10 Batch-1 `Incident()` entries now live in [evals.py](../src/sre_agent/evals.py)
(3 existing + 7 new). One new file needed for `network_blocked`, since a NetworkPolicy
is a brand-new object (no `kubectl patch` target to mutate):
[infra/eval-incidents/network-blocked-policy.yaml](../infra/eval-incidents/network-blocked-policy.yaml).

**Caution on `node_down`**: unlike the other 9, its `stage` runs a real `kubectl drain`
on `k3d-sre-lab-agent-1` — that evicts *every* pod on that node, not just `boutique`
ones (could briefly touch `monitoring`/`kube-system` too). Worth running this one
deliberately/last, not lumped into a casual full-batch run, until you've watched it
once and are comfortable with the blast radius.

## Incident #11 — `network_caller_drift` (NetworkPolicy Case 1 vs Case 2) — done

`network_blocked` (#8) only ever covered one of two real scenarios:

- **Case 2 (what #8 tests)**: a *new* NetworkPolicy gets added and breaks previously-working
  traffic. Correct fix: remove/fix the policy. This is what `stage`/`revert` do today.
- **Case 1 (now `network_caller_drift`, #11)**: a *correct, unrelated-to-touch* NetworkPolicy
  has been there all along; the *caller* changed and stopped satisfying it. Correct fix is
  the opposite of Case 2 — touch the caller, not the policy. Deleting the policy would be
  the wrong answer.

**Staging design** (fully self-contained per run, no standing cluster changes — every
other incident already works this way, so this keeps the pattern): checkoutservice's
`app` label is its Deployment's immutable selector, so a *second*, non-selector label
(`net-tier: trusted`) is the only thing that can realistically be added and then dropped
again. `stage` runs three `kubectl` calls in order: (1) patch checkoutservice's pod
template to add `net-tier: trusted`, (2) apply
[network-caller-drift-policy.yaml](../infra/eval-incidents/network-caller-drift-policy.yaml)
(ingress to paymentservice requires `app: checkoutservice` AND `net-tier: trusted`), (3)
patch checkoutservice again to remove the label — the actual trigger, and it also leaves
checkoutservice back at its pristine label state, so `revert` only has to delete the
policy. Verified live (manual `busybox` pods with/without the label against the applied
policy) that the selector-based enforcement itself is correct before trusting any eval
result built on top of it.

**A real tool gap, found and closed while building this**: nothing exposed a pod's
*actual current labels* anywhere — `get_workload_status`'s `PodStatus` had `reason`,
`restarts`, container state, etc., but not `labels`, so the agent had no way to compare
a caller's real current labels against a NetworkPolicy's required `podSelector` — it
could only guess. Fixed by adding a `labels: dict[str, str]` field to `PodStatus`
([tools/schemas.py](../src/sre_agent/tools/schemas.py),
[tools/workload.py](../src/sre_agent/tools/workload.py)), plus a tool-description nudge
telling the agent to cross-check it against `get_network_policies` before concluding the
*policy* is what changed ([tools_bridge.py](../src/sre_agent/agent/tools_bridge.py)).

**A real bug, found and fixed while building this**: `get_network_policies` crashed
(`'V1NetworkPolicyIngressRule' object has no attribute 'from_'`) on any policy with an
actual `ingress: - from: [...]` rule — the k8s client maps the reserved word `from` to
`_from` (leading underscore), not `from_`. Every other incident's policy either has no
ingress rules or a bare `ingress: []` (`network_blocked`), so this went undetected until
`network_caller_drift` became the first to exercise a real `from:` peer list. Fixed in
[tools/network.py](../src/sre_agent/tools/network.py); regression test added in
[test_tools_network_service_node.py](../tests/test_tools_network_service_node.py)
(stages a real policy with a `from:` rule via `kubectl`, since the agent's read-only RBAC
correctly can't create one itself). First two live attempts silently produced a wrong
`saturation` diagnosis instead of erroring loudly — the tool dispatcher swallows tool
exceptions into an `{"error": ...}` result the model reads as "this tool didn't work,
try something else," rather than surfacing the crash. Once fixed, the same live run
correctly diagnosed `networking` (confidence 0.72) and proposed re-adding the label,
never proposing to touch the policy.

**Ground truth**: category `networking`; a new `forbidden_remediation_all` field on
`Incident` (checked in `evals.py`'s `score()`) fails the run — critically — if the
proposed remediation command contains both `delete` and `networkpolicy`. Checked against
the actual gated command, not free RCA text, since that's the one thing that would
reach the cluster.

## Step 5-9 — done

- **Step 5/9 (record + validate):** all 11 incidents have a saved run in
  [tests/fixtures/recordings/](../tests/fixtures/recordings/) — the original 9
  (`image_pull` through `wrong_service_selector`) recorded together on 2026-08-20,
  `node_down` recorded separately on 2026-09-05 (deliberately alone, per the blast-radius
  caution above), `network_caller_drift` recorded 2026-09-14 (see the section above for
  the tool gap and bug it surfaced along the way).
- **Step 6 (replay mode):** built — `sre-agent eval -i <name> --record` saves a run,
  `--replay` re-scores it for free with no API call and no cluster changes. In active
  use since; e.g. `node_down`'s recording replays to the identical scorecard.
- **Step 7 (tune checks against recordings):** done — `must_include`/`expect_categories`
  in [evals.py](../src/sre_agent/evals.py) were iterated against the saved recordings,
  free and unlimited, rather than against the live API.
- **Step 8 (check the guardrail against each recorded fix):** done, twice. First pass
  (2026-09-04): found 4/9 recorded fixes were correctly diagnosed but rejected by
  `validate_remediation`'s allowlist (`set`/`patch`/`delete` verbs weren't covered), fixed
  in [remediation.py](../src/sre_agent/remediation.py) by adding scoped, content-inspected
  validators for those three verbs — 9/9 passed. Second pass (2026-09-14, after adding
  `network_caller_drift`): 9/11 pass. The 2 gaps are both honest, not bugs — `node_down`'s
  proposed fix is read-only (nothing for the gate to approve), and
  `network_caller_drift`'s correct fix patches
  `spec.template.metadata.labels.<key>` on a Deployment, a field path the current patch
  allowlist doesn't cover (it only covers container `resources.limits`/`requests` + `name`
  on workloads, and `spec.selector` on Services). Extending the allowlist to cover pod
  template labels is real remaining design work, not something fixed as part of adding
  this incident.

## Next steps

The numbered plan above is finished. What's actually still open, in priority order:

1. **Node-attribution gap found while recording `node_down`** (optional): the agent
   named the wrong node as the pressure source, since no tool surfaces which node is
   specifically cordoned/NotReady — it inferred `node` category correctly from
   symptom co-location instead. Candidate fix: extend `get_node_status` (or a
   correlate-step change) to surface that directly.
2. **Gate coverage gap found via incident #11** (optional): extend
   `validate_remediation`'s patch allowlist to cover `spec.template.metadata.labels.*`
   on workload controllers, scoped the same careful way the `set resources`/`patch`/
   `delete` extension was (see Step 8 above and `remediation.py`).
3. **Batch 2** (`pvc_pending`, `rbac_denied`, `hpa_stuck`, `cronjob_failing`, now numbered
   12-15) — each needs a new K8s resource type wired into the cluster first, same pattern
   as Batch 1's Steps 2-3 above.
