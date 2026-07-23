"""The LangGraph investigation agent.

Pipeline (Phase 4): gather -> correlate -> hypothesize -> rank -> propose.

  gather       ReAct tool loop; collects evidence with the read-only tools.
  correlate    LLM: builds a timeline + (for cascades) a dependency chain.
  hypothesize  LLM: emits competing root-cause hypotheses, each with a confidence.
  rank         pure Python: sorts hypotheses by confidence — the cheap, deterministic step.
  propose      LLM: writes the final structured RCA on the top hypothesis + a gated fix.

Model calls use the official Anthropic SDK (adaptive thinking + effort). The gather
loop caches the stable system+tools prefix to cut cost. With verbose=True each step
streams to the console. The agent is read-only; the proposed fix is never executed.
"""

from __future__ import annotations

import json
import re

import anthropic
from langgraph.graph import END, START, StateGraph
from rich.console import Console

from ..config import get_settings
from ..k8s import load_readonly_clients
from ..observability import get_tracer
from .prompts import (
    CORRELATE_INSTRUCTION,
    HYPOTHESIZE_INSTRUCTION,
    REPORT_INSTRUCTION,
    SYSTEM_PROMPT,
    render_incident,
)
from .schemas import (
    AgentState,
    Correlation,
    Hypotheses,
    IncidentContext,
    RCAReport,
)
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
        if usage is None:
            return
        inp = getattr(usage, "input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        totals["input"] += inp
        totals["cache_write"] += cw
        totals["cache_read"] += cr
        totals["output"] += out
        say(f"     [dim]tokens: in={inp}  cache(write={cw} read={cr})  out={out}[/]")

    def _analyze_call(messages, ask, force_no_tools=False):
        # Mirror gather's call shape (system/tools/thinking/cache_control, tool_choice left
        # at the default "auto") so the request prefix matches gather's cached prefix and
        # this call only pays full price for its own newly-appended tail, not the whole
        # transcript. A messages.parse()/output_config.format rewrite was tried instead (to
        # get schema-validated JSON) but each call has a DIFFERENT schema, so the schema gets
        # baked into the prefix and breaks the cache match across calls — cost went UP. Hence:
        # ask for JSON in the prompt and parse it by hand.
        kwargs = dict(
            model=settings.agent_model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            thinking=thinking,
            output_config={"effort": settings.agent_effort},
            tools=ANTHROPIC_TOOLS,
            messages=messages + [{"role": "user", "content": ask}],
            cache_control={"type": "ephemeral"},
        )
        if force_no_tools:
            kwargs["tool_choice"] = {"type": "none"}
        resp = client.messages.create(**kwargs)
        track(resp.usage)
        return resp

    def _extract_json_object(text: str) -> dict:
        text = text.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        return json.loads(text)

    def analyze(messages, instruction, model_cls):
        """One structured-output analysis call over the gathered evidence (no new tools)."""
        schema_hint = json.dumps(model_cls.model_json_schema())
        ask = (
            f"{instruction}\n\nDo not call any tools. Respond with ONLY a single JSON "
            "object (no markdown fences, no prose before or after) matching this JSON "
            f"schema:\n{schema_hint}"
        )
        resp = _analyze_call(messages, ask)
        if resp.stop_reason == "tool_use":
            say("     [dim](model reached for a tool instead of JSON — forcing tool_choice=none)[/]")
            resp = _analyze_call(messages, ask, force_no_tools=True)

        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        try:
            return model_cls.model_validate(_extract_json_object(text))
        except (json.JSONDecodeError, ValueError) as exc:
            say(f"     [dim](JSON parse failed: {exc} — retrying with a firmer instruction)[/]")
            firm_ask = ask + "\n\nYour previous reply was not valid JSON. Output ONLY the JSON object, nothing else."
            resp = _analyze_call(messages, firm_ask, force_no_tools=True)
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return model_cls.model_validate(_extract_json_object(text))

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

    def correlate(state: AgentState) -> dict:
        with _tracer.start_as_current_span("agent.correlate"):
            say("\n[bold cyan]▶ correlate[/] — building the timeline + dependency chain…")
            corr: Correlation = analyze(state["messages"], CORRELATE_INSTRUCTION, Correlation)
            if console:
                for e in corr.timeline:
                    say(f"  [dim]•[/] [white]{e.when}[/] — {e.what}")
                if corr.dependency_chain:
                    say("  [magenta]chain:[/] " + " [dim]→[/] ".join(corr.dependency_chain))
                say(f"  [dim]changed:[/] {corr.what_changed}")
            return {"correlation": corr}

    def hypothesize(state: AgentState) -> dict:
        with _tracer.start_as_current_span("agent.hypothesize"):
            say("\n[bold cyan]▶ hypothesize[/] — weighing competing root causes…")
            result: Hypotheses = analyze(state["messages"], HYPOTHESIZE_INSTRUCTION, Hypotheses)
            hyps = result.hypotheses
            if console:
                for h in hyps:
                    say(f"  [dim][{h.confidence:.2f}][/] [white]{h.category}[/]: {h.cause}")
            return {"hypotheses": hyps}

    def rank(state: AgentState) -> dict:
        # Deterministic, no LLM call: order hypotheses by confidence, most-likely first.
        with _tracer.start_as_current_span("agent.rank"):
            ranked = sorted(state["hypotheses"], key=lambda h: h.confidence, reverse=True)
            if ranked:
                top = ranked[0]
                say(
                    f"\n[bold cyan]▶ rank[/] — top: [white]{top.cause}[/] "
                    f"[dim](confidence {top.confidence:.2f})[/]"
                )
            return {"hypotheses": ranked}

    def propose(state: AgentState) -> dict:
        with _tracer.start_as_current_span("agent.propose"):
            say("\n[bold cyan]▶ propose[/] — writing the RCA + gated remediation…")
            corr = state["correlation"]
            ranked = state["hypotheses"]
            # Hand the propose call the structured analysis it should write up (compact JSON).
            analysis = "Your analysis so far:\n"
            if corr is not None:
                analysis += "Correlation: " + corr.model_dump_json() + "\n"
            analysis += "Ranked hypotheses (top first): " + Hypotheses(hypotheses=ranked).model_dump_json()
            report = analyze(state["messages"], analysis + "\n\n" + REPORT_INSTRUCTION, RCAReport)

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
            return {"report": report}

    g = StateGraph(AgentState)
    g.add_node("gather", gather)
    g.add_node("correlate", correlate)
    g.add_node("hypothesize", hypothesize)
    g.add_node("rank", rank)
    g.add_node("propose", propose)
    g.add_edge(START, "gather")
    g.add_edge("gather", "correlate")
    g.add_edge("correlate", "hypothesize")
    g.add_edge("hypothesize", "rank")
    g.add_edge("rank", "propose")
    g.add_edge("propose", END)
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
            "correlation": None,
            "hypotheses": [],
            "report": None,
        }
        final = app.invoke(initial)
        span.set_attribute("agent.iterations", final["iterations"])
        return final["report"]
