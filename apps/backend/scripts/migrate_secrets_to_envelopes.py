"""One-time: move every per-owner Secrets Manager credential into ``credential-envelopes`` and
soft-delete the dead secrets nothing on main reads.

Runs INSIDE the backend task (ECS exec), after the image that reads envelopes for every class is
deployed (gate + settlement Lambdas included):

    uv run python -m scripts.migrate_secrets_to_envelopes --dry-run
    uv run python -m scripts.migrate_secrets_to_envelopes --apply   [--allow-prod]
    uv run python -m scripts.migrate_secrets_to_envelopes --verify  [--allow-prod]

Moves (the envelope's provider is what the readers ask ``integration_secret_store`` for):
    lightbulb/<env>/integrations/<owner>/<provider>      -> (<owner>, <provider>)       JSON as stored
    lightbulb/<env>/openrouter/owners/<owner>/<hash>     -> (<owner>, openrouter/<hash>) {"key": <plaintext>}
Dead (soft-deleted, nothing written): ``DEAD_PATTERNS``.

Per secret, ``--apply``: read, seal, write the row (an existing row is newer and is kept), open it
back and compare, then DeleteSecret with the default 30-day recovery window (never forced). The
values are never printed. ``--verify`` exits 1 while any class b/c secret is still live or any row
fails to open. Per-owner Hermes boot secrets and BYOK ``user-keys`` are not touched (read outside
this store).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys

import boto3

from core.config import settings
from core.services.credentials import integration_secret_store as store

logger = logging.getLogger(__name__)

OWNER = r"((?:user|org)_[A-Za-z0-9]+)"
DEAD_PATTERNS = [
    rf"integrations/{OWNER}/apps/.+",  # Apps, deleted in #4975
    rf"integrations/{OWNER}/projects/.+",  # Stripe Projects rail, deleted in #4975
]  # Dead platform singletons sit outside the backend role's delete grant: the lead deletes those by hand.


def classify(name: str, env: str) -> tuple[str, str, str] | None:
    """("move", owner, provider) | ("dead", "", "") | None (stays)."""
    prefix = f"lightbulb/{env}/"
    if not name.startswith(prefix):
        return None
    rest = name[len(prefix) :]
    if any(re.fullmatch(p, rest) for p in DEAD_PATTERNS):
        return ("dead", "", "")
    if m := re.fullmatch(rf"openrouter/owners/{OWNER}/([^/]+)", rest):
        return ("move", m[1], f"openrouter/{m[2]}")
    if m := re.fullmatch(rf"integrations/{OWNER}/(.+)", rest):
        return ("move", m[1], m[2])
    return None


def _payload(provider: str, secret: str) -> dict:
    return {"key": secret} if provider.startswith("openrouter/") else json.loads(secret)


def _live_names(sm) -> list[str]:
    names, kwargs = (
        [],
        {"MaxResults": 100, "Filters": [{"Key": "name", "Values": [f"lightbulb/{settings.ENVIRONMENT}/"]}]},
    )
    while True:
        page = sm.list_secrets(**kwargs)
        names += [s["Name"] for s in page.get("SecretList", []) if not s.get("DeletedDate")]
        if not page.get("NextToken"):
            return names
        kwargs["NextToken"] = page["NextToken"]


def _row(owner: str, provider: str) -> dict | None:
    return (
        store._envelope_table().get_item(Key={"owner_id": owner, "provider": provider}, ConsistentRead=True).get("Item")
    )


def migrate_one(sm, name: str, owner: str, provider: str) -> str:
    payload = _payload(provider, sm.get_secret_value(SecretId=name)["SecretString"])
    row = _row(owner, provider)
    if row is None:
        store._envelope_table().put_item(Item=store._seal(owner, provider, json.dumps(payload)))
        row = _row(owner, provider)
        if row is None or store._open(row) != payload:
            raise RuntimeError("sealed row does not open to the secret")
        outcome = "moved"
    else:
        store._open(row)  # a newer write already sealed it; it must still open
        outcome = "kept-newer-row"
    sm.delete_secret(SecretId=name)  # default 30-day recovery window
    return outcome


def run(mode: str, sm=None) -> int:
    sm = sm or boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    env = settings.ENVIRONMENT
    failed = pending = 0
    for name in sorted(_live_names(sm)):
        cls = classify(name, env)
        if cls is None:
            continue
        kind, owner, provider = cls
        if mode == "dry-run":
            print(f"{name} would-{'move -> ' + owner + ' ' + provider if kind == 'move' else 'soft-delete'}")
        elif mode == "verify":
            pending += 1
            print(f"{name} still-live ({kind})")
        else:
            try:
                if kind == "move":
                    print(f"{name} {migrate_one(sm, name, owner, provider)}")
                else:
                    sm.delete_secret(SecretId=name)
                    print(f"{name} soft-deleted")
            except Exception as exc:  # noqa: BLE001 -- one secret's failure must not stop the rest
                failed += 1
                logger.error("migrate: %s FAILED: %s", name, type(exc).__name__)
    if mode == "verify":
        failed += _verify_rows()
    return 1 if failed or pending else 0


def _verify_rows() -> int:
    bad, kwargs = 0, {}
    while True:
        page = store._envelope_table().scan(**kwargs)
        for row in page.get("Items", []):
            try:
                store._open(row)
            except Exception as exc:  # noqa: BLE001
                bad += 1
                logger.error("verify: %s/%s does not open: %s", row["owner_id"], row["provider"], type(exc).__name__)
        if not page.get("LastEvaluatedKey"):
            return bad
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("--dry-run", "--apply", "--verify"):
        mode.add_argument(flag, dest="mode", action="store_const", const=flag[2:])
    parser.add_argument("--allow-prod", action="store_true")
    args = parser.parse_args(argv)
    if not settings.ENVIRONMENT or not settings.CREDENTIAL_KMS_KEY_ARN:
        print("ENVIRONMENT and CREDENTIAL_KMS_KEY_ARN must be set (run inside the backend task)", file=sys.stderr)
        return 2
    if settings.ENVIRONMENT == "prod" and args.mode != "dry-run" and not args.allow_prod:
        print("refusing prod without --allow-prod", file=sys.stderr)
        return 2
    return run(args.mode)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
