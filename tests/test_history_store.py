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
    # A pre-Phase-10 DB predates Phase 11 too — strip all three later columns to
    # accurately simulate it, then fix the now-trailing comma after eval_checks_json.
    old_schema = (
        history_store._SCHEMA
        .replace("    prior_incidents_json TEXT,\n", "")
        .replace("    verification_status TEXT,\n", "")
        .replace("    verification_detail TEXT\n", "")
        .replace(",\n)", "\n)")
    )
    assert "prior_incidents_json" not in old_schema
    conn = sqlite3.connect(db_path)
    conn.execute(old_schema)
    conn.commit()
    conn.close()

    run_id = history_store.save_run(_result(), namespace="boutique", workload=None,
                                    alert="x", mode="propose")
    assert history_store.get_run(run_id)["prior_incidents_json"] == "[]"


def test_save_run_records_verification_status_and_detail(db_path):
    run_id = history_store.save_run(
        _result(), namespace="boutique", workload="emailservice", alert="x",
        mode="execute", approval_status="approved_applied",
        verification_status="confirmed_healthy",
        verification_detail="emailservice: healthy for 3 consecutive checks (~15s)",
    )
    row = history_store.get_run(run_id)
    assert row["verification_status"] == "confirmed_healthy"
    assert "3 consecutive checks" in row["verification_detail"]


def test_verification_status_defaults_to_none_when_not_passed(db_path):
    run_id = history_store.save_run(_result(), namespace="boutique", workload=None,
                                    alert="x", mode="propose")
    row = history_store.get_run(run_id)
    assert row["verification_status"] is None
    assert row["verification_detail"] is None


def test_migration_adds_verification_columns_to_old_db(db_path):
    # Simulate a pre-Phase-11 DB: same table minus both new columns.
    old_schema = (
        history_store._SCHEMA
        # the run_config / open-token columns came after Phase 11: strip them first so
        # verification_detail is the last column again
        .replace(
            ",\n    run_config TEXT,\n    open_input_tokens INTEGER,\n    open_output_tokens INTEGER\n)",
            "\n)",
        )
        .replace("    verification_status TEXT,\n", "")
        .replace("    verification_detail TEXT\n", "")
        .replace(",\n)", "\n)")  # drop the now-trailing comma after prior_incidents_json
    )
    assert "verification_status" not in old_schema
    assert "verification_detail" not in old_schema
    conn = sqlite3.connect(db_path)
    conn.execute(old_schema)
    conn.commit()
    conn.close()

    run_id = history_store.save_run(
        _result(), namespace="boutique", workload="emailservice", alert="x",
        mode="execute", verification_status="still_unhealthy",
        verification_detail="emailservice: still unhealthy after 90s",
    )
    row = history_store.get_run(run_id)
    assert row["verification_status"] == "still_unhealthy"
    assert row["verification_detail"] == "emailservice: still unhealthy after 90s"


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


# --- run config + the open model's own tokens ---------------------------------------------


def test_save_run_records_run_config_and_open_model_tokens(db_path):
    result = _result().model_copy(
        update={"run_config": "all:deepseek/deepseek-v4.1-flash",
                "open_input_tokens": 12_000, "open_output_tokens": 800}
    )
    run_id = history_store.save_run(result, namespace="boutique", workload=None,
                                    alert="x", mode="eval")
    row = history_store.get_run(run_id)
    assert row["run_config"] == "all:deepseek/deepseek-v4.1-flash"
    assert (row["open_input_tokens"], row["open_output_tokens"]) == (12_000, 800)
    # The open model's tokens must NOT leak into the Claude columns the cache view reads.
    assert row["input_tokens"] == 0 and row["output_tokens"] == 0


def test_a_result_without_a_config_stays_unknown(db_path):
    # e.g. a recording made before this field existed: None, not a guessed "claude".
    run_id = history_store.save_run(_result(), namespace="boutique", workload=None,
                                    alert="x", mode="eval")
    assert history_store.get_run(run_id)["run_config"] is None


def test_migration_adds_the_config_columns_and_old_rows_stay_null(db_path):
    old_schema = history_store._SCHEMA.replace(
        ",\n    run_config TEXT,\n    open_input_tokens INTEGER,\n    open_output_tokens INTEGER\n)",
        "\n)",
    )
    assert "run_config" not in old_schema  # guard: the replace actually removed them
    conn = sqlite3.connect(db_path)
    conn.execute(old_schema)
    conn.execute(
        "INSERT INTO runs (id, started_at, duration_s, namespace, mode) "
        "VALUES ('old', '2026-01-01T00:00:00', 1.0, 'boutique', 'eval')"
    )
    conn.commit()
    conn.close()

    new_id = history_store.save_run(
        _result().model_copy(update={"run_config": "claude"}),
        namespace="boutique", workload=None, alert="x", mode="eval",
    )
    assert history_store.get_run(new_id)["run_config"] == "claude"
    old = history_store.get_run("old")
    assert old["run_config"] is None and old["open_input_tokens"] is None


def test_run_config_label_names_the_models_in_use():
    from types import SimpleNamespace

    from sre_agent.agent.graph import _run_config_label

    assert _run_config_label(SimpleNamespace(gather_model="", all_stages_model="")) == "claude"
    assert _run_config_label(SimpleNamespace(gather_model="m", all_stages_model="")) == "gather:m"
    assert _run_config_label(SimpleNamespace(gather_model="m", all_stages_model="m")) == "all:m"
