"""Phase 9/10: history persistence — triggered_by, runs_since, prior-incident recall."""

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


# --- Phase 10: memory recall ------------------------------------------------


def test_save_run_records_prior_incidents(db_path):
    from sre_agent.agent.schemas import PriorIncident

    prior = [PriorIncident(run_id="abc123", when="2026-07-01T00:00:00", category="dependency",
                           confidence_score=0.9, root_cause="redis-cart scaled to 0",
                           remediation_command="kubectl scale ...", outcome_label="proposed only")]
    result = _result()
    result.prior_incidents = prior
    run_id = history_store.save_run(result, namespace="boutique", workload="redis-cart",
                                    alert="x", mode="propose")
    row = history_store.get_run(run_id)
    import json
    assert json.loads(row["prior_incidents_json"])[0]["run_id"] == "abc123"


def test_migration_adds_prior_incidents_json_to_old_db(db_path):
    old_schema = history_store._SCHEMA.replace("    prior_incidents_json TEXT\n", "").replace(
        ",\n)", "\n)"  # drop the now-trailing comma after eval_checks_json
    )
    assert "prior_incidents_json" not in old_schema
    conn = sqlite3.connect(db_path)
    conn.execute(old_schema)
    conn.commit()
    conn.close()

    run_id = history_store.save_run(_result(), namespace="boutique", workload=None,
                                    alert="x", mode="propose")
    assert history_store.get_run(run_id)["prior_incidents_json"] == "[]"


class TestFindRelatedRuns:
    def test_matches_namespace_and_workload(self, db_path):
        history_store.save_run(_result(), namespace="boutique", workload="redis-cart",
                               alert="x", mode="propose")
        history_store.save_run(_result(), namespace="boutique", workload="cartservice",
                               alert="y", mode="propose")
        found = history_store.find_related_runs("boutique", "redis-cart")
        assert len(found) == 1
        assert found[0]["workload"] == "redis-cart"

    def test_excludes_eval_mode(self, db_path):
        history_store.save_run(_result(), namespace="boutique", workload="redis-cart",
                               alert="x", mode="eval")
        assert history_store.find_related_runs("boutique", "redis-cart") == []

    def test_workload_none_matches_namespace_only(self, db_path):
        history_store.save_run(_result(), namespace="boutique", workload="emailservice",
                               alert="x", mode="propose")
        found = history_store.find_related_runs("boutique", None)
        assert len(found) == 1

    def test_respects_limit_and_orders_newest_first(self, db_path, monkeypatch):
        import time as _time

        real_strftime = _time.strftime
        stamps = iter(["2026-07-01T00:00:00", "2026-07-02T00:00:00", "2026-07-03T00:00:00"])
        monkeypatch.setattr(history_store.time, "strftime", lambda *_a: next(stamps))
        for _ in range(3):
            history_store.save_run(_result(), namespace="boutique", workload="redis-cart",
                                   alert="x", mode="propose")
        found = history_store.find_related_runs("boutique", "redis-cart", limit=2)
        assert len(found) == 2
        assert found[0]["started_at"] == "2026-07-03T00:00:00"
