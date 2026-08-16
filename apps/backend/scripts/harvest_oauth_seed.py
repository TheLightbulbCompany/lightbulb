"""Harvest an owner's rotated ChatGPT OAuth tokens back into the DDB seed row.

The backend flows that destroy an owner's Litestream replica prefix run this
harvest automatically (litestream_replicas.delete_replica_prefix). This script
is the MANUAL lever for ops flows that delete replicas outside the backend —
the prod-org-reprovision runbook's ``aws s3 rm .../$OWNER/ --recursive`` step.
Run it BEFORE that delete (and after the container has drained), or the next
provision re-stages a spent refresh token and the reprovisioned container is
dead to OpenAI while the UI says Connected.

Run inside the backend ECS task (the env carries the table names, bucket and
litestream binary)::

    cd /app
    uv run python -m scripts.harvest_oauth_seed <owner_id>

Prints what happened; exits 0 on "seed refreshed" AND on the benign no-ops
(no replica, owner not on chatgpt_oauth, seed already current).
"""

import asyncio
import sys

from core.services.oauth_service import harvest_rotated_oauth_seed


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m scripts.harvest_oauth_seed <owner_id>", file=sys.stderr)
        raise SystemExit(2)
    owner_id = sys.argv[1]
    wrote = asyncio.run(harvest_rotated_oauth_seed(owner_id=owner_id))
    print(
        f"seed refreshed from replica for {owner_id}"
        if wrote
        else f"no-op for {owner_id} (no replica / not chatgpt_oauth / seed already current — see logs)"
    )


if __name__ == "__main__":
    main()
