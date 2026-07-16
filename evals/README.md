# Evaluation harness (Phase 6)

Codifies each staged incident as a scenario:
1. **inject** the fault (e.g. bad image tag, scale redis to 0),
2. **run** the agent,
3. **assert** the produced root cause matches ground truth,
4. **score** and (in CI) fail the build on regression.

Ground-truth incidents derived from the manual investigations:
- `imagepullbackoff` — emailservice bad image tag  → root cause: image tag not found
- `crashloopbackoff` — emailservice missing module → root cause: crash on startup (ModuleNotFoundError)
- `redis-cascade`   — redis-cart scaled to 0       → root cause: redis-cart unavailable (dependency)

This is the "prove it, don't just demo it" layer — the senior-level signal.
