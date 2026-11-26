"""Dev check of the Spark data store against real S3. Run by a person with dev credentials, not CI.

    AWS_PROFILE=<dev profile> uv run python -m scripts.spark_store_check

With a throwaway user `check_<random>`: builds one task with a step and one typed line, writes them as OpenTelemetry
traces through the backend's own `spark_store.put`, lists and reads the object back, parses every line as OTLP/JSON,
calls `spark_store.forget_account` and asserts the object is gone. Prints PASS/FAIL per step; exits non-zero on the
first FAIL. Never run it against prod: it refuses unless ENVIRONMENT is dev (set below).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import secrets
import sys
import time

os.environ.setdefault("CLERK_ISSUER", "https://check.invalid")  # core.config insists on one; nothing here uses it
os.environ["ENVIRONMENT"] = "dev"
os.environ["SPARK_STORE_ENABLED"] = "1"

from core.observability import spark_store, spark_traces  # noqa: E402


def check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        sys.exit(1)


def objects(user_id: str) -> list[str]:
    s3, found = spark_store._client(), []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=spark_store.store_bucket(), Prefix=f"data/traces/{user_id}/"
    ):
        found += [o["Key"] for o in page.get("Contents", [])]
    return found


async def main() -> None:
    assert spark_store.store_bucket() == "lightbulb-dev-spark-store", "dev bucket only"
    user = f"check_{secrets.token_hex(4)}"
    now_ms = int(time.time() * 1000)
    body = {
        "typed": [{"id": "ln_1", "at": now_ms - 1000, "text": "check line"}],
        "task": {
            "id": "dlg_checkcheck1", "status": "failed", "source": "typed", "createdAt": now_ms - 900,
            "doneAt": now_ms - 100, "request": "check request", "answer": "check answer",
            "steps": [{"kind": "tool", "tool": "click", "app": "Check", "input": "i", "result": "r", "ok": False, "ms": 5}],
        },
    }  # fmt: skip
    turns, steps = spark_store.log_rows(user, "check", body, "problem", time.time_ns() // 1000, {"platform": "mac"})
    await spark_store.put(user, spark_traces.to_otlp(turns, steps), time.monotonic())
    keys = objects(user)
    check(
        len(keys) == 1 and re.fullmatch(rf"data/traces/{user}/\d{{4}}-\d\d-\d\d/[0-9a-f]{{32}}\.jsonl\.gz", keys[0]),
        f"one object: {keys}",
    )
    raw = spark_store._client().get_object(Bucket=spark_store.store_bucket(), Key=keys[0])["Body"].read()
    exports = [json.loads(x) for x in gzip.decompress(raw).decode().splitlines()]
    spans = [sp for e in exports for rs in e["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"]]
    check(
        all(
            re.fullmatch(r"[0-9a-f]{32}", sp["traceId"]) and re.fullmatch(r"[0-9a-f]{16}", sp["spanId"]) for sp in spans
        )
        and all(isinstance(sp["startTimeUnixNano"], str) for sp in spans)
        and {sp["name"] for sp in spans} == {"invoke_agent spark", "execute_tool click", "chat"},
        f"{len(exports)} line(s), {len(spans)} span(s) parse as OTLP/JSON traces",
    )
    spark_store.SECOND_SWEEP_S = 0
    await spark_store.forget_account(user)
    from core.voice.os_voice import _BACKGROUND

    await asyncio.gather(*_BACKGROUND)
    check(objects(user) == [], "objects gone after forget")


if __name__ == "__main__":
    asyncio.run(main())
