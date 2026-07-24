"""Phase 8: Streamlit dashboard over the run-history store (Phase 7).

Run with `streamlit run src/sre_agent/dashboard.py` from the repo root (same CWD
convention as the CLI, so history_store's relative data/history.db path resolves).

Design: Direction B, "Clarity" (see docs/dashboard-plan.md). Rendered as raw HTML via
st.markdown(unsafe_allow_html=True) — native st.metric/st.dataframe can't hit this level
of visual control. CSS is injected once as global :root rules (not scoped per-block), so
each HTML fragment just uses the classes without a padded wrapper around every element.

Each run is presented as a named "incident" (Incident #N + a descriptive title derived
from the RCA), with the time the agent first looked at it, and a side panel that walks
through how the agent actually approached it: evidence gathered -> correlation/timeline
-> ranked hypotheses -> root cause -> proposed remediation and who (if anyone) applied
it -> eval checks.

Row selection: a per-card action button (columns layout). Streamlit wraps every
st.markdown/st.button in its own element-container div, so an invisible overlay button
via a CSS sibling selector does not work (and variable card heights rule out a
negative-margin overlay); a real button beside each card is the robust choice.
"""

from __future__ import annotations

import html
import json
import sqlite3

import streamlit as st

from sre_agent import history_store

CSS = """
:root {
  --ink: #1b2420; --paper: #f4f6f4; --card: #ffffff; --border: #dfe6e1; --muted: #6b7772;
  --ember: #c1622b; --ember-soft: #f6e2d3; --moss: #3f6e5c; --moss-soft: #e1ece7;
  --good: #2f9e63; --good-soft: #e1f2e8; --warn: #c98a2e; --warn-soft: #f6ebd7;
}
.stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] { background: var(--paper); }
[data-testid="stMainBlockContainer"] { padding-top: 1.6rem; max-width: 1240px; }
html, body, [data-testid="stAppViewContainer"] * {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  color: var(--ink);
}

/* ---- top header ---- */
.topbar { display: flex; align-items: baseline; gap: 0.7rem; margin: 0.2rem 0 1.3rem; }
.topbar h1 { font-size: 1.35rem; font-weight: 700; margin: 0; letter-spacing: -0.01em; }
.topbar .tag { font-size: 0.82rem; color: var(--muted); }
.topbar .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--good); display: inline-block; margin-right: 0.35rem; }

/* ---- KPI row ---- */
.kpirow { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.9rem; margin-bottom: 1.5rem; }
.kpi { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 0.85rem 1rem; box-shadow: 0 1px 2px rgba(20,30,25,0.04); }
.kpi .label { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 700; }
.kpi .value { font-size: 1.55rem; font-weight: 700; margin-top: 0.15rem; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.kpi .value.accent { color: var(--ember); }
.kpi .value.moss { color: var(--moss); }

/* ---- section headings for the two columns ---- */
.col-head { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 700; margin: 0 0 0.7rem; }

/* ---- incident cards (left feed) ---- */
.row { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 0.8rem 0.95rem; display: flex; justify-content: space-between; align-items: center; gap: 0.8rem; box-shadow: 0 1px 2px rgba(20,30,25,0.03); }
.row.selected { border-color: var(--ember); box-shadow: 0 0 0 2px var(--ember-soft); }
.row .row-main { min-width: 0; flex: 1; }
.row .row-top { display: flex; align-items: baseline; gap: 0.5rem; }
.row .inum { font-size: 0.72rem; font-weight: 700; color: var(--muted); font-variant-numeric: tabular-nums; }
.row .who { font-weight: 600; font-size: 0.92rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.row .sub { font-size: 0.75rem; color: var(--muted); margin-top: 0.18rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.row .row-side { text-align: right; flex-shrink: 0; }
.cat { display: inline-block; background: var(--moss-soft); color: var(--moss); font-size: 0.68rem; font-weight: 700; padding: 0.05rem 0.4rem; border-radius: 5px; letter-spacing: 0.02em; }
.pill { font-size: 0.7rem; font-weight: 700; padding: 0.2rem 0.6rem; border-radius: 999px; white-space: nowrap; }
.pill.good { color: var(--good); background: var(--good-soft); }
.pill.warn { color: var(--warn); background: var(--warn-soft); }
.pill.bad  { color: #b23b3b; background: #f6dede; }
.cost { font-variant-numeric: tabular-nums; font-weight: 600; font-size: 0.82rem; color: var(--moss); margin-top: 0.25rem; }

/* ---- detail panel (right, sticky) ---- */
.panel { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 1.25rem 1.35rem; box-shadow: 0 2px 10px rgba(20,30,25,0.06); position: sticky; top: 1rem; }
.panel .p-num { font-size: 0.72rem; font-weight: 700; color: var(--ember); letter-spacing: 0.03em; }
.panel h3 { margin: 0.15rem 0 0.2rem; font-size: 1.18rem; letter-spacing: -0.01em; }
.panel .p-sub { font-size: 0.78rem; color: var(--muted); margin-bottom: 0.4rem; }
.panel .p-meta { display: flex; flex-wrap: wrap; gap: 0.4rem 0.9rem; font-size: 0.76rem; color: var(--muted); margin-bottom: 0.6rem; }
.panel .p-meta b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }
.section-label { font-size: 0.67rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 700; margin: 1.05rem 0 0.4rem; }

/* pipeline strip */
.pipe { display: flex; flex-wrap: wrap; align-items: center; gap: 0.25rem; margin-top: 0.2rem; }
.step { font-size: 0.7rem; font-weight: 700; padding: 0.18rem 0.5rem; border-radius: 6px; background: var(--paper); color: var(--muted); border: 1px solid var(--border); }
.step.done { background: var(--moss-soft); color: var(--moss); border-color: var(--moss-soft); }
.step.apply { background: var(--good-soft); color: var(--good); border-color: var(--good-soft); }
.pipe .arr { color: var(--border); font-size: 0.7rem; }

/* evidence rows */
.ev { display: flex; gap: 0.5rem; font-size: 0.78rem; margin: 0.3rem 0; align-items: flex-start; }
.ev .ok { color: var(--good); font-weight: 700; }
.ev .no { color: #b23b3b; font-weight: 700; }
.ev .tool { font-weight: 600; }
.ev .args { color: var(--muted); }
.ev .summ { color: var(--muted); }

/* timeline */
.tl { display: flex; gap: 0.6rem; font-size: 0.78rem; margin: 0.32rem 0; }
.tl .when { color: var(--ember); font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; min-width: 6.5rem; }
.tl .what { color: var(--ink); }

/* dependency chain */
.chain-pill { display: inline-block; background: var(--moss-soft); color: var(--moss); font-size: 0.75rem; font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 6px; }
.chain-arr { color: var(--muted); margin: 0 0.15rem; }

/* hypotheses */
.hyp { margin: 0.5rem 0; }
.hyp .hyp-top { display: flex; align-items: center; gap: 0.6rem; font-size: 0.78rem; }
.hyp .n { width: 2.4rem; color: var(--muted); font-variant-numeric: tabular-nums; font-weight: 600; }
.hypbar-track { background: var(--border); border-radius: 4px; height: 6px; overflow: hidden; flex: 1; }
.hypbar-fill { background: var(--ember); height: 100%; }
.hyp .hyp-txt { font-size: 0.76rem; color: var(--muted); margin: 0.12rem 0 0 3rem; }
.hyp.win .hypbar-fill { background: var(--ember); }
.hyp.win .hyp-txt { color: var(--ink); }
.hyp.win .hyp-cat { color: var(--ember); font-weight: 700; }
.hyp .hyp-cat { color: var(--muted); font-weight: 700; }

/* verdict + impact */
.verdict { background: var(--ember-soft); border-radius: 9px; padding: 0.75rem 0.9rem; font-size: 0.82rem; margin-top: 0.3rem; }
.verdict .band { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ember); font-weight: 700; }
.impact { font-size: 0.8rem; color: var(--ink); margin-top: 0.3rem; }

/* remediation */
.fixbox { background: var(--moss-soft); border-radius: 9px; padding: 0.75rem 0.9rem; font-size: 0.8rem; }
.fixbox .why { color: var(--muted); font-size: 0.76rem; margin-top: 0.2rem; }
.fixbox code { display: block; margin-top: 0.5rem; color: var(--moss); font-size: 0.78rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: rgba(63,110,92,0.08); padding: 0.4rem 0.55rem; border-radius: 6px; }
.applied { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.55rem; font-size: 0.79rem; }
.applied .badge { font-size: 0.68rem; font-weight: 700; padding: 0.2rem 0.55rem; border-radius: 999px; }
.applied .why2 { color: var(--muted); }

/* eval checks */
.chk { display: flex; gap: 0.5rem; align-items: flex-start; font-size: 0.78rem; margin: 0.28rem 0; }
.chk .mk.ok { color: var(--good); font-weight: 700; }
.chk .mk.no { color: #b23b3b; font-weight: 700; }
.chk .cn { font-weight: 600; }
.chk .crit { font-size: 0.66rem; color: var(--muted); }
.chk .cd { color: var(--muted); }

/* the per-card action button */
div[data-testid="stButton"] button {
  border: 1px solid var(--border); color: var(--muted); background: var(--card);
  border-radius: 8px; font-size: 0.95rem; line-height: 1; font-weight: 700;
  width: 2.5rem; height: 2.5rem; min-height: 0; padding: 0;
}
div[data-testid="stButton"] button:hover { border-color: var(--ember); color: var(--ember); background: var(--ember-soft); }
div[data-testid="stButton"] button:focus:not(:active) { border-color: var(--ember); color: var(--ember); }
"""

# category slug -> human-readable incident phrase
_CATEGORY_NAME = {
    "dependency": "Dependency outage",
    "node": "Node instability",
    "rollout": "Bad rollout",
    "image": "Image pull failure",
    "image_pull": "Image pull failure",
    "crash": "Crash loop",
    "crashloop": "Crash loop",
    "workload": "Application fault",
    "scheduling": "Scheduling failure",
    "networking": "Network failure",
    "network": "Network failure",
    "saturation": "Resource saturation",
    "auth": "Auth failure",
    "config": "Misconfiguration",
}


def esc(s: object) -> str:
    return html.escape(str(s))


def _pretty_category(cat: str | None) -> str:
    if not cat:
        return "Incident"
    return _CATEGORY_NAME.get(cat.lower(), cat.replace("_", " ").capitalize())


def _incident_title(row: sqlite3.Row) -> str:
    """A short descriptive name derived from the RCA — e.g. 'Dependency outage — redis-cart'."""
    pretty = _pretty_category(row["category"])
    subject = None
    if row["correlation_json"]:
        chain = (json.loads(row["correlation_json"]) or {}).get("dependency_chain") or []
        if chain:
            subject = chain[-1]
    if not subject:
        subject = row["workload"]
    return f"{pretty} — {subject}" if subject else pretty


def _fmt_when(row: sqlite3.Row) -> tuple[str, str]:
    raw = row["started_at"] or ""
    if "T" in raw:
        date, _, time = raw.partition("T")
        return date, time[:5]
    return raw, ""


def _status_pill(row: sqlite3.Row) -> tuple[str, str]:
    """(pill css class, pill text) — mirrors cli.py _status_label()."""
    mode = row["mode"]
    if mode == "eval":
        return ("good", "pass") if row["resolved"] else ("bad", "fail")
    if mode == "execute":
        return {
            "approved_applied": ("good", "applied"),
            "rejected": ("warn", "rejected"),
            "blocked": ("bad", "blocked"),
            "dry_run_failed": ("bad", "dry-run failed"),
            "apply_failed": ("bad", "apply failed"),
        }.get(row["approval_status"], ("warn", row["approval_status"]))
    return ("warn", "proposed")


def _remediation_status(row: sqlite3.Row) -> tuple[str, str, str]:
    """(badge css class, label, explanation) for who (if anyone) applied the fix.

    The agent never applies autonomously — it proposes; a human approves and applies
    with their own kubectl identity (Phase 5). Eval runs auto-stage/revert.
    """
    mode = row["mode"]
    if mode == "propose":
        return ("warn", "Proposed only", "Investigation run — the fix was surfaced, not applied.")
    if mode == "eval":
        ok = bool(row["resolved"])
        return (
            "good" if ok else "bad",
            "Validated & reverted",
            "Eval harness: staged the incident, checked the proposed fix against the "
            "safety gate, then auto-reverted the cluster.",
        )
    # execute
    return {
        "approved_applied": ("good", "Applied after human approval",
                             "A human reviewed the dry-run and approved; applied with the operator's own kubectl identity."),
        "rejected": ("warn", "Rejected by human", "A human reviewed the proposal and declined to apply it."),
        "blocked": ("bad", "Blocked by safety gate", "The command failed the allowlist validator before any dry-run."),
        "dry_run_failed": ("bad", "Dry-run failed", "The server-side dry-run rejected the command; nothing was applied."),
        "apply_failed": ("bad", "Apply failed", "Approved, but the apply command errored."),
    }.get(row["approval_status"], ("warn", row["approval_status"], ""))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _kpi_row(rows: list) -> str:
    n = len(rows)
    confs = [r["confidence_score"] for r in rows if r["confidence_score"] is not None]
    avg_conf = sum(confs) / len(confs) if confs else None
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    total_cost = sum(costs) if costs else None
    eval_rows = [r for r in rows if r["mode"] == "eval"]
    eval_pass = sum(1 for r in eval_rows if r["resolved"])

    conf_s = f"{avg_conf:.2f}" if avg_conf is not None else "–"
    cost_s = f"${total_cost:.2f}" if total_cost is not None else "–"
    eval_s = f"{eval_pass} / {len(eval_rows)}" if eval_rows else "–"
    return f"""
    <div class="kpirow">
      <div class="kpi"><div class="label">Incidents</div><div class="value">{n}</div></div>
      <div class="kpi"><div class="label">Avg confidence</div><div class="value">{conf_s}</div></div>
      <div class="kpi"><div class="label">Total spend</div><div class="value accent">{cost_s}</div></div>
      <div class="kpi"><div class="label">Eval pass rate</div><div class="value moss">{eval_s}</div></div>
    </div>
    """


def _card_html(row: sqlite3.Row, number: int, selected: bool) -> str:
    date, time = _fmt_when(row)
    title = _incident_title(row)
    pill_class, pill_text = _status_pill(row)
    conf = row["confidence_score"]
    pill_full = f"{pill_text} · {conf:.2f}" if conf is not None else pill_text
    cost = f"${row['cost_usd']:.4f}" if row["cost_usd"] is not None else "–"
    cat = f'<span class="cat">{esc(row["category"])}</span>' if row["category"] else ""
    sel = " selected" if selected else ""
    return f"""
    <div class="row{sel}">
      <div class="row-main">
        <div class="row-top"><span class="inum">#{number}</span><span class="who">{esc(title)}</span></div>
        <div class="sub">{esc(row["namespace"])} · noticed {esc(date)} {esc(time)} · {row["duration_s"]:.0f}s · {cat}</div>
      </div>
      <div class="row-side">
        <span class="pill {pill_class}">{esc(pill_full)}</span>
        <div class="cost">{cost}</div>
      </div>
    </div>
    """


def _pipeline_html(row: sqlite3.Row, evidence: list, correlation: dict | None, hypotheses: list) -> str:
    applied = row["mode"] == "execute" and row["approval_status"] == "approved_applied"
    stages = [
        ("Gather", bool(evidence), "done"),
        ("Correlate", bool(correlation and (correlation.get("timeline") or correlation.get("dependency_chain"))), "done"),
        ("Hypothesize", bool(hypotheses), "done"),
        ("Rank", bool(hypotheses), "done"),
        ("Propose", True, "done"),
        ("Apply", applied, "apply"),
    ]
    parts = []
    for i, (name, done, cls) in enumerate(stages):
        if i:
            parts.append('<span class="arr">→</span>')
        klass = f"step {cls}" if done else "step"
        parts.append(f'<span class="{klass}">{name}</span>')
    return f'<div class="pipe">{"".join(parts)}</div>'


def _evidence_html(evidence: list) -> str:
    rows = []
    for e in evidence:
        mark = '<span class="ok">✓</span>' if e.get("ok") else '<span class="no">✗</span>'
        args = ", ".join(f"{k}={v}" for k, v in (e.get("input") or {}).items())
        args_html = f'<span class="args">({esc(args)})</span>' if args else ""
        rows.append(
            f'<div class="ev">{mark}<div><span class="tool">{esc(e.get("tool"))}</span>'
            f'{args_html} — <span class="summ">{esc(e.get("summary"))}</span></div></div>'
        )
    return "".join(rows)


def _detail_panel(row: sqlite3.Row, number: int) -> str:
    date, time = _fmt_when(row)
    title = _incident_title(row)
    evidence = json.loads(row["evidence_json"] or "[]")
    correlation = json.loads(row["correlation_json"]) if row["correlation_json"] else None
    hypotheses = json.loads(row["hypotheses_json"] or "[]")
    report = json.loads(row["report_json"])
    rem = report["remediation"]

    parts = [
        f'<div class="p-num">INCIDENT #{number}</div>',
        f"<h3>{esc(title)}</h3>",
        f'<div class="p-sub">{esc(report.get("summary") or report["root_cause"])[:180]}</div>',
    ]

    meta = [
        f'<span>namespace <b>{esc(row["namespace"])}</b></span>',
        f'<span>noticed <b>{esc(date)} {esc(time)}</b></span>',
        f'<span>took <b>{row["duration_s"]:.0f}s</b></span>',
    ]
    if row["cost_usd"] is not None:
        meta.append(f'<span>cost <b>${row["cost_usd"]:.4f}</b></span>')
    if row["incident_name"]:
        meta.append(f'<span>eval <b>{esc(row["incident_name"])}</b></span>')
    parts.append(f'<div class="p-meta">{"".join(meta)}</div>')

    # approach pipeline
    parts.append('<div class="section-label">How the agent approached it</div>')
    parts.append(_pipeline_html(row, evidence, correlation, hypotheses))

    # evidence gathered
    if evidence:
        parts.append('<div class="section-label">Evidence gathered</div>')
        parts.append(_evidence_html(evidence))

    # dependency chain
    if correlation and correlation.get("dependency_chain"):
        chain = correlation["dependency_chain"]
        pills = '<span class="chain-arr">→</span>'.join(
            f'<span class="chain-pill">{esc(s)}</span>' for s in chain
        )
        parts.append('<div class="section-label">Dependency chain</div>')
        parts.append(f"<div>{pills}</div>")

    # timeline
    if correlation and correlation.get("timeline"):
        parts.append('<div class="section-label">Correlated timeline</div>')
        for ev in correlation["timeline"]:
            parts.append(
                f'<div class="tl"><span class="when">{esc(ev.get("when"))}</span>'
                f'<span class="what">{esc(ev.get("what"))}</span></div>'
            )

    # ranked hypotheses
    if hypotheses:
        parts.append('<div class="section-label">Ranked hypotheses</div>')
        for i, h in enumerate(hypotheses):
            win = " win" if i == 0 else ""
            pct = f"{h['confidence'] * 100:.0f}"
            parts.append(
                f'<div class="hyp{win}">'
                f'<div class="hyp-top"><span class="n">{h["confidence"]:.2f}</span>'
                f'<div class="hypbar-track"><div class="hypbar-fill" style="width:{pct}%"></div></div></div>'
                f'<div class="hyp-txt"><span class="hyp-cat">{esc(h.get("category"))}</span> — {esc(h.get("cause"))}</div>'
                f"</div>"
            )

    # root cause verdict
    band = report.get("confidence", "")
    parts.append('<div class="section-label">Root cause</div>')
    parts.append(
        f'<div class="verdict"><span class="band">{esc(report["category"])} · '
        f'{esc(band)} confidence · {report["confidence_score"]:.2f}</span><br>{esc(report["root_cause"])}</div>'
    )
    if report.get("impact"):
        parts.append(f'<div class="impact"><b>Impact:</b> {esc(report["impact"])}</div>')

    # remediation
    parts.append('<div class="section-label">Proposed remediation</div>')
    why = f'<div class="why">{esc(rem.get("rationale"))}</div>' if rem.get("rationale") else ""
    parts.append(
        f'<div class="fixbox">{esc(rem["action"])}{why}<code>{esc(rem["command"])}</code></div>'
    )
    badge_cls, label, explanation = _remediation_status(row)
    parts.append(
        f'<div class="applied"><span class="pill {badge_cls}">{esc(label)}</span>'
        f'<span class="why2">{esc(explanation)}</span></div>'
    )

    # eval checks
    eval_checks = json.loads(row["eval_checks_json"]) if row["eval_checks_json"] else None
    if eval_checks:
        parts.append('<div class="section-label">Eval checks</div>')
        for c in eval_checks:
            mk = '<span class="mk ok">✓</span>' if c["passed"] else '<span class="mk no">✗</span>'
            crit = "critical" if c["critical"] else "info"
            parts.append(
                f'<div class="chk">{mk}<div><span class="cn">{esc(c["name"])}</span> '
                f'<span class="crit">({crit})</span><br><span class="cd">{esc(c["detail"])}</span></div></div>'
            )

    return f'<div class="panel">{"".join(parts)}</div>'


def main() -> None:
    st.set_page_config(page_title="SRE Agent — Incident History", layout="wide")
    st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)

    rows = history_store.list_runs(limit=100)

    st.markdown(
        '<div class="topbar"><h1>SRE Agent</h1>'
        '<span class="tag"><span class="dot"></span>Autonomous Kubernetes incident investigator · run history</span></div>',
        unsafe_allow_html=True,
    )

    if not rows:
        st.markdown(
            '<div class="panel">No incidents recorded yet — <code>investigate</code> or '
            "<code>eval</code> writes to this history.</div>",
            unsafe_allow_html=True,
        )
        return

    st.markdown(_kpi_row(rows), unsafe_allow_html=True)

    # stable chronological numbering: oldest = #1 (rows come newest-first)
    n = len(rows)
    numbers = {row["id"]: n - i for i, row in enumerate(rows)}

    if "selected_run_id" not in st.session_state or st.session_state.selected_run_id not in numbers:
        st.session_state.selected_run_id = rows[0]["id"]

    feed_col, detail_col = st.columns([1.25, 1], gap="large")

    with feed_col:
        st.markdown('<div class="col-head">Incident feed</div>', unsafe_allow_html=True)
        for row in rows:
            is_selected = row["id"] == st.session_state.selected_run_id
            card_col, btn_col = st.columns([12, 1], gap="small", vertical_alignment="center")
            with card_col:
                st.markdown(_card_html(row, numbers[row["id"]], is_selected), unsafe_allow_html=True)
            with btn_col:
                if st.button("›", key=f"sel_{row['id']}", help="View details"):
                    st.session_state.selected_run_id = row["id"]
                    st.rerun()

    with detail_col:
        st.markdown('<div class="col-head">Investigation detail</div>', unsafe_allow_html=True)
        selected = history_store.get_run(st.session_state.selected_run_id)
        if selected is not None:
            st.markdown(_detail_panel(selected, numbers.get(selected["id"], 0)), unsafe_allow_html=True)


if __name__ == "__main__":
    main()
