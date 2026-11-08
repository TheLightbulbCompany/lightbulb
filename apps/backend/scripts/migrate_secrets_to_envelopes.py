"""One-time: move every per-owner Secrets Manager credential into ``credential-envelopes`` and
soft-delete the dead secrets nothing on main reads.

``apps/backend/scripts/migrate_secrets_to_envelopes.py``. Runs INSIDE the backend task (ECS exec), from
``apps/backend``, after the image that reads envelopes for every class is
deployed (gate + settlement Lambdas included):

    uv run python -m scripts.migrate_secrets_to_envelopes --dry-run
    uv run python -m scripts.migrate_secrets_to_envelopes --apply   [--allow-prod]
    uv run python -m scripts.migrate_secrets_to_envelopes --verify  [--allow-prod]

Moves (the envelope's provider is what the readers ask ``integration_secret_store`` for):
    lightbulb/<env>/integrations/<owner>/<provider>      -> (<owner>, <provider>)       JSON as stored
    lightbulb/<env>/openrouter/owners/<owner>/<hash>     -> (<owner>, openrouter/<hash>) {"key": <plaintext>}
Dead (soft-deleted, nothing written): ``DEAD_PATTERNS``.

Per live secret, ``--apply``: read, seal, write the row (an existing row that opens is newer and is
kept), open it back and compare, then DeleteSecret with the default 30-day recovery window. The write is
conditional on the row read, so a concurrent live write wins and that secret is kept. A row that
fails to decrypt for any reason but bad ciphertext (throttling, access) fails that secret untouched.
Per secret ALREADY scheduled for deletion (#5083's sealed ``put`` scheduled the legacy copy): if its
envelope opens, force-delete it; else leave it to expire (it may be a disconnected credential). So a
second ``--apply`` force-deletes what the first soft-deleted, each only once its envelope opens. The
values are never printed. ``--verify`` exits 1 while any class b/c secret is still live or any row
fails to open. Per-owner Hermes boot secrets and BYOK ``user-keys``
are not touched (read outside this store).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys

import boto3
from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidTag

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


def _names(sm) -> list[tuple[str, bool]]:
    """(name, scheduled for deletion) for every secret under this stage, planned deletions included:
    #5083's sealed ``put`` scheduled the legacy copy, and those still need their envelope checked."""
    names, kwargs = (
        [],
        {
            "MaxResults": 100,
            "IncludePlannedDeletion": True,
            "Filters": [{"Key": "name", "Values": [f"lightbulb/{settings.ENVIRONMENT}/"]}],
        },
    )
    while True:
        page = sm.list_secrets(**kwargs)
        names += [(s["Name"], bool(s.get("DeletedDate"))) for s in page.get("SecretList", [])]
        if not page.get("NextToken"):
            return names
        kwargs["NextToken"] = page["NextToken"]


def _row(owner: str, provider: str) -> dict | None:
    return (
        store._envelope_table().get_item(Key={"owner_id": owner, "provider": provider}, ConsistentRead=True).get("Item")
    )


def _opens(row: dict | None) -> bool:
    """Whether the row decrypts. False only for no row or a row KMS / AES-GCM reject as bad
    ciphertext; any other error (throttling, access, network) raises, so that secret fails untouched."""
    if row is None:
        return False
    try:
        store._open(row)
    except InvalidTag:
        return False
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "InvalidCiphertextException":
            return False
        raise
    return True


def migrate_one(sm, name: str, owner: str, provider: str) -> str:
    """Seal the live secret, open it back, then soft-delete the secret (default 30-day window).
    A row that opens is newer and is kept. The write is conditional on the row read (absent, or the
    same bad ciphertext), so a live write in between wins: skipped, and the secret is not deleted."""
    payload = _payload(provider, sm.get_secret_value(SecretId=name)["SecretString"])
    row = _row(owner, provider)
    if _opens(row):
        outcome = "kept-newer-row"
    else:
        if row is None:
            condition = {"ConditionExpression": "attribute_not_exists(owner_id)"}
        else:
            condition = {
                "ConditionExpression": "ciphertext = :read",
                "ExpressionAttributeValues": {":read": row["ciphertext"]},
            }
        try:
            store._envelope_table().put_item(Item=store._seal(owner, provider, json.dumps(payload)), **condition)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return "skipped (written concurrently; secret kept)"
            raise
        row = _row(owner, provider)
        if row is None or store._open(row) != payload:
            raise RuntimeError("sealed row does not open to the secret")
        outcome = "moved"
    sm.delete_secret(SecretId=name)
    return outcome


def retire_scheduled(sm, name: str, owner: str, provider: str) -> str:
    """A secret already scheduled for deletion: force-delete it only when its envelope opens. One
    without such an envelope may be a credential its owner disconnected, so it is left to expire."""
    if not _opens(_row(owner, provider)):
        return "scheduled, no envelope: left to expire"
    try:
        sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
    except sm.exceptions.InvalidRequestException:
        # DeleteSecret lists "The secret is scheduled for deletion" as a cause. No restore (that would
        # make a possibly-disconnected credential readable again): it expires on its own window.
        return "envelope opens; AWS refused the forced delete, left to expire"
    return "force-deleted (envelope opens)"


def run(mode: str, sm=None) -> int:
    sm = sm or boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    env = settings.ENVIRONMENT
    failed = pending = 0
    for name, scheduled in sorted(_names(sm)):
        cls = classify(name, env)
        if cls is None or (scheduled and cls[0] == "dead"):
            continue
        kind, owner, provider = cls
        if mode == "dry-run":
            if scheduled:
                ok = _opens(_row(owner, provider))
                print(f"{name} scheduled, would-{'force-delete' if ok else 'leave-to-expire'}")
            else:
                print(f"{name} would-{'move -> ' + owner + ' ' + provider if kind == 'move' else 'soft-delete'}")
        elif mode == "verify":
            if not scheduled:
                pending += 1
                print(f"{name} still-live ({kind})")
        else:
            try:
                if scheduled:
                    print(f"{name} {retire_scheduled(sm, name, owner, provider)}")
                elif kind == "move":
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
