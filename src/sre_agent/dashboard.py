"""Phase 8: Streamlit dashboard over the run-history store (Phase 7).

Run with `streamlit run src/sre_agent/dashboard.py` from the repo root (same CWD
convention as the CLI, so history_store's relative data/history.db path resolves).

Design: Direction B, "Clarity" (see docs/dashboard-plan.md). Rendered as raw HTML via
st.markdown(unsafe_allow_html=True) — native st.metric/st.dataframe can't hit this level
of visual control. CSS is injected once as global :root rules.

Navigation is three views driven by st.session_state (Streamlit has no real router):
  - "feed"      — home page: header, KPI row, and the incident feed. Each run is a
                  named incident (Incident #N + a title derived from the RCA) with the
                  time the agent noticed it and a one-line summary.
  - "detail"    — a full-width page for one incident (replaces the earlier sticky side
                  panel, which stacked every section into one tall narrow column). The
                  investigation story is laid out as a grid of section cards: header
                  band (title, status, meta, pipeline strip), then root cause /
                  remediation / eval checks beside dependency chain / hypotheses /
                  evidence, with the correlated timeline flowing in two columns under.
  - "analytics" — aggregate agentic metrics across all runs: cost per run, prompt-
                  cache economics (the Phase-4 caching story), incidents by category,
                  tool usage (which tools the agent reaches for), and the human-
                  approval funnel. Chart colors were validated with the dataviz
                  palette validator (#1f7a50 chart green passes the chroma floor the
                  UI's muted --moss token fails; #c1622b ember passes as-is); forms
                  follow its guidance — single-hue magnitude bars, a meter for the
                  cache-hit ratio, status pills (never color alone) for the funnel.

Row activation: a real st.button beside each card. Streamlit wraps every st.markdown/
st.button in its own element-container div, so an invisible overlay button via a CSS
sibling selector does not work; a visible button is the robust choice.
"""

from __future__ import annotations

import html
import json
import sqlite3

import streamlit as st

from sre_agent import history_store

CSS = """
/* Typography: IBM Plex Sans for UI, IBM Plex Mono for code/ids/token readouts.
   Webfont with a system fallback — offline the dashboard degrades to Segoe/system
   sans, nothing breaks. @import must precede every other rule in the sheet. */
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');

:root {
  --ink: #1b2420; --paper: #f4f6f4; --card: #ffffff; --border: #dfe6e1; --muted: #6b7772;
  --ember: #c1622b; --ember-soft: #f6e2d3; --moss: #3f6e5c; --moss-soft: #e1ece7;
  --good: #2f9e63; --good-soft: #e1f2e8; --warn: #c98a2e; --warn-soft: #f6ebd7;
  --bad: #b23b3b; --bad-soft: #f6dede;
}
.stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] { background: var(--paper); }
[data-testid="stMainBlockContainer"] { padding-top: 1.5rem; max-width: 1260px; }
html, body, [data-testid="stAppViewContainer"] * {
  font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
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
.kpi .value { font-size: 1.55rem; font-weight: 700; margin-top: 0.15rem; letter-spacing: -0.01em; }
.kpi .value.accent { color: var(--ember); }
.kpi .value.moss { color: var(--moss); }

.col-head { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 700; margin: 0 0 0.7rem; }

/* ---- incident cards (feed) ---- */
.row { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 0.85rem 1rem; display: flex; justify-content: space-between; align-items: center; gap: 1rem; box-shadow: 0 1px 2px rgba(20,30,25,0.03); }
.row .row-main { min-width: 0; flex: 1; }
.row .row-top { display: flex; align-items: baseline; gap: 0.55rem; }
.row .inum { font-size: 0.72rem; font-weight: 700; color: var(--ember); font-variant-numeric: tabular-nums; }
.row .who { font-weight: 650; font-size: 0.95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.row .sub { font-size: 0.77rem; color: var(--muted); margin-top: 0.22rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.row .row-side { text-align: right; flex-shrink: 0; }
.row .noticed { font-size: 0.7rem; color: var(--muted); margin-top: 0.28rem; font-variant-numeric: tabular-nums; }
.cat { display: inline-block; background: var(--moss-soft); color: var(--moss); font-size: 0.68rem; font-weight: 700; padding: 0.05rem 0.45rem; border-radius: 5px; letter-spacing: 0.02em; }
.auto-chip { display: inline-block; background: var(--ember-soft); color: var(--ember); font-size: 0.66rem; font-weight: 700; padding: 0.05rem 0.45rem; border-radius: 5px; letter-spacing: 0.04em; text-transform: uppercase; }
.pill { font-size: 0.7rem; font-weight: 700; padding: 0.2rem 0.6rem; border-radius: 999px; white-space: nowrap; }
.pill.good { color: var(--good); background: var(--good-soft); }
.pill.warn { color: var(--warn); background: var(--warn-soft); }
.pill.bad  { color: var(--bad); background: var(--bad-soft); }
.cost { font-variant-numeric: tabular-nums; font-weight: 600; font-size: 0.82rem; color: var(--moss); margin-top: 0.25rem; }

/* ---- detail page ---- */
.hdr { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 1.15rem 1.3rem 1.05rem; box-shadow: 0 2px 10px rgba(20,30,25,0.06); margin-bottom: 1rem; }
.hdr .p-num { font-size: 0.72rem; font-weight: 700; color: var(--ember); letter-spacing: 0.04em; }
.hdr .title-row { display: flex; align-items: center; gap: 0.7rem; margin-top: 0.15rem; }
.hdr h2 { margin: 0; font-size: 1.35rem; letter-spacing: -0.01em; flex: 1; }
.hdr .p-sub { font-size: 0.82rem; color: var(--muted); margin-top: 0.35rem; max-width: 62rem; }
.metarow { display: flex; flex-wrap: wrap; gap: 0.45rem; margin-top: 0.75rem; }
.meta-chip { background: var(--paper); border: 1px solid var(--border); border-radius: 999px; padding: 0.22rem 0.7rem; font-size: 0.73rem; color: var(--muted); }
.meta-chip b { color: var(--ink); font-weight: 650; font-variant-numeric: tabular-nums; }

/* section cards */
.scard { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 1rem 1.15rem 1.05rem; box-shadow: 0 1px 3px rgba(20,30,25,0.04); margin-bottom: 0.9rem; }
.scard .scard-label { font-size: 0.67rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 700; margin-bottom: 0.55rem; }

/* pipeline strip */
.pipe { display: flex; flex-wrap: wrap; align-items: center; gap: 0.3rem; margin-top: 0.85rem; }
.step { font-size: 0.71rem; font-weight: 700; padding: 0.22rem 0.6rem; border-radius: 6px; background: var(--paper); color: var(--muted); border: 1px solid var(--border); }
.step.done { background: var(--moss-soft); color: var(--moss); border-color: var(--moss-soft); }
.step.apply { background: var(--good-soft); color: var(--good); border-color: var(--good-soft); }
.pipe .arr { color: var(--border); font-size: 0.75rem; }

/* evidence rows */
.ev { display: flex; gap: 0.5rem; font-size: 0.78rem; margin: 0.34rem 0; align-items: flex-start; }
.ev .ok { color: var(--good); font-weight: 700; }
.ev .no { color: var(--bad); font-weight: 700; }
.ev .tool { font-weight: 600; }
.ev .args { color: var(--muted); }
.ev .summ { color: var(--muted); }

/* timeline: two-column grid of entries */
.tlgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.55rem 1.6rem; }
@media (max-width: 1100px) { .tlgrid { grid-template-columns: 1fr; } }
.tl { font-size: 0.78rem; }
.tl .when { color: var(--ember); font-weight: 650; font-variant-numeric: tabular-nums; display: block; }
.tl .what { color: var(--ink); }

/* dependency chain */
.chain-pill { display: inline-block; background: var(--moss-soft); color: var(--moss); font-size: 0.76rem; font-weight: 600; padding: 0.18rem 0.55rem; border-radius: 6px; }
.chain-arr { color: var(--muted); margin: 0 0.2rem; }

/* hypotheses */
.hyp { margin: 0.55rem 0; }
.hyp .hyp-top { display: flex; align-items: center; gap: 0.6rem; font-size: 0.78rem; }
.hyp .n { width: 2.4rem; color: var(--muted); font-variant-numeric: tabular-nums; font-weight: 600; }
.hypbar-track { background: var(--border); border-radius: 4px; height: 6px; overflow: hidden; flex: 1; }
.hypbar-fill { background: var(--ember); height: 100%; }
.hyp .hyp-txt { font-size: 0.76rem; color: var(--muted); margin: 0.14rem 0 0 3rem; }
.hyp.win .hyp-txt { color: var(--ink); }
.hyp.win .hyp-cat { color: var(--ember); font-weight: 700; }
.hyp .hyp-cat { color: var(--muted); font-weight: 700; }

/* verdict + impact */
.verdict { background: var(--ember-soft); border-radius: 9px; padding: 0.8rem 0.95rem; font-size: 0.82rem; }
.verdict .band { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ember); font-weight: 700; }
.impact { font-size: 0.8rem; color: var(--ink); margin-top: 0.55rem; }

/* cited evidence / alternatives — the RCA's own free-text citations (report.evidence,
   report.alternatives), distinct from "Evidence gathered" (the raw tool-call trail). */
.citelist { font-size: 0.8rem; line-height: 1.55; }
.citelist .item { margin: 0.32rem 0; padding-left: 0.9rem; position: relative; }
.citelist .item::before { content: "•"; position: absolute; left: 0; color: var(--muted); }
.citelist .item.dim { color: var(--muted); }

/* remediation */
.fixbox { background: var(--moss-soft); border-radius: 9px; padding: 0.8rem 0.95rem; font-size: 0.8rem; }
.fixbox .why { color: var(--muted); font-size: 0.76rem; margin-top: 0.25rem; }
.fixbox code { display: block; margin-top: 0.5rem; color: var(--moss); font-size: 0.78rem; font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace; background: rgba(63,110,92,0.08); padding: 0.45rem 0.6rem; border-radius: 6px; }
.applied { display: flex; align-items: flex-start; gap: 0.55rem; margin-top: 0.65rem; font-size: 0.78rem; }
.applied .why2 { color: var(--muted); }

/* eval checks */
.chk { display: flex; gap: 0.5rem; align-items: flex-start; font-size: 0.78rem; margin: 0.32rem 0; }
.chk .mk.ok { color: var(--good); font-weight: 700; }
.chk .mk.no { color: var(--bad); font-weight: 700; }
.chk .cn { font-weight: 600; }
.chk .crit { font-size: 0.66rem; color: var(--muted); }
.chk .cd { color: var(--muted); }

/* ---- analytics charts ----
   Colors validated with the dataviz palette validator against the white card
   surface: chart green #1f7a50 (the UI's --moss #3f6e5c fails the chroma floor as a
   *mark* color — it reads gray) and ember #c1622b. Single-hue magnitude bars, value
   at the tip, 4px rounded data-end, square at the baseline. Cache-hit is a ratio ->
   a meter whose track is a lighter step of the same ramp (--moss-soft). */
.bar-row { display: flex; align-items: center; gap: 0.6rem; margin: 0.42rem 0; font-size: 0.78rem; }
.bar-row .bl { width: 11rem; flex-shrink: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bar-row .bl .blsub { color: var(--muted); font-size: 0.72rem; }
.bar-row .bar-track { flex: 1; height: 14px; position: relative; }
.bar-row .bar-fill { height: 100%; border-radius: 0 4px 4px 0; }
.bar-fill.green { background: #1f7a50; }
.bar-fill.ember { background: #c1622b; }
.bar-row .bar-val { min-width: 3.4rem; font-variant-numeric: tabular-nums; font-weight: 600; text-align: right; }
.meter-track { flex: 1; height: 10px; background: var(--moss-soft); border-radius: 5px; overflow: hidden; }
.meter-fill { height: 100%; background: #1f7a50; border-radius: 0 4px 4px 0; }
.tok-line { font-size: 0.74rem; color: var(--muted); margin-top: 0.35rem; font-variant-numeric: tabular-nums; }
.fun { display: flex; align-items: center; gap: 0.6rem; font-size: 0.79rem; margin: 0.45rem 0; }
.fun .fn { flex: 1; }
.fun .fc { font-variant-numeric: tabular-nums; font-weight: 650; }
.note { font-size: 0.76rem; color: var(--muted); }

/* buttons (nav, feed "Open", detail "back") */
div[data-testid="stButton"] button {
  border: 1px solid var(--border); color: var(--muted); background: var(--card);
  border-radius: 8px; min-height: 0; padding: 0.32rem 0.75rem;
}
div[data-testid="stButton"] button p { font-size: 0.78rem; font-weight: 650; }
div[data-testid="stButton"] button:hover { border-color: var(--ember); color: var(--ember); background: var(--ember-soft); }
div[data-testid="stButton"] button:hover p { color: var(--ember); }
div[data-testid="stButton"] button:focus:not(:active) { border-color: var(--ember); }
div[data-testid="stButton"] button[kind="primary"],
[data-testid="stBaseButton-primary"] {
  background: var(--ember); border-color: var(--ember);
}
div[data-testid="stButton"] button[kind="primary"] p,
[data-testid="stBaseButton-primary"] p { color: #ffffff; }
div[data-testid="stButton"] button[kind="primary"]:hover,
[data-testid="stBaseButton-primary"]:hover { background: #a9541f; border-color: #a9541f; }

/* ---- Plex refinements (last so they win ties): mono accents, air, legibility ---- */
/* Streamlit's own theme styles headings with its "Source Sans" at higher specificity
   than the container-wide * rule — restate the family on headings explicitly. */
.topbar h1, .hdr h2, .scard h3, [data-testid="stAppViewContainer"] h1,
[data-testid="stAppViewContainer"] h2, [data-testid="stAppViewContainer"] h3 {
  font-family: "IBM Plex Sans", -apple-system, "Segoe UI", system-ui, sans-serif !important;
}
.topbar h1 { letter-spacing: -0.02em; }
.hdr h2 { font-size: 1.4rem; letter-spacing: -0.015em; }
.hdr .p-sub { font-size: 0.85rem; line-height: 1.55; }
.p-num, .inum {
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace; letter-spacing: 0.05em;
}
.tok-line, .ev .args {
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace; font-size: 0.71rem;
}
.row .who { letter-spacing: -0.005em; }
.row .sub { font-size: 0.79rem; }
.ev, .tl, .chk { font-size: 0.8rem; line-height: 1.5; }
.hyp .hyp-top { font-size: 0.8rem; }
.hyp .hyp-txt { font-size: 0.78rem; line-height: 1.45; }
.verdict { font-size: 0.84rem; line-height: 1.55; }
.impact { font-size: 0.82rem; line-height: 1.5; }
.fixbox { font-size: 0.82rem; line-height: 1.5; }
.scard .scard-label, .section-label, .col-head, .kpi .label { letter-spacing: 0.07em; }
.kpi .value { font-size: 1.6rem; }
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


def _is_auto(row: sqlite3.Row) -> bool:
    """True when the run was triggered by an alert (Phase 9 listener), not a human."""
    return "triggered_by" in row.keys() and row["triggered_by"] == "alert"


def _summary_line(row: sqlite3.Row) -> str:
    report = json.loads(row["report_json"])
    return report.get("summary") or report["root_cause"]


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
    """(badge css class, label, explanation) for who (if anyone) applied the fix,
    and — Phase 11 — whether it was actually verified to have worked. "Applied" and
    "worked" are different facts; the badge must say which one it's reporting.

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
    if row["approval_status"] == "approved_applied":
        v = row["verification_status"]
        if v == "confirmed_healthy":
            return (
                "good", "Applied & verified healthy",
                f"{row['verification_detail']} — a bounded check taken right after "
                "applying, not a permanent guarantee.",
            )
        if v == "still_unhealthy":
            return (
                "bad", "Applied — verification FAILED",
                f"{row['verification_detail']} — the fix was applied but did not "
                "resolve the issue.",
            )
        # not_checked, or a pre-Phase-11 row with no verification_status at all
        return (
            "warn", "Applied (not verified)",
            "A human approved and it was applied, but not independently verified "
            "whether it resolved the issue.",
        )
    return {
        "rejected": ("warn", "Rejected by human", "A human reviewed the proposal and declined to apply it."),
        "blocked": ("bad", "Blocked by safety gate", "The command failed the allowlist validator before any dry-run."),
        "dry_run_failed": ("bad", "Dry-run failed", "The server-side dry-run rejected the command; nothing was applied."),
        "apply_failed": ("bad", "Apply failed", "Approved, but the apply command errored."),
    }.get(row["approval_status"], ("warn", row["approval_status"], ""))


def _scard(label: str, inner: str) -> str:
    return f'<div class="scard"><div class="scard-label">{esc(label)}</div>{inner}</div>'


def _bullet_list(items: list[str], dim: bool = False) -> str:
    """Render report.evidence / report.alternatives — the RCA's own free-text
    citations, distinct from the tool-call trail rendered by _evidence_html()."""
    cls = "item dim" if dim else "item"
    return '<div class="citelist">' + "".join(f'<div class="{cls}">{esc(i)}</div>' for i in items) + "</div>"


# ---------------------------------------------------------------------------
# feed view
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


def _card_html(row: sqlite3.Row, number: int) -> str:
    date, time = _fmt_when(row)
    title = _incident_title(row)
    pill_class, pill_text = _status_pill(row)
    conf = row["confidence_score"]
    pill_full = f"{pill_text} · {conf:.2f}" if conf is not None else pill_text
    cost = f"${row['cost_usd']:.4f}" if row["cost_usd"] is not None else "–"
    cat = f'<span class="cat">{esc(row["category"])}</span>' if row["category"] else ""
    auto = '<span class="auto-chip">auto</span>' if _is_auto(row) else ""
    return f"""
    <div class="row">
      <div class="row-main">
        <div class="row-top"><span class="inum">#{number}</span><span class="who">{esc(title)}</span>{cat}{auto}</div>
        <div class="sub">{esc(_summary_line(row))}</div>
      </div>
      <div class="row-side">
        <span class="pill {pill_class}">{esc(pill_full)}</span>
        <div class="cost">{cost}</div>
        <div class="noticed">noticed {esc(date)} {esc(time)}</div>
      </div>
    </div>
    """


def _render_feed(rows: list, numbers: dict[str, int]) -> None:
    st.markdown(_kpi_row(rows), unsafe_allow_html=True)
    st.markdown('<div class="col-head">Incident feed</div>', unsafe_allow_html=True)
    for row in rows:
        card_col, btn_col = st.columns([11, 1], gap="small", vertical_alignment="center")
        with card_col:
            st.markdown(_card_html(row, numbers[row["id"]]), unsafe_allow_html=True)
        with btn_col:
            if st.button("Open", key=f"open_{row['id']}", use_container_width=True):
                st.session_state.selected_run_id = row["id"]
                st.session_state.view = "detail"
                st.rerun()


# ---------------------------------------------------------------------------
# analytics view
# ---------------------------------------------------------------------------


def _token_stats(row: sqlite3.Row) -> tuple[int, int, int, int, float | None]:
    """(fresh_input, cache_write, cache_read, output, cache_hit_ratio)."""
    fresh = row["input_tokens"] or 0
    cw = row["cache_write_tokens"] or 0
    cr = row["cache_read_tokens"] or 0
    out = row["output_tokens"] or 0
    prompt = fresh + cw + cr
    return fresh, cw, cr, out, (cr / prompt if prompt else None)


def _bar_rows(items: list[tuple[str, str, float, str]], color: str) -> str:
    """items = [(label_html, sub_html, value, display)] -> single-hue bar rows."""
    mx = max((v for _, _, v, _ in items), default=0) or 1
    parts = []
    for label, sub, value, display in items:
        sub_html = f'<span class="blsub"> {sub}</span>' if sub else ""
        parts.append(
            f'<div class="bar-row"><div class="bl">{label}{sub_html}</div>'
            f'<div class="bar-track"><div class="bar-fill {color}" style="width:{value / mx * 100:.0f}%"></div></div>'
            f'<div class="bar-val">{display}</div></div>'
        )
    return "".join(parts)


def _meter_row(label_html: str, ratio: float, display: str) -> str:
    return (
        f'<div class="bar-row"><div class="bl">{label_html}</div>'
        f'<div class="meter-track"><div class="meter-fill" style="width:{ratio * 100:.0f}%"></div></div>'
        f'<div class="bar-val">{display}</div></div>'
    )


def _render_analytics(rows: list, numbers: dict[str, int]) -> None:
    chron = sorted(rows, key=lambda r: r["started_at"])  # oldest first

    # headline tiles
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    total_cost = sum(costs) if costs else None
    avg_cost = total_cost / len(costs) if costs else None
    tot_fresh = tot_cw = tot_cr = 0
    for r in rows:
        fresh, cw, cr, _out, _hit = _token_stats(r)
        tot_fresh, tot_cw, tot_cr = tot_fresh + fresh, tot_cw + cw, tot_cr + cr
    overall_prompt = tot_fresh + tot_cw + tot_cr
    overall_hit = tot_cr / overall_prompt if overall_prompt else None
    execute_rows = [r for r in rows if r["mode"] == "execute"]
    accepted = sum(1 for r in execute_rows if r["approval_status"] == "approved_applied")

    cost_s = f"${total_cost:.2f}" if total_cost is not None else "–"
    avg_s = f"${avg_cost:.3f}" if avg_cost is not None else "–"
    hit_s = f"{overall_hit * 100:.0f}%" if overall_hit is not None else "–"
    acc_s = f"{accepted} / {len(execute_rows)}" if execute_rows else "–"
    st.markdown(
        f"""
        <div class="kpirow">
          <div class="kpi"><div class="label">Total spend</div><div class="value accent">{cost_s}</div></div>
          <div class="kpi"><div class="label">Avg cost / investigation</div><div class="value">{avg_s}</div></div>
          <div class="kpi"><div class="label">Prompt cache hit rate</div><div class="value moss">{hit_s}</div></div>
          <div class="kpi"><div class="label">Proposals accepted</div><div class="value">{acc_s}</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    left, right = st.columns(2, gap="medium")

    with left:
        # cost per run, chronological
        cost_items = [
            (
                f"#{numbers[r['id']]} {esc(_incident_title(r))}",
                esc(r["mode"]),
                r["cost_usd"] or 0.0,
                f"${(r['cost_usd'] or 0):.2f}",
            )
            for r in chron
        ]
        st.markdown(_scard("Cost per investigation", _bar_rows(cost_items, "green")), unsafe_allow_html=True)

        # prompt-cache economics per run
        cache_parts = []
        for r in chron:
            fresh, cw, cr, out, hit = _token_stats(r)
            if hit is None:
                continue
            cache_parts.append(_meter_row(f"#{numbers[r['id']]} {esc(_incident_title(r))}", hit, f"{hit * 100:.0f}%"))
            cache_parts.append(
                f'<div class="tok-line">fresh in {fresh:,} · cache write {cw:,} · '
                f"cache read {cr:,} · output {out:,}</div>"
            )
        cache_parts.append(
            '<div class="note" style="margin-top:0.6rem">Share of prompt tokens served from the Anthropic '
            "prompt cache — the correlate/hypothesize/propose passes reuse gather's cache prefix.</div>"
        )
        st.markdown(_scard("Prompt cache economics", "".join(cache_parts)), unsafe_allow_html=True)

        # human approval funnel
        propose_rows = [r for r in rows if r["mode"] == "propose"]
        eval_rows = [r for r in rows if r["mode"] == "eval"]
        eval_pass = sum(1 for r in eval_rows if r["resolved"])
        fun = [
            f'<div class="fun"><span class="pill warn">proposed</span>'
            f'<span class="fn">Investigations (propose-only — fix surfaced, never applied)</span>'
            f'<span class="fc">{len(propose_rows)}</span></div>',
            f'<div class="fun"><span class="pill good">eval</span>'
            f'<span class="fn">Eval-harness runs (staged, scored, auto-reverted) — {eval_pass} passed</span>'
            f'<span class="fc">{len(eval_rows)}</span></div>',
        ]
        if execute_rows:
            by_status: dict[str, int] = {}
            for r in execute_rows:
                by_status[r["approval_status"]] = by_status.get(r["approval_status"], 0) + 1
            label_for = {
                "approved_applied": ("good", "approved & applied by a human"),
                "rejected": ("warn", "rejected by a human"),
                "blocked": ("bad", "blocked by the safety gate"),
                "dry_run_failed": ("bad", "failed server dry-run"),
                "apply_failed": ("bad", "apply errored after approval"),
            }
            for status, count in sorted(by_status.items(), key=lambda kv: -kv[1]):
                cls, text = label_for.get(status, ("warn", status))
                fun.append(
                    f'<div class="fun"><span class="pill {cls}">{esc(status.replace("_", " "))}</span>'
                    f'<span class="fn">Execute runs {esc(text)}</span><span class="fc">{count}</span></div>'
                )
            rate = accepted / len(execute_rows) * 100
            fun.append(f'<div class="note" style="margin-top:0.5rem">Human acceptance rate: {rate:.0f}%.</div>')
        else:
            fun.append(
                '<div class="note" style="margin-top:0.5rem">No execute-mode runs recorded yet — this funnel '
                "fills in when <code>investigate -x</code> puts a proposal through the approval gate.</div>"
            )
        st.markdown(_scard("Human approval funnel", "".join(fun)), unsafe_allow_html=True)

    with right:
        # incidents by category
        cat_counts: dict[str, int] = {}
        for r in rows:
            cat = r["category"] or "unknown"
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        cat_items = [
            (esc(_pretty_category(cat)), esc(cat), float(count), str(count))
            for cat, count in sorted(cat_counts.items(), key=lambda kv: -kv[1])
        ]
        st.markdown(_scard("Incidents by category", _bar_rows(cat_items, "ember")), unsafe_allow_html=True)

        # tool usage — which tools the agent reaches for
        tool_counts: dict[str, int] = {}
        tool_cats: dict[str, set[str]] = {}
        total_calls = 0
        for r in rows:
            for e in json.loads(r["evidence_json"] or "[]"):
                tool = e.get("tool") or "?"
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
                tool_cats.setdefault(tool, set()).add(r["category"] or "unknown")
                total_calls += 1
        tool_items = [
            (esc(tool), esc(", ".join(sorted(tool_cats[tool]))), float(count), str(count))
            for tool, count in sorted(tool_counts.items(), key=lambda kv: -kv[1])
        ]
        tools_html = _bar_rows(tool_items, "ember") + (
            f'<div class="note" style="margin-top:0.6rem">{total_calls} tool calls across '
            f"{len(rows)} investigations — the sub-label shows which incident categories "
            "each tool was reached for.</div>"
        )
        st.markdown(_scard("Tool usage", tools_html), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# detail view
# ---------------------------------------------------------------------------


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


def _header_band(row: sqlite3.Row, number: int, evidence: list, correlation: dict | None, hypotheses: list) -> str:
    date, time = _fmt_when(row)
    title = _incident_title(row)
    pill_class, pill_text = _status_pill(row)
    conf = row["confidence_score"]
    pill_full = f"{pill_text} · {conf:.2f}" if conf is not None else pill_text

    chips = [
        f'<span class="meta-chip">namespace <b>{esc(row["namespace"])}</b></span>',
        f'<span class="meta-chip">noticed <b>{esc(date)} {esc(time)}</b></span>',
        f'<span class="meta-chip">investigation took <b>{row["duration_s"]:.0f}s</b></span>',
    ]
    if row["cost_usd"] is not None:
        chips.append(f'<span class="meta-chip">cost <b>${row["cost_usd"]:.4f}</b></span>')
    if row["incident_name"]:
        chips.append(f'<span class="meta-chip">eval scenario <b>{esc(row["incident_name"])}</b></span>')
    chips.append(f'<span class="meta-chip">mode <b>{esc(row["mode"])}</b></span>')
    auto = '<span class="auto-chip">auto · alert-triggered</span>' if _is_auto(row) else ""

    return f"""
    <div class="hdr">
      <div class="p-num">INCIDENT #{number}</div>
      <div class="title-row"><h2>{esc(title)}</h2>{auto}<span class="pill {pill_class}">{esc(pill_full)}</span></div>
      <div class="p-sub">{esc(_summary_line(row))}</div>
      <div class="metarow">{"".join(chips)}</div>
      {_pipeline_html(row, evidence, correlation, hypotheses)}
    </div>
    """


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


def _render_detail(row: sqlite3.Row, number: int) -> None:
    evidence = json.loads(row["evidence_json"] or "[]")
    correlation = json.loads(row["correlation_json"]) if row["correlation_json"] else None
    hypotheses = json.loads(row["hypotheses_json"] or "[]")
    report = json.loads(row["report_json"])
    rem = report["remediation"]

    if st.button("← All incidents"):
        st.session_state.view = "feed"
        st.rerun()

    st.markdown(_header_band(row, number, evidence, correlation, hypotheses), unsafe_allow_html=True)

    left, right = st.columns([1.05, 1], gap="medium")

    with left:
        # root cause verdict
        band = report.get("confidence", "")
        verdict = (
            f'<div class="verdict"><span class="band">{esc(report["category"])} · '
            f'{esc(band)} confidence · {report["confidence_score"]:.2f}</span><br>{esc(report["root_cause"])}</div>'
        )
        if report.get("impact"):
            verdict += f'<div class="impact"><b>Impact:</b> {esc(report["impact"])}</div>'
        st.markdown(_scard("Root cause", verdict), unsafe_allow_html=True)

        # the RCA's own cited evidence (report.evidence) -- NOT the tool-call trail;
        # that's the separate "Evidence gathered" card in the right column below.
        if report.get("evidence"):
            st.markdown(_scard("Evidence cited", _bullet_list(report["evidence"])), unsafe_allow_html=True)

        # alternatives the model considered and rejected
        if report.get("alternatives"):
            st.markdown(
                _scard("Alternatives considered", _bullet_list(report["alternatives"], dim=True)),
                unsafe_allow_html=True,
            )

        # remediation + who applied it
        why = f'<div class="why">{esc(rem.get("rationale"))}</div>' if rem.get("rationale") else ""
        badge_cls, label, explanation = _remediation_status(row)
        fix = (
            f'<div class="fixbox">{esc(rem["action"])}{why}<code>{esc(rem["command"])}</code></div>'
            f'<div class="applied"><span class="pill {badge_cls}">{esc(label)}</span>'
            f'<span class="why2">{esc(explanation)}</span></div>'
        )
        st.markdown(_scard("Proposed remediation", fix), unsafe_allow_html=True)

        # eval checks
        eval_checks = json.loads(row["eval_checks_json"]) if row["eval_checks_json"] else None
        if eval_checks:
            chk_parts = []
            for c in eval_checks:
                mk = '<span class="mk ok">✓</span>' if c["passed"] else '<span class="mk no">✗</span>'
                crit = "critical" if c["critical"] else "info"
                chk_parts.append(
                    f'<div class="chk">{mk}<div><span class="cn">{esc(c["name"])}</span> '
                    f'<span class="crit">({crit})</span><br><span class="cd">{esc(c["detail"])}</span></div></div>'
                )
            st.markdown(_scard("Eval checks", "".join(chk_parts)), unsafe_allow_html=True)

    with right:
        # dependency chain
        if correlation and correlation.get("dependency_chain"):
            pills = '<span class="chain-arr">→</span>'.join(
                f'<span class="chain-pill">{esc(s)}</span>' for s in correlation["dependency_chain"]
            )
            st.markdown(_scard("Dependency chain", f"<div>{pills}</div>"), unsafe_allow_html=True)

        # ranked hypotheses
        if hypotheses:
            hyp_parts = []
            for i, h in enumerate(hypotheses):
                win = " win" if i == 0 else ""
                pct = f"{h['confidence'] * 100:.0f}"
                hyp_parts.append(
                    f'<div class="hyp{win}">'
                    f'<div class="hyp-top"><span class="n">{h["confidence"]:.2f}</span>'
                    f'<div class="hypbar-track"><div class="hypbar-fill" style="width:{pct}%"></div></div></div>'
                    f'<div class="hyp-txt"><span class="hyp-cat">{esc(h.get("category"))}</span> — {esc(h.get("cause"))}</div>'
                    f"</div>"
                )
            st.markdown(_scard("Ranked hypotheses", "".join(hyp_parts)), unsafe_allow_html=True)

        # evidence trail
        if evidence:
            st.markdown(_scard("Evidence gathered", _evidence_html(evidence)), unsafe_allow_html=True)

        # cost & tokens for this run
        fresh, cw, cr, out, hit = _token_stats(row)
        if hit is not None:
            tok = _meter_row("prompt cached", hit, f"{hit * 100:.0f}%") + (
                f'<div class="tok-line">fresh in {fresh:,} · cache write {cw:,} · '
                f"cache read {cr:,} · output {out:,}</div>"
            )
            if row["cost_usd"] is not None:
                tok += f'<div class="tok-line">total cost <b>${row["cost_usd"]:.4f}</b></div>'
            st.markdown(_scard("Cost & tokens", tok), unsafe_allow_html=True)

    # correlated timeline, full width, two-column flow
    if correlation and correlation.get("timeline"):
        entries = "".join(
            f'<div class="tl"><span class="when">{esc(ev.get("when"))}</span>'
            f'<span class="what">{esc(ev.get("what"))}</span></div>'
            for ev in correlation["timeline"]
        )
        st.markdown(_scard("Correlated timeline", f'<div class="tlgrid">{entries}</div>'), unsafe_allow_html=True)


# ---------------------------------------------------------------------------


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
            '<div class="scard">No incidents recorded yet — <code>investigate</code> or '
            "<code>eval</code> writes to this history.</div>",
            unsafe_allow_html=True,
        )
        return

    # stable chronological numbering: oldest = #1 (rows come newest-first)
    n = len(rows)
    numbers = {row["id"]: n - i for i, row in enumerate(rows)}

    if "view" not in st.session_state:
        st.session_state.view = "feed"
    if st.session_state.view == "detail" and st.session_state.get("selected_run_id") not in numbers:
        st.session_state.view = "feed"

    if st.session_state.view == "detail":
        selected = history_store.get_run(st.session_state.selected_run_id)
        if selected is not None:
            _render_detail(selected, numbers[selected["id"]])
            return
        st.session_state.view = "feed"

    # nav between the two top-level views (detail has its own back button)
    view = st.session_state.view
    nav_feed, nav_analytics, _spacer = st.columns([1.1, 1.1, 8], gap="small")
    with nav_feed:
        if st.button("Incidents", type="primary" if view == "feed" else "secondary", use_container_width=True):
            st.session_state.view = "feed"
            st.rerun()
    with nav_analytics:
        if st.button("Analytics", type="primary" if view == "analytics" else "secondary", use_container_width=True):
            st.session_state.view = "analytics"
            st.rerun()

    if view == "analytics":
        _render_analytics(rows, numbers)
    else:
        _render_feed(rows, numbers)


if __name__ == "__main__":
    main()
