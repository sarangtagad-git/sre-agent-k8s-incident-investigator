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
) -> None:
    """Run the agent: gather evidence, correlate, and PROPOSE a fix (never auto-runs it)."""
    from rich.panel import Panel

    from .agent import IncidentContext, investigate as run

    with console.status("[bold]investigating…[/] (the agent is calling read-only tools)"):
        report = run(IncidentContext(namespace=namespace, workload=workload, alert=alert))

    console.print(Panel(f"[bold]{report.summary}[/bold]", title="Root-cause analysis", border_style="cyan"))
    console.print(f"[bold]Root cause[/] ({report.category}, confidence: {report.confidence}):")
    console.print(f"  {report.root_cause}\n")
    console.print(f"[bold]Impact:[/] {report.impact}\n")

    if report.evidence:
        console.print("[bold]Evidence:[/]")
        for e in report.evidence:
            console.print(f"  • {e}")
    if report.ruled_out:
        console.print("\n[bold]Ruled out:[/]")
        for r in report.ruled_out:
            console.print(f"  • {r}")

    rem = report.remediation
    console.print(
        Panel(
            f"[bold]{rem.action}[/bold]\n[dim]{rem.rationale}[/dim]\n\n"
            f"[green]$ {rem.command}[/green]",
            title="⚠  PROPOSED remediation — requires human approval (not executed)",
            border_style="yellow",
        )
    )


if __name__ == "__main__":
    app()
