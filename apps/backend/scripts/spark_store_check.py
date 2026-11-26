"""Dev check of the Spark data store against real S3. Run by a person with dev credentials, not by CI.

    AWS_PROFILE=<dev profile> uv run python -m scripts.spark_store_check

With a throwaway user `check_<random>`: writes one turn and one task (task row plus a step) through the backend's
own `spark_store.put`, lists and reads the objects back, calls `spark_store.forget` and asserts both are gone.
Prints PASS/FAIL per step; exits non-zero on the first FAIL.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import secrets
import sys
import time

os.environ.setdefault("CLERK_ISSUER", "https://check.invalid")  # core.config insists on one; nothing here uses it
os.environ["ENVIRONMENT"] = "dev"
os.environ["SPARK_STORE_ENABLED"] = "1"

from core.observability import spark_store  # noqa: E402


def check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        sys.exit(1)


def objects(user_id: str) -> list[str]:
    s3 = spark_store._client()
    found = []
    for table in spark_store.TABLES:
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=spark_store.store_bucket(), Prefix=f"data/{table}/"
        ):
            found += [o["Key"] for o in page.get("Contents", []) if f"/{user_id}__" in o["Key"]]
    return found  # fine for a check: the throwaway user's day is small


async def main() -> None:
    user = f"check_{secrets.token_hex(4)}"
    now_us = time.time_ns() // 1000
    base = {"ts": now_us, "user_id": user, "platform": "check", "app_version": "0"}
    await spark_store.put(
        "turns", user, "t1", [base | {"row_id": "t", "role": "person", "source": "typed", "text": "check turn"}]
    )
    await spark_store.put("task_steps", user, "k1", [base | {"row_id": f"s{n}", "step_no": n} for n in range(2)])
    keys = objects(user)
    check(len(keys) == 2, f"two objects written: {keys}")
    for key in keys:
        body = spark_store._client().get_object(Bucket=spark_store.store_bucket(), Key=key)["Body"].read()
        rows = [json.loads(x) for x in gzip.decompress(body).decode().splitlines()]
        check(
            all(r["user_id"] == user and isinstance(r["ts"], str) for r in rows),
            f"{key}: {len(rows)} line(s) read back",
        )
    spark_store.SECOND_SWEEP_S = 0
    spark_store.forget(user)
    from core.voice.os_voice import _BACKGROUND

    await asyncio.gather(*_BACKGROUND)
    check(objects(user) == [], "objects gone after forget")


if __name__ == "__main__":
    asyncio.run(main())
