"""The human-in-the-loop remediation gate (Phase 5).

The investigation agent is read-only and only ever *proposes* a fix. This module is the
gate between that proposal and any change to the cluster. It exists so a hallucinated or
overreaching command can never reach the API server:

  1. validate  — default-deny allowlist. Only three verbs may mutate (scale, rollout undo,
     rollout restart); a few read-only verbs are allowed for verification. Everything else
     — delete, apply, patch, exec, secrets, --kubeconfig redirection, shell metacharacters,
     system namespaces — is rejected.
  2. dry-run   — the mutating command is replayed with `--dry-run=server` so the API server
     validates it without changing anything.
  3. approve   — a human confirms (CLI), then it runs for real.

Separation of privilege: the agent investigates through its restricted read-only kubeconfig
(the Python client), but remediation shells out to `kubectl` using your *default* context —
so anything that runs here runs with a human's credentials and a human's approval, never the
agent's identity.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field

# Verbs that may change cluster state — the ONLY ones the gate will ever execute.
_ALLOWED_MUTATING: set[tuple[str, ...]] = {
    ("scale",),
    ("rollout", "undo"),
    ("rollout", "restart"),
}
# Read-only verbs permitted inside a proposed command (e.g. a `&&`-chained verify step).
_ALLOWED_READONLY: set[tuple[str, ...]] = {
    ("get",),
    ("describe",),
    ("rollout", "status"),
    ("rollout", "history"),
}

# Flags that take a following-token value (space form); needed to find the verb positionally.
_VALUE_FLAGS = {
    "-n", "--namespace", "-l", "--selector", "-o", "--output",
    "-f", "--filename", "--replicas", "--to-revision", "--current",
    "--kubeconfig", "--context", "--dry-run",
}
# Flags that redirect identity/cluster — never allowed in a proposed command.
_FORBIDDEN_FLAGS = {"--kubeconfig", "--context", "--as", "--as-group", "--token", "--server"}
# Namespaces the gate refuses to touch even with an allowed verb.
_PROTECTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}
# Shell metacharacters that enable chaining/injection (`&&` is split out before this check).
_SHELL_METACHARS = set(";|`$()<>&\n")


@dataclass
class GateDecision:
    """Result of validating a proposed remediation command."""

    allowed: bool
    reason: str = ""
    mutating: list[list[str]] = field(default_factory=list)  # arg lists that change state
    readonly: list[list[str]] = field(default_factory=list)  # arg lists that only read


def _verb_key(tokens: list[str]) -> tuple[str, ...]:
    """Extract the (verb[, subverb]) from a kubectl token list, skipping flags + values."""
    positionals: list[str] = []
    skip = False
    for tok in tokens[1:]:  # tokens[0] is "kubectl"
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok in _VALUE_FLAGS:
                skip = True  # its value is the next token
            continue
        positionals.append(tok)
    if not positionals:
        return ()
    verb = positionals[0]
    if verb == "rollout" and len(positionals) > 1:
        return (verb, positionals[1])
    return (verb,)


def _namespace(tokens: list[str]) -> str | None:
    for i, tok in enumerate(tokens):
        if tok in ("-n", "--namespace") and i + 1 < len(tokens):
            return tokens[i + 1]  # space form: -n boutique
        if tok.startswith("--namespace="):
            return tok.split("=", 1)[1]  # --namespace=boutique
        if tok.startswith("-n="):
            return tok.split("=", 1)[1]  # -n=boutique
        if tok.startswith("-n") and len(tok) > 2 and not tok.startswith("-n="):
            return tok[2:]  # glued short form: -nboutique / -nkube-system
    return None


def validate_remediation(command: str) -> GateDecision:
    """Validate a proposed kubectl command against the allowlist (default-deny).

    A command may be a single kubectl invocation or several joined by `&&` (e.g. a fix plus
    read-only verification). Every segment must independently pass, or the whole thing is
    rejected.
    """
    command = (command or "").strip()
    if not command:
        return GateDecision(False, "empty command")

    mutating: list[list[str]] = []
    readonly: list[list[str]] = []

    for segment in command.split("&&"):
        segment = segment.strip()
        if not segment:
            return GateDecision(False, "empty command segment")
        if _SHELL_METACHARS & set(segment):
            bad = "".join(sorted(_SHELL_METACHARS & set(segment)))
            return GateDecision(False, f"shell metacharacter(s) not allowed: {bad!r}")

        try:
            tokens = shlex.split(segment)
        except ValueError as e:
            return GateDecision(False, f"could not parse command: {e}")
        if not tokens or tokens[0] != "kubectl":
            return GateDecision(False, f"only kubectl commands are allowed: {segment!r}")

        for tok in tokens:
            flag = tok.split("=", 1)[0]
            if flag in _FORBIDDEN_FLAGS:
                return GateDecision(False, f"flag not allowed (identity/cluster redirect): {flag}")

        ns = _namespace(tokens)
        if ns in _PROTECTED_NAMESPACES:
            return GateDecision(False, f"refusing to act on protected namespace: {ns}")

        key = _verb_key(tokens)
        if key in _ALLOWED_MUTATING:
            mutating.append(tokens)
        elif key in _ALLOWED_READONLY:
            readonly.append(tokens)
        else:
            verb = " ".join(key) or "(none)"
            return GateDecision(False, f"verb not on the allowlist: {verb!r}")

    if not mutating:
        return GateDecision(False, "no allowed mutating action found in the command")
    return GateDecision(True, "ok", mutating=mutating, readonly=readonly)


def run_kubectl(args: list[str], dry_run: bool = False, timeout: int = 30) -> tuple[int, str, str]:
    """Run a validated kubectl arg list with the caller's default context (no shell).

    dry_run=True appends `--dry-run=server` so the API server validates without changing
    anything. Never pass a raw string here — only the validated token lists from GateDecision.
    """
    cmd = list(args)
    if dry_run:
        cmd = cmd + ["--dry-run=server"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "kubectl not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"kubectl timed out after {timeout}s"
