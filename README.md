# Kubernetes Incident Investigator (SRE Agent)

An autonomous agent that investigates Kubernetes incidents when an alert fires.
Alertmanager routes the alert to a local listener; the agent gathers evidence with
read-only tools, correlates it, ranks competing root-cause hypotheses, and **proposes** a
fix behind a human approval gate. It never mutates the cluster itself — its Kubernetes
credentials can't, by RBAC.

**Proven end to end:** break the cluster with one kubectl command and touch nothing else —
4½ minutes later a diagnosed incident (correct root cause, gated fix, $0.15) appears on the
dashboard wearing an "auto" badge.

```
Alertmanager ─▶ listener (trigger policy: allowlist · cooldown · daily cap)
                    │                                                                                    propose only
      CLI ──────────┴▶ gather (Claude ⇄ read-only tools) ─▶ recall ─▶ correlate ─▶ hypothesize ─▶ rank ─▶ propose ─▶ human approval gate ─▶ verify (consecutive healthy checks)
                            ▲ typed evidence                    ▲ this workload's                                  (never auto-executes)
                                                                   own past incidents      every run ─▶ SQLite history ─▶ CLI + Streamlit dashboard
```

`gather` runs a bounded ReAct loop against the live cluster. `recall` then adds this
workload's own past incidents (plain Python, no LLM call) — always *after* the cluster has
been checked fresh. `correlate` builds a timeline and, for cascades, a dependency chain.
`hypothesize` scores competing causes 0–1 with evidence for and against; `rank` sorts them
in plain Python; `propose` writes up the top one with a single gated fix.

> Learn-in-public project. Design brief:
> [`k8s-incident-investigator-brief.md`](k8s-incident-investigator-brief.md) ·
> Diagrams: [`docs/architecture/`](docs/architecture/) ·
> What shipped when: [`docs/build-log.md`](docs/build-log.md) ·
> What broke first: [`docs/engineering-log.md`](docs/engineering-log.md)

## Why it's built this way

- **Safety by construction** — a view-only ServiceAccount, enforced by the API server, not
  by prompt rules. The agent's own credentials cannot delete a pod or read a Secret.
  Remediation runs through an allowlist validator, a server-side dry-run, and an explicit
  confirmation — applied with *your* kubectl identity, never the agent's.
- **Cause, not just symptom** — an explicit `correlate → hypothesize → rank → propose`
  pipeline scores competing explanations and writes up only the top-ranked one, instead of
  jumping at the first plausible story.
- **Provably correct** — 11 scripted incidents stage real faults and score the RCA against
  ground truth. The harness has caught real bugs, including a crashed tool that was
  producing confident wrong answers.
- **Autonomy on a leash** — alert-triggered runs are propose-mode only, forever, and
  spend-bounded: namespace allowlist, per-alert cooldown, daily run cap, all enforced in
  code and logged when they decline.
- **Memory that informs, never inflates** — prior incidents reach the model as text to
  weigh, never as a code-side confidence bump, and eval runs are isolated from memory in
  both directions so the regression suite stays a cold test.
- **Trust, but verify** — an applied fix isn't "resolved" until the workload polls healthy
  for several *consecutive* checks, mirroring Kubernetes' own `successThreshold`. That
  verdict feeds back into memory honestly: a confirmed-failed fix actively tells the model
  not to repeat it.
- **Cost-aware** — every LLM call is priced and shown. A caching bug that made analysis
  50% *more* expensive was found by reading those numbers, not by guessing.
- **Observable** — OpenTelemetry spans on every node, tool, and LLM call, exported to a
  self-hosted Opik instance: every reasoning step of any past run is inspectable.

## Quickstart

**Prerequisites:** Docker, [k3d](https://k3d.io), `kubectl`, `helm`, Python 3.11+, and an
`ANTHROPIC_API_KEY`. On Windows, run everything from WSL with Docker Desktop's WSL
integration enabled.

```bash
# 1. Create the cluster (1 control-plane + 2 workers)
cd infra/k3d && k3d cluster create --config k3d-cluster.yaml && cd ../..

# 2. Deploy the demo app the agent investigates
kubectl create namespace boutique
kubectl apply -n boutique -f infra/apps/online-boutique/kubernetes-manifests.yaml

# 3. Create the agent's read-only identity and see exactly what it can't do
make rbac && make verify-rbac && make kubeconfig

# 4. Install the project, then point it at your API key
make install && make test
cp .env.example .env        # add ANTHROPIC_API_KEY

# 5. Prove the agent can read the cluster (read-only)
make doctor
```

Already set up and just rebooted? `make up` brings the cluster, Opik, the Boutique
port-forward, and the dashboard up together and waits until they answer. `make up-lite`
skips Opik (~3–4 GB lighter). From Windows use
[`scripts/start-env.ps1`](scripts/start-env.ps1), which also launches Docker Desktop.

## Usage

```bash
# Investigate — gathers evidence, ranks hypotheses, prints an RCA + a PROPOSED fix
sre-agent investigate boutique -w emailservice -a "rollout not progressing"
sre-agent investigate boutique -v          # stream reasoning, tool calls, per-call cost

# Close the loop: allowlist check → server dry-run → confirm → apply → verify recovery
sre-agent investigate boutique -w redis-cart -x

# Regression-test against ground truth (mutates the cluster, costs money, always reverts)
sre-agent eval                             # all 11 incidents
sre-agent eval -i cascade --record         # save the run...
sre-agent eval -i cascade --replay         # ...then re-score it free, no API call

# Browse what the agent has done
sre-agent history                          # recent runs: category, confidence, cost, outcome
make dashboard                             # Streamlit: incident feed, detail pages, analytics
```

Each read-only tool is also a CLI command for driving the cluster by hand: `sre-agent
status | events | logs | rollout | metrics`.

**Let the cluster page the agent** (see [`docs/alerts-plan.md`](docs/alerts-plan.md)):

```bash
kubectl apply -f infra/observability/boutique-alert-rules.yaml
helm upgrade kps prometheus-community/kube-prometheus-stack -n monitoring \
  -f infra/observability/kube-prometheus-stack.values.yaml

sre-agent listen --dry-run   # test the whole pipe free — logs every decision, no LLM call
sre-agent listen             # firing alert → guardrails → autonomous propose-only run
```

## Incidents proven live

All 11 are real `sre-agent eval --record` runs against the actual cluster — not mocked
examples — each with a ground-truth check in [`evals.py`](src/sre_agent/evals.py) and a
saved recording in [`tests/fixtures/recordings/`](tests/fixtures/recordings/) for free
`--replay` scoring. See
[`docs/incident-taxonomy-plan.md`](docs/incident-taxonomy-plan.md) for how each was built.

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
| `network_caller_drift`‡ | An old, correct NetworkPolicy blocks a caller whose OWN rollout dropped a required label | networking | 0.72 | $0.40 |

\* claude-sonnet-5, `AGENT_EFFORT=medium`, after the caching fix.
† Correct category, wrong node named — see Known gaps.
‡ The opposite ground truth from `network_blocked`: it correctly fixed the caller instead
of deleting the policy.

The CrashLoopBackOff scenario has also been diagnosed **fully autonomously**: emailservice
was broken at 20:02:41 with nothing else touched — `BoutiquePodStuck` fired, Alertmanager
called the listener, the guardrails passed, and by 20:07:12 the agent had filed the
incident (workload, 0.85, $0.15, `ModuleNotFoundError` pulled from the crashed container's
`--previous` logs). No human ran anything.

## Three things that broke first

The full set, with the fixes, is in [`docs/engineering-log.md`](docs/engineering-log.md).

**The eval harness catching its own bug.** The first `cascade` run scored the RCA
`scheduling` instead of `dependency` — the model conflated a deliberate scale-down with a
pod that can't be *placed*. The diagnosis was right; the taxonomy was wrong. Fixed with an
explicit category guide injected into both prompts that set `category`, since they're
independent LLM calls with no shared context. Exactly the bug a quick eyeball of a
"looks right" RCA would have missed.

**Memory that talked itself into false confidence — twice, in opposite directions.**
Re-investigating one persisting incident repeatedly, the RCA began describing near-identical
priors from the same staged fault as "multiple independent confirmations." Fixed by telling
the model that closely-timed identical priors are one event observed repeatedly. That fix
then over-corrected: a genuinely human-confirmed fix stopped being cited at all. **The
lesson generalizes** — a confidence-calibration fix has to be tested in both directions,
because a fix that only proves one side can quietly break the other.

**A crashed tool that produced a confident wrong answer.** Three live runs diagnosed
`saturation` when the real cause was a NetworkPolicy — because `get_network_policies` was
crashing on a client attribute name, and the crash was handed to the model as ordinary text
and read by nothing else: no eval check, no dashboard badge, no mention in the report. A
crashed tool now fails the eval critically, and the report must name what evidence was
missing. An error path that nothing surfaces is indistinguishable from no error handling.

## Known gaps

- **`node_down` names the wrong node.** It infers the `node` category from symptom
  co-location rather than from a tool that directly surfaces which node is
  cordoned/NotReady, so it blames the node most pods happen to share.
- **`network_caller_drift`'s correct fix is gated.** Restoring a dropped pod-template label
  needs `kubectl patch` on `spec.template.metadata.labels`, which the allowlist doesn't
  cover. Arguably correct — that label grants access through a NetworkPolicy — but it means
  a human applies this one by hand.
- **4 stretch incidents** (`pvc_pending`, `rbac_denied`, `hpa_stuck`, `cronjob_failing`)
  each need a new Kubernetes resource type in the cluster first.
- **The cheaper-gather-model swap ships dormant.** An optional open model (Qwen via
  OpenRouter, `GATHER_MODEL` in `.env.example`) measured ~17–18% cheaper on simple
  incidents with no quality loss, but one `cascade` run cost +215% when it produced an
  oversized transcript. Unset by default; not yet decided.
- **Memory's match key is exact namespace + workload.** An alert carrying a pod label but
  no deployment label leaves `workload=None` and can never be recalled.
- **Verification is narrow.** It polls only the workload named in the remediation, not
  downstream services in the correlation chain, and it's a one-shot check at apply time
  rather than a background re-check.
- **Lab-grade operationally.** The listener is a foreground process (Ctrl-C is the kill
  switch), no daemon or HA, and the dashboard is local-only.

## Repository layout

```
infra/           k3d cluster · kube-prometheus-stack + alert rules · Online Boutique · read-only RBAC
src/sre_agent/
  tools/         8 read-only evidence tools + Pydantic schemas
  agent/         LangGraph graph, tool bridge, prompts, state/RCA schemas
  remediation.py the allowlist validator, dry-run/apply gate, and recovery verifier
  evals.py       the 11 scripted incidents (stage/revert/ground-truth) + scoring
  dashboard.py · history_store.py · alerts.py · listener.py · cli.py
scripts/         one-command dev-env startup (start-env.ps1 → start-env.sh)
tests/           unit + live-cluster integration tests (auto-skip when offline)
docs/            architecture diagrams · build log · engineering log · plans
```
