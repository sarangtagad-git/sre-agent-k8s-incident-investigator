"""CLI entrypoint. Grows into `sre-agent investigate ...` in later phases."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from . import __version__

app = typer.Typer(help="SRE Incident Investigator — read-only, human-gated K8s incident agent.")
console = Console()


@app.command()
def version() -> None:
    """Print the agent version."""
    typer.echo(f"sre-agent {__version__}")


@app.command()
def doctor() -> None:
    """Verify config + read-only cluster access are wired up correctly."""
    from .config import get_settings
    from .k8s import load_readonly_clients

    s = get_settings()
    typer.echo(f"model={s.agent_model}  kubeconfig={s.agent_kubeconfig}  prometheus={s.prometheus_url}")
    clients = load_readonly_clients()
    pods = clients["core"].list_namespaced_pod("boutique").items
    typer.echo(f"OK: read {len(pods)} pods in 'boutique' via the read-only identity.")


@app.command()
def status(
    namespace: str = typer.Argument("boutique"),
    selector: str = typer.Option(None, "--selector", "-l", help="label selector"),
    as_json: bool = typer.Option(False, "--json", help="emit structured JSON (what the agent sees)"),
) -> None:
    """Show workload status for a namespace (tool: get_workload_status)."""
    from .tools import get_workload_status

    ws = get_workload_status(namespace, selector)
    if as_json:
        console.print_json(ws.model_dump_json())
        return
    dt = Table(title=f"Deployments · {namespace}")
    for col in ("name", "ready", "avail", "updated", "unavail"):
        dt.add_column(col)
    for d in ws.deployments:
        dt.add_row(d.name, f"{d.ready}/{d.desired}", str(d.available), str(d.updated), str(d.unavailable))
    console.print(dt)

    pt = Table(title=f"Pods · {namespace}")
    for col in ("name", "ready", "phase", "restarts", "reason"):
        pt.add_column(col)
    for p in ws.pods:
        pt.add_row(p.name, "✓" if p.ready else "✗", p.phase, str(p.restarts), p.reason or "-")
    console.print(pt)


@app.command()
def events(
    pod: str = typer.Argument(..., help="pod (or object) name"),
    namespace: str = typer.Option("boutique", "--namespace", "-n"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show events for a pod (tool: get_pod_events)."""
    from .tools import get_pod_events

    evs = get_pod_events(namespace, pod)
    if as_json:
        console.print_json(data=[e.model_dump(mode="json") for e in evs])
        return
    t = Table(title=f"Events · {namespace}/{pod}")
    for col in ("type", "reason", "count", "last_seen", "message"):
        t.add_column(col, overflow="fold")
    for e in evs:
        t.add_row(e.type, e.reason or "-", str(e.count), str(e.last_seen or "-"), (e.message or "")[:80])
    console.print(t)


@app.command()
def logs(
    pod: str = typer.Argument(...),
    namespace: str = typer.Option("boutique", "--namespace", "-n"),
    container: str = typer.Option(None, "--container", "-c"),
    previous: bool = typer.Option(False, "--previous", "-p", help="logs of the crashed instance"),
    tail: int = typer.Option(50, "--tail"),
) -> None:
    """Show pod logs (tool: get_pod_logs). Use --previous for a crashed container."""
    from .tools import get_pod_logs

    r = get_pod_logs(namespace, pod, container=container, previous=previous, tail_lines=tail)
    if r.note:
        console.print(f"[yellow]note:[/] {r.note}")
    console.print(f"[dim]{r.pod}/{r.container}  previous={r.previous}  lines={r.line_count}[/dim]")
    for line in r.lines[-tail:]:
        console.print(line, markup=False, highlight=False)


@app.command()
def rollout(
    deployment: str = typer.Argument(...),
    namespace: str = typer.Option("boutique", "--namespace", "-n"),
) -> None:
    """Show rollout history with per-revision images (tool: get_rollout_history)."""
    from .tools import get_rollout_history

    h = get_rollout_history(namespace, deployment)
    t = Table(title=f"Rollout history · {namespace}/{deployment} (current rev {h.current_revision})")
    for col in ("rev", "current", "replicas", "images", "change_cause"):
        t.add_column(col, overflow="fold")
    for r in h.revisions:
        t.add_row(
            str(r.revision),
            "★" if r.current else "",
            str(r.replicas),
            ", ".join(r.images),
            r.change_cause or "-",
        )
    console.print(t)


@app.command()
def metrics(
    query: str = typer.Argument(..., help="PromQL expression"),
    prometheus_url: str = typer.Option(None, "--url", help="override Prometheus URL"),
) -> None:
    """Run an instant PromQL query (tool: query_prometheus). Needs Prometheus port-forward."""
    from .tools import query_prometheus

    r = query_prometheus(query, base_url=prometheus_url)
    if r.error:
        console.print(f"[red]error:[/] {r.error}")
        return
    t = Table(title=f"{query}   ({r.result_type})")
    t.add_column("value")
    t.add_column("labels", overflow="fold")
    for s in r.samples:
        labels = ", ".join(f"{k}={v}" for k, v in sorted(s.labels.items())) or "(scalar)"
        t.add_row(f"{s.value:g}", labels)
    console.print(t)


@app.command()
def investigate(
    namespace: str = typer.Argument("boutique"),
    workload: str = typer.Option(None, "--workload", "-w", help="suspected deployment/pod"),
    alert: str = typer.Option(None, "--alert", "-a", help="the alert text / symptom"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="stream each step (reasoning, tool calls) live"),
    execute: bool = typer.Option(
        False, "--execute", "-x",
        help="after the RCA, open the approval gate to (dry-run, confirm, then) apply the fix",
    ),
) -> None:
    """Run the agent: gather evidence, correlate, and PROPOSE a fix.

    By default the fix is only proposed. With --execute, the proposed command is checked
    against the safety allowlist, server-dry-run'd, and applied only after you confirm —
    using your own kubectl context, never the agent's read-only identity.
    """
    from rich.panel import Panel

    from . import history_store
    from .agent import IncidentContext, investigate as run

    incident = IncidentContext(namespace=namespace, workload=workload, alert=alert)
    if verbose:
        result = run(incident, verbose=True)  # streams steps itself; no spinner
    else:
        with console.status("[bold]investigating…[/] (the agent is calling read-only tools)"):
            result = run(incident)
    report = result.report

    console.print(Panel(f"[bold]{report.summary}[/bold]", title="Root-cause analysis", border_style="cyan"))
    console.print(
        f"[bold]Root cause[/] ({report.category}, confidence: {report.confidence} "
        f"· {report.confidence_score:.2f}):"
    )
    console.print(f"  {report.root_cause}\n")
    console.print(f"[bold]Impact:[/] {report.impact}\n")

    if report.evidence:
        console.print("[bold]Evidence:[/]")
        for e in report.evidence:
            console.print(f"  • {e}")
    if report.alternatives:
        console.print("\n[bold]Alternatives considered:[/]")
        for a in report.alternatives:
            console.print(f"  • [dim]{a}[/]")

    rem = report.remediation
    console.print(
        Panel(
            f"[bold]{rem.action}[/bold]\n[dim]{rem.rationale}[/dim]\n\n"
            f"[green]$ {rem.command}[/green]",
            title="⚠  PROPOSED remediation — requires human approval (not executed)",
            border_style="yellow",
        )
    )

    approval_status = "n/a"
    verification_status: str | None = None
    verification_detail: str | None = None
    if execute:
        approval_status = _approval_gate(rem.command, reversible=rem.reversible)
        if approval_status == "approved_applied":
            verification = _verify_recovery_and_report(namespace, workload)
            verification_status = verification.status
            verification_detail = verification.detail

    # Phase 11: "resolved" now reflects what verification actually observed, not just
    # whether the apply command errored — confirmed_healthy/still_unhealthy are the
    # only statuses that answer the question either way; not_checked (or no
    # verification at all) stays an honest "we don't know" rather than assuming success.
    resolved = {"confirmed_healthy": True, "still_unhealthy": False}.get(verification_status)

    run_id = history_store.save_run(
        result,
        namespace=namespace,
        workload=workload,
        alert=alert,
        mode="execute" if execute else "propose",
        approval_status=approval_status,
        resolved=resolved,
        verification_status=verification_status,
        verification_detail=verification_detail,
    )
    console.print(f"\n[dim]saved as run {run_id} — see `sre-agent history {run_id}`[/]")


@app.command("eval")
def eval_cmd(
    incident: str = typer.Option(None, "--incident", "-i", help="run only this incident (by name)"),
    keep: bool = typer.Option(False, "--keep", help="leave the incident staged (don't revert)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the cost/mutation confirmation"),
) -> None:
    """Regression-test the agent: stage each incident, run it, assert the RCA, then revert.

    Costs ~$0.15 per incident and mutates the cluster, so it is a deliberate command — not
    part of the (free, cluster-less) unit test suite. Exits non-zero if any incident fails.
    """
    import time

    from rich.table import Table as _Table

    from . import history_store
    from .agent import investigate as run
    from .evals import INCIDENTS, Check, incident_passed, score
    from .remediation import run_kubectl

    incidents = [i for i in INCIDENTS if incident in (None, i.name)]
    if not incidents:
        names = ", ".join(i.name for i in INCIDENTS)
        console.print(f"[red]No incident named {incident!r}.[/] Known: {names}")
        raise typer.Exit(code=2)

    if not yes:
        console.print(
            f"[yellow]This stages/reverts {len(incidents)} real incident(s) and runs the live "
            f"agent (~$0.15 each).[/]"
        )
        if not typer.confirm("Proceed?", default=False):
            raise typer.Abort()

    results: list[tuple[str, bool, list, float]] = []
    for inc in incidents:
        console.rule(f"[bold]{inc.name}[/] — {inc.description}")
        for args in inc.stage:
            run_kubectl(args)
        console.print(f"[dim]staged; waiting {inc.wait_seconds}s for the failure to surface…[/]")
        time.sleep(inc.wait_seconds)
        try:
            result = run(inc.context)
            report = result.report
            checks = score(report, inc)
            ok = incident_passed(checks)
            console.print(
                f"  RCA: [bold]{report.category}[/] · {report.confidence_score:.2f} — "
                f"{report.root_cause[:90]}…"
            )
            for c in checks:
                mark = "[green]✓[/]" if c.passed else "[red]✗[/]"
                tag = "[dim](critical)[/]" if c.critical else "[dim](info)[/]"
                console.print(f"    {mark} {c.name} {tag}  [dim]{c.detail}[/]")
            results.append((inc.name, ok, checks, result.duration_s))
            run_id = history_store.save_run(
                result,
                namespace=inc.context.namespace,
                workload=inc.context.workload,
                alert=inc.context.alert,
                mode="eval",
                incident_name=inc.name,
                resolved=ok,
                eval_checks=checks,
            )
            console.print(f"  [dim]saved as run {run_id}[/]")
        except Exception as e:  # noqa: BLE001 — surface any run failure as an incident failure
            console.print(f"  [red]run error: {e}[/]")
            results.append((inc.name, False, [Check("run", False, True, str(e))], 0.0))
        finally:
            if not keep:
                for args in inc.revert:
                    run_kubectl(args)
                console.print("[dim]reverted.[/]")

    console.rule("[bold]Scorecard[/]")
    t = _Table()
    for col in ("incident", "result", "latency"):
        t.add_column(col)
    for name, ok, _checks, dt in results:
        t.add_row(name, "[green]PASS[/]" if ok else "[red]FAIL[/]", f"{dt:.1f}s")
    console.print(t)

    failed = [name for name, ok, _c, _dt in results if not ok]
    if failed:
        console.print(f"[red]FAILED: {', '.join(failed)}[/]")
        raise typer.Exit(code=1)
    console.print("[green]All incidents passed.[/]")


def _status_label(row) -> str:
    mode = row["mode"]
    if mode == "eval":
        return "[green]PASS[/]" if row["resolved"] else "[red]FAIL[/]"
    if mode == "execute":
        return {
            "approved_applied": "[green]Applied[/]",
            "rejected": "[yellow]Rejected[/]",
            "blocked": "[red]Blocked[/]",
            "dry_run_failed": "[red]Dry-run failed[/]",
            "apply_failed": "[red]Apply failed[/]",
        }.get(row["approval_status"], row["approval_status"])
    return "[cyan]Proposed[/]"


@app.command()
def history(
    run_id: str = typer.Argument(None, help="show this run in full detail (omit to list recent runs)"),
    limit: int = typer.Option(20, "--limit", "-n", help="how many recent runs to list"),
) -> None:
    """List past investigate/eval runs, or show one in full detail."""
    import json

    from rich.panel import Panel

    from . import history_store

    if run_id is None:
        rows = history_store.list_runs(limit=limit)
        if not rows:
            console.print("[dim]No runs recorded yet — `investigate` or `eval` writes to this history.[/]")
            return
        t = Table()
        for col in ("id", "when", "target", "category", "conf", "cost", "mode", "status"):
            t.add_column(col)
        for row in rows:
            target = row["workload"] or row["namespace"]
            t.add_row(
                row["id"][:8],
                row["started_at"].replace("T", " "),
                target,
                row["category"] or "-",
                f"{row['confidence_score']:.2f}" if row["confidence_score"] is not None else "-",
                f"${row['cost_usd']:.4f}" if row["cost_usd"] is not None else "-",
                row["mode"],
                _status_label(row),
            )
        console.print(t)
        return

    row = history_store.get_run(run_id)
    if row is None:
        console.print(f"[red]No run matching {run_id!r}.[/]")
        raise typer.Exit(code=2)

    console.print(
        Panel(
            f"[bold]{row['alert'] or '(no alert text given)'}[/]\n"
            f"[dim]namespace={row['namespace']}  workload={row['workload'] or '-'}  "
            f"mode={row['mode']}  incident={row['incident_name'] or '-'}[/]",
            title=f"Run {row['id']} — {row['started_at'].replace('T', ' ')} "
            f"({row['duration_s']:.1f}s, ${row['cost_usd']:.4f})",
            border_style="cyan",
        )
    )

    evidence = json.loads(row["evidence_json"] or "[]")
    if evidence:
        console.print("\n[bold]Evidence gathered:[/]")
        for e in evidence:
            mark = "[green]✓[/]" if e["ok"] else "[red]✗[/]"
            args = ", ".join(f"{k}={v}" for k, v in (e.get("input") or {}).items())
            console.print(f"  {mark} [yellow]{e['tool']}[/]([dim]{args}[/]) — {e['summary']}")

    correlation = json.loads(row["correlation_json"]) if row["correlation_json"] else None
    if correlation:
        console.print("\n[bold]Timeline:[/]")
        for entry in correlation["timeline"]:
            console.print(f"  [dim]•[/] {entry['when']} — {entry['what']}")
        if correlation["dependency_chain"]:
            console.print("  [magenta]chain:[/] " + " [dim]→[/] ".join(correlation["dependency_chain"]))

    hypotheses = json.loads(row["hypotheses_json"] or "[]")
    if hypotheses:
        console.print("\n[bold]Hypotheses considered (ranked):[/]")
        for h in hypotheses:
            console.print(f"  [dim][{h['confidence']:.2f}][/] [white]{h['category']}[/]: {h['cause']}")

    report = json.loads(row["report_json"])
    console.print(f"\n[bold]Root cause[/] ({report['category']}, {report['confidence']} · {report['confidence_score']:.2f}):")
    console.print(f"  {report['root_cause']}")
    console.print(f"\n[bold]Impact:[/] {report['impact']}")

    if report.get("evidence"):
        console.print("\n[bold]Evidence:[/]")
        for e in report["evidence"]:
            console.print(f"  • {e}")
    if report.get("alternatives"):
        console.print("\n[bold]Alternatives considered:[/]")
        for a in report["alternatives"]:
            console.print(f"  • [dim]{a}[/]")

    rem = report["remediation"]
    console.print(
        Panel(
            f"[bold]{rem['action']}[/bold]\n[dim]{rem['rationale']}[/dim]\n\n[green]$ {rem['command']}[/green]",
            title="Proposed remediation",
            border_style="yellow",
        )
    )
    console.print(f"\n[bold]Approval status:[/] {row['approval_status']}")
    if "triggered_by" in row.keys():
        console.print(f"[bold]Triggered by:[/] {row['triggered_by']}")
    if row["verification_status"] == "confirmed_healthy":
        console.print(f"[bold]Verification:[/] [green]confirmed healthy[/] — {row['verification_detail']}")
    elif row["verification_status"] == "still_unhealthy":
        console.print(f"[bold]Verification:[/] [red]still unhealthy[/] — {row['verification_detail']}")
    elif row["verification_status"] == "not_checked":
        console.print(f"[bold]Verification:[/] [yellow]not checked[/] — {row['verification_detail']}")
    if row["resolved"] is not None:
        console.print(f"[bold]Resolved:[/] {'yes' if row['resolved'] else 'no'}")

    eval_checks = json.loads(row["eval_checks_json"]) if row["eval_checks_json"] else None
    if eval_checks:
        console.print("\n[bold]Eval checks:[/]")
        for c in eval_checks:
            mark = "[green]✓[/]" if c["passed"] else "[red]✗[/]"
            tag = "[dim](critical)[/]" if c["critical"] else "[dim](info)[/]"
            console.print(f"  {mark} {c['name']} {tag}  [dim]{c['detail']}[/]")


@app.command()
def listen(
    port: int = typer.Option(None, "--port", "-p", help="listen port (default: ALERT_LISTEN_PORT / 9095)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="log trigger decisions but never call the LLM"),
) -> None:
    """Listen for Alertmanager webhooks and auto-investigate (propose-only, spend-capped).

    Phase 9: Alertmanager routes boutique alerts to this listener; each firing alert
    passes the guardrails (namespace allowlist, per-alert cooldown, daily run cap)
    before the agent investigates. Fixes are NEVER applied automatically. Ctrl-C is
    the kill switch. See docs/alerts-plan.md.
    """
    import uvicorn

    from .config import get_settings
    from .listener import create_app

    s = get_settings()
    p = port or s.alert_listen_port
    console.print(
        f"[bold]sre-agent listener[/] on 0.0.0.0:{p}  "
        f"{'[cyan](dry-run: no LLM calls)[/]' if dry_run else ''}\n"
        f"[dim]policy: namespaces={s.alert_namespaces}  daily cap={s.alert_daily_run_cap}  "
        f"cooldown={s.alert_cooldown_minutes}m  mode=propose-only (never executes fixes)[/]\n"
        f"[dim]Alertmanager webhook URL (from inside the cluster): http://host.docker.internal:{p}/alerts[/]"
    )
    uvicorn.run(create_app(dry_run=dry_run), host="0.0.0.0", port=p, log_level="warning")


def _approval_gate(command: str, reversible: bool = True) -> str:
    """The Phase-5 gate: allowlist -> server dry-run -> human confirm -> apply.

    Returns one of "blocked" | "dry_run_failed" | "rejected" | "apply_failed" |
    "approved_applied", so the caller can record what actually happened.
    """
    from rich.panel import Panel

    from .remediation import run_kubectl, validate_remediation

    decision = validate_remediation(command)
    if not decision.allowed:
        console.print(
            Panel(
                f"[bold]{decision.reason}[/]\n\n[dim]$ {command}[/]",
                title="⛔  Blocked by the safety gate — not executed",
                border_style="red",
            )
        )
        return "blocked"

    # Preview: server-side dry-run of the mutating command(s) — validates, changes nothing.
    console.print("\n[bold]Dry-run[/] [dim](server-side validation, no changes made):[/]")
    for args in decision.mutating:
        rc, out, err = run_kubectl(args, dry_run=True)
        console.print(f"  [green]$ {' '.join(args)} --dry-run=server[/]")
        for line in (out or err or "(no output)").strip().splitlines():
            console.print(f"    [dim]{line}[/]")
        if rc != 0:
            console.print("[red]Dry-run failed — aborting; no changes made.[/]")
            return "dry_run_failed"

    if not reversible:
        console.print("[yellow]⚠  This action is marked NOT easily reversible.[/]")
    if not typer.confirm("\nApply this change to the cluster now?", default=False):
        console.print("[yellow]Aborted by human — no changes made.[/]")
        return "rejected"

    # Apply the mutating command(s) for real, then run any read-only verification steps.
    console.print("\n[bold]Applying…[/]")
    for args in decision.mutating:
        rc, out, err = run_kubectl(args)
        console.print(f"  [green]$ {' '.join(args)}[/]")
        console.print(f"    {(out or err).strip()}")
        if rc != 0:
            console.print("[red]Command failed — stopping.[/]")
            return "apply_failed"
    for args in decision.readonly:
        rc, out, err = run_kubectl(args)
        console.print(f"  [dim]$ {' '.join(args)}[/]")
        for line in (out or err).strip().splitlines():
            console.print(f"    [dim]{line}[/]")
    # Phase 11 verifies recovery right after this returns (see investigate()) — this
    # message only ever meant "the apply command didn't error," never "it worked."
    console.print("\n[green]✓ Apply command succeeded.[/]")
    return "approved_applied"


def _verify_recovery_and_report(namespace: str, workload: str | None) -> "VerificationResult":
    """Phase 11: after an apply succeeds, actually check whether the workload
    recovered — instead of trusting that the kubectl command not erroring meant the
    incident is resolved. Uses the agent's own READ-ONLY identity (verification is
    pure observation), not the operator's kubeconfig used for the apply itself. See
    docs/verification-plan.md."""
    from .config import get_settings
    from .k8s import load_readonly_clients
    from .remediation import VerificationResult, verify_recovery
    from .tools import get_workload_status

    settings = get_settings()
    if not settings.verify_after_apply:
        return VerificationResult("not_checked", "verification disabled (VERIFY_AFTER_APPLY=false)")

    clients = load_readonly_clients()

    def check() -> "NamespaceWorkloadStatus":  # noqa: F821 - imported for typing only
        return get_workload_status(namespace, clients=clients)

    console.print(
        f"\n[bold]Verifying recovery…[/] [dim](up to {settings.verify_timeout_s}s, "
        f"needs {settings.verify_stability_checks} consecutive healthy checks)[/]"
    )
    result = verify_recovery(
        workload, check,
        timeout_s=settings.verify_timeout_s,
        poll_interval_s=settings.verify_poll_interval_s,
        stability_checks=settings.verify_stability_checks,
    )
    if result.status == "confirmed_healthy":
        console.print(f"[green]✓ Recovery confirmed.[/] [dim]{result.detail}[/]")
    elif result.status == "still_unhealthy":
        console.print(
            f"[red]⚠ Applied, but recovery was NOT confirmed — investigate again.[/] "
            f"[dim]{result.detail}[/]"
        )
    else:  # not_checked
        console.print(f"[yellow]— Recovery not independently verified:[/] [dim]{result.detail}[/]")
    return result


if __name__ == "__main__":
    app()
