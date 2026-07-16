# Kubernetes Incident Investigator (SRE Agent)

An autonomous agent that investigates Kubernetes incidents when an alert fires —
it pulls evidence, correlates it, produces a root-cause hypothesis, and **proposes**
remediation behind a **human-in-the-loop approval gate**. It authenticates as a
**read-only** identity and can never mutate the cluster itself.

> Learn-in-public project. Full design brief: [`k8s-incident-investigator-brief.md`](k8s-incident-investigator-brief.md).
> Architecture diagrams: [`docs/architecture/`](docs/architecture/).

## Why it's built this way
- **Safety by construction** — a view-only ServiceAccount (RBAC), not just prompt rules.
- **Tools return facts, the LLM reasons** — typed, structured evidence; no scraping.
- **Observe the observer** — OpenTelemetry traces every step and tool call.
- **Provably correct** — an eval harness stages real incidents and scores the RCA in CI.

## Architecture

When an alert fires (or you run the CLI), the agent runs a **LangGraph** state machine.
In `gather` it drives a bounded **ReAct loop** — Claude picks a read-only tool, the tool
runs, the result is fed back — until it has enough evidence; then it emits a **structured
root-cause report** with a *proposed* fix that a human must approve. Every tool call is an
OpenTelemetry span.

```
Alert / CLI ─▶ gather (Claude ⇄ read-only tools) ─▶ correlate ─▶ hypothesize ─▶ rank ─▶ propose ─▶ human approval gate
                     ▲ typed evidence                                                    (never auto-executes)
```

Diagrams (in [`docs/architecture/`](docs/architecture/) — click to view):
- [Investigation chain](docs/architecture/agent-investigation-chain.svg) — alert → gather → RCA → gated fix
- [Agent loop (LangGraph)](docs/architecture/phase3-agent-loop.svg) — the state machine + ReAct loop
- [Read-only tools](docs/architecture/phase2-tools.svg) — the five evidence-gathering tools
- [Agent package](docs/architecture/phase3-agent-files.svg) — how the `src/sre_agent/agent/` files collaborate
- [Tool execution flow](docs/architecture/workload-execution-flow.svg) — raw Kubernetes objects → typed facts

## Local stack
- **Cluster:** k3d / k3s (`sre-lab`) — see [`infra/k3d/`](infra/k3d/). (k3d not kind because
  the dev disk is a slow HDD; k3s's SQLite datastore tolerates it.)
- **Observability:** kube-prometheus-stack — see [`infra/observability/`](infra/observability/).
- **Demo app ("patient"):** Google Online Boutique — see [`infra/apps/online-boutique/`](infra/apps/online-boutique/).
- **Agent:** Python + LangGraph + Claude + read-only Kubernetes/Prometheus tools — [`src/sre_agent/`](src/sre_agent/).

## Status & roadmap
- [x] **1 · Scaffold + read-only RBAC** — view-only ServiceAccount, enforced by the API server
- [x] **2 · Read-only tools** — workload · events · logs (`--previous`) · rollout history · PromQL (typed + tested)
- [x] **3 · Agent loop (LangGraph)** — `gather → report` with Claude (adaptive thinking) + structured RCA _(v0)_
- [ ] **4 · Correlation + confidence** — split `correlate/hypothesize/rank`; solve the dependency cascade
- [ ] **5 · Safety gate** — propose → human approves → allowlisted, dry-run remediation
- [ ] **6 · Eval harness** — stage each incident, assert the RCA vs ground truth, CI-gated

_Cross-cutting from day 1: OpenTelemetry tracing + audit log._

## Repository layout
```
infra/           k3d cluster · kube-prometheus-stack · Online Boutique · read-only RBAC
src/sre_agent/
  tools/         the 5 read-only evidence tools (+ Pydantic schemas)
  agent/         LangGraph graph, tool bridge, prompts, state/RCA schemas
  cli.py         status · events · logs · rollout · metrics · investigate
tests/           unit + live-cluster integration tests (auto-skip when offline)
evals/           (Phase 6) ground-truth incident scenarios
docs/architecture/  the diagrams above
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

## Usage
Each read-only tool is also a CLI command (great for driving the cluster by hand):
```bash
sre-agent status boutique                 # deployments + pods (add --json to see the agent's view)
sre-agent events <pod> -n boutique        # events (the smoking gun for ImagePullBackOff)
sre-agent logs <pod> --previous           # crashed-container logs (for CrashLoopBackOff)
sre-agent rollout emailservice            # revision history with per-revision images
sre-agent metrics 'sum(up)'               # PromQL (needs the Prometheus port-forward)
```
Then let the agent investigate on its own (requires `ANTHROPIC_API_KEY` in `.env`):
```bash
sre-agent investigate boutique -w emailservice -a "rollout not progressing"
# → gathers evidence, prints a root-cause report + a PROPOSED fix (never auto-run)
```

## Incidents investigated (derived from manual runs)
| Class | Example | Evidence lens |
|---|---|---|
| Workload | ImagePullBackOff (bad tag) | events |
| Workload | CrashLoopBackOff (crash on start) | logs (`--previous`) |
| Dependency | redis-cart cascade | cross-service correlation |
