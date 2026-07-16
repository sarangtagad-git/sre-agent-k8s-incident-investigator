# Kubernetes Incident Investigator (SRE Agent)

> Learn-in-public agentic AI project. An autonomous agent that investigates Kubernetes
> incidents when an alert fires — pulls evidence, correlates it, and produces a
> root-cause hypothesis with a human-in-the-loop approval gate before any remediation.

---

## 1. What we're building

When a Prometheus alert fires (e.g. a pod is in `CrashLoopBackOff`, a node is under
memory pressure, a service is erroring), the agent autonomously:

1. **Gathers evidence** — pod events, logs, recent deployments/rollouts, node
   conditions, and relevant Prometheus/Grafana metrics.
2. **Correlates** the signals into a coherent picture of what changed and when.
3. **Produces a root-cause hypothesis** with the supporting evidence attached.
4. **Proposes remediation** — but does NOT auto-execute. A human-in-the-loop
   approval gate is the headline design decision and the "responsible agentic AI"
   story worth writing about.

The differentiator vs. prior work (AxionOps/InfraAgent, which provisioned infra):
this agent **diagnoses and reasons over live cluster state**, it doesn't provision.

---

## 2. Why it's a strong learn-in-public project

- **Hot category, proven demand:** AI SRE is a funded space — Resolve.ai (~$1B
  valuation), Cleric, and Parity are all closed-source, which proves demand while
  leaving room for an open, learn-in-public version.
- **Open-source prior art to study & later contribute to:** HolmesGPT (Apache 2.0,
  CNCF Sandbox — Prometheus/Grafana/Helm native), Aurora by Arvo AI (Apache 2.0,
  LangGraph-based), K8sGPT (CNCF).
- **Maps directly onto target roles** (Grafana Labs, Civo, Astronomer, SigNoz):
  reuses existing Prometheus/Grafana/Kubernetes credibility.
- **Extremely demo-able:** "watch the agent debug a CrashLoopBackOff live" is a
  strong LinkedIn/portfolio video.

---

## 3. Local-first stack (zero cloud budget)

No EKS required — everything runs locally, which is *better* here because the
project depends on deliberately breaking things.

| Layer            | Choice                                                        | Notes |
|------------------|---------------------------------------------------------------|-------|
| **Cluster**      | **kind** (multi-node, 2 workers)                              | Upstream K8s in Docker → production-fidelity events/conditions. k3d if RAM-constrained. |
| **Observability**| **kube-prometheus-stack** Helm chart                          | Bundles Prometheus + Grafana + Alertmanager + node-exporter. Feeds agent metrics AND fires the alerts. |
| **Logs**         | Loki + Promtail (optional at first)                          | To start, just read pod logs via the K8s API — simpler. |
| **Chaos**        | Hand-rolled failures first, then **chaos-mesh**              | Start hand-rolled: you control exactly what the agent should find. |
| **Demo app**     | Google "Online Boutique" (microservices-demo)               | Enough services to make investigations non-trivial. |
| **Agent**        | **LangGraph** + K8s Python client + Prometheus HTTP API      | Tools = evidence-gathering functions. Reuses LangGraph knowledge from AxionOps. |

**Resource budget:** kind + kube-prometheus-stack + demo app ≈ 6–8 GB RAM.
If tight: use k3d instead of kind, skip Loki initially.

---

## 4. Build sequence (do NOT build the agent first)

The ordering is deliberate — you must do one investigation *by hand* before you
know what tools and reasoning steps to give the agent.

1. **Cluster + observability up.** Stand up kind (multi-node) + kube-prometheus-stack.
   Confirm you can query Prometheus and pull pod events manually.
2. **Deploy the demo app.** Online Boutique / microservices-demo.
3. **Break something by hand & trace it yourself end-to-end.**
   Start with hand-rolled failures:
   - bad image tag → `CrashLoopBackOff`
   - too-low memory limit → `OOMKilled`
   - bad readiness probe → never-ready
4. **Only now build the agent.** You now know exactly which tools + reasoning steps
   it needs, because you just performed the investigation yourself.
5. **Add the human-in-the-loop approval gate** for proposed remediation.
6. **Scope to ~3 failure modes done well.** A tight, working agent + clean README +
   architecture diagram + 3-min demo video beats a sprawling half-finished one.

---

## 5. Agent design notes

- **Tools (evidence-gathering functions the agent can call):**
  - get pod events / describe pod
  - get pod logs (via K8s API to start)
  - list recent deployments / rollout history
  - get node conditions / resource pressure
  - query Prometheus (HTTP API) for relevant metrics
- **Orchestration:** LangGraph state machine — gather → correlate → hypothesize →
  propose. Consider a ReAct-style loop (study HolmesGPT as prior art here).
- **The guardrail is the point:** agent proposes, human approves. Never auto-execute
  remediation. This is the responsible-AI narrative.

---

## 6. Learn-in-public arc

Structure as a **series**, not a single reveal:
- Design decisions (why local-first, why LangGraph, why a human gate)
- Failure stories (what the agent got wrong and how you constrained it)
- Guardrail choices (the approval gate, read-only access)
- Final: architecture diagram + demo video

**Optional follow-on:** after building your own, contribute to **HolmesGPT**
(CNCF Sandbox → strong resume credential). Contributions from someone who's clearly
wrestled with the same problems get merged faster.

---

## 7. Immediate next step

Set up the local cluster: kind multi-node config, kube-prometheus-stack Helm
install, and a first hand-rolled failure scenario to trace manually.
