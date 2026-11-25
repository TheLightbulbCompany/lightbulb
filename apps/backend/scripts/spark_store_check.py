"""Dev check of the Spark data store, end to end, against real AWS. Run by a person, not by CI.

    uv run python -m scripts.spark_store_check --env dev [--dry-run]

With a throwaway user `check_<random>`: puts one turn, one task (task row + 2 steps) and one event through the real
Firehose streams, polls Athena until each table returns its row, runs the delete pass (compacting) and asserts zero
rows for that user in all three tables. The `errors/` sweep is not part of it (it needs only S3). Shells out to the
``aws`` CLI like scripts/spark_store_ddl.py (profile from the ambient ``AWS_PROFILE``) and runs its SQL on the
``lightbulb-{env}-spark-adhoc`` workgroup. Prints PASS/FAIL per step; exits non-zero on the first FAIL.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("CLERK_ISSUER", "https://check.invalid")  # core.config insists on one; nothing here uses it

from core.observability import spark_store, spark_store_jobs  # noqa: E402
from scripts.spark_store_ddl import aws  # noqa: E402

POLL_S, POLL_MAX_S = 15, 300
STREAMS = {"turns": "turns", "task_steps": "steps", "events": "events"}  # table -> stream suffix


def rows_for(user_id: str, now_us: int) -> dict[str, list[dict]]:
    """The synthetic rows per table; all text is fixed words, nothing real."""
    base = {"ts": now_us, "user_id": user_id, "platform": "check", "app_version": "0"}
    task = f"dlg_{secrets.token_hex(6)}"
    step = base | {"session_id": "check", "source": "typed", "task_id": task}
    return {
        "turns": [base | {"row_id": f"{user_id}:t", "session_id": "check", "role": "person", "source": "typed", "text": "check turn"}],
        "task_steps": [
            step | {"row_id": f"{user_id}:s{n}", "step_no": n, "kind": "task" if n == 0 else "tool", "text_kept": True}
            for n in range(3)
        ],
        "events": [
            {"row_id": f"{user_id}:e", "ts": now_us, "distinct_id": user_id, "user_id": user_id, "event": "check", "properties": "{}"}
        ],
    }  # fmt: skip


def records(rows: list[dict]) -> list[dict]:
    """Firehose records for the CLI (Data base64), built by the same code the backend uses."""
    return [{"Data": base64.b64encode(spark_store._record(r)["Data"]).decode()} for r in rows]


def count_sql(env_db: str, table: str, user_id: str) -> tuple[str, list[str]]:
    return f"SELECT count(*) FROM {env_db}.{table} WHERE user_bucket = ? AND user_id = ?", [
        str(spark_store.bucket(user_id)),
        f"'{user_id}'",
    ]


def _athena(sql: str, params: list[str], workgroup: str) -> list[list[str]]:
    args = ["athena", "start-query-execution", "--work-group", workgroup, "--query-string", sql]
    if params:
        args += ["--execution-parameters", *params]
    qid = aws(*args)["QueryExecutionId"]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        st = aws("athena", "get-query-execution", "--query-execution-id", qid)["QueryExecution"]["Status"]
        if st["State"] == "SUCCEEDED":
            res = aws("athena", "get-query-results", "--query-execution-id", qid)
            return [[c.get("VarCharValue", "") for c in r["Data"]] for r in res["ResultSet"]["Rows"][1:]]
        if st["State"] in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"{st['State']}: {st.get('StateChangeReason', '')}")
        time.sleep(2)
    raise TimeoutError(qid)


def _count(db: str, table: str, user_id: str, wg: str) -> int:
    sql, params = count_sql(db, table, user_id)
    return int(_athena(sql, params, wg)[0][0])


def plan(env: str, user_id: str, now_us: int) -> list[str]:
    """What a real run does, as lines (for --dry-run)."""
    db = f"lightbulb_{env}_spark"
    out = [f"user id: {user_id} (bucket {spark_store.bucket(user_id)})"]
    for table, rows in rows_for(user_id, now_us).items():
        out.append(f"put {len(rows)} row(s) to firehose stream lightbulb-{env}-spark-{STREAMS[table]}")
    out += [
        f"poll (every {POLL_S}s, up to {POLL_MAX_S}s) until each of {db}.{{turns,task_steps,events}} has the user's rows"
    ]
    for sql, params in [*spark_store_jobs.deletes(user_id), *spark_store_jobs.compaction(user_id)]:
        out.append(f"run {sql}  params={params}")
    out.append("assert count(*) = 0 for the user in all three tables")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", choices=("dev", "prod"), required=True)
    ap.add_argument("--dry-run", action="store_true", help="print what would be done, call nothing")
    args = ap.parse_args(argv)
    user_id, now_us = f"check_{secrets.token_hex(6)}", time.time_ns() // 1000
    before, spark_store.settings.ENVIRONMENT = spark_store.settings.ENVIRONMENT, args.env  # db() and the SQL read it
    try:
        return _main(args, user_id, now_us)
    finally:
        spark_store.settings.ENVIRONMENT = before


def _main(args, user_id: str, now_us: int) -> int:
    if args.dry_run:
        print("\n".join(plan(args.env, user_id, now_us)))
        return 0
    db, wg = spark_store.db(), f"lightbulb-{args.env}-spark-adhoc"
    wanted = {t: len(r) for t, r in (rows_for(user_id, now_us)).items()}
    try:
        for table, rows in rows_for(user_id, now_us).items():
            with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
                json.dump(records(rows), f)
                f.flush()
                out = aws("firehose", "put-record-batch", "--delivery-stream-name", f"lightbulb-{args.env}-spark-{STREAMS[table]}",
                          "--cli-binary-format", "base64", "--records", f"file://{f.name}")  # fmt: skip
            ok = out["FailedPutCount"] == 0
            print(f"{'PASS' if ok else 'FAIL'} put {table}")
            if not ok:
                return 1
        deadline, seen = time.monotonic() + POLL_MAX_S, {}
        while time.monotonic() < deadline and seen != wanted:
            time.sleep(POLL_S)
            seen = {t: _count(db, t, user_id, wg) for t in wanted}
        print(f"{'PASS' if seen == wanted else 'FAIL'} rows visible {seen} (wanted {wanted})")
        if seen != wanted:
            return 1
        for sql, params in [*spark_store_jobs.deletes(user_id), *spark_store_jobs.compaction(user_id)]:
            _athena(sql, [spark_store._literal(p) for p in params], wg)
        print("PASS delete pass (compacting) ran")
        left = {t: _count(db, t, user_id, wg) for t in wanted}
        print(f"{'PASS' if not any(left.values()) else 'FAIL'} zero rows left {left}")
        return 0 if not any(left.values()) else 1
    except (subprocess.CalledProcessError, RuntimeError, TimeoutError) as e:
        print(f"FAIL {type(e).__name__}: {getattr(e, 'stderr', None) or e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
