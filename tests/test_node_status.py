"""get_node_status: taints + timed events, and the node_down target check.

Pure — fake node/event objects, no cluster. The motivating miss: during a drain of
agent-1 the agent saw "agent-1 is cordoned/unschedulable but not hosting any affected
pods", ruled it out, and blamed server-0 — the node the evicted pods RESTARTED ON. A drain
always leaves the drained node empty, so "hosts nothing" is what a drain looks like, not
an alibi. The node object also never says WHEN a node was cordoned; only an event does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from sre_agent.agent.schemas import RCAReport, Remediation
from sre_agent.agent.tools_bridge import ANTHROPIC_TOOLS
from sre_agent.evals import INCIDENTS, incident_passed, score
from sre_agent.tools.node import _age, _events_by_node, _node_status, get_node_status

NOW = datetime(2026, 9, 20, 9, 0, 0, tzinfo=timezone.utc)


def _node(name, *, unschedulable=False, ready=True, taints=()):
    # a taint is (key, effect) or (key, effect, seconds_ago_it_was_added)
    def taint(t):
        return NS(
            key=t[0], effect=t[1],
            time_added=(NOW - timedelta(seconds=t[2])) if len(t) > 2 else None,
        )

    return NS(
        metadata=NS(name=name),
        spec=NS(
            unschedulable=unschedulable,
            taints=[taint(t) for t in taints] or None,
        ),
        status=NS(
            conditions=[NS(type="Ready", status="True" if ready else "False")],
            node_info=NS(kubelet_version="v1.35.5+k3s1"),
        ),
    )


def _event(node, reason, ago_s, message="msg", *, last=True):
    when = NOW - timedelta(seconds=ago_s)
    return NS(
        involved_object=NS(name=node),
        reason=reason,
        message=message,
        last_timestamp=when if last else None,
        event_time=None,
        first_timestamp=when,
        metadata=NS(creation_timestamp=when),
    )


# --- taints ---------------------------------------------------------------------------

def test_taints_are_rendered_key_and_effect():
    n = _node("a1", unschedulable=True, taints=[("node.kubernetes.io/unschedulable", "NoSchedule")])
    s = _node_status(n, now=NOW)
    assert s.taints == ["node.kubernetes.io/unschedulable:NoSchedule"]  # no timeAdded -> none shown
    assert s.schedulable is False


def test_a_taint_with_time_added_says_when_the_node_was_cordoned():
    # Live-verified on k3s: cordoning stamps the unschedulable taint with timeAdded. That is
    # the durable record of WHEN — it lasts as long as the cordon, unlike the ~1h event.
    n = _node("a1", unschedulable=True, taints=[("node.kubernetes.io/unschedulable", "NoSchedule", 130)])
    s = _node_status(n, now=NOW)
    assert s.taints == [
        "node.kubernetes.io/unschedulable:NoSchedule (added 2m ago, 2026-09-20T08:57:50Z)"
    ]


def test_a_node_with_no_taints_has_an_empty_list_not_none():
    assert _node_status(_node("a0")).taints == []


# --- age formatting -------------------------------------------------------------------

def test_age_uses_a_human_scale():
    assert _age(NOW - timedelta(seconds=40), NOW) == "40s ago"
    assert _age(NOW - timedelta(minutes=12), NOW) == "12m ago"
    assert _age(NOW - timedelta(hours=5), NOW) == "5h ago"
    assert _age(NOW - timedelta(days=3), NOW) == "3d ago"


def test_age_never_goes_negative_on_clock_skew():
    assert _age(NOW + timedelta(seconds=30), NOW) == "0s ago"


# --- events ---------------------------------------------------------------------------

def test_events_are_grouped_by_node_newest_first_with_age_and_timestamp():
    events = [
        _event("a1", "NodeReady", 3600),
        _event("a1", "NodeNotSchedulable", 40, "Node a1 status is now: NodeNotSchedulable"),
        _event("s0", "NodeReady", 7200),
    ]
    by = _events_by_node(events, NOW)
    assert by["a1"][0].startswith("NodeNotSchedulable 40s ago (2026-09-20T08:59:20Z)")
    assert "status is now: NodeNotSchedulable" in by["a1"][0]
    assert by["a1"][1].startswith("NodeReady 60m ago")  # minutes up to 90m, then hours
    assert len(by["s0"]) == 1


def test_events_are_capped_per_node():
    events = [_event("a1", f"E{i}", i * 10) for i in range(12)]
    assert len(_events_by_node(events, NOW)["a1"]) == 5


def test_events_fall_back_when_last_timestamp_is_unset():
    by = _events_by_node([_event("a1", "NodeNotSchedulable", 60, last=False)], NOW)
    assert by["a1"][0].startswith("NodeNotSchedulable 60s ago")


def test_events_with_no_time_or_no_node_are_skipped_not_crashed_on():
    undated = NS(
        involved_object=NS(name="a1"), reason="X", message="", last_timestamp=None,
        event_time=None, first_timestamp=None, metadata=NS(creation_timestamp=None),
    )
    nameless = NS(
        involved_object=None, reason="Y", message="", last_timestamp=NOW,
        event_time=None, first_timestamp=NOW, metadata=NS(creation_timestamp=NOW),
    )
    assert _events_by_node([undated, nameless], NOW) == {}


# --- the tool, end to end over fake clients ------------------------------------------

def test_get_node_status_attaches_events_to_the_right_node_and_filters_to_node_events():
    seen = {}

    def list_events(field_selector=None):
        seen["selector"] = field_selector
        return NS(items=[_event("a1", "NodeNotSchedulable", 30)])

    clients = {
        "core": NS(
            list_node=lambda: NS(items=[_node("a1", unschedulable=True), _node("s0")]),
            list_event_for_all_namespaces=list_events,
        )
    }
    nodes = {n.name: n for n in get_node_status(clients=clients, now=NOW)}
    assert seen["selector"] == "involvedObject.kind=Node"  # not every event in the cluster
    assert nodes["a1"].schedulable is False
    assert nodes["a1"].recent_events[0].startswith("NodeNotSchedulable 30s ago")
    assert nodes["s0"].recent_events == []


# --- the reasoning guidance actually reaches the model --------------------------------

def test_tool_description_says_a_drained_node_hosting_nothing_is_not_ruled_out():
    desc = next(t for t in ANTHROPIC_TOOLS if t["name"] == "get_node_status")["description"]
    assert "NOT ruled out" in desc
    assert "drain" in desc
    assert "restart on the OTHER nodes" in desc


# --- eval: the root cause itself must name the right node ------------------------------

_NODE_DOWN = next(i for i in INCIDENTS if i.name == "node_down")


def _report(root_cause, alternatives=()):
    return RCAReport(
        summary="pods restarted together",
        root_cause=root_cause,
        category="node",
        confidence="medium",
        confidence_score=0.7,
        alternatives=list(alternatives),
        impact="multiple services restarted",
        remediation=Remediation(action="inspect", command="kubectl get nodes", rationale="x"),
    )


def _by_name(checks):
    return {c.name: c for c in checks}


def test_blaming_the_landing_node_fails_even_if_the_drained_node_is_mentioned_and_dismissed():
    # The real recorded failure: the report mentioned agent-1 — while ruling it out.
    report = _report(
        "Node k3d-sre-lab-server-0 periodically experiences memory pressure and evicts pods",
        alternatives=["get_node_status shows agent-1 is cordoned but not hosting any affected pods"],
    )
    checks = score(report, _NODE_DOWN)
    by = _by_name(checks)
    assert by["root_cause_match"].passed          # the old keyword check is fooled
    assert not by["root_cause_names_target"].passed
    assert by["root_cause_names_target"].critical
    assert not incident_passed(checks)


def test_naming_the_drained_node_in_the_root_cause_passes():
    report = _report("Node k3d-sre-lab-agent-1 was cordoned and drained, evicting its pods")
    checks = score(report, _NODE_DOWN)
    assert _by_name(checks)["root_cause_names_target"].passed
    assert incident_passed(checks)


def test_the_target_match_is_case_insensitive():
    assert _by_name(score(_report("AGENT-1 was drained"), _NODE_DOWN))["root_cause_names_target"].passed


def test_other_incidents_get_no_target_check():
    # Opt-in only — every other incident keeps exactly its old check list.
    cascade = next(i for i in INCIDENTS if i.name == "cascade")
    assert "root_cause_names_target" not in _by_name(score(_report("redis-cart scaled to 0"), cascade))
