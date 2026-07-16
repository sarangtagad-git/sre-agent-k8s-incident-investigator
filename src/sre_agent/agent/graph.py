"""The LangGraph investigation agent: gather (ReAct loop) -> report (structured RCA).

Uses the official Anthropic SDK for model calls (adaptive thinking + effort), a
manual tool-calling loop for `gather`, and structured outputs for the final RCA.
The `correlate/hypothesize/rank` steps are folded into Claude's reasoning for v0
and will be split into their own nodes in Phase 4.
"""

from __future__ import annotations

import anthropic
from langgraph.graph import END, START, StateGraph

from ..config import get_settings
from ..k8s import load_readonly_clients
from ..observability import get_tracer
from .prompts import REPORT_INSTRUCTION, SYSTEM_PROMPT, render_incident
from .schemas import AgentState, IncidentContext, RCAReport
from .tools_bridge import ANTHROPIC_TOOLS, execute_tool

_tracer = get_tracer()


def _build_graph(client: anthropic.Anthropic, clients: dict, settings):
    def gather(state: AgentState) -> dict:
        """ReAct loop: Claude picks a tool, we run it, repeat until it stops or caps out."""
        with _tracer.start_as_current_span("agent.gather"):
            messages = state["messages"]
            evidence = state["evidence"]
            iterations = state["iterations"]

            while iterations < settings.agent_max_tool_iterations:
                resp = client.messages.create(
                    model=settings.agent_model,
                    max_tokens=16000,
                    system=SYSTEM_PROMPT,
                    thinking={"type": "adaptive"},
                    output_config={"effort": settings.agent_effort},
                    tools=ANTHROPIC_TOOLS,
                    messages=messages,
                )
                iterations += 1
                messages.append({"role": "assistant", "content": resp.content})

                if resp.stop_reason != "tool_use":
                    break  # end_turn / refusal / max_tokens -> done gathering

                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        content, is_error, record = execute_tool(block.name, dict(block.input), clients)
                        evidence.append(record)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": content,
                                "is_error": is_error,
                            }
                        )
                messages.append({"role": "user", "content": tool_results})

            return {"messages": messages, "evidence": evidence, "iterations": iterations}

    def report(state: AgentState) -> dict:
        """Turn the gathered evidence into a structured RCA (tools disabled here)."""
        with _tracer.start_as_current_span("agent.report"):
            messages = state["messages"] + [{"role": "user", "content": REPORT_INSTRUCTION}]
            parsed = client.messages.parse(
                model=settings.agent_model,
                max_tokens=4000,
                messages=messages,
                tools=ANTHROPIC_TOOLS,
                tool_choice={"type": "none"},
                output_format=RCAReport,
            )
            return {"report": parsed.parsed_output}

    g = StateGraph(AgentState)
    g.add_node("gather", gather)
    g.add_node("report", report)
    g.add_edge(START, "gather")
    g.add_edge("gather", "report")
    g.add_edge("report", END)
    return g.compile()


def investigate(incident: IncidentContext) -> RCAReport:
    """Run a full investigation and return the structured RCA."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (copy .env.example -> .env and add it).")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    clients = load_readonly_clients()

    with _tracer.start_as_current_span("agent.investigate") as span:
        span.set_attribute("incident.namespace", incident.namespace)
        app = _build_graph(client, clients, settings)
        initial: AgentState = {
            "incident": incident,
            "messages": [{"role": "user", "content": render_incident(incident)}],
            "evidence": [],
            "iterations": 0,
            "report": None,
        }
        final = app.invoke(initial)
        span.set_attribute("agent.iterations", final["iterations"])
        return final["report"]
