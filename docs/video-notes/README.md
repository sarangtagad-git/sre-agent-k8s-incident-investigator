# Video notes

Working material for the "learn in public" video series, kept separate from
[`docs/architecture/`](../architecture/) (published, README-referenced diagrams in an
older/simpler style) since this folder is informal and video-specific.

## Episode 1 — "I gave an AI agent kubectl access. Here's how I made it safe."

- **`episode1-script.md`** — full shot list + ~9 min script (capture checklist, staged
  rejection demo, YouTube/LinkedIn/X cuts, title/thumbnail options).
- **`demo_gate_rejections.py`** — the on-camera `validate_remediation()` rejection demo
  (`kubectl delete pod ...` and a `kube-system`-targeted scale, both bounced by the gate).
  Run from WSL: `.venv/bin/python demo_gate_rejections.py` (project venv is WSL-based).
- **`agent-chain-diagram.html`** / **`agent-chain-diagram.png`** — the full "chain of
  calls" diagram (incident → CLI command → 6-stage LangGraph pipeline, with `gather`'s
  ReAct loop expanded → proposal output → 4-step human-gated remediation with reject
  off-ramps → `verify_recovery()` → saved to history/dashboard). HTML is self-contained,
  theme-aware (light/dark) source; PNG (2600×4200) is ready to drop straight into a video
  editor as a full-frame graphic or B-roll. Regenerate the PNG after editing the HTML with:

  ```bash
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" --headless=new \
    --disable-gpu --no-sandbox --force-device-scale-factor=2 \
    --screenshot="docs/video-notes/agent-chain-diagram.png" \
    --window-size=1300,2100 "docs/video-notes/agent-chain-diagram.html"
  ```

  Content is grounded in [`src/sre_agent/agent/graph.py`](../../src/sre_agent/agent/graph.py)
  (node order, gather's ReAct loop) and [`src/sre_agent/remediation.py`](../../src/sre_agent/remediation.py)
  (the allowlist/dry-run/approval/verify chain) — re-check both if the pipeline shape
  changes before reusing this diagram in a new video.

## Episode 2 — "How do you know your AI agent is actually right?"

- **`episode2-script.md`** — full shot list + ~8.5 min script covering the eval harness:
  the `Incident`/`score()` mechanics, the record/replay cost trick, and the real
  `cpu_throttled` bug the harness caught (a fluent, confident, wrong diagnosis that
  passed on keyword matching alone). Includes real captured terminal output from
  `sre-agent eval --replay` (both single-incident and full-suite) so on-screen text
  matches an actual run, not a mockup.

## Episode 1 thumbnails

- **`thumbnails/`** — 3 thumbnail options, 1280×720, plus their editable HTML/SVG
  sources: `ep1-thumbnail-rejected` (the "REJECTED" gate-rejection stamp — strongest
  pure hook), `ep1-thumbnail-4-layers` (the lock chain from LLM to cluster — best if
  the thumbnail should also explain the video's structure), `ep1-thumbnail-approved`
  ("AI Proposed. I Approved." — most personal framing). Re-render a `.html` after
  editing with the same headless-Edge command as the chain diagram above, swapping
  `--window-size=1300,2100` for `--window-size=1280,720` and dropping
  `--force-device-scale-factor` (thumbnails need exact 1280×720, not 2x).

## Cross-episode reference

- **`demo-commands.txt`** — copy-paste-ready stage / investigate / revert commands for
  all 10 incidents in one plain-text file (the investigate command uses `--execute
  --verbose` so it streams the full propose → approve flow live). Notes which incidents'
  proposed fixes pass vs. get rejected by the gate, and flags `node_down`'s larger blast
  radius.
- **`incident_taxonomy.xlsx`** — two sheets covering all 10 taxonomy incidents: "Recorded
  Incidents" (category, root cause, proposed fix, confidence, whether `validate_remediation`
  accepts the proposed fix, cost/duration) and "How to Create the Issue" (exact stage +
  revert `kubectl` commands per incident, from [`evals.py`](../../src/sre_agent/evals.py)).
  Useful for picking which incidents to demo in any future episode, not just Episode 1.
