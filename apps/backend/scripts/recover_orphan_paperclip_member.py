"""Recover an org member wedged by an orphan Paperclip Better Auth user.

Scenario: ``provision_member`` failed after the member's Better Auth
sign-up (e.g. the redundant approve 409 that this PR fixes), discarding
the member's stored paperclip_user_id + password and leaving their Better
Auth user orphaned. Subsequent attempts then re-sign-up and 422 against
the now-existing user, so the member is permanently wedged with a
``failed`` DDB row (``/teams`` shows "not provisioned"). Paperclip exposes
no user-delete/reset, so the only recovery is to delete the orphan Aurora
``user`` row (cascades account/session) and the failed DDB row; the next
``/container/status`` poll re-provisions the member cleanly.

Dry-run by default. Pass ``--apply`` to mutate. Requires AWS SSO
(``aws sso login --profile isol8-admin``).

    cd apps/backend && uv run python -m scripts.recover_orphan_paperclip_member \
        --env prod --email member@example.com            # dry run
    cd apps/backend && uv run python -m scripts.recover_orphan_paperclip_member \
        --env prod --email member@example.com --owner-id org_xxx --apply
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import boto3

# Bootstrap admins that must NEVER be deleted (they own the
# PaperclipAdminClient session; deleting one breaks provisioning).
BOOTSTRAP_EMAILS = {"admin@isol8.co", "admin+paperclip@isol8.co"}

ACCOUNT_ID = "877352799272"
REGION = "us-east-1"


@dataclass
class RecoveryPlan:
    email: str
    aurora_user_id: str
    ddb_user_ids: list[str] = field(default_factory=list)


def plan_recovery(
    *,
    email: str,
    aurora_rows: list[dict],
    ddb_failed_rows: list[dict],
) -> RecoveryPlan:
    """Pure decision logic — no AWS. ``SystemExit`` on any unsafe shape.

    ``aurora_rows`` is ``[{"id", "email"}, ...]`` from the Better Auth
    ``user`` table; ``ddb_failed_rows`` is ``[{"user_id", "status"}, ...]``
    pre-filtered to the org (caller scopes by owner_id). We delete exactly
    one orphan Aurora user and the member's ``failed`` DDB rows.
    """
    if email in BOOTSTRAP_EMAILS:
        sys.exit(f"REFUSING: {email} is a bootstrap admin and must be preserved.")
    matches = [r for r in aurora_rows if r.get("email") == email]
    if len(matches) == 0:
        sys.exit(f"No Aurora Better Auth user found for {email}; nothing to recover.")
    if len(matches) > 1:
        sys.exit(f"REFUSING: {len(matches)} Aurora users share email {email}; resolve by hand.")
    return RecoveryPlan(
        email=email,
        aurora_user_id=matches[0]["id"],
        ddb_user_ids=[r["user_id"] for r in ddb_failed_rows if r.get("status") == "failed"],
    )


def _secret_arn(env: str) -> str:
    sm = boto3.client("secretsmanager", region_name=REGION)
    for s in sm.list_secrets()["SecretList"]:
        if f"isol8-{env}-paperclip-db-credentials" in s["Name"]:
            return s["ARN"]
    sys.exit(f"Could not find paperclip-db-credentials secret for env={env}")


def _aurora(env: str):
    return (
        boto3.client("rds-data", region_name=REGION),
        f"arn:aws:rds:{REGION}:{ACCOUNT_ID}:cluster:isol8-{env}-paperclip-db",
        _secret_arn(env),
    )


def _aurora_users(client, db_arn, secret_arn) -> list[dict]:
    resp = client.execute_statement(
        resourceArn=db_arn,
        secretArn=secret_arn,
        database="paperclip",
        sql='SELECT id, email FROM "user" ORDER BY email',
    )
    return [
        {"id": rec[0].get("stringValue"), "email": rec[1].get("stringValue")}
        for rec in resp.get("records", [])
    ]


def _ddb_failed_rows(env: str, owner_id: str | None) -> list[dict]:
    ddb = boto3.client("dynamodb", region_name=REGION)
    resp = ddb.scan(TableName=f"isol8-{env}-paperclip-companies")
    out = []
    for it in resp.get("Items", []):
        row = {
            "user_id": it["user_id"]["S"],
            "status": it.get("status", {}).get("S"),
            "org_id": it.get("org_id", {}).get("S"),
        }
        if row["status"] == "failed" and (owner_id is None or row["org_id"] == owner_id):
            out.append(row)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--email", required=True)
    ap.add_argument("--owner-id", default=None, help="owner id to scope the failed DDB rows")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    client, db_arn, secret_arn = _aurora(args.env)
    plan = plan_recovery(
        email=args.email,
        aurora_rows=_aurora_users(client, db_arn, secret_arn),
        ddb_failed_rows=_ddb_failed_rows(args.env, args.owner_id),
    )

    print(f"Aurora user to DELETE    : {plan.aurora_user_id} ({plan.email})")
    print(f"DDB failed rows to DELETE: {plan.ddb_user_ids or '(none)'}")
    if not args.apply:
        print("\nDRY RUN — re-run with --apply to execute.")
        return

    client.execute_statement(
        resourceArn=db_arn,
        secretArn=secret_arn,
        database="paperclip",
        sql='DELETE FROM "user" WHERE id = :id',
        parameters=[{"name": "id", "value": {"stringValue": plan.aurora_user_id}}],
    )
    print(f"Deleted Aurora user {plan.aurora_user_id}")

    ddb = boto3.client("dynamodb", region_name=REGION)
    for uid in plan.ddb_user_ids:
        ddb.delete_item(
            TableName=f"isol8-{args.env}-paperclip-companies",
            Key={"user_id": {"S": uid}},
        )
        print(f"Deleted DDB row {uid}")

    print("\nDone. Ask the member to reload /teams — member-join re-provisions cleanly.")


if __name__ == "__main__":
    main()
