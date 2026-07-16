"""SRE Incident Investigator — an autonomous, read-only Kubernetes incident agent.

The agent gathers evidence via read-only tools, correlates it, produces a
root-cause hypothesis, and proposes remediation behind a human approval gate.
It never mutates the cluster itself (enforced by RBAC, not just by prompt).
"""

__version__ = "0.1.0"
