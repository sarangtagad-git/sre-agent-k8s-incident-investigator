# Engineering log — the things that broke first

The parts of this project that didn't work the first time, and what fixed them. Each entry
was written when the bug was live, not reconstructed afterwards. The README carries the
three most transferable; this file is the complete set.

---

## 1. The eval harness catching its own bug

The first live run of the `cascade` scenario scored the RCA `scheduling` instead of
`dependency` — the model reasoned "desired replicas = 0 means nothing gets scheduled,"
conflating a deliberate scale-down with a pod that can't be *placed*. Diagnostically the
report was right (redis-cart, correct fix, 0.85 confidence); the taxonomy was wrong. Fixed
with an explicit `CATEGORY_GUIDE` injected into both prompts that set `category`, since
they're independent LLM calls with no shared context. This is exactly what an eval harness
is for — a bug a quick eyeball of a "looks right" RCA would likely have missed.

## 2. A caching fix that made things worse before it made them better

Three of the agent's four LLM calls per run (`correlate`/`hypothesize`/`propose`) each
re-sent the *entire* gathered transcript at full price, because `messages.parse()` — used
for guaranteed schema validation — rejects prompt caching outright. The first fix attempt
(switch to `messages.create()` + structured-output mode, which *can* take `cache_control`)
backfired: each call asks for a different JSON shape, and Anthropic bakes that shape into
the same cacheable region as the system prompt, so the three calls never shared a cache
with each other or with `gather`. Cost went from $0.22/run to **$0.33**.

The fix that actually worked: make every analysis call mirror `gather`'s request shape
byte-for-byte (same system prompt, tools, thinking config, and — the detail that mattered —
leaving `tool_choice` at its default instead of forcing it), then ask for JSON in the
prompt and parse it by hand instead of relying on structured-output mode. Verified live:
all three calls now read the same cached transcript at a fraction of the price.
**$0.1586/run**, down from $0.22, with the RCA unchanged.

## 3. The alert that never came back

The first end-to-end Phase 9 demo produced… silence. The pipe had already worked in a dry
run minutes earlier; re-staging the *same* incident produced no second webhook. Cause: with
`send_resolved: false`, Alertmanager dedups a re-fired identical alert against its
notification log until `repeat_interval` (then 4h) passes — an alert *resolving* does not
reset that clock. The fix is a deliberately short `repeat_interval: 15m` with the real
spend control living in the agent's own cooldown + daily cap, which is where it belongs
anyway.

Honorable mention from the same session: this k3d cluster never injects
`host.k3d.internal` — pods reach the host via `host.docker.internal` through Docker
Desktop's DNS, which the plan only caught because "reachability" was flagged as the
riskiest assumption and tested before anything else.

## 4. Memory that talked itself into false confidence — twice, in opposite directions

Once `recall` shipped, the obvious risk test was to stage one incident and re-investigate
it repeatedly without reverting — a persisting-issue scenario, and the one most likely to
create an echo chamber. Confidence didn't run away (it plateaued around 0.85–0.92, then
dropped), but the RCA's own *narrative* did drift: by the fifth repeat, near-identical
priors from the same staged fault, re-run an hour apart, were being described as "multiple
independent confirmations." True count, false framing — three sightings of one event aren't
three corroborating incidents. Fixed by telling the model explicitly that closely-timed,
near-identical priors are one event observed repeatedly, not independent evidence;
re-verified live, the false-corroboration language disappeared entirely and the confidence
band dropped from high (0.85–0.92) to medium (0.72–0.78) on the same repeated incident.

That fix then over-corrected: a second test — stage an incident, apply the agent's own fix
for real through the full approval gate, then re-stage the *same* fault — found the RCA no
longer cited the prior at all, even though it was now a human-confirmed, validated outcome
and not a repeated guess. Fixed by carving out confirmed outcomes as categorically
different from repetition; re-verified, the new RCA cited only the confirmed entry, with
the other unverified priors correctly still ignored.

**The lesson generalizes:** a confidence-calibration fix needs to be tested in both
directions — does it stop over-trusting repetition, *and* does it still make use of memory
that's actually earned trust — because a fix that only proves one side can quietly break
the other.

## 5. Trusting a fix that was never checked

`approved_applied` meant only that `kubectl apply` returned exit 0 — nothing downstream
ever confirmed the workload actually recovered, which stopped being a theoretical gap the
moment memory started treating that label as reinforcing evidence. Fixed with a
stability-window poller (`verify_recovery()`) that requires several *consecutive* healthy
reads before a run is marked resolved — a single early design would have been fooled by a
pod that passes one check and then crashes again, so it mirrors Kubernetes' own
`successThreshold` instead of stopping at the first good reading.

Live-verified on both paths: a genuinely fixed workload was correctly marked
`confirmed_healthy`, and a synthetic "known to have failed" prior fed back into a fresh
investigation dropped confidence to 0.55 — the lowest of any run all session — while the
model explicitly cited the prior failure by revision number and still proposed the right
class of fix, just without unearned certainty.

## 6. A crashed tool that produced a confident wrong answer

Building the `network_caller_drift` incident, three live runs in a row diagnosed
`saturation` at 0.62 confidence when the real cause was a NetworkPolicy. The cause wasn't
the model: `get_network_policies` was crashing with `'V1NetworkPolicyIngressRule' object
has no attribute 'from_'` — the Kubernetes client maps the reserved word `from` to `_from`
(leading underscore), not `from_`. Every other incident's policy has no ingress rules or a
bare `ingress: []`, so nothing had ever exercised a real `from:` peer list, and the bug sat
undetected.

What made it expensive was the reporting, not the typo. The tool dispatcher catches every
tool exception and hands it to the model as ordinary text — deliberately, so one flaky tool
can't abort an investigation. But nothing else read that failure: not the eval scorecard,
not the dashboard feed, not the report. The model quietly reasoned around the missing
evidence and answered with normal confidence, and the only trace was a ✗ on the run-detail
page. Three paid runs (about $1.06) were spent before the actual cause surfaced.

A second, quieter variant turned up in the same audit: `query_prometheus` reports failure
*inside* a normal result rather than raising, so an unreachable Prometheus was recorded as
a green check. Thirteen such calls across eight saved runs had gone unnoticed.

Fixed in three places: a critical `no_tool_errors` eval check (a crashed tool fails the
incident, relevant to that incident or not — the eval doubles as a test of the tools
themselves), an `evidence_gaps` field the report must fill when a tool failed, added in
code so disclosure doesn't depend on the model's compliance, and a badge on the dashboard
feed. Live-verified in both directions: with Prometheus down the report named the missing
metric and held confidence at 0.85 because of it; with Prometheus up, `evidence_gaps` came
back empty.

**The lesson:** an error-handling path that never surfaces anywhere a human or a test looks
is indistinguishable from no error handling at all. The first live test of the fix found a
second defect — the model had no definition of `evidence_gaps` and filled it with checks it
had simply chosen not to run — which only showed up because the healthy direction was
tested too, not just the failing one.

## 7. The drained node that hosts nothing — and a fix I couldn't fully prove

During the `node_down` incident (a worker node cordoned and drained), the agent ruled out the
right node and blamed the wrong one. My first diagnosis of the bug was itself wrong. I wrote
that no tool showed which node was cordoned — but `get_node_status` did, and the agent had
read it, concluding "agent-1 is cordoned/unschedulable but not hosting any affected pods".

That sentence is the actual error, and it's a reasoning trap worth naming: a drain moves
every pod *off* a node, so the drained node always hosts nothing, and the evicted pods restart
on the *other* nodes. The agent saw a wave of restarts clustered on `server-0` and blamed it —
the node they landed on, not the one that displaced them. The node object records that a node
is cordoned, never *when*, and the timing is what separates "a cordon just preceded these
restarts" from "this node has a problem".

The eval had let it through for a second reason. `must_include_any` searches the whole report
including the `alternatives` list, so a line *dismissing* `agent-1` satisfied it. A new
`root_cause_must_name` check requires the root cause itself to name the target, and the old
recording fails it.

The fix added taints with their `timeAdded` (k3s stamps a cordon's taint with it — found by
probing the live cluster before building anything) and timed node events, plus a tool
description saying a drained node hosting nothing is not ruled out.

**Then the control.** The re-recorded run named `agent-1` correctly, at higher confidence and
half the cost. Before claiming that, I ran the *old* tool on the same cluster — and it got the
right answer too. The reboot that preceded the run had wiped every pod's restart history, and
that history was exactly what had misled the first run. The improvement can't be cleanly
attributed to the change. What the control did surface: its own report named the precise gap
this change fills. So the honest claim is narrower than the tempting one — better grounding and
a stricter check, not a demonstrated fix for the original failure.

**The lesson:** when a fix and an environmental change land together, an improvement is
evidence of nothing until you've run the old code in the new environment. It cost $0.14 and
changed what I was willing to say.
