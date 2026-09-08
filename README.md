# Kubernetes Incident Investigator (SRE Agent)

An autonomous agent that investigates Kubernetes incidents when an alert fires —
literally: Alertmanager routes the alert to a local listener, the agent pulls evidence,
correlates it, ranks competing root-cause hypotheses, and **proposes** remediation
behind a **human-in-the-loop approval gate**. Every run (manual or alert-triggered)
lands in a local history and a Streamlit dashboard of named incidents. The agent
authenticates as a **read-only** identity and can never mutate the cluster itself; its
autonomy is spend-capped and propose-only; a regression suite stages real incidents
against it and scores the RCA before any of this is trusted.

**Proven end to end:** break the cluster with one kubectl command and touch nothing
else — 4½ minutes later a diagnosed incident (correct root cause, gated fix, $0.15)
appears on the dashboard wearing an "auto" badge. The agent also **remembers** — it
recalls this workload's own prior incidents before writing up a new one — and **checks
its own work** — an applied fix isn't marked resolved until the workload is polled
healthy for several consecutive checks, not just until `kubectl apply` exits 0.

> Learn-in-public project — built and hardened in public, including the parts that
> didn't work the first time. Full design brief: [`k8s-incident-investigator-brief.md`](k8s-incident-investigator-brief.md).
> Architecture diagrams: [`docs/architecture/`](docs/architecture/). Demo video: coming soon.

## Why it's built this way
- **Safety by construction** — a view-only ServiceAccount (RBAC), not just prompt rules.
  The agent's own Kubernetes credentials cannot delete a pod or read a Secret, full stop.
- **Tools return facts, the LLM reasons** — typed, structured evidence; no scraping.
- **Cause, not just symptom** — an explicit `correlate → hypothesize → rank → propose`
  pipeline scores competing explanations (each with supporting/against evidence) and only
  writes up the top-ranked one, instead of jumping at the first plausible story.
- **A human approves every mutation** — `propose` never executes anything. `--execute`
  runs the fix through an allowlist validator, a server-side dry-run, and an explicit
  confirmation, applied with *your* kubectl identity, never the agent's.
- **Provably correct** — an eval harness stages real incidents and scores the RCA against
  ground truth; it has already caught (and driven the fix for) a real classification bug.
- **Autonomy on a leash** — alert-triggered runs are *propose-mode only, forever*, and
  spend-bounded by design: a namespace allowlist, a per-alert cooldown (which also counts
  manual runs), and a daily run cap — all enforced in code and logged when they decline.
  The listener is a foreground process; Ctrl-C is the kill switch.
- **Cost-aware, not just cost-blind** — every LLM call is priced and shown to you; a
  caching bug that made analysis 50% *more* expensive was found and fixed by reading the
  numbers, not by guessing. The dashboard's Analytics view shows cache-hit meters and
  cost-per-investigation bars across all runs.
- **Observe the observer** — OpenTelemetry spans on every node, tool call, and LLM call,
  exported to a self-hosted Opik instance: every reasoning step, tool call, and token
  count for any past run is inspectable end to end, not just the final RCA.
- **Memory that informs, never inflates** — the agent recalls this workload's own past
  incidents, but the digest is text for the LLM to weigh, never a code-side confidence
  bump; the cluster is always checked fresh *before* the past is allowed to influence
  anything; and eval runs neither write to nor read from memory, so the regression suite
  stays a cold, repeatable test.
- **Trust, but verify the fix actually worked** — after a human-approved apply, the agent
  polls the workload for several *consecutive* healthy checks (not just one, and not just
  "the command exited 0") before calling it resolved. That verdict then feeds back into
  memory honestly — a confirmed-healthy prior earns more trust, a confirmed-still-broken
  prior actively tells the model not to repeat the same fix blind.

## Architecture

Two ways in: `sre-agent investigate` by hand, or **`sre-agent listen`** — Alertmanager
routes boutique alerts to a local webhook; each firing alert must pass the trigger
policy (firing-only → namespace allowlist → cooldown → daily cap) before the agent
spends a cent. Either way, the agent runs the same **LangGraph** state machine.
`gather` drives a bounded **ReAct loop** — Claude picks a read-only tool, the tool runs,
the result feeds back — until there's enough evidence, always against the live cluster,
before memory ever enters the picture. `recall` then looks up this workload's own past
non-eval incidents (plain Python, no LLM call) and hands `hypothesize`/`propose` a short
digest — including an honest label if a past fix was later confirmed to have worked or
failed. `correlate` lays out a timeline and (for cascades) a service dependency chain.
`hypothesize` names competing root causes, each scored 0–1 with evidence for and against.
`rank` is plain Python — sort by confidence, no LLM call. `propose` writes the final RCA
on the top hypothesis with a single gated fix. That fix only ever reaches the cluster
through the human approval gate — and once applied, the gate itself polls the workload
for several consecutive healthy checks before the run is recorded as resolved. Every
node and tool call is an OpenTelemetry span.

```
Alertmanager ─▶ listener (trigger policy: allowlist · cooldown · daily cap)
                    │                                                                                    propose only
      CLI ──────────┴▶ gather (Claude ⇄ read-only tools) ─▶ recall ─▶ correlate ─▶ hypothesize ─▶ rank ─▶ propose ─▶ human approval gate ─▶ verify (consecutive healthy checks)
                            ▲ typed evidence                    ▲ this workload's                                  (never auto-executes)
                                                                   own past incidents      every run ─▶ SQLite history ─▶ CLI + Streamlit dashboard
```

Diagrams (in [`docs/architecture/`](docs/architecture/) — click to view):
- [Investigation chain](docs/architecture/agent-investigation-chain.svg) — alert → gather → RCA → human gate → gated remediation
- [Agent loop (LangGraph)](docs/architecture/phase3-agent-loop.svg) — the full gather/correlate/hypothesize/rank/propose state machine
- [Read-only tools](docs/architecture/phase2-tools.svg) — the five evidence-gathering tools
- [Agent package](docs/architecture/phase3-agent-files.svg) — how the `src/sre_agent/agent/` files collaborate
- [Tool execution flow](docs/architecture/workload-execution-flow.svg) — raw Kubernetes objects → typed facts

## Local stack
- **Cluster:** k3d / k3s (`sre-lab`) — see [`infra/k3d/`](infra/k3d/). (k3d not kind because
  the dev disk is a slow HDD; k3s's SQLite datastore tolerates it — see the build log below.)
- **Observability:** kube-prometheus-stack (cluster metrics) — see
  [`infra/observability/`](infra/observability/) — plus a self-hosted
  [Opik](https://github.com/comet-ml/opik) instance (agent tracing), run via its own
  `docker compose`, external to this repo; `scripts/start-env.sh` brings it up alongside
  everything else.
- **Demo app ("patient"):** Google Online Boutique — see [`infra/apps/online-boutique/`](infra/apps/online-boutique/).
- **Agent:** Python + LangGraph + Claude + read-only Kubernetes/Prometheus tools — [`src/sre_agent/`](src/sre_agent/).

## Status & roadmap
- [x] **1 · Scaffold + read-only RBAC** — view-only ServiceAccount, enforced by the API server
- [x] **2 · Read-only tools** — workload · events · logs (`--previous`) · rollout history · PromQL (typed + tested)
- [x] **3 · Agent loop (LangGraph)** — `gather → report` with Claude (adaptive thinking) + structured RCA
- [x] **4 · Correlation + confidence** — explicit `correlate/hypothesize/rank/propose`; solves the dependency cascade
- [x] **5 · Safety gate** — propose → human approves → allowlisted, server-dry-run remediation
- [x] **6 · Eval harness** — stages each incident, asserts the RCA vs ground truth, exits non-zero on failure
- [x] **Cost pass** — cut the Phase 4 analysis calls from ~$0.22 to ~$0.16/run (see below)
- [x] **7 · Run history** — every run persisted to SQLite (evidence trail, hypotheses, RCA, cost, outcome); `sre-agent history`
- [x] **8 · Dashboard** — Streamlit incident feed + full-page investigation detail + an Analytics view (cache economics, tool usage, approval funnel)
- [x] **9 · Alert-triggered investigations** — Alertmanager → webhook listener → guardrails → autonomous propose-only run; live-demoed end to end
- [x] **10 · Incident memory** — a `recall` node feeds this workload's own past incidents into the RCA, text-only, cluster-checked-fresh-first, eval-isolated; two rounds of live calibration testing (over-trust, then under-use) hardened the prompt
- [x] **11 · Applied-fix verification** — after a human-approved apply, poll for consecutive healthy checks before calling it resolved; the verdict feeds back into memory honestly
- [x] **Eval taxonomy expansion** — 3 → 10 scripted incident classes (adds OOM, CPU
  throttling, unschedulable, bad config, NetworkPolicy block, wrong Service selector,
  node down), 3 new read-only tools (`get_network_policies`/`get_service_status`/
  `get_node_status`), and a record/replay mechanism so checks tune against a saved run
  instead of the live API
- [x] **Gate coverage** — extended `validate_remediation`'s allowlist with scoped,
  content-inspected `set resources`/`patch`/`delete` validators, closing a real gap where
  4 of 9 correctly-diagnosed fixes were rejected on verb alone; 9/9 pass now
- [x] **Tracing** — self-hosted Opik captures every LLM call, tool call, and reasoning
  span end to end (see "Observe the observer" above)
- [x] **One-command dev environment** — `make up` brings up the cluster, Opik, the
  Boutique port-forward, and the dashboard together, polling until all three are ready
- [ ] Demo video + this write-up finalized

_Cross-cutting from day 1: OpenTelemetry spans on every node/tool call, exported to a
self-hosted Opik instance (`docker compose --profile opik up -d`, wired into `make up`)._

## Repository layout
```
infra/           k3d cluster · kube-prometheus-stack (+ boutique alert rules & the
                 sre-agent Alertmanager route) · Online Boutique · read-only RBAC
src/sre_agent/
  tools/         8 read-only evidence tools (workload · events · logs · rollout ·
                 PromQL · network policies · service status · node status) + Pydantic schemas
  agent/         LangGraph graph, tool bridge, prompts, state/RCA schemas; the optional
                 open-model gather swap lives here too (dormant unless GATHER_MODEL is set)
  remediation.py the Phase 5 allowlist validator + dry-run/apply gate (scoped `set
                 resources`/`patch`/`delete` validators included), plus the Phase 11
                 consecutive-healthy-checks recovery verifier
  evals.py       the 10 scripted incidents (stage/revert/ground-truth) + scoring
  eval_recording.py  save/replay a run so check-tuning is free after the first (paid) one
  history_store.py  SQLite persistence for every run (data/history.db, git-ignored),
                 including recalled prior incidents and verification outcomes
  dashboard.py   the Streamlit dashboard (incident feed · detail pages · analytics)
  alerts.py      Alertmanager payload models + the pure trigger policy (Phase 9)
  listener.py    the webhook listener behind `sre-agent listen`
  cli.py         status · events · logs · rollout · metrics · investigate · eval · history · listen
scripts/         one-command dev-env startup — start-env.ps1 (Windows entrypoint,
                 launches Docker Desktop if needed) -> start-env.sh (WSL: cluster, Opik,
                 port-forward, dashboard, in parallel); also `make up`
tests/           unit + live-cluster integration tests (auto-skip when offline)
tests/fixtures/recordings/  one saved real run per incident, for free `--replay` scoring
evals/README.md  how to run the eval harness (specs live in src/sre_agent/evals.py)
docs/            architecture diagrams · dashboard-plan.md · alerts-plan.md ·
                 incident-taxonomy-plan.md (the 10-incident build log)
```

## Quickstart (Phase 1)
```bash
# 1. Create the read-only identity in the cluster
kubectl apply -f infra/rbac/sre-agent-rbac.yaml

# 2. See exactly what it can / cannot do
make verify-rbac

# 3. Generate the agent's read-only kubeconfig
bash infra/rbac/gen-kubeconfig.sh

# 4. Install the Python project + run smoke tests
make install && make test

# 5. Prove the agent can read the cluster (read-only)
cp .env.example .env      # add your ANTHROPIC_API_KEY later
make doctor
```
Already have the cluster built and just restarted your machine? `make up` (or
[`scripts/start-env.ps1`](scripts/start-env.ps1) from Windows) brings the cluster, Opik,
the Boutique port-forward, and the dashboard up together and waits until all three answer.

## Usage
Each read-only tool is also a CLI command (great for driving the cluster by hand):
```bash
sre-agent status boutique                 # deployments + pods (add --json to see the agent's view)
sre-agent events <pod> -n boutique        # events (the smoking gun for ImagePullBackOff)
sre-agent logs <pod> --previous           # crashed-container logs (for CrashLoopBackOff)
sre-agent rollout emailservice            # revision history with per-revision images
sre-agent metrics 'sum(up)'               # PromQL (needs the Prometheus port-forward)
```
Let the agent investigate on its own (requires `ANTHROPIC_API_KEY` in `.env`):
```bash
sre-agent investigate boutique -w emailservice -a "rollout not progressing"
# → gathers evidence, ranks hypotheses, prints a root-cause report + a PROPOSED fix
sre-agent investigate boutique -v          # stream reasoning, tool calls, and per-call cost live
```
Close the loop — propose, dry-run, approve, apply — using *your* kubectl identity, not the agent's:
```bash
sre-agent investigate boutique -w redis-cart -x
# → same investigation, then: allowlist check → server --dry-run preview → confirm → apply → re-verify
```
Regression-test the agent against ground truth (mutates the cluster, costs real money, always reverts):
```bash
sre-agent eval                        # all 10 scripted incidents
sre-agent eval -i cascade -y          # just one, skip the confirmation
sre-agent eval -i cascade --record    # save the run for free replay later
sre-agent eval -i cascade --replay    # re-score the saved run — no API call, no cluster changes
```
Browse what the agent has done — every run is persisted:
```bash
sre-agent history              # recent runs: target, category, confidence, cost, outcome
sre-agent history 304358e8     # one run in full: evidence trail, timeline, hypotheses, RCA
make dashboard                 # the Streamlit UI: incident feed, detail pages, analytics
```
Let the cluster page the agent itself (Phase 9 — see [`docs/alerts-plan.md`](docs/alerts-plan.md)):
```bash
# one-time wiring: fast boutique alert rules + the Alertmanager → listener route
kubectl apply -f infra/observability/boutique-alert-rules.yaml
helm upgrade kps prometheus-community/kube-prometheus-stack -n monitoring \
  -f infra/observability/kube-prometheus-stack.values.yaml

sre-agent listen --dry-run     # test the whole pipe free: logs every trigger decision, never calls the LLM
sre-agent listen               # the real thing: firing alert → guardrails → autonomous propose-only run
```
Alert-triggered runs pass a namespace allowlist, a 30-min per-alert cooldown, and a
daily run cap before a cent is spent; every decline is logged with its reason, and the
resulting incidents show up in the dashboard with an "auto" badge.

## Incidents proven live
All 10 are real `sre-agent eval --record` runs against the actual cluster — not mocked
examples — each with its own ground-truth check in [`evals.py`](src/sre_agent/evals.py)
and a saved recording in [`tests/fixtures/recordings/`](tests/fixtures/recordings/) for
free `--replay` scoring. See [`docs/incident-taxonomy-plan.md`](docs/incident-taxonomy-plan.md)
for how each one was built.

| Incident | What broke | Category | Confidence | Cost* |
|---|---|---|---|---|
| `image_pull` | Deployment references a non-existent image tag (currencyservice) | rollout | 0.95 | $0.07 |
| `crash_loop` | Bad command crashes the container on startup (`ModuleNotFoundError`, emailservice) | workload | 0.75 | $0.10 |
| `cascade` | A dependency (redis-cart) scaled to 0, no workload hint given | dependency | 0.95 | $0.10 |
| `oom_killed` | Memory limit set too low, kernel OOM-kills the container (recommendationservice) | saturation | 0.80 | $0.11 |
| `cpu_throttled` | CPU limit set too low, service works but is throttled (checkoutservice) | config | 0.92 | $0.09 |
| `unschedulable` | A pod requests more CPU than any node has (paymentservice) | saturation | 0.85 | $0.12 |
| `bad_config` | Pod spec references a Secret that doesn't exist (cartservice) | config | 0.95 | $0.07 |
| `network_blocked` | A NetworkPolicy blocks previously-working traffic (checkoutservice → paymentservice) | networking | 0.95 | $0.20 |
| `wrong_service_selector` | A Service's selector stops matching its own pods (frontend) | config | 0.85 | $0.11 |
| `node_down`† | A worker node is cordoned and drained; pods evicted/rescheduled | node | 0.55 | $0.24 |

\* claude-sonnet-5, `AGENT_EFFORT=medium`, after the caching fix below.
† Correctly categorized as `node`, but named the wrong node as the pressure source — see
Known gaps.

The CrashLoopBackOff scenario has also been diagnosed **fully autonomously**: emailservice
was broken at 20:02:41 with nothing else touched — `BoutiquePodStuck` fired, Alertmanager
called the listener, the guardrails passed, and by 20:07:12 the agent had filed the
incident (workload, 0.85, $0.15, `ModuleNotFoundError` correctly pulled from the crashed
container's `--previous` logs). No human ran anything.

## Keeping it honest and keeping it cheap: five things that broke first

**The eval harness catching its own bug.** The first live run of the `cascade` scenario
scored the RCA `scheduling` instead of `dependency` — the model reasoned "desired replicas
= 0 means nothing gets scheduled," conflating a deliberate scale-down with a pod that can't
be *placed*. Diagnostically the report was right (redis-cart, correct fix, 0.85
confidence); the taxonomy was wrong. Fixed with an explicit `CATEGORY_GUIDE` injected into
both prompts that set `category`, since they're independent LLM calls with no shared
context. This is exactly what an eval harness is for — a bug a quick eyeball of a "looks
right" RCA would likely have missed.

**A caching fix that made things worse before it made them better.** Three of the agent's
four LLM calls per run (`correlate`/`hypothesize`/`propose`) each re-sent the *entire*
gathered transcript at full price, because `messages.parse()` — used for guaranteed schema
validation — rejects prompt caching outright. The first fix attempt (switch to
`messages.create()` + structured-output mode, which *can* take `cache_control`) backfired:
each call asks for a different JSON shape, and Anthropic bakes that shape into the same
cacheable region as the system prompt, so the three calls never shared a cache with each
other or with `gather`. Cost went from $0.22/run to **$0.33**. The fix that actually
worked: make every analysis call mirror `gather`'s request shape byte-for-byte (same
system prompt, tools, thinking config, and — the detail that mattered — leaving
`tool_choice` at its default instead of forcing it), then ask for JSON in the prompt and
parse it by hand instead of relying on structured-output mode. Verified live: all three
calls now read the same cached transcript at a fraction of the price. **$0.1586/run**, down
from $0.22, with the RCA unchanged.

**The alert that never came back.** The first end-to-end Phase 9 demo produced… silence.
The pipe had already worked in a dry run minutes earlier; re-staging the *same* incident
produced no second webhook. Cause: with `send_resolved: false`, Alertmanager dedups a
re-fired identical alert against its notification log until `repeat_interval` (then 4h)
passes — an alert *resolving* does not reset that clock. The fix is a deliberately short
`repeat_interval: 15m` with the real spend control living in the agent's own cooldown +
daily cap, which is where it belongs anyway. Honorable mention from the same session:
this k3d cluster never injects `host.k3d.internal` — pods reach the host via
`host.docker.internal` through Docker Desktop's DNS, which the plan only caught because
"reachability" was flagged as the riskiest assumption and tested before anything else.

**Memory that talked itself into false confidence — twice, in opposite directions.**
Once `recall` shipped, the obvious risk test was to stage one incident and re-investigate
it repeatedly without reverting — a persisting-issue scenario, and the one most likely to
create an echo chamber. Confidence didn't run away (it plateaued around 0.85–0.92, then
dropped), but the RCA's own *narrative* did drift: by the fifth repeat, near-identical
priors from the same staged fault, re-run an hour apart, were being described as
"multiple independent confirmations." True count, false framing — three sightings of one
event aren't three corroborating incidents. Fixed by telling the model explicitly that
closely-timed, near-identical priors are one event observed repeatedly, not independent
evidence; re-verified live, the false-corroboration language disappeared entirely and the
confidence band dropped from high (0.85–0.92) to medium (0.72–0.78) on the same repeated
incident. That fix then over-corrected: a second test — stage an incident, apply the
agent's own fix for real through the full approval gate, then re-stage the *same* fault —
found the RCA no longer cited the prior at all, even though it was now a
human-confirmed, validated outcome and not a repeated guess. Fixed by carving out
confirmed outcomes as categorically different from repetition; re-verified, the new RCA
cited only the confirmed entry, with the other unverified priors correctly still ignored.
**The lesson generalizes:** a confidence-calibration fix needs to be tested in both
directions — does it stop over-trusting repetition, *and* does it still make use of
memory that's actually earned trust — because a fix that only proves one side can quietly
break the other.

**Trusting a fix that was never checked.** `approved_applied` meant only that
`kubectl apply` returned exit 0 — nothing downstream ever confirmed the workload actually
recovered, which stopped being a theoretical gap the moment memory started treating that
label as reinforcing evidence. Fixed with a stability-window poller
(`verify_recovery()`) that requires several *consecutive* healthy reads before a run is
marked resolved — a single early design would have been fooled by a pod that passes one
check and then crashes again, so it mirrors Kubernetes' own `successThreshold` instead of
stopping at the first good reading. Live-verified on both paths: a genuinely fixed
workload was correctly marked `confirmed_healthy`, and a synthetic "known to have failed"
prior fed back into a fresh investigation dropped confidence to 0.55 — the lowest of any
run all session — while the model explicitly cited the prior failure by revision number
and still proposed the right class of fix, just without unearned certainty.

## Known gaps
- 10 incident classes are scripted and recorded; 4 stretch incidents (`pvc_pending`,
  `rbac_denied`, `hpa_stuck`, `cronjob_failing`) each need a new K8s resource type added
  to the cluster first, and a `NetworkPolicy` "Case 1" (an old, legitimate policy the
  *app* should route around, not a new one to delete) needs its own incident with
  opposite ground truth from `network_blocked` — see
  [`docs/incident-taxonomy-plan.md`](docs/incident-taxonomy-plan.md).
- `node_down`'s diagnosis named the wrong node as the pressure source — it correctly
  inferred the `node` category from symptom co-location (which pods restarted together)
  rather than from a tool that directly surfaces which node is cordoned/NotReady, so it
  pinned the blame on the node most pods happened to share, not the one actually drained.
- An optional swap of the gather phase to a cheaper open model (Qwen via OpenRouter, see
  `.env.example`'s `GATHER_MODEL`) ships dormant — unset by default, so it changes
  nothing unless explicitly turned on. Measured ~17-18% cheaper on simple incidents with
  no quality loss, but one `cascade` (the hardest incident) run cost +215% when the open
  model produced an oversized transcript; not yet decided whether to enable it, and if
  so, whether to route it only for simple incidents rather than as an all-or-nothing switch.
- The listener is a foreground lab tool by design — no daemon/systemd, no HA, and the
  dashboard is local-only (it reads the same local SQLite file the CLI writes).
- The dashboard shows finished runs; an in-progress investigation isn't streamed live yet.
- Memory's match key is exact namespace + workload only — an alert that carries a pod
  label but no deployment label (some Prometheus alerts do) leaves `workload=None` and
  can never be recalled. A known narrowness, not yet broadened.
- Verification only polls the workload named in the remediation, not downstream/dependent
  services in the correlation chain; it's a synchronous one-shot check at apply time, not
  a background re-check job; and pre-Phase-11 `approved_applied` rows were never
  backfilled with a verification verdict.
