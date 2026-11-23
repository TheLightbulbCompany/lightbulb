"""Print the last N days of saved Spark voice transcripts, grouped by session. Read-only.

    uv run python -m scripts.spark_transcripts --env dev --days 7

Each person shows as a short stable hash of their user id, never a name or email. Shells out to the ``aws``
CLI (boto3's SSO refresh is broken in this venv); the profile comes from the ambient ``AWS_PROFILE``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from collections import defaultdict
from datetime import UTC, datetime


def person(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()[:8]


def plain(item: dict) -> dict:
    """DynamoDB JSON ({"S": ...} / {"N": ...}) to plain values."""
    return {k: int(v["N"]) if "N" in v else v.get("S") for k, v in item.items()}


def render(items: list[dict]) -> str:
    sessions: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in items:
        sessions[(item["sk"].split("#", 2)[0], item["sessionId"])].append(item)
    out = []
    for (started, sid), turns in sorted(sessions.items()):
        when = datetime.fromtimestamp(int(started), UTC).strftime("%Y-%m-%d %H:%M UTC")
        out.append(f"== {when}  person {person(turns[0]['user_id'])}  {sid}")
        out += [f"  {t['role']:>5}: {t['text']}" for t in sorted(turns, key=lambda t: t["sk"])]
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", choices=("dev", "prod"), required=True)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    since = int(time.time()) - args.days * 86400
    raw = subprocess.run(
        [
            "aws", "dynamodb", "scan", "--output", "json",
            "--table-name", f"lightbulb-{args.env}-spark-transcripts",
            "--filter-expression", "#at >= :since",
            "--expression-attribute-names", '{"#at": "at"}',
            "--expression-attribute-values", json.dumps({":since": {"N": str(since)}}),
        ],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    print(render([plain(i) for i in json.loads(raw).get("Items", [])]))


if __name__ == "__main__":
    main()
