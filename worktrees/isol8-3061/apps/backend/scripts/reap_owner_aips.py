"""Delete every Bedrock application inference profile (AIP) the backend minted for one env.

Nothing mints AIPs any more (Bedrock was removed; managed inference is OpenRouter). This reaps
what is left, INCLUDING orphans whose containers row is long gone -- so it lists AWS, not
DynamoDB:

    uv run python -m scripts.reap_owner_aips --env dev              # dry run: list only
    uv run python -m scripts.reap_owner_aips --env dev --apply
    uv run python -m scripts.reap_owner_aips --env prod --owner-id org_x --apply

A profile is in scope when it is tagged ``ManagedBy=lightbulb-backend`` AND its name carries the
env prefix (``lightbulb-<env>-`` or the pre-rename ``isol8-<env>-``). Dev and prod share one AWS
account, so the prefix is the only env fence; a tagged profile matching neither env is reported
and never deleted. Run it AFTER the fleet ``roll_hermes_runtimes --all --respec`` that drops the
Bedrock runtime env.

Per-owner isolation: profiles are grouped by their ``owner_id`` tag (``<untagged>`` for none) and
each owner is deleted on its own, so one owner's failure never stops the rest. Prints ARNs, owner
ids and tiers only. Exit 1 if any owner failed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict

from core.billing.bedrock_aip import _bedrock_client, delete_owner_aips
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)

MANAGED_BY = "lightbulb-backend"
_UNTAGGED = "<untagged>"


def _list_profiles(client) -> list[dict]:
    profiles, token = [], None
    while True:
        kwargs: dict = {"typeEquals": "APPLICATION", "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_inference_profiles(**kwargs)
        profiles.extend(resp.get("inferenceProfileSummaries", []))
        token = resp.get("nextToken")
        if not token:
            return profiles


def _in_env(name: str, env: str) -> bool:
    return name.startswith((f"lightbulb-{env}-", f"isol8-{env}-"))


def find_env_aips(client, env: str, owner_id: str | None = None) -> dict[str, list[dict]]:
    """``{owner_id: [{"arn", "name", "tier"}]}`` for every in-scope profile of ``env``."""
    found: dict[str, list[dict]] = defaultdict(list)
    for prof in _list_profiles(client):
        arn, name = prof["inferenceProfileArn"], prof.get("inferenceProfileName", "")
        tags = {t["key"]: t["value"] for t in client.list_tags_for_resource(resourceARN=arn).get("tags", [])}
        if tags.get("ManagedBy") != MANAGED_BY:
            continue
        if not _in_env(name, env):
            if not any(_in_env(name, other) for other in ("dev", "prod")):
                logger.warning("skipping %s (%s): managed but matches no env prefix", arn, name)
            continue
        owner = tags.get("owner_id") or _UNTAGGED
        if owner_id and owner != owner_id:
            continue
        found[owner].append({"arn": arn, "name": name, "tier": tags.get("tier", "?")})
    return dict(found)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=("dev", "prod"))
    parser.add_argument("--owner-id", help="reap only this owner's profiles")
    parser.add_argument("--apply", action="store_true", help="delete (default is a dry run)")
    args = parser.parse_args(argv)

    client = _bedrock_client()
    by_owner = await run_in_thread(find_env_aips, client, args.env, args.owner_id)
    failed = 0
    for owner, profiles in sorted(by_owner.items()):
        for p in profiles:
            print(f"{owner} {p['tier']} {p['arn']}")
        if not args.apply:
            continue
        try:
            removed = await delete_owner_aips([p["arn"] for p in profiles])
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the rest
            failed += 1
            logger.error("reap: owner=%s FAILED: %s: %s", owner, type(exc).__name__, exc)
            continue
        print(f"{owner} deleted {len(removed)}")
    total = sum(len(p) for p in by_owner.values())
    mode = "apply" if args.apply else "dry run"
    print(f"{mode}: {total} profile(s) in scope across {len(by_owner)} owner(s), {failed} owner(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
