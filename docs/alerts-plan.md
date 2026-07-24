# Phase 9: Alert-triggered investigations — build plan

**Status: scoped, not yet implemented.** Written so a fresh session (no prior
conversation context) can pick this up and build it directly. Read this file top to
bottom before writing any code.

## What this is

Today the agent only investigates when a human runs `sre-agent investigate`. This phase
closes the loop on the *noticing* side: an alert fires in the cluster's existing
Alertmanager (kube-prometheus-stack, release `kps`, ns `monitoring`) → a small local
listener receives the webhook → the agent investigates automatically → the run lands in
`data/history.db` like any other → it appears in the dashboard's incident feed, marked
as alert-triggered. The story changes from "a CLI I invoke" to "an agent watching my
cluster."

`IncidentContext` (src/sre_agent/agent/schemas.py) was designed for this from Phase 1 —
its docstring already reads "from an alert or the CLI." Nothing in the agent core
changes; this phase is purely a new *trigger path* in front of the existing
`investigate()`.

## Non-negotiable safety posture

- **Propose-mode only, forever.** An alert-triggered run NEVER runs the approval gate,
  never touches `--execute`. Autonomy applies to noticing + diagnosing; mutation always
  goes through a human reading the proposal (dashboard/CLI) and running the fix, or
  re-running `investigate -x` themselves.
- **Bounded spend.** Each investigation costs ~$0.15. Guardrails, all in `Settings`
  (env-overridable):
  - `alert_daily_run_cap` (default **5**): max auto-investigations per calendar day.
    Counted by querying `runs` where `triggered_by='alert'` and `started_at` is today —
    no new state file.
  - `alert_cooldown_minutes` (default **30**): skip if a run for the same
    (namespace, alertname) exists within the window. Also queried from `runs`.
  - Kill switch: the listener is a foreground process (`sre-agent listen`); Ctrl-C ends
    all autonomy. No daemonization in this phase.
- Every skipped alert is still logged to the console with the reason
  (cooldown / cap / filtered / resolved-status), so the operator sees what the agent
  *declined* to do — silence is not ambiguity.

## Architecture decision: webhook receiver, not polling

Two options considered:
1. **Webhook receiver (chosen)** — Alertmanager POSTs its standard webhook payload to a
   local HTTP listener. Event-driven, uses Alertmanager's own grouping/dedup
   (`group_wait`, `repeat_interval`) as the first debounce layer, and is the pattern a
   real SRE team would wire into PagerDuty/Slack.
2. Polling the Alertmanager API on an interval — no inbound wiring, but laggy, ignores
   Alertmanager's grouping, and reads as a hack. Rejected.

Reachability (verify early — this is the riskiest assumption): the listener runs in WSL
Ubuntu on the host; the cluster runs in k3d containers on the same Docker. k3d injects
**`host.k3d.internal`** into the cluster's CoreDNS, resolving to the host — so the
in-cluster Alertmanager can POST to `http://host.k3d.internal:9095/alerts` with the
listener bound to `0.0.0.0:9095`. Smoke-test with a `curl` from a cluster pod before
debugging anything else:
`kubectl -n monitoring exec alertmanager-kps-kube-prometheus-stack-alertmanager-0 -- wget -qO- --post-data='{}' http://host.k3d.internal:9095/healthz` (or similar).

## Components

### 1. `src/sre_agent/alerts.py` — payload models + trigger policy (pure, unit-testable)

- Pydantic models for the Alertmanager v4 webhook payload (only the fields we use):
  `WebhookPayload{status, alerts: [Alert{status, labels: dict, annotations: dict,
  startsAt, fingerprint}], commonLabels, ...}`.
- `to_incident_context(alert) -> IncidentContext`:
  - `namespace` = `labels["namespace"]` (required — alerts without it are filtered out).
  - `workload` = `labels.get("deployment")` if present (KubeDeployment* alerts have it),
    else None — pod-level alerts (e.g. KubePodCrashLooping) pass the pod name inside the
    alert text instead and let the agent locate the workload; deriving a deployment name
    by stripping pod-name hash suffixes is a heuristic we deliberately avoid.
  - `alert` = `"{alertname}: {annotations.summary or description}"` — the same free-text
    field the CLI's `--alert` flag fills today.
- `should_investigate(alert, now, recent_runs) -> Decision` — returns
  `go | skip(reason)` applying, in order: status is `firing` (never investigate
  `resolved`), namespace allowlist (`alert_namespaces`, default `["boutique"]`),
  cooldown, daily cap. Pure function over passed-in data → trivially unit-testable, no
  cluster or clock mocking beyond `now`.

### 2. `src/sre_agent/listener.py` + `sre-agent listen` CLI command

- Starlette app (new `alerts` extra in pyproject: `starlette`, `uvicorn` — both already
  land transitively with the `dashboard` extra, but declare them explicitly; never rely
  on a transitive dep) with two routes:
  - `POST /alerts` — parse payload; for each firing alert run the policy; if `go`,
    run the investigation **synchronously** (one at a time — an SRE lab does not need a
    queue, and serializing is itself a spend guardrail; Alertmanager retries/re-groups
    fine if the webhook is slow) and `history_store.save_run(..., mode="propose",
    triggered_by="alert")`.
  - `GET /healthz` — for the reachability smoke test.
- `sre-agent listen [--port 9095]` — starts uvicorn, prints the policy in effect
  (namespaces, cap, cooldown) at startup so a demo screenshot shows the guardrails.

### 3. History schema: `triggered_by` column

- Add `triggered_by TEXT NOT NULL DEFAULT 'manual'` to the `runs` table. Migration:
  after `CREATE TABLE IF NOT EXISTS`, attempt
  `ALTER TABLE runs ADD COLUMN triggered_by TEXT NOT NULL DEFAULT 'manual'` inside a
  `try/except sqlite3.OperationalError` (idempotent, no migration framework for a
  single-user file).
- `save_run()` gains `triggered_by: str = "manual"`.
- Dashboard: feed card + detail header band get an "auto" chip when
  `triggered_by == "alert"`; the Analytics funnel can later split manual vs auto.
- CLI `sre-agent history` list gains nothing (column stays narrow) — detail view prints it.

### 4. Alert rules that actually fire fast: `infra/observability/boutique-alert-rules.yaml`

The stock kube-prometheus-stack rules mostly need 15m of sustained failure before
firing — correct for production, terrible for a demo. Ship a `PrometheusRule` with a
small set of fast boutique rules (**label it `release: kps`** or the operator's
`ruleSelector` will not discover it):
- `BoutiqueDeploymentUnavailable`: `kube_deployment_status_replicas_available == 0 and
  kube_deployment_spec_replicas > 0` for **2m** → covers the cascade eval incident.
  (Note: `spec_replicas > 0` keeps a deliberate scale-to-0 from firing this; the
  cascade incident's scale-to-0 sets `spec_replicas` to 0, so cover it with the
  KubeDeploymentReplicasMismatch-style rule below or alert on the *victims*: cartservice
  crash/restart symptoms. Decide when writing the rules — the eval incidents are the
  test fixtures, so the rules must be written against what those incidents actually do
  to the metrics.)
- `BoutiquePodCrashLooping`: restarts increasing over 4m in ns boutique → covers the
  crash_loop and image_pull eval incidents.
- Namespace-scoped to `boutique` so the rest of the cluster stays on stock rules.

### 5. Alertmanager routing: update `infra/observability/kube-prometheus-stack.values.yaml`

Add to the Helm values (then `helm upgrade kps ... -n monitoring -f <values>`):

```yaml
alertmanager:
  config:
    route:
      routes:
        - matchers: ['namespace = "boutique"']
          receiver: sre-agent
          group_by: ["alertname", "namespace"]
          group_wait: 30s
          repeat_interval: 4h
    receivers:
      - name: sre-agent
        webhook_configs:
          - url: "http://host.k3d.internal:9095/alerts"
            send_resolved: false   # we act on firing only; resolved is noise for us
```

(Exact merge point depends on the existing values file — read it first; kps default
config has a `route`/`receivers` block to extend, and the default `"null"` receiver
must remain the fallback.)

## Order of work

1. `alerts.py` models + policy + `tests/test_alerts.py` (synthetic payloads: firing,
   resolved, wrong-namespace, cooldown hit, cap hit). Pure Python — no cluster, no cost.
2. `triggered_by` column + `save_run` param + migration guard + test.
3. `listener.py` + `sre-agent listen` + `alerts` extra. Local test with `curl` posting a
   canned Alertmanager payload — but with `ANTHROPIC_API_KEY` unset or a `--dry-run`
   listener flag (log the decision, skip the LLM call) so the plumbing test is free.
4. Reachability smoke test (`host.k3d.internal` from a cluster pod → `/healthz`).
5. PrometheusRule + Alertmanager values + `helm upgrade`. Verify the rule appears in
   Prometheus (`/api/v1/rules`) and the route in Alertmanager UI.
6. **One paid end-to-end demo**: `sre-agent listen` running → stage the crash_loop eval
   incident by hand (`kubectl` from evals.py's stage list) → wait for the rule to fire
   (~2-4m) → webhook → auto-investigation → revert incident → show the run in the
   dashboard with the "auto" chip. Budget: 1 run ≈ $0.15.
7. Dashboard/CLI `triggered_by` chips (small; can ride along with step 2's PR or come last).

## Explicitly out of scope (this phase)

- Auto-execute of remediations (permanently out, not just this phase).
- Live-streaming an in-progress investigation into the dashboard (separate phase; the
  feed just shows the finished run after the listener saves it).
- Slack/e-mail notification of completed auto-investigations (natural stretch goal;
  note it in the README's roadmap but don't build).
- Daemonizing/systemd-izing the listener; multi-cluster; HA. It's a foreground lab tool.

## Open questions (defaults chosen; user can override)

- Daily cap default 5 and cooldown 30m — sane for a lab on Sonnet pricing?
- Namespace allowlist default `["boutique"]` — expand to `monitoring` self-alerts later?
- Does the demo also want `send_resolved: true` + a "resolved" annotation on the
  incident row someday? (Currently: no — firing-only keeps the model simple.)
