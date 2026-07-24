# Phase 8: Streamlit dashboard — build plan

**Status: design chosen, not yet implemented.** This doc is written so a fresh session
(no prior conversation context) can pick this up and build it directly. Read this file
top to bottom before writing any code.

## What this is

A local Streamlit web UI on top of the run-history persistence layer built in Phase 7
(`src/sre_agent/history_store.py`). It answers "what has the agent actually done" —
every `investigate`/`eval` run, its evidence trail, its RCA, its cost, whether it was
approved/resolved — browsable visually instead of only via the `sre-agent history` CLI
command.

Explicitly **not** adopting an off-the-shelf agent-observability tool (Langfuse, Arize
Phoenix, LangSmith) for this — they show raw LLM spans, not domain concepts like
"incident" / "approved" / "resolved". This is a small hand-built page reading our own
SQLite file instead. See the "Keeping it honest and keeping it cheap" / cost sections of
the main README for the project's overall narrative if useful context.

## Design chosen: Direction B, "Clarity"

Three visual directions were mocked up and shown to the user as a single HTML artifact
(three toggleable style directions over the same real data — Ops Terminal / Clarity /
Console Grid). **The user picked B, "Clarity."** The live mockup (if still available) was
published at `https://claude.ai/code/artifact/7f15ed9f-e2be-4e2d-907c-8a382d5b2377` — try
fetching it for a visual reference, but don't depend on it still being reachable; the
verbatim CSS/markup below is the authoritative spec either way.

### Design tokens

**Color** (named hex, light-mode; this direction was mocked up light-only — no dark
variant was designed, decide with the user whether to add one or leave it committed to
light):
- `--paper: #f4f6f4` — page background (cool off-white, NOT cream — deliberately steered
  away from the generic warm-cream+terracotta AI-design cliché)
- `--ink: #1b2420` — primary text
- `--card: #ffffff` — card/panel background
- `--border: #dfe6e1`
- `--muted: #6b7772` — secondary text
- `--ember: #c1622b` — primary accent (copper/rust — alert/attention without being
  literally red; used for links, active/selected states, hypothesis-confidence bars)
- `--moss: #3f6e5c` — secondary accent (calm/resolved tone; used for cost figures,
  dependency-chain pills, the remediation box)
- Semantic (separate from the accent, per dashboard design convention):
  `--good: #2f9e63` / `--good-soft: #e1f2e8` (pass/healthy),
  `--warn: #c98a2e` / `--warn-soft: #f6ebd7` (proposed/pending/attention)

**Type:** system sans throughout (`-apple-system, BlinkMacSystemFont, "Segoe UI",
system-ui, sans-serif`) — no monospace except `font-variant-numeric: tabular-nums` on
numeric columns (cost, confidence). KPI values are bold/large (1.5rem, weight 700,
tight tracking); section labels are small-caps-style (0.68rem, uppercase, letter-spacing
0.05em, `--muted` color, weight 700).

**Layout:** a KPI row (4 stat cards in a grid) at the top, then a two-column
master-detail layout below it — `grid-template-columns: 1.3fr 1fr` — a scrollable list of
run cards on the left, a **sticky** detail panel on the right for whichever run is
selected. Cards and panel are white on the paper background, subtle shadow
(`box-shadow: 0 1px 2px rgba(20,30,25,0.04)`), border radius ~10px, not the exaggerated
`rounded-lg`-everywhere look.

### Reference markup (from the mockup, adapt directly)

KPI row:
```html
<div class="kpirow">
  <div class="kpi"><div class="label">Runs</div><div class="value">4</div></div>
  <div class="kpi"><div class="label">Avg confidence</div><div class="value">0.81</div></div>
  <div class="kpi"><div class="label">Avg cost</div><div class="value accent">$0.250</div></div>
  <div class="kpi"><div class="label">Eval pass rate</div><div class="value">3 / 3</div></div>
</div>
```

Run list row (one per run, `.selected` on the active one):
```html
<div class="row selected">
  <div><div class="who">cascade</div><div class="sub">boutique · redis-cart scaled to 0 · dependency</div></div>
  <div style="text-align:right"><span class="pill good">pass · 0.85</span><div class="cost">$0.1586</div></div>
</div>
```
Use `.pill.warn` (not `.good`) for anything not a passing eval / applied-and-verified
execute run (i.e. `propose`-mode runs, rejected/blocked executes).

Detail panel (right column, sticky):
```html
<div class="panel">
  <h3>cascade</h3>
  <div class="sub">boutique · 2026-07-24 · 62.8s · $0.1586</div>
  <div class="section-label">Dependency chain</div>
  <span class="chain-pill">frontend</span>→<span class="chain-pill">cartservice</span>→<span class="chain-pill">redis-cart</span>
  <div class="section-label">Hypotheses</div>
  <div class="hyprow"><span class="n">0.85</span><div class="hypbar-track"><div class="hypbar-fill" style="width:85%"></div></div></div>
  <div class="section-label">Proposed remediation</div>
  <div class="fixbox">Scale redis-cart back up to restore the cart backend.<code>kubectl -n boutique scale deployment redis-cart --replicas=1</code></div>
</div>
```

Full CSS block (copy verbatim, then adapt selectors for whatever wrapper Streamlit ends
up rendering into — see "Streamlit-specific constraints" below):
```css
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
```

## Data layer already built (Phase 7 — do not re-derive, just use it)

`src/sre_agent/history_store.py` already exists and is the source of truth:
- `list_runs(limit=20) -> list[sqlite3.Row]` — most recent first
- `get_run(run_id) -> sqlite3.Row | None` — exact id or unique prefix
- Both connect to the SQLite file at `Settings.history_db_path` (default
  `data/history.db`, git-ignored, resolved relative to CWD — run Streamlit from the repo
  root same as the CLI)
- Row columns: `id, started_at, duration_s, namespace, workload, alert, mode,
  incident_name, category, confidence_score, root_cause, cost_usd, input_tokens,
  cache_write_tokens, cache_read_tokens, output_tokens, approval_status, resolved,
  evidence_json, correlation_json, hypotheses_json, report_json, eval_checks_json`
- `evidence_json` → list of `ToolRecord` dumps (`tool`, `input`, `ok`, `summary`)
- `correlation_json` → `Correlation` dump (`timeline: [{when, what}]`,
  `dependency_chain: [str]`, `what_changed`, `summary`) — **may be null**
- `hypotheses_json` → list of `Hypothesis` dumps (`cause`, `category`, `confidence`,
  `supporting`, `against`) — ranked, highest confidence first
- `report_json` → full `RCAReport` dump (`summary`, `root_cause`, `category`,
  `confidence` (band: high/medium/low), `confidence_score`, `evidence: [str]`,
  `alternatives: [str]`, `impact`, `remediation: {action, command, rationale,
  reversible}`)
- `eval_checks_json` → list of `Check` dumps (`name`, `passed`, `critical`, `detail`) —
  **only present for `mode="eval"` runs**, null otherwise

`sre-agent history` / `sre-agent history <id>` in `cli.py` already do exactly this kind
of read + render (as a rich-console table/panels instead of HTML) — read that code
(`history` command near the end of `src/sre_agent/cli.py`) as a second reference for
"what fields to show and how to compute derived values" (e.g. the `_status_label()`
helper's logic for mapping `mode`/`approval_status`/`resolved` to a status string is
exactly what the dashboard's status pill needs to replicate).

**As of the last session, `data/history.db` has 4 real rows in it** (from live
`investigate`/`eval` runs) — enough to build and verify against without needing to run
the (paid) agent again during dashboard development. Don't invent/mock data; read the
real file.

## Streamlit implementation plan

1. **New file:** `src/sre_agent/dashboard.py`. Entry point:
   `streamlit run src/sre_agent/dashboard.py` from the repo root (same CWD convention as
   the CLI, so `history_store`'s relative `data/history.db` path resolves correctly).
   Add a Makefile target (`make dashboard`) alongside the existing ones
   (`install`/`test`/`doctor`/etc.) once it works.
2. **New dependency:** add `streamlit` to `pyproject.toml` (a new optional extra, e.g.
   `dashboard`, so it doesn't bloat the core install — mirror how `dev` extras are
   already split out).
3. **Styling approach — inject the CSS above via
   `st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)`** once at the top of
   the script, then render the KPI row, run list, and detail panel as raw HTML blocks via
   further `st.markdown(..., unsafe_allow_html=True)` calls using the reference markup
   above — NOT Streamlit's native `st.metric`/`st.dataframe`, which can't hit this level
   of visual control.
4. **Open implementation decision — row selection is not free with raw HTML.** A
   `<div class="row">` rendered via `st.markdown` has no way to signal a click back to
   Python. Two real options, pick one when you get here:
   - **(a) Real `st.button` per row**, styled via CSS to look like/overlay the card (or
     just placed unobtrusively, e.g. a small "view →" button at the row's right edge).
     Simplest to get working correctly; slightly less visually seamless.
   - **(b) `st.radio` with `label_visibility="collapsed"` and heavy CSS overrides** to
     reskin the radio options as the card list (a known Streamlit trick for exactly this
     master-detail pattern). More seamless if done well; more CSS fighting Streamlit's
     own classes (which change between Streamlit versions — check the installed
     version's actual DOM before writing these selectors).
   Start with (a) to get a correct, testable page fast; upgrade to (b) only if the extra
   polish is worth the fragility once (a) works.
5. **Order of work:**
   - Get `list_runs()` rendering as the KPI row + a plain list (even ugly) first —
     confirms the data path end to end.
   - Layer in the exact Clarity CSS/markup for the KPI row and list.
   - Add selection (option (a) above) + the detail panel, reading `get_run(selected_id)`.
   - Only then revisit polish (hover states, option (b) if wanted, empty-state message
     when `data/history.db` has zero rows — reuse the CLI's exact copy: "No runs recorded
     yet — `investigate` or `eval` writes to this history.").
6. **Verify against real data**, not synthetic — run it against the existing
   `data/history.db` (4 rows) before considering it done. Don't spend a paid live
   `investigate`/`eval` run just to test the dashboard; the existing rows are sufficient.

## Not decided yet — ask the user when it comes up

- Dark mode for this page was never designed (the Clarity mockup is light-only). Decide
  whether to leave it committed to light (a legitimate choice per the project's own
  design conventions) or design a dark variant, rather than guessing.
- Whether the dashboard also needs a "current cluster health" panel (this was
  brainstormed as a "what more could it show" idea in the same session that produced this
  plan, but was never committed to scope — confirm before adding it).
- Whether to add the aggregate/trend view (cost-over-time, category distribution) in
  this first pass, or ship the list+detail view alone first and treat trends as a
  follow-up. The plan above deliberately scopes to list+detail only for a first working
  version.
