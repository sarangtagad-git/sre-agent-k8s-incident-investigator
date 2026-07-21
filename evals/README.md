# Evaluation harness (Phase 6)

Codifies each staged incident as a reproducible scenario and regression-tests the agent
against it:

1. **stage** the fault (admin kubectl context),
2. **run** the agent (its own read-only identity),
3. **assert** the produced RCA matches ground truth (category, root-cause text, that the
   proposed fix passes the Phase-5 safety gate, confidence),
4. **revert** — always, even on failure.

## Running it

```bash
sre-agent eval                 # all incidents (asks to confirm — mutates cluster, ~$0.22 each)
sre-agent eval -i cascade      # just one
sre-agent eval -y --keep       # skip the prompt; leave the incident staged
```

Exits non-zero if any incident fails, so it can gate a pipeline. It is **not** part of the
`pytest` suite: it needs a live cluster + `ANTHROPIC_API_KEY` and costs money per run. The
scoring logic itself (`sre_agent/evals.py` → `score()`) is unit-tested in
`tests/test_evals.py` with synthetic reports, so it stays covered in (free, cluster-less) CI.

## Incidents (derived from the manual investigations)

| name | fault | ground truth |
|---|---|---|
| `image_pull` | currencyservice bad image tag | category rollout/config; RCA names the image/pull failure; fix = `rollout undo` |
| `crash_loop` | emailservice bad command (`import` of a missing module) | category workload/config; RCA names the crash/module error; fix = `rollout undo` |
| `cascade` | redis-cart scaled to 0 | category dependency; RCA names redis-cart at 0 replicas; fix = `scale …=1` |

Specs and expected ground truth live in [`src/sre_agent/evals.py`](../src/sre_agent/evals.py).

This is the "prove it, don't just demo it" layer — the senior-level signal.
