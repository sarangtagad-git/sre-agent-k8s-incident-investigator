"""The LangGraph investigation agent: gather (ReAct loop) -> report (structured RCA).

Uses the official Anthropic SDK for model calls (adaptive thinking + effort), a
manual tool-calling loop for `gather`, and structured outputs for the final RCA.
Prompt caching is applied to the stable system+tools prefix to cut cost. With
verbose=True, each step (reasoning summary, tool call, result) is streamed to the
console — useful for understanding and demos.

The `correlate/hypothesize/rank` steps are folded into Claude's reasoning for v0
and will be split into their own nodes in Phase 4.
"""

from __future__ import annotations

import anthropic
from langgraph.graph import END, START, StateGraph
from rich.console import Console

from ..config import get_settings
from ..k8s import load_readonly_clients
from ..observability import get_tracer
from .prompts import REPORT_INSTRUCTION, SYSTEM_PROMPT, render_incident
from .schemas import AgentState, IncidentContext, RCAReport
from .tools_bridge import ANTHROPIC_TOOLS, execute_tool

_tracer = get_tracer()

# Approx $ per 1M tokens (input, output) — for the verbose cost estimate only.
_PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}


def _price_for(model: str) -> tuple[float, float]:
    for key, price in _PRICES.items():
        if key in model:
            return price
    return _PRICES["opus"]


def _build_graph(client, clients, settings, verbose=False, console=None):
    # display=summarized surfaces Claude's reasoning summary (billed either way — free to show)
    thinking = {"type": "adaptive", "display": "summarized"} if verbose else {"type": "adaptive"}

    def say(msg, style=""):
        if console:
            console.print(msg, style=style)

    # Accumulate token usage across every model call in the run.
    totals = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}

    def track(usage) -> None:
        inp = getattr(usage, "input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        totals["input"] += inp
        totals["cache_write"] += cw
        totals["cache_read"] += cr
        totals["output"] += out
        say(f"     [dim]tokens: in={inp}  cache(write={cw} read={cr})  out={out}[/]")

    def gather(state: AgentState) -> dict:
        with _tracer.start_as_current_span("agent.gather"):
            messages = state["messages"]
            evidence = state["evidence"]
            iterations = state["iterations"]
            say("\n[bold cyan]▶ gather[/] — investigating with read-only tools")

            while iterations < settings.agent_max_tool_iterations:
                resp = client.messages.create(
                    model=settings.agent_model,
                    max_tokens=16000,
                    system=SYSTEM_PROMPT,
                    thinking=thinking,
                    output_config={"effort": settings.agent_effort},
                    tools=ANTHROPIC_TOOLS,
                    messages=messages,
                    cache_control={"type": "ephemeral"},  # cache the stable prefix -> cheaper
                )
                iterations += 1
                messages.append({"role": "assistant", "content": resp.content})
                track(resp.usage)

                if console:
                    for block in resp.content:
                        if block.type == "thinking" and getattr(block, "thinking", "").strip():
                            say(f"  [dim italic]🧠 {block.thinking.strip()}[/]")
                        elif block.type == "text" and block.text.strip():
                            say(f"  [white]{block.text.strip()}[/]")

                if resp.stop_reason != "tool_use":
                    break

                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        args = ", ".join(f"{k}={v}" for k, v in dict(block.input).items())
                        say(f"  [yellow]🔧 {block.name}[/]([dim]{args}[/])")
                        content, is_error, record = execute_tool(block.name, dict(block.input), clients)
                        evidence.append(record)
                        mark = "[red]✗[/]" if is_error else "[green]✓[/]"
                        say(f"     {mark} [dim]{record.summary}[/]")
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
        with _tracer.start_as_current_span("agent.report"):
            say("\n[bold cyan]▶ report[/] — synthesizing the root-cause analysis…")
            messages = state["messages"] + [{"role": "user", "content": REPORT_INSTRUCTION}]
            parsed = client.messages.parse(
                model=settings.agent_model,
                max_tokens=4000,
                messages=messages,
                tools=ANTHROPIC_TOOLS,
                tool_choice={"type": "none"},
                output_format=RCAReport,
                cache_control={"type": "ephemeral"},
            )
            if getattr(parsed, "usage", None) is not None:
                track(parsed.usage)
            if console:
                price_in, price_out = _price_for(settings.agent_model)
                est = (
                    totals["input"]
                    + totals["cache_write"] * 1.25
                    + totals["cache_read"] * 0.10
                ) * price_in / 1e6 + totals["output"] * price_out / 1e6
                saved = totals["cache_read"] * 0.90 * price_in / 1e6  # vs paying full price
                say(
                    f"\n[bold]run totals[/] — input={totals['input']}  "
                    f"cache(write={totals['cache_write']} read={totals['cache_read']})  "
                    f"output={totals['output']}"
                )
                say(
                    f"[bold]est. cost[/] ~${est:.4f}  "
                    f"[dim](approx, {settings.agent_model}; caching saved ~${saved:.4f})[/]"
                )
            return {"report": parsed.parsed_output}

    g = StateGraph(AgentState)
    g.add_node("gather", gather)
    g.add_node("report", report)
    g.add_edge(START, "gather")
    g.add_edge("gather", "report")
    g.add_edge("report", END)
    return g.compile()


def investigate(incident: IncidentContext, verbose: bool = False) -> RCAReport:
    """Run a full investigation and return the structured RCA.

    verbose=True streams each step (reasoning summary, tool call, result) to stdout.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (copy .env.example -> .env and add it).")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    clients = load_readonly_clients()
    console = Console() if verbose else None

    with _tracer.start_as_current_span("agent.investigate") as span:
        span.set_attribute("incident.namespace", incident.namespace)
        app = _build_graph(client, clients, settings, verbose=verbose, console=console)
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
