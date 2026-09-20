# Build log — what shipped, in order

The project was built in numbered phases, each one live-verified before the next started.
This is the record; the README describes the system as it stands now.

## Phases

| # | Phase | What shipped |
|---|---|---|
| 1 | Scaffold + read-only RBAC | View-only ServiceAccount, enforced by the API server — not by prompt rules |
| 2 | Read-only tools | workload · events · logs (`--previous`) · rollout history · PromQL, typed and tested |
| 3 | Agent loop (LangGraph) | `gather → report` with Claude (adaptive thinking) + a structured RCA |
| 4 | Correlation + confidence | Explicit `correlate/hypothesize/rank/propose`; solves the dependency cascade |
| 5 | Safety gate | propose → human approves → allowlisted, server-dry-run remediation |
| 6 | Eval harness | Stages each incident, asserts the RCA against ground truth, exits non-zero on failure |
| 7 | Run history | Every run persisted to SQLite (evidence trail, hypotheses, RCA, cost, outcome) |
| 8 | Dashboard | Streamlit incident feed + investigation detail + Analytics (cache economics, tool usage, approval funnel) |
| 9 | Alert-triggered investigations | Alertmanager → webhook listener → guardrails → autonomous propose-only run; live-demoed end to end |
| 10 | Incident memory | A `recall` node feeds this workload's own past incidents into the RCA — text-only, cluster-checked-fresh-first, eval-isolated |
| 11 | Applied-fix verification | Poll for consecutive healthy checks before calling a fix resolved; the verdict feeds back into memory honestly |

## Since the numbered phases

**Cost pass.** Cut the Phase 4 analysis calls from ~$0.22 to ~$0.16/run by making every
analysis call mirror `gather`'s request shape so they share one prompt cache. The attempt
that made it *worse* first is in [engineering-log.md](engineering-log.md#2-a-caching-fix-that-made-things-worse-before-it-made-them-better).

**Eval taxonomy expansion.** 3 → 11 scripted incident classes, adding OOM, CPU throttling,
unschedulable, bad config, NetworkPolicy block, wrong Service selector, node down, and
NetworkPolicy "Case 1" — a correct policy blocking a caller whose own rollout drifted out
of compliance, the opposite ground truth from the NetworkPolicy block case. Needed three
new read-only tools (`get_network_policies`, `get_service_status`, `get_node_status`), pod
`labels` on `get_workload_status`, and a record/replay mechanism so check-tuning runs
against a saved run instead of the live API. See
[incident-taxonomy-plan.md](incident-taxonomy-plan.md).

**Gate coverage.** Extended `validate_remediation`'s allowlist with scoped,
content-inspected `set resources`/`patch`/`delete` validators, closing a real gap where 4
of 9 correctly-diagnosed fixes were rejected on the verb alone. 9 of 11 pass now; the two
that don't are documented in the README's Known gaps.

**Tracing.** OpenTelemetry spans were wired into every node and tool call from day 1;
they now export to a self-hosted [Opik](https://github.com/comet-ml/opik) instance, so
every LLM call, tool call, and reasoning step of any past run is inspectable end to end.

**Tool-failure visibility.** A crashed tool used to be handed to the model as ordinary
text and read by nothing else — no eval check, no dashboard badge, no mention in the
report. A crash now fails the eval critically, a tool that returns no data warns, and the
report must name what was missing. Story in
[engineering-log.md](engineering-log.md#6-a-crashed-tool-that-produced-a-confident-wrong-answer).

**Node timing.** `get_node_status` reports taints with when they were added and timed node
events, and `node_down` must now name the drained node in the root cause itself. See
[incident-taxonomy-plan.md](incident-taxonomy-plan.md#node_down-revisited--target-attribution-2026-09-20)
and [engineering-log.md](engineering-log.md#7-the-drained-node-that-hosts-nothing--and-a-fix-i-couldnt-fully-prove).

**One-command dev environment.** `make up` brings up the cluster, Opik, the Boutique
port-forward, and the dashboard together, polling until all are ready. `make up-lite`
skips Opik, which is about 3–4 GB lighter.

## Still open

- Demo video + the public write-up.
- Four stretch incidents (`pvc_pending`, `rbac_denied`, `hpa_stuck`, `cronjob_failing`),
  each needing a new Kubernetes resource type in the cluster first.
