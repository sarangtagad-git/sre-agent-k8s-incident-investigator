"""Clip #6 — staged rejection demo for Episode 1.

This project's venv is WSL-based, so run it from WSL, not Windows Python:

    wsl.exe -e bash -lc 'cd "/mnt/d/Claude Code/SRE Agent - K8S Incident Investigator" \
        && .venv/bin/python /path/to/demo_gate_rejections.py'

Or simplest for the recording: copy this file into the repo root temporarily,
open a WSL terminal there, and run `.venv/bin/python demo_gate_rejections.py`.
No cluster needed — validate_remediation() is pure Python, no API calls.

Verified output (captured 2026-08-22):

    $ kubectl-gate check: 'kubectl delete pod foo -n boutique'
      allowed = False
      reason  = "verb not on the allowlist: 'delete'"

    $ kubectl-gate check: 'kubectl scale deployment foo -n kube-system --replicas=3'
      allowed = False
      reason  = 'refusing to act on protected namespace: kube-system'
"""

from sre_agent.remediation import validate_remediation

demos = [
    "kubectl delete pod foo -n boutique",
    "kubectl scale deployment foo -n kube-system --replicas=3",
]

for cmd in demos:
    print(f"\n$ kubectl-gate check: {cmd!r}")
    decision = validate_remediation(cmd)
    print(f"  allowed = {decision.allowed}")
    print(f"  reason  = {decision.reason!r}")
