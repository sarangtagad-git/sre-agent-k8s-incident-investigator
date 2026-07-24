"""Phase 8: Streamlit dashboard over the run-history store (Phase 7).

Run with `streamlit run src/sre_agent/dashboard.py` from the repo root (same CWD
convention as the CLI, so history_store's relative data/history.db path resolves).

Design: Direction B, "Clarity" (see docs/dashboard-plan.md for the full spec this
CSS/markup is copied from). Rendered as raw HTML via st.markdown(unsafe_allow_html=True)
since native st.metric/st.dataframe can't hit this level of visual control. Row
selection uses plain st.button per row (option (a) in the plan) — simplest thing that
works; the CSS-reskinned-radio approach (option (b)) is deferred as a polish pass.
"""

from __future__ import annotations

import json
import sqlite3

import streamlit as st

from sre_agent import history_store

CSS = """
.clarity {
  --ink: #1b2420; --paper: #f4f6f4; --card: #ffffff; --border: #dfe6e1; --muted: #6b7772;
  --ember: #c1622b; --ember-soft: #f6e2d3; --moss: #3f6e5c; --moss-soft: #e1ece7;
  --good: #2f9e63; --good-soft: #e1f2e8; --warn: #c98a2e; --warn-soft: #f6ebd7;
  background: var(--paper); color: var(--ink); padding: 1.8rem;
}
.clarity * { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
.clarity .kpirow { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.9rem; margin-bottom: 1.4rem; }
.clarity .kpi { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 0.9rem 1rem; box-shadow: 0 1px 2px rgba(20,30,25,0.04); }
.clarity .kpi .label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 600; }
.clarity .kpi .value { font-size: 1.5rem; font-weight: 700; margin-top: 0.2rem; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.clarity .kpi .value.accent { color: var(--ember); }
.clarity .grid { display: grid; grid-template-columns: 1.3fr 1fr; gap: 1.1rem; align-items: start; }
.clarity .list { display: flex; flex-direction: column; gap: 0.6rem; }
.clarity .row { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 0.85rem 1rem; display: flex; justify-content: space-between; align-items: center; gap: 0.8rem; cursor: pointer; box-shadow: 0 1px 2px rgba(20,30,25,0.03); }
.clarity .row:hover { border-color: var(--ember); }
.clarity .row.selected { border-color: var(--ember); box-shadow: 0 0 0 2px var(--ember-soft); }
.clarity .row .who { font-weight: 600; font-size: 0.9rem; }
.clarity .row .sub { font-size: 0.76rem; color: var(--muted); margin-top: 0.1rem; }
.clarity .pill { font-size: 0.7rem; font-weight: 700; padding: 0.2rem 0.6rem; border-radius: 999px; white-space: nowrap; }
.clarity .pill.good { color: var(--good); background: var(--good-soft); }
.clarity .pill.warn { color: var(--warn); background: var(--warn-soft); }
.clarity .cost { font-variant-numeric: tabular-nums; font-weight: 600; font-size: 0.85rem; color: var(--moss); }
.clarity .panel { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 1.2rem 1.3rem; box-shadow: 0 2px 8px rgba(20,30,25,0.05); position: sticky; top: 1rem; }
.clarity .panel h3 { margin: 0 0 0.15rem; font-size: 1.05rem; }
.clarity .panel .sub { font-size: 0.78rem; color: var(--muted); margin-bottom: 0.9rem; }
.clarity .section-label { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 700; margin: 0.8rem 0 0.35rem; }
.clarity .chain-pill { display: inline-block; background: var(--moss-soft); color: var(--moss); font-size: 0.75rem; font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 6px; margin-right: 0.3rem; }
.clarity .hypbar-track { background: var(--border); border-radius: 4px; height: 6px; overflow: hidden; flex: 1; }
.clarity .hypbar-fill { background: var(--ember); height: 100%; }
.clarity .hyprow { display: flex; align-items: center; gap: 0.6rem; font-size: 0.78rem; margin: 0.35rem 0; }
.clarity .hyprow .n { width: 2.4rem; color: var(--muted); font-variant-numeric: tabular-nums; }
.clarity .fixbox { margin-top: 0.8rem; background: var(--moss-soft); border-radius: 8px; padding: 0.7rem 0.85rem; font-size: 0.8rem; }
.clarity .fixbox code { display: block; margin-top: 0.4rem; color: var(--moss); font-size: 0.78rem; }

/* Streamlit-specific: the per-row "view" button lives in its own Streamlit-managed
   container (not a DOM sibling of the .clarity markup), so it can't be reskinned into
   an invisible overlay via a CSS sibling selector — Streamlit wraps every st.markdown/
   st.button call in its own element-container div. Style it as a small plain ember
   outline button instead (option (a) in docs/dashboard-plan.md: correct first, seamless
   later). */
div[data-testid="stButton"] button {
  border: 1px solid #c1622b; color: #c1622b; background: transparent;
  border-radius: 6px; font-size: 0.75rem; padding: 0.15rem 0.6rem; min-height: 0;
}
div[data-testid="stButton"] button:hover { background: #f6e2d3; color: #c1622b; border-color: #c1622b; }
"""


def _status_pill(row: sqlite3.Row) -> tuple[str, str]:
    """Mirror cli.py's _status_label(): (pill class, pill text)."""
    mode = row["mode"]
    if mode == "eval":
        return ("good", "pass") if row["resolved"] else ("warn", "fail")
    if mode == "execute":
        label = {
            "approved_applied": ("good", "applied"),
            "rejected": ("warn", "rejected"),
            "blocked": ("warn", "blocked"),
            "dry_run_failed": ("warn", "dry-run failed"),
            "apply_failed": ("warn", "apply failed"),
        }.get(row["approval_status"], ("warn", row["approval_status"]))
        return label
    return ("warn", "proposed")


def _kpi_row(rows: list) -> str:
    n = len(rows)
    confidences = [r["confidence_score"] for r in rows if r["confidence_score"] is not None]
    avg_conf = sum(confidences) / len(confidences) if confidences else None
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    avg_cost = sum(costs) / len(costs) if costs else None
    eval_rows = [r for r in rows if r["mode"] == "eval"]
    eval_pass = sum(1 for r in eval_rows if r["resolved"])

    return f"""
    <div class="clarity">
    <div class="kpirow">
      <div class="kpi"><div class="label">Runs</div><div class="value">{n}</div></div>
      <div class="kpi"><div class="label">Avg confidence</div><div class="value">{f"{avg_conf:.2f}" if avg_conf is not None else "–"}</div></div>
      <div class="kpi"><div class="label">Avg cost</div><div class="value accent">{f"${avg_cost:.4f}" if avg_cost is not None else "–"}</div></div>
      <div class="kpi"><div class="label">Eval pass rate</div><div class="value">{f"{eval_pass} / {len(eval_rows)}" if eval_rows else "–"}</div></div>
    </div>
    </div>
    """


def _row_html(row, selected: bool) -> str:
    who = row["incident_name"] or row["workload"] or row["namespace"]
    target = row["workload"] or row["namespace"]
    sub_bits = [target]
    if row["root_cause"]:
        sub_bits.append(row["root_cause"])
    if row["category"]:
        sub_bits.append(row["category"])
    sub = " · ".join(sub_bits)

    pill_class, pill_text = _status_pill(row)
    conf = f"{pill_text} · {row['confidence_score']:.2f}" if row["confidence_score"] is not None else pill_text
    cost = f"${row['cost_usd']:.4f}" if row["cost_usd"] is not None else "–"
    selected_class = " selected" if selected else ""

    return f"""
    <div class="clarity">
    <div class="row{selected_class}">
      <div><div class="who">{who}</div><div class="sub">{sub}</div></div>
      <div style="text-align:right"><span class="pill {pill_class}">{conf}</span><div class="cost">{cost}</div></div>
    </div>
    </div>
    """


def _detail_panel(row) -> str:
    who = row["incident_name"] or row["workload"] or row["namespace"]
    target = row["workload"] or row["namespace"]
    sub = f"{target} · {row['started_at'].replace('T', ' ')} · {row['duration_s']:.1f}s"
    if row["cost_usd"] is not None:
        sub += f" · ${row['cost_usd']:.4f}"

    sections = []

    correlation = json.loads(row["correlation_json"]) if row["correlation_json"] else None
    if correlation and correlation.get("dependency_chain"):
        chain = "→".join(f'<span class="chain-pill">{s}</span>' for s in correlation["dependency_chain"])
        sections.append(f'<div class="section-label">Dependency chain</div>{chain}')
    if correlation and correlation.get("timeline"):
        timeline_html = "".join(
            f'<div class="hyprow"><span class="n">{e["when"]}</span><div>{e["what"]}</div></div>'
            for e in correlation["timeline"]
        )
        sections.append(f'<div class="section-label">Timeline</div>{timeline_html}')

    hypotheses = json.loads(row["hypotheses_json"] or "[]")
    if hypotheses:
        hyp_html = "".join(
            f'<div class="hyprow"><span class="n">{h["confidence"]:.2f}</span>'
            f'<div class="hypbar-track"><div class="hypbar-fill" style="width:{h["confidence"] * 100:.0f}%"></div></div></div>'
            f'<div class="sub" style="margin:-0.2rem 0 0.3rem 3rem">{h["category"]}: {h["cause"]}</div>'
            for h in hypotheses
        )
        sections.append(f'<div class="section-label">Hypotheses (ranked)</div>{hyp_html}')

    report = json.loads(row["report_json"])
    rem = report["remediation"]
    sections.append(
        f'<div class="section-label">Proposed remediation</div>'
        f'<div class="fixbox">{rem["action"]}<code>{rem["command"]}</code></div>'
    )

    eval_checks = json.loads(row["eval_checks_json"]) if row["eval_checks_json"] else None
    if eval_checks:
        checks_html = "".join(
            f'<span class="pill {"good" if c["passed"] else "warn"}" style="margin-right:0.3rem">{c["name"]}</span>'
            for c in eval_checks
        )
        sections.append(f'<div class="section-label">Eval checks</div>{checks_html}')

    body = "\n".join(sections)
    return f"""
    <div class="clarity">
    <div class="panel">
      <h3>{who}</h3>
      <div class="sub">{sub}</div>
      {body}
    </div>
    </div>
    """


def main() -> None:
    st.set_page_config(page_title="SRE Agent — Run History", layout="wide")
    st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)

    rows = history_store.list_runs(limit=50)

    if not rows:
        st.markdown(
            '<div class="clarity"><div class="panel">'
            "No runs recorded yet — <code>investigate</code> or <code>eval</code> writes to this history."
            "</div></div>",
            unsafe_allow_html=True,
        )
        return

    st.markdown(_kpi_row(rows), unsafe_allow_html=True)

    if "selected_run_id" not in st.session_state:
        st.session_state.selected_run_id = rows[0]["id"]

    list_col, detail_col = st.columns([1.3, 1], gap="medium")

    with list_col:
        for row in rows:
            is_selected = row["id"] == st.session_state.selected_run_id
            card_col, button_col = st.columns([9, 1], gap="small", vertical_alignment="center")
            with card_col:
                st.markdown(_row_html(row, is_selected), unsafe_allow_html=True)
            with button_col:
                if st.button("view →", key=f"select_{row['id']}"):
                    st.session_state.selected_run_id = row["id"]
                    st.rerun()

    with detail_col:
        selected = history_store.get_run(st.session_state.selected_run_id)
        if selected is not None:
            st.markdown(_detail_panel(selected), unsafe_allow_html=True)


if __name__ == "__main__":
    main()
