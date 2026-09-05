# Incident taxonomy — planning note

**Status: draft list, nothing built yet.** This is Step 1 of the eval-expansion plan
(see below) — just the list of incidents to eventually turn into `Incident()` entries
in `evals.py`. No code, no cluster changes, no API calls yet.

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

Not urgent — do these once Batch 1 is fully working end-to-end.

| # | Name | Why it needs extra setup first |
|---|------|----------------------------------|
| 11 | `pvc_pending` | Nothing in `boutique` uses persistent storage today — needs a small test workload that does (PVC + StorageClass) |
| 12 | `rbac_denied` | None of the current services call the Kubernetes API themselves — needs a tiny "canary" pod with its own ServiceAccount to break permissions on |
| 13 | `hpa_stuck` | Nothing auto-scales today — needs an HPA added to a deployment first |
| 14 | `cronjob_failing` | No scheduled jobs exist today — needs a CronJob added first |

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

## Known gap in `network_blocked` (#8) — Case 1 vs Case 2

`network_blocked` as built only covers one of two real scenarios:

- **Case 2 (what #8 tests)**: a *new* NetworkPolicy gets added and breaks previously-working
  traffic. Correct fix: remove/fix the policy. This is what `stage`/`revert` do today.
- **Case 1 (not tested anywhere yet)**: an *old, legitimate* NetworkPolicy has been there
  all along; the *app* changed and started sending traffic the policy correctly blocks.
  Correct fix here is the opposite — touch the app, not the policy. Deleting the policy
  would be the wrong answer.

These need opposite ground truth, so Case 1 can't just be a tweak to incident #8 — it
needs its own incident with its own `must_include`/`expect_categories` (crucially, the
RCA should NOT recommend removing the policy). Proposed as **incident #11 (Case 1)**,
deferred for now: stage it by first creating an old-and-stable-looking NetworkPolicy
protecting some service, waiting, *then* rolling out a change to a different service that
starts calling it in a way the policy blocks. Ground truth: category `networking`, RCA
must NOT suggest deleting the policy, must point at the caller's new traffic pattern instead.

**Fixed already**: `get_network_policies` now returns `created` (when the policy was
made), and its tool description tells the agent to compare that against the calling
service's rollout history before recommending policy removal — see
[tools/schemas.py](../src/sre_agent/tools/schemas.py),
[tools/network.py](../src/sre_agent/tools/network.py),
[tools_bridge.py](../src/sre_agent/agent/tools_bridge.py). This makes the agent *capable*
of telling Case 1 from Case 2 for real incidents even before #11 exists as an eval.

## Next step

Step 5: record one real run per incident on a cheap model (Haiku), and save the result
instead of discarding it. Step 6 (the replay mechanism) should exist before this, per
the earlier ordering discussion.
