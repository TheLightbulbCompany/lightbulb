import base64
import json
import subprocess

from core.observability import spark_store
from scripts import spark_store_check

NOW = 1_732_500_000_123_456


def test_rows_cover_the_three_tables_with_one_user():
    rows = spark_store_check.rows_for("check_ab", NOW)
    assert {t: len(r) for t, r in rows.items()} == {"turns": 1, "task_steps": 3, "events": 1}
    assert {r["user_id"] for rs in rows.values() for r in rs} == {"check_ab"}
    assert len({r["row_id"] for rs in rows.values() for r in rs}) == 5
    assert [r["step_no"] for r in rows["task_steps"]] == [0, 1, 2]


def test_records_are_base64_json_lines_with_dt_and_bucket():
    rec = spark_store_check.records(spark_store_check.rows_for("check_ab", NOW)["turns"])[0]
    row = json.loads(base64.b64decode(rec["Data"]))
    assert row["dt"] == "2024-11-25" and row["user_bucket"] == spark_store.bucket("check_ab")
    assert base64.b64decode(rec["Data"]).endswith(b"\n")


def test_count_sql_is_parameterised_and_quoted():
    sql, params = spark_store_check.count_sql("db", "turns", "check_ab")
    assert "?" in sql and params == [str(spark_store.bucket("check_ab")), "'check_ab'"]


def test_dry_run_prints_the_plan_and_calls_nothing(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("subprocess called")

    monkeypatch.setattr(subprocess, "run", boom)
    assert spark_store_check.main(["--env", "dev", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "lightbulb-dev-spark-steps" in out and "DELETE FROM lightbulb_dev_spark.events" in out
    assert "VACUUM lightbulb_dev_spark.turns" in out and "check_" in out
