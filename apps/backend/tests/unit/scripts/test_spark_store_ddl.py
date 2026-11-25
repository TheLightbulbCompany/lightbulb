import subprocess

from scripts import spark_store_ddl


def test_statements_in_order_with_placeholders_filled():
    stmts = spark_store_ddl.statements("dev")
    assert [n for n, _ in stmts] == ["001_database.sql", "002_turns.sql", "003_task_steps.sql", "004_events.sql"]
    sql = "\n".join(s for _, s in stmts)
    assert "{" not in sql
    assert "CREATE DATABASE IF NOT EXISTS lightbulb_dev_spark" in sql
    assert "LOCATION 's3://lightbulb-dev-spark-store/warehouse/task_steps/'" in sql
    for _, s in stmts:
        assert s.count(";") == 0 and "EXTERNAL" not in s and "timestamp(" not in s
        if "CREATE TABLE" in s:
            assert "IF NOT EXISTS" in s and "PARTITIONED BY (dt, user_bucket)" in s


def test_prod_names():
    assert "lightbulb_prod_spark" in spark_store_ddl.statements("prod")[0][1]


def test_dry_run_makes_no_subprocess_call(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("subprocess called")

    monkeypatch.setattr(subprocess, "run", boom)
    assert spark_store_ddl.main(["--env", "dev", "--dry-run"]) == 0
    assert "CREATE TABLE IF NOT EXISTS lightbulb_dev_spark.events" in capsys.readouterr().out


def test_stops_and_exits_nonzero_on_first_failure(monkeypatch, capsys):
    calls = []

    def fake_run(sql, workgroup):
        calls.append(workgroup)
        return ("FAILED", "boom") if len(calls) == 2 else ("SUCCEEDED", "")

    monkeypatch.setattr(spark_store_ddl, "run", fake_run)
    assert spark_store_ddl.main(["--env", "dev"]) == 1
    assert calls == ["lightbulb-dev-spark-adhoc"] * 2
    assert "002_turns.sql: FAILED boom" in capsys.readouterr().out


def test_aws_cli_failure_prints_stderr_and_returns_1(monkeypatch, capsys):
    def fail(*a, **k):
        raise subprocess.CalledProcessError(254, ["aws"], stderr="AccessDeniedException: no athena")

    monkeypatch.setattr(subprocess, "run", fail)
    assert spark_store_ddl.main(["--env", "dev"]) == 1
    assert "AccessDeniedException: no athena" in capsys.readouterr().err
