"""Save/replay a `sre-agent eval` run, so a real (paid) agent call only has to happen
once per incident — every check-tuning iteration after that replays the saved file
for free instead of calling the live agent again.

Used by `sre-agent eval --record` (save) and `sre-agent eval --replay` (load).
See docs/incident-taxonomy-plan.md, step 6.

Recordings are committed to the repo (not gitignored like data/) — they're fixtures,
not runtime state, so replay works the same for anyone who clones the repo, no cluster
or API key required.
"""

from __future__ import annotations

from pathlib import Path

from .agent.schemas import RunResult

RECORDINGS_DIR = Path("tests/fixtures/recordings")


def save_recording(incident_name: str, result: RunResult, dir: Path = RECORDINGS_DIR) -> Path:
    """Write a RunResult to <dir>/<incident_name>.json, overwriting any prior recording."""
    dir.mkdir(parents=True, exist_ok=True)
    path = dir / f"{incident_name}.json"
    path.write_text(result.model_dump_json(indent=2))
    return path


def load_recording(incident_name: str, dir: Path = RECORDINGS_DIR) -> RunResult:
    """Read a previously-saved RunResult back. Raises FileNotFoundError if none exists."""
    path = dir / f"{incident_name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No recording for {incident_name!r} at {path}. "
            f"Run `sre-agent eval -i {incident_name} --record` first."
        )
    return RunResult.model_validate_json(path.read_text())
