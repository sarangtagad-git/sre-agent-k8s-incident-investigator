"""Prompts for the investigation agent — the reasoning we derived by hand."""

from __future__ import annotations

from .schemas import IncidentContext, PriorIncident

SYSTEM_PROMPT = """\
You are an SRE incident investigator for a Kubernetes cluster. You gather evidence \
with read-only tools, correlate it, and produce a root-cause hypothesis. You have \
READ-ONLY access — you cannot and must not change anything; you only PROPOSE a fix \
for a human to approve.

Method (follow it):
1. STATE: get_workload_status to see what's healthy vs not.
2. Pick the next tool from the failure signature — do NOT blindly call every tool:
   - ImagePullBackOff / ErrImagePull  -> get_pod_events (the reason names WHY: NotFound
     = bad tag, denied = auth, timeout = network). Logs are useless here; skip them.
   - CrashLoopBackOff / restarts climbing -> get_pod_logs with previous=true (the crash
     reason lives in the previous instance's logs).
   - A rollout/version change is suspected -> get_rollout_history (see which revision
     changed the image).
   - Errors but every pod looks Ready (a cascade) -> read the alerting service's logs;
     the gRPC/HTTP error text names the downstream it depends on. Follow that chain to
     the service that is actually down (e.g. frontend -> cartservice -> redis-cart).
     Then confirm the leaf with get_workload_status. Use query_prometheus to quantify
     impact and timing.
3. Traps to avoid:
   - "Ready" != "working". A pod can be Ready while failing every request.
   - Distinguish CAUSE from SYMPTOM. Probe failures, downstream 5xx, and high latency
     are usually symptoms of something upstream — don't stop at the first red thing.
   - Ground every claim in a tool result. Never invent cluster state.
4. When you have enough evidence, STOP calling tools and say so in a short message.

Be efficient: gather the minimum evidence that pins the root cause. You will then be \
asked to output a structured RCA report.\
"""

CORRELATE_INSTRUCTION = (
    "Stop gathering. Correlate the evidence you have. Produce:\n"
    "1. timeline — the key events in order (image change, first CrashLoop, first error "
    "spike…), each grounded in a tool result, with when/what.\n"
    "2. dependency_chain — ONLY for a cascade: if the alerting service is Ready but its "
    "logs reference a downstream (a gRPC/HTTP target, a host it dials), trace the chain "
    "from the entrypoint to the failing leaf, leaf LAST (e.g. frontend -> cartservice -> "
    "redis-cart). Leave it empty when the failure is local to one workload.\n"
    "3. what_changed — the single change or event that most plausibly triggered this.\n"
    "Do not diagnose yet; just lay out how the facts fit together."
)

CATEGORY_GUIDE = (
    "Category guide — pick exactly one, the two easiest to confuse are dependency vs "
    "scheduling:\n"
    "- dependency: a Ready/healthy pod cannot reach something else it needs (another "
    "service, a DB, an external API) — including when that something else is a "
    "Deployment someone scaled to 0. The failure is one hop downstream of the pod "
    "reporting the error.\n"
    "- scheduling: a pod is Pending and CANNOT BE PLACED on any node — insufficient "
    "CPU/memory, taints/tolerations, node affinity/anti-affinity. Does NOT apply when "
    "replicas were deliberately set to 0 or a rollout is in progress — there is no "
    "placement attempt to fail in that case.\n"
    "- workload: the failing pod's OWN spec/definition is broken (bad command, bad env "
    "var, crashes on startup) — use this for the workload's own investigation, not for "
    "a caller reporting that workload as unavailable (that caller's report is dependency).\n"
    "- rollout: a new image/revision introduced the break; the fix is a rollout undo.\n"
    "- config, networking, storage, node, saturation, unknown: as named."
)

HYPOTHESIZE_INSTRUCTION = (
    "Now list the candidate root causes as competing hypotheses — plural. Even when one "
    "looks obvious, name at least one alternative you can rule out, so the ranking is "
    "honest. For each hypothesis give: cause, category, a confidence in [0,1] reflecting "
    "how strongly the evidence supports THAT cause, the supporting evidence, and any "
    "evidence against it. Distinguish cause from symptom: a hypothesis that only explains "
    "a downstream symptom must score lower than one that explains the trigger. Ground "
    "every point in a tool result; do not invent cluster state.\n\n" + CATEGORY_GUIDE
)

REPORT_INSTRUCTION = (
    "Using your correlation and the ranked hypotheses (top = most confident), produce the "
    "final root-cause analysis on the TOP hypothesis. Set confidence_score to that "
    "hypothesis's confidence and map it to the confidence band (>=0.8 high, >=0.5 medium, "
    "else low). List the other hypotheses in `alternatives` as \"cause (score): why "
    "rejected\". Cite specific evidence (event reasons, log lines, image tags, metric "
    "values). Propose a single remediation as the exact kubectl command a human would run "
    "— remember it must be approved by a human before anyone runs it.\n\n" + CATEGORY_GUIDE
)


def render_memory_digest(prior: list[PriorIncident]) -> str:
    """Phase 10 (memory): format the recall step's findings for the LLM to weigh —
    never a fact to cite in place of fresh evidence, never a confidence modifier in
    code (see docs/memory-plan.md). Empty string when there's nothing relevant, so no
    filler text like "no prior incidents found" ever reaches the model."""
    if not prior:
        return ""
    lines = [
        "\n\nPrior incidents for this workload (for context only — weigh them, don't "
        "assume they still apply; ground your actual conclusion in THIS investigation's "
        "evidence):"
    ]
    for p in prior:
        conf = f"{p.confidence_score:.2f}" if p.confidence_score is not None else "?"
        lines.append(
            f"- [{p.when}] {p.category or 'unknown'} (confidence {conf}): {p.root_cause}\n"
            f"  fix proposed: {p.remediation_command}\n"
            f"  outcome: {p.outcome_label}"
        )
    return "\n".join(lines)


def render_incident(incident: IncidentContext) -> str:
    lines = [f"Investigate an incident in namespace `{incident.namespace}`."]
    if incident.workload:
        lines.append(f"Suspected workload: `{incident.workload}`.")
    if incident.alert:
        lines.append(f"Alert / symptom: {incident.alert}")
    lines.append("Begin by checking workload status.")
    return "\n".join(lines)
