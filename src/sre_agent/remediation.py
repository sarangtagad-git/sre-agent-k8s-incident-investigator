"""The human-in-the-loop remediation gate (Phase 5).

The investigation agent is read-only and only ever *proposes* a fix. This module is the
gate between that proposal and any change to the cluster. It exists so a hallucinated or
overreaching command can never reach the API server:

  1. validate  — default-deny allowlist. `scale`, `rollout undo`/`restart` are allowed
     outright; `set resources`, `patch`, and `delete` are allowed only in narrow,
     content-inspected forms (see the scoped validators below — resource-limit edits on
     workload controllers, merge/strategic patches touching only a small safe field set,
     and NetworkPolicy deletion by name); a few read-only verbs are allowed for
     verification. Everything else — apply, exec, secrets, --kubeconfig redirection,
     shell metacharacters, system namespaces — is rejected.
  2. dry-run   — the mutating command is replayed with `--dry-run=server` so the API server
     validates it without changing anything.
  3. approve   — a human confirms (CLI), then it runs for real.

Separation of privilege: the agent investigates through its restricted read-only kubeconfig
(the Python client), but remediation shells out to `kubectl` using your *default* context —
so anything that runs here runs with a human's credentials and a human's approval, never the
agent's identity.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from .tools.schemas import NamespaceWorkloadStatus

# Verbs that may change cluster state unconditionally — no further inspection needed.
_ALLOWED_MUTATING: set[tuple[str, ...]] = {
    ("scale",),
    ("rollout", "undo"),
    ("rollout", "restart"),
}
# Verbs that may change cluster state, but only in narrow forms — each has a dedicated
# validator below that inspects flags/positionals/payload before the command is allowed.
# Populated after the validator functions are defined (see bottom of the scoped-verb
# section).
_SCOPED_MUTATING_VALIDATORS: dict[tuple[str, ...], Callable[[list[str]], str | None]] = {}
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
    "-c", "--containers", "--limits", "--requests", "-p", "--patch", "--type",
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


def _positionals(tokens: list[str]) -> list[str]:
    """All non-flag, non-flag-value tokens after `kubectl`, in order."""
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
    return positionals


def _flag_values(tokens: list[str], names: set[str]) -> list[str]:
    """Collect values for `--flag=value` and `--flag value` occurrences of any name in `names`."""
    values: list[str] = []
    toks = tokens[1:]  # tokens[0] is "kubectl"
    i = 0
    while i < len(toks):
        tok = toks[i]
        if "=" in tok and tok.split("=", 1)[0] in names:
            values.append(tok.split("=", 1)[1])
        elif tok in names and i + 1 < len(toks):
            values.append(toks[i + 1])
            i += 1
        i += 1
    return values


def _verb_key(tokens: list[str]) -> tuple[str, ...]:
    """Extract the (verb[, subverb]) from a kubectl token list, skipping flags + values."""
    positionals = _positionals(tokens)
    if not positionals:
        return ()
    verb = positionals[0]
    if verb in ("rollout", "set") and len(positionals) > 1:
        return (verb, positionals[1])
    return (verb,)


def _resource_type_and_name(rest: list[str]) -> tuple[str, str]:
    """Split a `TYPE NAME` or `TYPE/NAME` positional pair into (type, name). `name` is
    "" if not given."""
    type_tok = rest[0]
    if "/" in type_tok:
        rtype, _, rname = type_tok.partition("/")
        return rtype, rname
    return type_tok, (rest[1] if len(rest) > 1 else "")


# --- kubectl set resources ------------------------------------------------------------
# Scoped to editing CPU/memory limits or requests on a workload controller — the one
# thing `cpu_throttled`-style incidents need and nothing else. Any other flag or
# resource type is rejected.

_WORKLOAD_ALIASES = {
    "deploy": "deployment", "deployment": "deployment", "deployments": "deployment",
    "sts": "statefulset", "statefulset": "statefulset", "statefulsets": "statefulset",
    "ds": "daemonset", "daemonset": "daemonset", "daemonsets": "daemonset",
}
_ALLOWED_SET_RESOURCES_FLAGS = {
    "-n", "--namespace", "-c", "--containers", "--limits", "--requests", "--dry-run",
}
_RESOURCE_KEY_RE = re.compile(r"^(cpu|memory|ephemeral-storage)$")
_RESOURCE_VAL_RE = re.compile(r"^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|Ti|Pi|Ei|K|M|G|T|P|E)?$")


def _valid_resource_list(value: str) -> bool:
    """`cpu=200m` or `cpu=200m,memory=512Mi` — comma-separated key=quantity pairs,
    keys restricted to the three real resource names."""
    parts = value.split(",")
    if not parts or any(not p for p in parts):
        return False
    for part in parts:
        if "=" not in part:
            return False
        k, v = part.split("=", 1)
        if not _RESOURCE_KEY_RE.match(k) or not _RESOURCE_VAL_RE.match(v):
            return False
    return True


def _validate_set_resources(tokens: list[str]) -> str | None:
    """Return None if the `set resources` command is allowed, else a rejection reason."""
    for tok in tokens:
        if tok.startswith("-"):
            flag = tok.split("=", 1)[0]
            if flag not in _ALLOWED_SET_RESOURCES_FLAGS:
                return f"set resources: flag not allowed: {flag}"

    rest = _positionals(tokens)[2:]  # after "set", "resources"
    if not rest:
        return "set resources: missing resource type/name"
    rtype, rname = _resource_type_and_name(rest)
    rtype_norm = _WORKLOAD_ALIASES.get(rtype)
    if rtype_norm is None:
        return f"set resources: resource type not allowed: {rtype!r}"
    if not rname:
        return "set resources: missing resource name"

    limit_values = _flag_values(tokens, {"--limits", "--requests"})
    if not limit_values:
        return "set resources: must specify --limits or --requests"
    for value in limit_values:
        if not _valid_resource_list(value):
            return f"set resources: invalid --limits/--requests value: {value!r}"
    return None


# --- kubectl patch ----------------------------------------------------------------------
# Scoped by *content*, not just verb: only merge/strategic patches (never --type=json,
# whose arbitrary "path" operations can't be bounded this way) whose payload touches
# nothing but a small allowlist of safe field paths per resource type.

_ALLOWED_PATCH_FLAGS = {"-n", "--namespace", "--type", "-p", "--patch", "--dry-run"}
_WORKLOAD_PATCH_PATTERNS = [
    re.compile(r"^spec\.template\.spec\.containers\[\]\.name$"),
    re.compile(
        r"^spec\.template\.spec\.containers\[\]\.resources\.(limits|requests)"
        r"\.(cpu|memory|ephemeral-storage)$"
    ),
]
_SERVICE_PATCH_PATTERNS = [re.compile(r"^spec\.selector\.[^.\[\]]+$")]
_PATCH_TYPE_ALIASES = {
    **_WORKLOAD_ALIASES,
    "svc": "service", "service": "service", "services": "service",
}
_PATCH_SCOPES: dict[str, list[re.Pattern]] = {
    "deployment": _WORKLOAD_PATCH_PATTERNS,
    "statefulset": _WORKLOAD_PATCH_PATTERNS,
    "daemonset": _WORKLOAD_PATCH_PATTERNS,
    "service": _SERVICE_PATCH_PATTERNS,
}


def _json_leaf_paths(obj: object, prefix: str = "") -> list[str]:
    """Dotted paths to every leaf value in a JSON patch payload, with `[]` standing in
    for any list index (we don't care which container, only which field)."""
    paths: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            paths.extend(_json_leaf_paths(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for item in obj:
            paths.extend(_json_leaf_paths(item, f"{prefix}[]"))
    else:
        paths.append(prefix)
    return paths


def _validate_patch(tokens: list[str]) -> str | None:
    """Return None if the `patch` command is allowed, else a rejection reason."""
    for tok in tokens:
        if tok.startswith("-"):
            flag = tok.split("=", 1)[0]
            if flag not in _ALLOWED_PATCH_FLAGS:
                return f"patch: flag not allowed: {flag}"

    patch_types = _flag_values(tokens, {"--type"})
    if patch_types and patch_types[-1] == "json":
        return "patch: --type=json (JSON Patch) is not allowed; use merge/strategic"

    rest = _positionals(tokens)[1:]  # after "patch"
    if not rest:
        return "patch: missing resource type/name"
    rtype, rname = _resource_type_and_name(rest)
    rtype_norm = _PATCH_TYPE_ALIASES.get(rtype)
    if rtype_norm is None:
        return f"patch: resource type not allowed: {rtype!r}"
    if not rname:
        return "patch: missing resource name"

    patch_payloads = _flag_values(tokens, {"-p", "--patch"})
    if not patch_payloads:
        return "patch: missing -p/--patch payload"
    try:
        payload = json.loads(patch_payloads[-1])
    except (json.JSONDecodeError, TypeError):
        return "patch: payload is not valid JSON"
    if not isinstance(payload, dict):
        return "patch: payload must be a JSON object (merge/strategic patch)"

    patterns = _PATCH_SCOPES[rtype_norm]
    for path in _json_leaf_paths(payload):
        if not any(p.match(path) for p in patterns):
            return f"patch: field not allowed for {rtype_norm}: {path}"
    return None


# --- kubectl delete ----------------------------------------------------------------------
# Scoped to a single named NetworkPolicy — the one delete shape a `network_blocked`-style
# fix needs. No --all, no label-selector bulk delete, no other resource type.

_ALLOWED_DELETE_FLAGS = {"-n", "--namespace", "--dry-run"}
_NETPOL_ALIASES = {
    "networkpolicy": "networkpolicy", "networkpolicies": "networkpolicy",
    "netpol": "networkpolicy", "netpols": "networkpolicy",
}


def _validate_delete(tokens: list[str]) -> str | None:
    """Return None if the `delete` command is allowed, else a rejection reason."""
    for tok in tokens:
        if tok.startswith("-"):
            flag = tok.split("=", 1)[0]
            if flag not in _ALLOWED_DELETE_FLAGS:
                return f"delete: flag not allowed: {flag}"

    rest = _positionals(tokens)[1:]  # after "delete"
    if not rest:
        return "delete: missing resource type/name"
    rtype, rname = _resource_type_and_name(rest)
    rtype_norm = _NETPOL_ALIASES.get(rtype)
    if rtype_norm is None:
        return f"delete: resource type not allowed: {rtype!r} (only NetworkPolicy)"
    if not rname:
        return "delete: a specific resource name is required (bulk/selector delete not allowed)"
    extra = rest[1:] if "/" in rest[0] else rest[2:]
    if extra:
        return "delete: only a single resource name is allowed"
    return None


_SCOPED_MUTATING_VALIDATORS.update({
    ("set", "resources"): _validate_set_resources,
    ("patch",): _validate_patch,
    ("delete",): _validate_delete,
})


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
        if key in _SCOPED_MUTATING_VALIDATORS:
            reason = _SCOPED_MUTATING_VALIDATORS[key](tokens)
            if reason is not None:
                return GateDecision(False, reason)
            mutating.append(tokens)
        elif key in _ALLOWED_MUTATING:
            mutating.append(tokens)
        elif key in _ALLOWED_READONLY:
            readonly.append(tokens)
        else:
            verb = " ".join(key) or "(none)"
            return GateDecision(False, f"verb not on the allowlist: {verb!r}")

    if not mutating:
        return GateDecision(False, "no allowed mutating action found in the command")
    return GateDecision(True, "ok", mutating=mutating, readonly=readonly)


@dataclass
class VerificationResult:
    """Outcome of polling for recovery after an applied fix — see
    docs/verification-plan.md. `status` is one of "confirmed_healthy" /
    "still_unhealthy" / "not_checked"; never a boolean, so a fix that demonstrably
    didn't work is a distinct, loud outcome rather than silence."""

    status: str
    detail: str


def is_workload_healthy(status: NamespaceWorkloadStatus, workload: str) -> bool | None:
    """True/False if `workload`'s Deployment was found and its replica counts say
    healthy or not (ready == desired and unavailable == 0 — the same signal a human
    reads off `kubectl get deploy`). None if the workload isn't in this namespace at
    all: never guess when we can't check."""
    dep = next((d for d in status.deployments if d.name == workload), None)
    if dep is None:
        return None
    return dep.ready == dep.desired and dep.unavailable == 0


def verify_recovery(
    workload: str | None,
    check_fn: Callable[[], NamespaceWorkloadStatus],
    *,
    timeout_s: int = 90,
    poll_interval_s: int = 5,
    stability_checks: int = 3,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> VerificationResult:
    """Poll `check_fn` (a get_workload_status call) until `workload` has been healthy
    for `stability_checks` CONSECUTIVE polls, or `timeout_s` elapses.

    Point-in-time only — "confirmed_healthy" means stable for the window actually
    checked, never a permanent guarantee (docs/verification-plan.md decision 3). A pod
    can pass one check and crash again seconds later, so a single healthy reading is
    never enough: any unhealthy reading during the window resets the consecutive-
    success counter to zero rather than merely pausing it, mirroring how Kubernetes'
    own probes use successThreshold instead of trusting one success.

    `sleep_fn`/`clock_fn` are injectable so tests can drive this without sleeping for
    real or mocking global time.
    """
    if workload is None:
        return VerificationResult("not_checked", "no workload named — nothing to verify")

    consecutive = 0
    attempts = 0
    last_healthy: bool | None = None
    start = clock_fn()
    while True:
        attempts += 1
        status = check_fn()
        healthy = is_workload_healthy(status, workload)
        if healthy is None:
            return VerificationResult(
                "not_checked", f"{workload}: not found in namespace {status.namespace}"
            )
        last_healthy = healthy
        if healthy:
            consecutive += 1
            if consecutive >= stability_checks:
                elapsed = clock_fn() - start
                return VerificationResult(
                    "confirmed_healthy",
                    f"{workload}: healthy for {consecutive} consecutive checks "
                    f"(~{elapsed:.0f}s)",
                )
        else:
            consecutive = 0  # a single bad reading resets the stability window

        if clock_fn() - start >= timeout_s:
            state = "healthy" if last_healthy else "unhealthy"
            return VerificationResult(
                "still_unhealthy",
                f"{workload}: still {state} after {timeout_s}s ({attempts} checks, "
                f"needed {stability_checks} consecutive)",
            )
        sleep_fn(poll_interval_s)


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
