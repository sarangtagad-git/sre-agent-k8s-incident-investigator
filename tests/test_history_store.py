"""Phase 9: history persistence — triggered_by column, migration, runs_since."""

from __future__ import annotations

import sqlite3

import pytest

from sre_agent import history_store
from sre_agent.agent.schemas import RCAReport, Remediation, RunResult


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "history.db"
    monkeypatch.setenv("HISTORY_DB_PATH", str(path))
    return path


def _result() -> RunResult:
    return RunResult(
        report=RCAReport(
            summary="redis-cart scaled to 0",
            root_cause="redis-cart deployment has desired=0",
            category="dependency",
            confidence="high",
            confidence_score=0.9,
            impact="cart 500s",
            remediation=Remediation(
                action="scale back up",
                command="kubectl -n boutique scale deployment/redis-cart --replicas=1",
                rationale="restore the backend",
            ),
        ),
        cost_usd=0.15,
        duration_s=42.0,
    )


def test_save_run_records_triggered_by(db_path):
    manual_id = history_store.save_run(_result(), namespace="boutique", workload=None,
                                       alert="x", mode="propose")
    auto_id = history_store.save_run(_result(), namespace="boutique", workload=None,
                                     alert="y", mode="propose", triggered_by="alert")
    assert history_store.get_run(manual_id)["triggered_by"] == "manual"
    assert history_store.get_run(auto_id)["triggered_by"] == "alert"


def test_migration_adds_triggered_by_to_old_db(db_path):
    # Simulate a pre-Phase-9 DB: same table minus the triggered_by column.
    old_schema = history_store._SCHEMA.replace(
        "    triggered_by TEXT NOT NULL DEFAULT 'manual',\n", ""
    )
    assert "triggered_by" not in old_schema  # guard: the replace actually removed it
    conn = sqlite3.connect(db_path)
    conn.execute(old_schema)
    conn.commit()
    conn.close()

    run_id = history_store.save_run(_result(), namespace="boutique", workload=None,
                                    alert="x", mode="propose", triggered_by="alert")
    assert history_store.get_run(run_id)["triggered_by"] == "alert"


def test_runs_since_filters_by_started_at(db_path):
    history_store.save_run(_result(), namespace="boutique", workload=None,
                           alert="recent", mode="propose", triggered_by="alert")
    assert [r["alert"] for r in history_store.runs_since("2000-01-01T00:00:00")] == ["recent"]
    assert history_store.runs_since("2999-01-01T00:00:00") == []
