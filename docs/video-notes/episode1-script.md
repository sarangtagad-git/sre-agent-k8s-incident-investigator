# Episode 1 — "I gave an AI agent kubectl access. Here's how I made it safe."

Target: YouTube 8–10 min core cut, then trimmed for LinkedIn (60–90s) and X/Twitter (thread + clip).
Grounded in the actual code — every claim below points at a real file/line so you're never overstating what it does.

---

## 0. Before you hit record — capture list

Record these clips first (silently, no narration needed yet), then voice over in editing. Screen at 1440p+, terminal font bumped to ~18pt so text reads on mobile.

1. **Terminal**: `kubectl auth can-i --list --as=system:serviceaccount:sre-agent:sre-agent` — full output, showing get/list/watch only, no create/delete/patch.
2. **Editor**: [infra/rbac/sre-agent-rbac.yaml](infra/rbac/sre-agent-rbac.yaml) — scroll slowly through the ClusterRole `rules:` block. Pause on the comment block at the top (lines 1–8) and on the `secrets` omission note (line 33).
3. **Editor**: [src/sre_agent/remediation.py](src/sre_agent/remediation.py) — the module docstring (lines 1–19) as a single readable slide, then `_ALLOWED_MUTATING` / `_ALLOWED_READONLY` (lines 32–43), then `_FORBIDDEN_FLAGS` and `_PROTECTED_NAMESPACES` (lines 52–54).
4. **Terminal**: a live incident run — `sre-agent investigate <namespace>` (or your actual CLI invocation) against one of your recorded/live incidents, showing the agent's read-only tool calls scrolling by (get_workload_status, get_pod_logs, get_events...).
5. **Terminal**: the proposed fix + approval prompt — the moment the CLI shows the kubectl command and asks for human y/n.
6. **Terminal (staged)**: deliberately feed a bad command through `validate_remediation` in a Python REPL to show a rejection — e.g. `kubectl delete pod foo` and `kubectl scale deploy foo -n kube-system --replicas=3` — and capture the `GateDecision(allowed=False, reason=...)` output for both.
7. **Dashboard**: [src/sre_agent/dashboard.py](src/sre_agent/dashboard.py) running — the remediation status pill going through its states if you have a recording that shows applied → verifying → healthy.
8. **Optional**: Opik trace view of the same run, if you want to tease Episode 4 at the end.

---

## 1. Script (YouTube cut, ~9 min)

**[0:00–0:15] Cold open — hook**
Voiceover over clip #5 (the approval prompt) or #6 (a rejected command):
> "I let an AI agent read live production-style Kubernetes data and propose fixes for real incidents. It never once got the chance to run something dangerous — not because the model is careful, but because it *architecturally can't*. Here's the four-layer gate that makes that true."

**[0:15–0:55] The premise**
Talking head or voiceover over B-roll of the dashboard:
> "This is an SRE agent — you point it at a namespace, it investigates like an on-call engineer would: reads pod status, logs, events, metrics, figures out what's wrong, and proposes a fix. The scary part of that sentence is 'proposes a fix' near a real cluster. So before I built any of the investigation logic, I built the thing that stops it from doing damage. Four layers, each one independently sufficient."

**[0:55–2:15] Layer 1 — RBAC: the agent's own credentials can't mutate anything**
Show clip #2, then #1.
> "Layer one is the boring one and it's the one that matters most: the agent authenticates as its own Kubernetes ServiceAccount, and that ServiceAccount's ClusterRole only grants `get`, `list`, `watch`. No `create`, no `update`, no `delete`, no `patch` — on anything. And notice what's *not* in the resource list: `secrets`. The agent can read pod specs, events, logs, node status, metrics — everything it needs to diagnose — but it can never read a Secret value, even by accident."
>
> "This isn't a prompt instruction. It's not 'please don't delete things.' If the model somehow decided to try, the Kubernetes API server itself would reject the call before it ever reached a resource. That's what 'safe by construction' means — the safety doesn't live in the model's judgment."

**[2:15–3:15] Layer 2 — separation of privilege between investigate and remediate**
Show the docstring lines 15–18 as a text overlay.
> "Layer two: even the *remediation* path — the part that's allowed to change things — doesn't run as the agent. Investigation uses the read-only ServiceAccount through the Python client. Remediation shells out to `kubectl` using *your* default context — your credentials, your identity. So nothing the agent proposes ever executes with the agent's own identity. There's a hard identity switch at the boundary between 'the AI suggested this' and 'this actually ran.'"

**[3:15–5:30] Layer 3 — the allowlist gate**
Show clip #3, walk through it live.
> "Layer three is where a proposed command actually gets judged. `validate_remediation` is a default-deny allowlist — only three verb shapes are allowed to mutate anything, ever: scale, rollout undo, rollout restart. That's the entire mutation surface of this agent. No delete, no apply, no patch, no exec — those aren't even on the list to reject, they're just not present, which means they fail closed."
>
> "It also blocks flags that redirect identity or cluster — `--kubeconfig`, `--context`, `--as`, `--token`, `--server` — so a command can't quietly retarget itself at a different cluster or impersonate a different user. It refuses `kube-system`, `kube-public`, `kube-node-lease` outright, even if the verb would otherwise be allowed. And it strips shell metacharacters — no `;`, no backticks, no `$()` — so there's no injection surface even though this is going through `shlex`, not a raw shell."
>
> [Staged demo, clip #6] "Let's break it. `kubectl delete pod foo` —" *(show rejection: "verb not on the allowlist")* "— rejected. `kubectl scale deploy foo -n kube-system --replicas=3` — a perfectly valid *scale* command, on the allowlist — but targeting a protected namespace —" *(show rejection: "refusing to act on protected namespace")* "— also rejected. The gate checks the verb AND the namespace independently; both have to clear."

**[5:30–6:30] Layer 4 — dry-run before real**
> "Even after a command clears the allowlist, it doesn't run for real yet. It's replayed with `--dry-run=server` first — so the actual Kubernetes API server validates the request, admission webhooks and all, without changing any state. If the dry run fails, we find out before touching the cluster, not after."

**[6:30–7:30] Layer 5 — a human has to say yes**
Show clip #5.
> "And only after all of that does a human see the proposed command and approve it explicitly. This is the one layer that *is* a judgment call rather than a hard rule — and that's on purpose. Everything before this point is there so that by the time a human is looking at the prompt, there's nothing left that could surprise them."

**[7:30–8:45] Put it together — a real run**
Show clip #4 → #5 → #7 (or #8 tease).
> "Here's what it looks like end to end on a real incident: [narrate the actual incident — e.g. a CrashLoopBackOff or OOMKilled pod] — the agent investigates read-only, proposes a scale or rollout fix, the gate gets it, dry-runs it, I approve it, and then it doesn't just declare victory — it polls the workload for three consecutive healthy checks before it says 'confirmed healthy.' One good reading isn't good enough; it has to stay good."

**[8:45–9:00] Close / CTA**
> "This is layer one of a bigger project — next up is how I actually test whether this thing's *diagnoses* are any good, which turned out to be a much harder problem than the safety gate. Follow along if you want to see where it breaks."

---

## 2. LinkedIn cut (60–90s + text post)

**Clip**: the staged rejection demo (clip #6) — both rejections back to back, maybe 20s — plus 10s of the RBAC `can-i --list` output as a cold open.

**Text post** (lead with the surprising claim, not the setup):

> I gave an AI agent read access to a Kubernetes cluster and let it propose fixes for real incidents.
>
> It never got the chance to run anything dangerous — not because the model was careful, but because it *architecturally couldn't*.
>
> Four independent layers, each one sufficient on its own:
> 1. Its own credentials can only get/list/watch — no write verbs exist on its RBAC role, and Secrets aren't even in the resource list.
> 2. Investigation and remediation run under different identities — nothing the agent proposes executes as the agent.
> 3. A default-deny allowlist checks every proposed command: only 3 verb shapes can ever mutate state, protected namespaces are refused outright, shell metacharacters are stripped.
> 4. Every mutating command dry-runs against the real API server before a human ever sees an approval prompt.
>
> The interesting engineering wasn't "write a good prompt." It was making the safe path the *only* path.
>
> Full breakdown (with the code) in the video 👇

## 3. X/Twitter cut (clip + thread)

**Clip**: same 20–30s rejection demo.

**Thread**:
1. I gave an AI agent kubectl access. Here's the 4-layer gate that means it never got to run something dangerous — even if it tried. 🧵
2. Layer 1: its ServiceAccount's RBAC role only grants get/list/watch. No create/update/delete/patch on anything. Secrets aren't even in the resource list. [screenshot of ClusterRole rules]
3. Layer 2: investigation and remediation run under *different identities*. The agent's own credentials never execute a mutating command — only the human's do, after approval.
4. Layer 3: a default-deny allowlist. Only 3 verb shapes can ever mutate cluster state: scale, rollout undo, rollout restart. Everything else fails closed — not because it's blocked, because it's just not on the list. [screenshot of rejection output]
5. Also blocked at layer 3: `--kubeconfig`/`--context`/`--as`/`--token` redirection, protected namespaces (kube-system etc.), shell metacharacters. Verified with two staged rejections in the video.
6. Layer 4: even an allowlisted command dry-runs against the real API server (`--dry-run=server`) before a human ever sees it.
7. Only after all 4 layers does a human get an approval prompt. By then there's nothing left to surprise them with.
8. Full video + code walkthrough: [link]

---

## 4. Title / thumbnail options

- "I gave an AI agent kubectl access (here's how I kept it safe)"
- "4 layers between an LLM and your production cluster"
- "My AI agent tried to delete a kube-system pod. Here's what happened."

Thumbnail: split-screen — left "kubectl delete pod" struck through in red, right the RBAC role showing only get/list/watch. Or: terminal screenshot of the `GateDecision(allowed=False, ...)` rejection, zoomed on `reason=`.

## 5. Notes for next episode

Close by teasing Episode 2 (eval harness) or Episode 3 (caught bug) — whichever you record next — so the series has a visible thread. Suggest recording #3 (caught bug) next since it's the strongest standalone hook if #1 does well.
