"""The LangGraph investigation agent.

Pipeline: gather -> recall -> correlate -> hypothesize -> rank -> propose.

  gather       ReAct tool loop; collects evidence with the read-only tools.
  recall       pure Python (Phase 10): looks up this workload's non-eval history in
               SQLite and hands hypothesize/propose a short, honestly-labeled digest
               of prior incidents — never a fact to cite in place of fresh evidence,
               never a confidence modifier in code. See docs/memory-plan.md. Runs
               AFTER gather (evidence-gathering stays memory-blind by design) and is
               skipped entirely for eval-mode incidents, which must stay a cold
               regression test.
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
import time
import uuid

import anthropic
import httpx
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace
from rich.console import Console

from .. import history_store
from ..config import get_settings
from ..k8s import load_readonly_clients
from ..observability import get_tracer, setup_tracing, truncate_for_trace as _truncate
from .prompts import (
    CORRELATE_INSTRUCTION,
    HYPOTHESIZE_INSTRUCTION,
    REPORT_INSTRUCTION,
    SYSTEM_PROMPT,
    render_incident,
    render_memory_digest,
)
from .schemas import (
    AgentState,
    Correlation,
    Hypotheses,
    Hypothesis,
    IncidentContext,
    PriorIncident,
    RCAReport,
    RunResult,
)
from .tools_bridge import ANTHROPIC_TOOLS, OPENAI_TOOLS, execute_tool

_tracer = get_tracer()

# Approx $ per 1M tokens (input, output) — for the verbose cost estimate only.
_PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}


def _price_for(model: str) -> tuple[float, float]:
    for key, price in _PRICES.items():
        if key in model:
            return price
    return _PRICES["opus"]


def _estimate_cost(totals: dict, model: str) -> float:
    price_in, price_out = _price_for(model)
    return (
        totals["input"] + totals["cache_write"] * 1.25 + totals["cache_read"] * 0.10
    ) * price_in / 1e6 + totals["output"] * price_out / 1e6


def _compact_schema(model_cls) -> str:
    """A minimal field:type sketch of model_cls, instead of Pydantic's verbose
    model_json_schema() dump (a "title" on every field, $defs/$ref indirection,
    "type":"object" boilerplate). Same shape info for the model to match, far
    fewer tokens — and this is prompt text, not `messages`, so it doesn't touch
    the gather-transcript cache prefix (see analyze()/_analyze_call() above)."""
    full = model_cls.model_json_schema()
    defs = full.get("$defs", {})

    def resolve(schema: dict):
        if "$ref" in schema:
            return resolve(defs[schema["$ref"].rsplit("/", 1)[-1]])
        if "enum" in schema:
            # A bare list here reads as "this field IS a list" rather than "pick one
            # of these" — live-verified: the model started emitting category as
            # ["rollout"] instead of "rollout" once this was a raw list. A string
            # placeholder keeps the field's type as a string in the sketch.
            return "one of: " + "|".join(schema["enum"])
        if schema.get("type") == "array":
            return [resolve(schema.get("items", {}))]
        if "properties" in schema:
            return {k: resolve(v) for k, v in schema["properties"].items()}
        return schema.get("type", "any")

    return json.dumps(resolve(full))


def _btype(b) -> str:
    """Read `.type` off a gather content block, which is either a plain dict (open
    model, gather-model-swap) or an Anthropic SDK content-block object (Claude)."""
    return b["type"] if isinstance(b, dict) else b.type


def _bget(b, key, default=None):
    return b.get(key, default) if isinstance(b, dict) else getattr(b, key, default)




def _response_output(blocks) -> str:
    """Render a response's content blocks as a compact string for tracing — the actual
    text plus a summary of any tool calls, since for gather 'what did it decide to do'
    often IS the tool calls, not prose. Reuses _btype/_bget so it handles both Claude
    SDK objects and the open-model gather path's plain dicts uniformly."""
    parts = []
    for b in blocks:
        t = _btype(b)
        if t == "text" and (_bget(b, "text") or "").strip():
            parts.append(_bget(b, "text").strip())
        elif t == "tool_use":
            args = dict(_bget(b, "input") or {})
            parts.append(f"[tool_call: {_bget(b, 'name')}({', '.join(f'{k}={v}' for k, v in args.items())})]")
    return _truncate("\n".join(parts))


def _last_message_summary(messages: list) -> str:
    """Compact rendering of the newest message for tracing 'input' — the initial
    incident render (a plain string) on gather's first call, or the tool results that
    triggered this call on later ones. Deliberately NOT the full transcript, which
    would repeat the same growing content across every span in a multi-round loop."""
    if not messages:
        return ""
    content = messages[-1].get("content")
    if isinstance(content, str):
        return _truncate(content)
    if isinstance(content, list):
        parts = [
            f"[{_bget(b, 'tool_use_id', '')}] {_bget(b, 'content', '')}"
            for b in content
            if _btype(b) == "tool_result"
        ]
        return _truncate("\n".join(parts)) if parts else _truncate(str(content))
    return _truncate(str(content))


def _messages_to_openai(system_prompt: str, messages: list) -> list[dict]:
    """Translate the Anthropic-shaped gather transcript into OpenAI chat format for
    the swapped-in gather model. Only ever called on a transcript gather itself
    built (plain dicts — see _openai_message_to_blocks below), since gather never
    mixes in real Claude SDK content-block objects when gather_model is set."""
    oai: list[dict] = [{"role": "system", "content": system_prompt}]
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            oai.append({"role": role, "content": content})
        elif role == "assistant":
            text = "".join(b["text"] for b in content if b["type"] == "text")
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                }
                for b in content
                if b["type"] == "tool_use"
            ]
            entry: dict = {"role": "assistant", "content": text or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            oai.append(entry)
        else:  # a user turn carrying tool_result blocks
            for b in content:
                oai.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
    return oai


def _openai_message_to_blocks(message: dict) -> list[dict]:
    """The inverse: one OpenAI-shaped response message -> Anthropic content blocks,
    so the rest of the pipeline (still Claude) sees the same transcript shape no
    matter which model produced this turn. Deliberately never emits a "thinking"
    block: Anthropic requires a signed signature on any thinking block replayed
    back while thinking is enabled (correlate/hypothesize/propose all run with it
    on), and an open model has no way to produce that signature.

    Also never reuses the provider's own tool_call id verbatim — live-verified:
    Qwen's ids happen to be plain alphanumeric and pass, but Kimi K2 returns ids
    like "functions.get_workload_status:0" (dots/colons), which Claude's
    tool_use.id validation rejects on replay. Mint our own safe id instead;
    _messages_to_openai() derives both the tool_calls[].id and the matching
    tool role message's tool_call_id from this same synthetic value, so the
    provider's original id never has to survive the round trip."""
    blocks = []
    text = message.get("content")
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in message.get("tool_calls") or []:
        blocks.append(
            {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex}",
                "name": tc["function"]["name"],
                "input": json.loads(tc["function"]["arguments"] or "{}"),
            }
        )
    return blocks


def _gather_call_open_model(settings, messages: list) -> tuple[dict, dict]:
    """One gather iteration against the swapped-in open model, over its
    OpenAI-compatible endpoint (works for OpenRouter/DeepInfra/DeepSeek/Groq/etc.
    unchanged — only base_url/model/api_key differ). Returns (message, usage)."""
    resp = httpx.post(
        f"{settings.gather_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.gather_api_key}"},
        json={
            "model": settings.gather_model,
            "messages": _messages_to_openai(SYSTEM_PROMPT, messages),
            "tools": OPENAI_TOOLS,
            "tool_choice": "auto",
            "max_tokens": 8000,
        },
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"], data.get("usage") or {}


def _estimate_gather_cost(gather_totals: dict, settings) -> float:
    return (
        gather_totals["input"] * settings.gather_price_in
        + gather_totals["output"] * settings.gather_price_out
    ) / 1e6


def _hypotheses_for_report(ranked: list[Hypothesis]) -> str:
    """Re-serialize the ranked hypotheses for propose's prompt tail. REPORT_INSTRUCTION
    uses the full hypothesis (incl. `supporting`) only for the top one it writes up;
    the rest are summarized as "cause (score): why rejected" — `against` is the "why
    rejected", `supporting` for a REJECTED cause is never read. Dropping it there saves
    real tokens without cutting anything the report actually cites."""
    dumped = []
    for i, h in enumerate(ranked):
        d = h.model_dump()
        if i > 0:
            d.pop("supporting", None)
        dumped.append(d)
    return json.dumps(dumped)


# Phase 10 (memory) — honest outcome labels for a recalled run. Never hides a bad
# outcome (a rejected/failed fix is useful memory too) — see docs/memory-plan.md
# decision 5. Only "execute" mode has a real approval_status; propose/eval runs are
# collapsed to "outcome unknown" since nothing downstream ever confirmed the fix.
#
# Phase 11 (verification) splits "approved_applied" into three further shades based
# on whether the fix was actually checked to have worked — see
# docs/verification-plan.md decision 6. A human approving a command is no longer an
# unconditionally positive signal; its polarity depends on what verification saw.
_APPROVED_OUTCOME_LABELS = {
    "confirmed_healthy": (
        "applied and approved by a human, verified healthy immediately after "
        "applying (a bounded check, not a permanent guarantee)"
    ),
    "still_unhealthy": (
        "applied and approved by a human, but verification found the issue did NOT "
        "resolve — do not propose this same fix again without new evidence"
    ),
}
_APPROVED_UNVERIFIED = (
    "applied and approved by a human (not independently verified whether it "
    "resolved the issue)"
)
_OUTCOME_LABELS = {
    "rejected": "proposed, but a human rejected this fix",
    "blocked": "proposed, but this fix failed the safety gate",
    "dry_run_failed": "proposed, but this fix failed the safety gate",
    "apply_failed": "proposed, but this fix failed the safety gate",
}
_UNKNOWN_OUTCOME = "proposed only — outcome unknown, never applied"


def _row_to_prior_incident(row) -> PriorIncident:
    command = ""
    try:
        command = json.loads(row["report_json"]).get("remediation", {}).get("command", "")
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    if row["mode"] != "execute":
        label = _UNKNOWN_OUTCOME
    elif row["approval_status"] == "approved_applied":
        label = _APPROVED_OUTCOME_LABELS.get(row["verification_status"], _APPROVED_UNVERIFIED)
    else:
        label = _OUTCOME_LABELS.get(row["approval_status"], _UNKNOWN_OUTCOME)
    return PriorIncident(
        run_id=row["id"],
        when=row["started_at"],
        category=row["category"],
        confidence_score=row["confidence_score"],
        root_cause=row["root_cause"] or "",
        remediation_command=command,
        outcome_label=label,
    )


def _reasoning_kwargs(model: str, effort: str, verbose: bool) -> dict:
    """Kwargs controlling reasoning depth, branched by model capability — the API gives
    no capability-negotiation, so this is a hardcoded model-name check, live-verified
    2026-08-19 (a `sre-agent eval --record` batch on Haiku failed every single incident
    with BadRequestError: "adaptive thinking is not supported on this model"):

    - Opus 4.7+ and the Claude 5 models (incl. our default, Sonnet 5) accept ONLY
      adaptive thinking (thinking.type="adaptive" + output_config.effort) — the older
      fixed-budget thinking API is rejected outright on these.
    - Haiku 4.5 is the reverse: it accepts ONLY the older fixed-budget thinking
      (thinking.type="enabled" + budget_tokens) and has no output_config.effort at all.

    If a future Haiku gains adaptive support, this is the one place to update.
    """
    if "haiku" in model.lower():
        return {"thinking": {"type": "enabled", "budget_tokens": 4000}}
    # display=summarized surfaces Claude's reasoning summary (billed either way — free to show)
    thinking = {"type": "adaptive", "display": "summarized"} if verbose else {"type": "adaptive"}
    return {"thinking": thinking, "output_config": {"effort": effort}}


def _build_graph(client, clients, settings, verbose=False, console=None):
    reasoning_kwargs = _reasoning_kwargs(settings.agent_model, settings.agent_effort, verbose)

    def say(msg, style=""):
        if console:
            console.print(msg, style=style)

    # Accumulate token usage across every model call in the run.
    totals = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    # Separate accumulator for the swapped-in gather model (gather_model set): its
    # usage shape (prompt_tokens/completion_tokens, no cache breakdown) and pricing
    # are unrelated to Claude's, so it's tracked and priced independently, then
    # folded into one total in investigate().
    gather_totals = {"input": 0, "output": 0}

    def track_gather(usage: dict) -> None:
        if not usage:
            return
        inp = usage.get("prompt_tokens", 0) or 0
        out = usage.get("completion_tokens", 0) or 0
        gather_totals["input"] += inp
        gather_totals["output"] += out
        say(f"     [dim]gather-model tokens: in={inp}  out={out}[/]")

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
            tools=ANTHROPIC_TOOLS,
            messages=messages + [{"role": "user", "content": ask}],
            cache_control={"type": "ephemeral"},
            **reasoning_kwargs,
        )
        if force_no_tools:
            kwargs["tool_choice"] = {"type": "none"}
        resp = client.messages.create(**kwargs)
        track(resp.usage)
        # GenAI semantic-convention attributes on whichever node span is active
        # (agent.correlate/hypothesize/rank/propose) — this is what makes Opik/
        # Langfuse render the call as an LLM span (cost, tokens) rather than a
        # blank one. Always Claude here, unlike gather's swapped path below.
        span = trace.get_current_span()
        span.set_attribute("gen_ai.system", "anthropic")
        span.set_attribute("gen_ai.request.model", settings.agent_model)
        span.set_attribute("gen_ai.usage.input_tokens", resp.usage.input_tokens or 0)
        span.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens or 0)
        span.set_attribute("gen_ai.input.messages", _truncate(ask))
        span.set_attribute("gen_ai.output.messages", _response_output(resp.content))
        return resp

    def _extract_json_object(text: str) -> dict:
        text = text.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        return json.loads(text)

    def analyze(messages, instruction, model_cls):
        """One structured-output analysis call over the gathered evidence (no new tools)."""
        schema_hint = _compact_schema(model_cls)
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
        with _tracer.start_as_current_span("agent.gather") as gather_span:
            messages = state["messages"]
            evidence = state["evidence"]
            iterations = state["iterations"]
            swapped = bool(settings.gather_model)
            # gen_ai.request./gen_ai.response* are the two Opik mapping rules that route
            # to Input/Output WITHOUT forcing spanType=llm or =tool (unlike gen_ai.input.*/
            # gen_ai.tool.*, which do) — the right choice for a phase-wrapper span that
            # isn't itself one call. See GenAIMappingRules.java in the Opik repo.
            gather_span.set_attribute("gen_ai.request.incident", _truncate(_last_message_summary(messages)))
            say(
                "\n[bold cyan]▶ gather[/] — investigating with read-only tools"
                + (f" [dim](gather model: {settings.gather_model})[/]" if swapped else "")
            )

            while iterations < settings.agent_max_tool_iterations:
                # Each iteration gets its own child span (rather than piling everything
                # onto the single "agent.gather" span above) so a multi-round-trip
                # gather phase shows up in Opik/Langfuse as N separate LLM calls with
                # their own cost/latency, not one call with N calls' worth of tokens.
                if swapped:
                    # Open-model path (gather-model-swap): same loop shape, but the
                    # call and its response go through the OpenAI<->Anthropic
                    # translation layer above so `messages` stays a valid Anthropic
                    # transcript for correlate/hypothesize/propose, which always
                    # stay on Claude regardless of this setting.
                    with _tracer.start_as_current_span("agent.gather.call") as call_span:
                        call_span.set_attribute("gen_ai.input.messages", _last_message_summary(messages))
                        msg, usage = _gather_call_open_model(settings, messages)
                        blocks = _openai_message_to_blocks(msg)
                        call_span.set_attribute("gen_ai.system", "openai")  # OpenAI-compatible wire format
                        call_span.set_attribute("gen_ai.request.model", settings.gather_model)
                        call_span.set_attribute("gen_ai.usage.input_tokens", usage.get("prompt_tokens", 0) or 0)
                        call_span.set_attribute("gen_ai.usage.output_tokens", usage.get("completion_tokens", 0) or 0)
                        call_span.set_attribute("gen_ai.output.messages", _response_output(blocks))
                    keep_going = bool(msg.get("tool_calls"))
                    track_gather(usage)
                else:
                    with _tracer.start_as_current_span("agent.gather.call") as call_span:
                        call_span.set_attribute("gen_ai.input.messages", _last_message_summary(messages))
                        resp = client.messages.create(
                            model=settings.agent_model,
                            max_tokens=16000,
                            system=SYSTEM_PROMPT,
                            tools=ANTHROPIC_TOOLS,
                            messages=messages,
                            cache_control={"type": "ephemeral"},  # cache the stable prefix -> cheaper
                            **reasoning_kwargs,
                        )
                        blocks = resp.content
                        call_span.set_attribute("gen_ai.system", "anthropic")
                        call_span.set_attribute("gen_ai.request.model", settings.agent_model)
                        call_span.set_attribute("gen_ai.usage.input_tokens", resp.usage.input_tokens or 0)
                        call_span.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens or 0)
                        call_span.set_attribute("gen_ai.output.messages", _response_output(blocks))
                    keep_going = resp.stop_reason == "tool_use"
                    track(resp.usage)

                iterations += 1
                messages.append({"role": "assistant", "content": blocks})

                if console:
                    for block in blocks:
                        btype = _btype(block)
                        if btype == "thinking" and (_bget(block, "thinking") or "").strip():
                            say(f"  [dim italic]🧠 {_bget(block, 'thinking').strip()}[/]")
                        elif btype == "text" and (_bget(block, "text") or "").strip():
                            say(f"  [white]{_bget(block, 'text').strip()}[/]")

                if not keep_going:
                    break

                tool_results = []
                for block in blocks:
                    if _btype(block) == "tool_use":
                        name = _bget(block, "name")
                        tool_input = dict(_bget(block, "input"))
                        tool_id = _bget(block, "id")
                        args = ", ".join(f"{k}={v}" for k, v in tool_input.items())
                        say(f"  [yellow]🔧 {name}[/]([dim]{args}[/])")
                        content, is_error, record = execute_tool(name, tool_input, clients)
                        evidence.append(record)
                        mark = "[red]✗[/]" if is_error else "[green]✓[/]"
                        say(f"     {mark} [dim]{record.summary}[/]")
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": content,
                                "is_error": is_error,
                            }
                        )
                messages.append({"role": "user", "content": tool_results})

            gather_span.set_attribute("gather.iterations", iterations)
            gather_span.set_attribute("gather.tool_calls", len(evidence))
            gather_span.set_attribute(
                "gather.tools_used", _truncate(json.dumps(sorted({e.tool for e in evidence})))
            )
            gather_span.set_attribute(
                "gen_ai.response.summary",
                _truncate(
                    f"{iterations} iterations, {len(evidence)} tool calls "
                    f"({', '.join(sorted({e.tool for e in evidence})) or 'none'})"
                ),
            )
            return {"messages": messages, "evidence": evidence, "iterations": iterations}

    def recall(state: AgentState) -> dict:
        # Deterministic, no LLM call — plain Python, same shape as `rank`. Runs AFTER
        # gather (evidence-gathering stays memory-blind by design) and is skipped
        # entirely for eval-mode incidents so the harness stays a cold regression
        # test — see docs/memory-plan.md decisions 1/2/4.
        with _tracer.start_as_current_span("agent.recall") as span:
            incident = state["incident"]
            span.set_attribute("recall.workload", incident.workload or "")
            span.set_attribute("recall.skipped", incident.skip_recall)
            span.set_attribute(
                "gen_ai.request.lookup",
                json.dumps({"namespace": incident.namespace, "workload": incident.workload}),
            )
            if incident.skip_recall:
                span.set_attribute("recall.prior_incident_count", 0)
                span.set_attribute("gen_ai.response.summary", "skipped (eval-mode incident stays memory-blind)")
                return {"prior_incidents": []}
            rows = history_store.find_related_runs(incident.namespace, incident.workload, limit=3)
            prior = [_row_to_prior_incident(r) for r in rows]
            span.set_attribute("recall.prior_incident_count", len(prior))
            span.set_attribute(
                "gen_ai.response.summary",
                _truncate(
                    f"{len(prior)} prior incident(s): "
                    + "; ".join(f"{p.category} ({p.outcome_label})" for p in prior)
                    if prior
                    else "0 prior incidents found"
                ),
            )
            if console and prior:
                say(f"\n[bold cyan]▶ recall[/] — {len(prior)} related past incident(s)")
                for p in prior:
                    say(
                        f"  [dim]{p.when}[/] {p.category}: {p.root_cause[:80]} "
                        f"[dim]({p.outcome_label})[/]"
                    )
            return {"prior_incidents": prior}

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
            digest = render_memory_digest(state["prior_incidents"])
            result: Hypotheses = analyze(state["messages"], HYPOTHESIZE_INSTRUCTION + digest, Hypotheses)
            hyps = result.hypotheses
            if console:
                for h in hyps:
                    say(f"  [dim][{h.confidence:.2f}][/] [white]{h.category}[/]: {h.cause}")
            return {"hypotheses": hyps}

    def rank(state: AgentState) -> dict:
        # Deterministic, no LLM call: order hypotheses by confidence, most-likely first.
        with _tracer.start_as_current_span("agent.rank") as span:
            hyps = state["hypotheses"]
            span.set_attribute(
                "rank.input_hypotheses", _truncate(json.dumps([h.cause for h in hyps]))
            )
            span.set_attribute(
                "gen_ai.request.hypotheses",
                _truncate(json.dumps([{"cause": h.cause, "confidence": h.confidence} for h in hyps])),
            )
            ranked = sorted(hyps, key=lambda h: h.confidence, reverse=True)
            if ranked:
                top = ranked[0]
                span.set_attribute("rank.top_cause", top.cause)
                span.set_attribute("rank.top_category", top.category or "")
                span.set_attribute("rank.top_confidence", top.confidence)
                span.set_attribute(
                    "gen_ai.response.summary",
                    _truncate(f"top: {top.category} — {top.cause} (confidence {top.confidence:.2f})"),
                )
                say(
                    f"\n[bold cyan]▶ rank[/] — top: [white]{top.cause}[/] "
                    f"[dim](confidence {top.confidence:.2f})[/]"
                )
            else:
                span.set_attribute("gen_ai.response.summary", "no hypotheses to rank")
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
            analysis += "Ranked hypotheses (top first): " + _hypotheses_for_report(ranked)
            digest = render_memory_digest(state["prior_incidents"])
            report = analyze(state["messages"], analysis + digest + "\n\n" + REPORT_INSTRUCTION, RCAReport)

            if console:
                price_in, _ = _price_for(settings.agent_model)
                claude_est = _estimate_cost(totals, settings.agent_model)
                saved = totals["cache_read"] * 0.90 * price_in / 1e6  # vs paying full price
                say(
                    f"\n[bold]run totals[/] — input={totals['input']}  "
                    f"cache(write={totals['cache_write']} read={totals['cache_read']})  "
                    f"output={totals['output']}"
                )
                say(
                    f"[bold]est. cost[/] (Claude, {settings.agent_model}) ~${claude_est:.4f}  "
                    f"[dim](caching saved ~${saved:.4f})[/]"
                )
                if settings.gather_model:
                    gather_est = _estimate_gather_cost(gather_totals, settings)
                    say(
                        f"[bold]est. cost[/] (gather model, {settings.gather_model}) "
                        f"~${gather_est:.4f}  [dim](in={gather_totals['input']} "
                        f"out={gather_totals['output']})[/]"
                    )
                    say(f"[bold]est. total[/] ~${claude_est + gather_est:.4f}")
            return {"report": report}

    g = StateGraph(AgentState)
    g.add_node("gather", gather)
    g.add_node("recall", recall)
    g.add_node("correlate", correlate)
    g.add_node("hypothesize", hypothesize)
    g.add_node("rank", rank)
    g.add_node("propose", propose)
    g.add_edge(START, "gather")
    g.add_edge("gather", "recall")
    g.add_edge("recall", "correlate")
    g.add_edge("correlate", "hypothesize")
    g.add_edge("hypothesize", "rank")
    g.add_edge("rank", "propose")
    g.add_edge("propose", END)
    return g.compile(), totals, gather_totals


def investigate(incident: IncidentContext, verbose: bool = False) -> RunResult:
    """Run a full investigation and return the RCA plus the trail behind it.

    verbose=True streams each step (reasoning summary, tool call, result) to stdout.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (copy .env.example -> .env and add it).")
    if settings.gather_model and not settings.gather_api_key:
        raise RuntimeError("GATHER_MODEL is set but GATHER_API_KEY is not (see .env.example).")

    # No-op unless OPIK_URL / LANGFUSE_URL are set — see observability.py. Idempotent,
    # so repeated investigate() calls in one process (e.g. `sre-agent eval`) are fine.
    setup_tracing(settings=settings)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    clients = load_readonly_clients()
    console = Console() if verbose else None

    start = time.perf_counter()
    with _tracer.start_as_current_span("agent.investigate") as span:
        span.set_attribute("incident.namespace", incident.namespace)
        span.set_attribute("incident.workload", incident.workload or "")
        span.set_attribute("incident.alert", incident.alert or "")
        span.set_attribute(
            "gen_ai.request.incident",
            json.dumps(
                {"namespace": incident.namespace, "workload": incident.workload, "alert": incident.alert}
            ),
        )
        app, totals, gather_totals = _build_graph(client, clients, settings, verbose=verbose, console=console)
        initial: AgentState = {
            "incident": incident,
            "messages": [{"role": "user", "content": render_incident(incident)}],
            "evidence": [],
            "iterations": 0,
            "correlation": None,
            "hypotheses": [],
            "prior_incidents": [],
            "report": None,
        }
        final = app.invoke(initial)
        span.set_attribute("agent.iterations", final["iterations"])
        report = final["report"]
        if report is not None:
            span.set_attribute("investigate.report_category", report.category)
            span.set_attribute("investigate.report_confidence", report.confidence_score)
            span.set_attribute("investigate.report_summary", _truncate(report.summary))
            span.set_attribute("investigate.remediation_command", _truncate(report.remediation.command))
            span.set_attribute(
                "gen_ai.response.summary",
                _truncate(
                    f"[{report.category}, confidence {report.confidence_score:.2f}] {report.summary}\n"
                    f"remediation: {report.remediation.command}"
                ),
            )
        return RunResult(
            report=final["report"],
            evidence=final["evidence"],
            correlation=final["correlation"],
            hypotheses=final["hypotheses"],
            prior_incidents=final["prior_incidents"],
            input_tokens=totals["input"],
            cache_write_tokens=totals["cache_write"],
            cache_read_tokens=totals["cache_read"],
            output_tokens=totals["output"],
            cost_usd=_estimate_cost(totals, settings.agent_model)
            + _estimate_gather_cost(gather_totals, settings),
            duration_s=time.perf_counter() - start,
        )
