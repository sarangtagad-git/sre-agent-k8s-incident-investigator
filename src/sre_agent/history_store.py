"""Phase 7: persistence for past investigate()/eval runs.

One SQLite file, one denormalized `runs` table. Most of what's captured (evidence
trail, correlation, ranked hypotheses, the RCA itself) is already a structured
Pydantic object with no reason to be split across relational tables for a
single-user local tool — store it as JSON alongside the handful of flat columns
a list/filter view actually needs.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent.schemas import RunResult
    from .evals import Check

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    duration_s REAL NOT NULL,
    namespace TEXT NOT NULL,
    workload TEXT,
    alert TEXT,
    mode TEXT NOT NULL,
    incident_name TEXT,
    category TEXT,
    confidence_score REAL,
    root_cause TEXT,
    cost_usd REAL,
    input_tokens INTEGER,
    cache_write_tokens INTEGER,
    cache_read_tokens INTEGER,
    output_tokens INTEGER,
    approval_status TEXT NOT NULL DEFAULT 'n/a',
    resolved INTEGER,
    triggered_by TEXT NOT NULL DEFAULT 'manual',
    evidence_json TEXT,
    correlation_json TEXT,
    hypotheses_json TEXT,
    report_json TEXT,
    eval_checks_json TEXT
)
"""


def _db_path() -> Path:
    from .config import get_settings

    return Path(get_settings().history_db_path)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    # Phase 9 migration: pre-existing DBs lack triggered_by. ALTER is idempotent-by-
    # exception — cheap enough at open time for a single-user file, no framework needed.
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN triggered_by TEXT NOT NULL DEFAULT 'manual'")
    except sqlite3.OperationalError:
        pass  # column already exists
    return conn


def save_run(
    result: "RunResult",
    *,
    namespace: str,
    workload: str | None,
    alert: str | None,
    mode: str,  # "propose" | "execute" | "eval"
    incident_name: str | None = None,
    approval_status: str = "n/a",
    resolved: bool | None = None,
    eval_checks: "list[Check] | None" = None,
    triggered_by: str = "manual",  # "manual" | "alert" (Phase 9 listener)
) -> str:
    """Persist one investigate()/eval run. Returns the new run id."""
    run_id = uuid.uuid4().hex[:12]
    report = result.report
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO runs (
                id, started_at, duration_s, namespace, workload, alert, mode,
                incident_name, category, confidence_score, root_cause, cost_usd,
                input_tokens, cache_write_tokens, cache_read_tokens, output_tokens,
                approval_status, resolved, triggered_by, evidence_json,
                correlation_json, hypotheses_json, report_json, eval_checks_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                time.strftime("%Y-%m-%dT%H:%M:%S"),
                result.duration_s,
                namespace,
                workload,
                alert,
                mode,
                incident_name,
                report.category,
                report.confidence_score,
                report.root_cause,
                result.cost_usd,
                result.input_tokens,
                result.cache_write_tokens,
                result.cache_read_tokens,
                result.output_tokens,
                approval_status,
                None if resolved is None else int(resolved),
                triggered_by,
                json.dumps([e.model_dump() for e in result.evidence]),
                json.dumps(result.correlation.model_dump()) if result.correlation else None,
                json.dumps([h.model_dump() for h in result.hypotheses]),
                report.model_dump_json(),
                json.dumps([dataclasses.asdict(c) for c in eval_checks]) if eval_checks else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


def list_runs(limit: int = 20) -> list[sqlite3.Row]:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()


def runs_since(since_iso: str) -> list[sqlite3.Row]:
    """Runs started at/after `since_iso` (local "%Y-%m-%dT%H:%M:%S", same format we
    write — lexicographic compare is chronological). Used by the Phase 9 alert policy
    for its cooldown + daily-cap checks, so autonomy state lives in the same table as
    everything else."""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT id, started_at, namespace, alert, triggered_by FROM runs "
            "WHERE started_at >= ? ORDER BY started_at DESC",
            (since_iso,),
        ).fetchall()
    finally:
        conn.close()


def get_run(run_id: str) -> sqlite3.Row | None:
    """Exact id, or a unique prefix of it (like a short git hash)."""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM runs WHERE id = ? OR id LIKE ? || '%' ORDER BY id LIMIT 1",
            (run_id, run_id),
        ).fetchone()
    finally:
        conn.close()
