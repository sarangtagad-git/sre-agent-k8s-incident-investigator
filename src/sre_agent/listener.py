"""Phase 9: the webhook listener — Alertmanager POSTs here, the agent investigates.

Run via `sre-agent listen`. Alert handling runs on worker threads (anyio.to_thread) so
the event loop — and GET /healthz — stay responsive during a minutes-long
investigation, while a module-level lock serializes the investigations themselves: one
at a time is a deliberate spend guardrail, not a missing feature. Alert-triggered runs
are propose-mode only; the listener holds the agent's read-only identity and never
touches the approval gate.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from rich.console import Console

from . import history_store
from .alerts import Alert, WebhookPayload, evaluate, to_incident_context

if TYPE_CHECKING:
    from starlette.applications import Starlette

console = Console()
_investigation_lock = threading.Lock()


def _handle_alert(alert: Alert, dry_run: bool) -> dict[str, Any]:
    decision = evaluate(alert)
    tag = f"[bold]{alert.alertname}[/] ({alert.labels.get('namespace', '-')})"
    if not decision.go:
        console.print(f"[yellow]skip[/] {tag} — {decision.reason}")
        return {"alert": alert.alertname, "action": "skipped", "reason": decision.reason}
    if dry_run:
        console.print(f"[cyan]dry-run[/] {tag} — would investigate ({decision.reason})")
        return {"alert": alert.alertname, "action": "dry_run", "reason": decision.reason}

    from .agent import investigate  # heavy import (anthropic/langgraph) kept off startup

    ctx = to_incident_context(alert)
    console.print(f"[green]investigating[/] {tag} — {decision.reason}")
    with _investigation_lock:  # serialize: one investigation at a time, ever
        result = investigate(ctx)
    run_id = history_store.save_run(
        result,
        namespace=ctx.namespace,
        workload=ctx.workload,
        alert=ctx.alert,
        mode="propose",  # NEVER execute — autonomy stops at diagnosis
        triggered_by="alert",
    )
    report = result.report
    console.print(
        f"[green]done[/] {tag} → run [bold]{run_id}[/]: {report.category} · "
        f"{report.confidence_score:.2f} · ${result.cost_usd:.4f}\n"
        f"  [dim]{report.root_cause[:120]}[/]"
    )
    return {
        "alert": alert.alertname,
        "action": "investigated",
        "run_id": run_id,
        "category": report.category,
        "confidence_score": report.confidence_score,
    }


def create_app(dry_run: bool = False) -> "Starlette":
    import functools

    import anyio
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "dry_run": dry_run})

    async def alerts(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            payload = WebhookPayload.model_validate_json(body)
        except ValueError as e:
            console.print(f"[red]bad payload:[/] {e}")
            return JSONResponse({"error": "unparseable payload"}, status_code=400)
        results = []
        for alert in payload.alerts:
            # The handler can run for minutes (a live investigation); push it onto a
            # worker thread so the event loop — and /healthz — stay responsive. The
            # module lock still serializes actual investigations across requests.
            results.append(
                await anyio.to_thread.run_sync(functools.partial(_handle_alert, alert, dry_run))
            )
        return JSONResponse({"results": results})

    return Starlette(routes=[
        Route("/healthz", healthz, methods=["GET"]),
        Route("/alerts", alerts, methods=["POST"]),
    ])
