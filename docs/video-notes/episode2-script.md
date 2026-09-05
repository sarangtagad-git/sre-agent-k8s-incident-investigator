# Episode 2 — "How do you know your AI agent is actually right?"

Target: YouTube 8–9 min core cut, then trimmed for LinkedIn (60–90s) and X/Twitter (thread + clip).
Grounded in the actual code and real captured terminal output — nothing below is invented.

---

## 0. Before you hit record — capture list

1. **Terminal**: `sre-agent eval --incident cpu_throttled --replay` — real output captured below. This is the centerpiece demo: free, no API call, no cluster mutation, and it shows a check *failing* while the incident still passes overall — the best teaching moment in the whole episode.
2. **Terminal**: `sre-agent eval --replay` (no `--incident`, runs all 10) — the full scorecard, captured below. Shows 9 PASS + 1 honest FAIL (`node_down`, not yet recorded), non-zero exit code.
3. **Editor**: [src/sre_agent/evals.py](../../src/sre_agent/evals.py) — the `Incident` dataclass (lines 19–32) and the `cpu_throttled` entry specifically (lines ~178–206), including its inline comments explaining the *live-verified* bug the `must_include` check exists to catch.
4. **Editor**: `score()` function (lines 49–94) — the 4 checks, critical vs informational.
5. **Editor**: [src/sre_agent/eval_recording.py](../../src/sre_agent/eval_recording.py) — `save_recording`/`load_recording`, and the docstring explaining why recordings are committed to the repo, not gitignored.
6. **Editor**: [docs/incident-taxonomy-plan.md](../incident-taxonomy-plan.md) — the 9-step plan itself (pay once, reuse forever) makes a good on-screen "here's the plan I followed" beat.

---

## 1. Script (YouTube cut, ~8.5 min)

**[0:00–0:20] Cold open — hook**
Voiceover over the `cpu_throttled --replay` output, specifically the moment the `remediation_gate_valid ✗` line appears:
> "Every 'I built an AI agent' video shows you the agent looking right. This is the part nobody shows: the test suite that would catch it when it's wrong — and one specific time it actually did."

**[0:20–1:00] The premise**
> "An LLM's output is fluent by default. 'Fluent' and 'correct' are different things, and the gap between them is invisible unless you go looking for it. So before I trusted any diagnosis this agent produced, I built something closer to a regression test suite than a demo: 10 real incidents, each with a known right answer, that I can re-run any time I change the prompts, the tools, or the model."

**[1:00–2:30] What an Incident actually is**
Show clip #3 — the `Incident` dataclass and one real entry.
> "Each incident is 4 things: the exact `kubectl` commands that break the cluster, the exact commands that fix it back, what I tell the agent — just an alert string, no hints — and the ground truth its report has to match. Here's `cpu_throttled`: stage sets checkoutservice's CPU limit to 5 millicores, revert undoes the rollout, and the ground truth says the category has to be 'saturation' or 'config', and the root-cause text has to mention checkoutservice by name."

**[2:30–4:00] How scoring actually works**
Show clip #4 — `score()`.
> "Every run gets scored on 4 checks. Two are critical — category has to match, and the root-cause text has to contain the right keywords — and if either critical check fails, the whole incident fails, full stop. The other two are informational: does the proposed fix actually clear the remediation gate from episode one, and does the confidence score clear a floor. Those don't fail the incident, they just tell me something worth knowing."
>
> [Live demo, clip #1] "Watch this. This is a real replay of a real recorded run." *(run `sre-agent eval --incident cpu_throttled --replay`, let it print)* "Category: correct. Root cause: correct, it names checkoutservice specifically. But look — `remediation_gate_valid` failed. The fix it proposed, `kubectl set resources`, isn't on the allowlist from episode one. The diagnosis is right and the proposed command still wouldn't run without me deciding to extend the gate. That's not a bug in the eval — that's the eval telling me something true and specific about where the system's edges are."

**[4:00–5:15] The record/replay trick — why this doesn't cost anything to iterate on**
> "A live eval run costs about 15 cents and takes a couple of minutes, because it stages a real fault, calls the real API, and reverts. That's fine to pay once. It's not fine to pay every time I tweak a keyword check. So `--record` saves the full result to a JSON fixture, committed to the repo — and `--replay` re-scores that saved result for free, unlimited times, no API key needed, no cluster touched. Every check you saw me tune this session happened against a replay, not a live call."

**[5:15–7:00] The bug it actually caught**
> "Here's the moment this stopped being theater. `cpu_throttled`'s root-cause check originally just looked for words like 'cpu' and 'throttled' anywhere in the report. Live-verified, the agent produced a fluent, confident, completely wrong diagnosis — it blamed emailservice's missing memory limit for checkout latency. And it still passed, because 'limit' and 'latency' both appear in the text, just attached to the wrong service."
>
> [Show clip #3's inline comment] "So the fix wasn't a prompt tweak — it was making the check itself harder to fool: require the actual affected service's name to appear, not just plausible-sounding words. And separately, the root *cause* of the wrong diagnosis was that no tool exposed container resource limits yet — so I built one. Now it correctly finds the real 5-millicore limit and cites actual Prometheus throttling data."

**[7:00–8:00] The full scorecard**
Show clip #2.
> "Ten incidents across seven root-cause categories: image pulls, crash loops, cascading dependency failures, OOM kills, CPU throttling, unschedulable pods, bad config, blocked network policies, wrong service selectors, and a whole node going down. Nine pass. The tenth — node going down — fails on purpose right now, because I haven't recorded it yet; it drains a real node, so I run that one deliberately, alone, not lumped into a casual batch. And notice: it fails *loud*. Non-zero exit code, clear reason printed. That's the same philosophy as the approval gate from episode one — nothing here is allowed to fail silently."

**[8:00–8:30] Close / CTA**
> "This is the difference between an agent that looks smart in a demo and one you'd actually trust to page you at 3 AM: not vibes, a scorecard. Next episode: what happens when I swap the reasoning model entirely and see which of these ten incidents still hold up."

---

## 2. Real captured terminal output (for on-screen text / verifying against your own take)

### `sre-agent eval --incident cpu_throttled --replay`

```
─ cpu_throttled — CPU limit set far too low → checkoutservice works but is th… ─
  (replayed from saved recording — no cluster changes, no API call)
  RCA: config · 0.92 — CPU resource limit of 5m (5 millicores) in the
checkoutservice pod specification is too lo…
    ✓ category (critical)  got 'config', expected one of ['config',
'saturation']
    ✓ root_cause_match (critical)  must_include=['checkoutservice']
any-of=['cpu', 'throttl', 'limit', 'latency']
    ✗ remediation_gate_valid (info)  verb not on the allowlist: 'set'
    ✓ confidence (info)  score=0.92 (floor 0.6)
────────────────────────────────── Scorecard ───────────────────────────────────
┏━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━┓
┃ incident      ┃ result ┃ latency ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━┩
│ cpu_throttled │ PASS   │ 121.1s  │
└───────────────┴────────┴─────────┘
All incidents passed.
```

### `sre-agent eval --replay` (full suite)

```
┏━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━┓
┃ incident               ┃ result ┃ latency ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━┩
│ image_pull             │ PASS   │ 70.9s   │
│ crash_loop             │ PASS   │ 134.5s  │
│ cascade                │ PASS   │ 105.9s  │
│ oom_killed             │ PASS   │ 139.1s  │
│ cpu_throttled          │ PASS   │ 121.1s  │
│ unschedulable          │ PASS   │ 101.8s  │
│ bad_config             │ PASS   │ 78.2s   │
│ network_blocked        │ PASS   │ 92.4s   │
│ wrong_service_selector │ PASS   │ 83.6s   │
│ node_down              │ FAIL   │ 0.0s    │
└────────────────────────┴────────┴─────────┘
FAILED: node_down
```
(`node_down` fails because it has no recording yet — `No recording for 'node_down' ... Run
sre-agent eval -i node_down --record first.` — not a crash, an honest, expected failure.)

---

## 3. LinkedIn cut (60–90s + text post)

**Clip**: the `cpu_throttled --replay` run, focused on the moment `remediation_gate_valid ✗` prints while the incident still shows `PASS` — ~15s, plus a 10s cut to the full 9/10 scorecard.

**Text post**:

> Everyone shows their AI agent looking right. Here's the part I don't see people show: the test suite that catches it when it's wrong.
>
> 10 real Kubernetes incidents, each with a known root cause. Every run gets scored on 2 critical checks (category, root-cause keywords) and 2 informational ones (does the proposed fix actually clear my safety gate, is the confidence score reasonable).
>
> One of those incidents caught a real bug: the agent once produced a fluent, confident, completely wrong diagnosis — blamed the wrong service for a latency spike — and it still passed on keyword matching alone. Tightening the check (require the actual affected service's name, not just plausible words) is what caught it. The real root cause was a missing tool; building it fixed the diagnosis for good.
>
> A live eval run costs ~$0.15 and mutates a real cluster. So I built record/replay: pay once per incident, then tune the checks against a saved recording for free, forever.
>
> The full breakdown (with real terminal output) in the video 👇

## 4. X/Twitter cut (clip + thread)

**Clip**: same 15s `remediation_gate_valid ✗` moment.

**Thread**:
1. Everyone shows their AI agent looking right. Here's the test suite that catches it when it's wrong. 🧵
2. 10 real K8s incidents, each with a known ground-truth root cause. Every run gets scored: 2 critical checks (category + root-cause keywords), 2 informational (does the fix clear my safety gate, is confidence reasonable). [screenshot: cpu_throttled replay]
3. A live eval run costs ~$0.15 and touches a real cluster. So: `--record` saves one real run as a fixture, `--replay` re-scores it for free, forever. Every check I've tuned happened against a replay, $0 spent.
4. This actually caught a bug. The agent once produced a fluent, wrong diagnosis — blamed the wrong service for a latency spike — and passed on keyword matching alone. [screenshot: the inline comment in evals.py explaining it]
5. Fix: tighten the check to require the *actual* affected service's name, not just plausible words. Real root cause of the wrong diagnosis: a missing tool. Built it. Now it's correct.
6. 9/10 incidents pass today. The 10th fails on purpose — it's not recorded yet (drains a real node, so I run it deliberately, alone). It fails loud: non-zero exit, clear reason. [screenshot: full scorecard]
7. Full video + code walkthrough: [link]

---

## 5. Title / thumbnail options

- "How do you know your AI agent is actually right?"
- "My AI agent gave a confident, wrong answer. Here's how I caught it."
- "I built a test suite for my AI agent's judgment"

Thumbnail: the scorecard table, cropped tight on the `remediation_gate_valid ✗` / `PASS` contradiction — the visual hook is "how can this fail and still pass?"

## 6. Notes for next episode

Closes by teasing Episode 5 (`gather-model-swap` — does a cheaper model still pass these same 10 incidents). That episode's entire premise depends on this one existing first, so the sequencing matters: this has to land before the model-swap episode for the stakes to make sense.
