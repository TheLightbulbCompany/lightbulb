"""Dev check of the Spark data store, end to end, against real AWS. Run by a person, not by CI.

    uv run python -m scripts.spark_store_check --env dev

With a throwaway user `check_<random>`: puts one turn, one task (task row + 2 steps) and one event through the real
Firehose streams, waits until Athena returns them, runs the backend's own `spark_store_jobs.forget_pass` (compacting,
on the `lightbulb-{env}-spark-jobs` workgroup) and asserts zero rows are left. Firehose puts shell out to the ``aws``
CLI like scripts/spark_store_ddl.py; Athena goes through the backend code (boto3, so it needs the ambient
``AWS_PROFILE`` credentials). Prints PASS/FAIL per step; exits non-zero on the first FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta

os.environ.setdefault("CLERK_ISSUER", "https://check.invalid")  # core.config insists on one; nothing here uses it

from core.observability import spark_store, spark_store_jobs  # noqa: E402
from scripts.spark_store_ddl import aws  # noqa: E402

POLL_S, POLL_MAX_S = 15, 300
STREAMS = {"turns": "turns", "task_steps": "steps", "events": "events"}  # table -> stream suffix


def rows_for(user_id: str, now_us: int) -> dict[str, list[dict]]:
    """The synthetic rows per table; all text is fixed words, nothing real."""
    base = {"ts": now_us, "user_id": user_id, "platform": "check", "app_version": "0"}
    step = base | {"session_id": "check", "source": "typed", "task_id": f"dlg_{secrets.token_hex(6)}"}
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


def count_sql(table: str, user_id: str) -> tuple[str, list]:
    return (
        f"SELECT count(*) FROM {spark_store.db()}.{table} WHERE user_bucket = ? AND user_id = ?",
        [spark_store.bucket(user_id), user_id],
    )


async def _counts(user_id: str, tables) -> dict[str, int]:
    out = {}
    for t in tables:
        sql, params = count_sql(t, user_id)
        rows = await spark_store.query(
            sql, params, workgroup=f"lightbulb-{spark_store.settings.ENVIRONMENT}-spark-jobs"
        )
        out[t] = int(rows[0]["_col0"])
    return out


def put(env: str, table: str, rows: list[dict]) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
        json.dump(records(rows), f)
        f.flush()
        out = aws("firehose", "put-record-batch", "--delivery-stream-name", f"lightbulb-{env}-spark-{STREAMS[table]}",
                  "--cli-binary-format", "base64", "--records", f"file://{f.name}")  # fmt: skip
    return out["FailedPutCount"] == 0


async def check(env: str, user_id: str) -> int:
    started = datetime.now(UTC)
    rows = rows_for(user_id, int(started.timestamp() * 1_000_000))
    wanted = {t: len(r) for t, r in rows.items()}
    for table, rs in rows.items():
        ok = put(env, table, rs)
        print(f"{'PASS' if ok else 'FAIL'} put {table}")
        if not ok:
            return 1
    deadline, seen = time.monotonic() + POLL_MAX_S, {}
    while time.monotonic() < deadline and seen != wanted:
        await asyncio.sleep(POLL_S)
        seen = await _counts(user_id, wanted)
    print(f"{'PASS' if seen == wanted else 'FAIL'} rows visible {seen} (wanted {wanted})")
    if seen != wanted:
        return 1
    left = await spark_store_jobs.forget_pass(user_id, True, started + timedelta(minutes=5))
    after = await _counts(user_id, wanted)
    ok = left == 0 and not any(after.values())
    print(f"{'PASS' if ok else 'FAIL'} forget pass (compacting) left {left}; per table {after}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", choices=("dev", "prod"), required=True)
    args = ap.parse_args(argv)
    before, spark_store.settings.ENVIRONMENT = spark_store.settings.ENVIRONMENT, args.env  # db() and names read it
    try:
        return asyncio.run(check(args.env, f"check_{secrets.token_hex(6)}"))
    except (subprocess.CalledProcessError, RuntimeError, TimeoutError) as e:
        print(f"FAIL {type(e).__name__}: {getattr(e, 'stderr', None) or e}", file=sys.stderr)
        return 1
    finally:
        spark_store.settings.ENVIRONMENT = before


if __name__ == "__main__":
    sys.exit(main())
