"""Create the Spark data store's Glue database and Iceberg tables with Athena DDL. Idempotent.

    uv run python -m scripts.spark_store_ddl --env dev [--dry-run]

Runs scripts/spark_store_sql/*.sql in name order on the ``lightbulb-{env}-spark-adhoc`` workgroup and waits for each
statement. Firehose only delivers to tables made through the Iceberg Glue catalog, which is what Athena DDL does
(CDK's Glue resources do not). Later schema changes are new numbered files. Shells out to the ``aws`` CLI like
scripts/spark_transcripts.py; the profile comes from the ambient ``AWS_PROFILE``. Exits non-zero on the first failure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SQL_DIR = Path(__file__).parent / "spark_store_sql"


def statements(env: str, sql_dir: Path = SQL_DIR) -> list[tuple[str, str]]:
    """(file name, SQL) in file-name order, placeholders filled; one statement per file."""
    subs = {"{db}": f"lightbulb_{env}_spark", "{bucket}": f"lightbulb-{env}-spark-store"}
    out = []
    for f in sorted(sql_dir.glob("*.sql")):
        sql = f.read_text()
        for k, v in subs.items():
            sql = sql.replace(k, v)
        out.append((f.name, sql.strip()))
    return out


def aws(*args: str) -> dict:
    return json.loads(
        subprocess.run(["aws", *args, "--output", "json"], check=True, capture_output=True, text=True).stdout
    )


def run(sql: str, workgroup: str, timeout: int = 300) -> tuple[str, str]:
    """Run one statement and wait; returns (state, reason)."""
    qid = aws("athena", "start-query-execution", "--work-group", workgroup, "--query-string", sql)["QueryExecutionId"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = aws("athena", "get-query-execution", "--query-execution-id", qid)["QueryExecution"]["Status"]
        if status["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return status["State"], status.get("StateChangeReason", "")
        time.sleep(2)
    return "TIMEOUT", f"query {qid} still running after {timeout}s"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", choices=("dev", "prod"), required=True)
    ap.add_argument("--dry-run", action="store_true", help="print the SQL, call nothing")
    args = ap.parse_args(argv)
    workgroup = f"lightbulb-{args.env}-spark-adhoc"
    for name, sql in statements(args.env):
        if args.dry_run:
            print(f"-- {name}\n{sql};\n")
            continue
        state, reason = run(sql, workgroup)
        print(f"{name}: {state} {reason}".rstrip())
        if state != "SUCCEEDED":
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
