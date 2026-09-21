"""Recover an org member wedged by an orphan Paperclip Better Auth user.

Scenario: ``provision_member`` failed after the member's Better Auth
sign-up (e.g. the redundant approve 409, since fixed), discarding
the member's stored paperclip_user_id + password and leaving their Better
Auth user orphaned. Subsequent attempts then re-sign-up and 422 against
the now-existing user, so the member is permanently wedged with a
``failed`` DDB row (``/teams`` shows "not provisioned"). Paperclip exposes
no user-delete/reset, so the only recovery is to delete the orphan Aurora
``user`` row (cascades account/session) and the failed DDB row; the next
``/container/status`` poll re-provisions the member cleanly. Identities with
ANY company membership are refused: recover their existing credentials
instead, preserving their identity and workspace attribution.

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
# PaperclipAdminClient session; deleting one breaks provisioning). Imported,
# not re-declared — see the constant's own comment for why both addresses
# are in it.
from core.services.paperclip_user_delete import BOOTSTRAP_EMAILS

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
    member_user_id: str | None = None,
) -> RecoveryPlan:
    """Pure decision logic — no AWS. ``SystemExit`` on any unsafe shape.

    ``aurora_rows`` is ``[{"id", "email", "membership_count"}, ...]`` from the Better Auth
    ``user`` table; ``ddb_failed_rows`` is ``[{"user_id", "status"}, ...]``
    pre-filtered to the org (caller scopes by owner_id).

    Aurora side: delete exactly one orphan user, matched by email (refuse
    on zero or multiple matches).

    DDB side — only ever delete the explicitly named member's row:
      The DDB ``paperclip-companies`` row is keyed by Clerk user_id and
      carries no email; the orphan's empty ``paperclip_user_id`` can't link
      it back to the email-matched Aurora user. So there is NO safe way to
      correlate a failed DDB row to ``email`` — selecting "the lone failed
      row" could delete an UNRELATED member's row (e.g. the target has only
      an Aurora orphan while a different member is the sole failed row, or
      ``--owner-id`` was mistyped). Therefore:
      - any failed rows in scope → ``member_user_id`` is REQUIRED, and we
        delete only the row whose user_id == member_user_id (a no-op if that
        member has no failed row — Aurora-only recovery).
      - no failed rows in scope → Aurora-only recovery, ``member_user_id``
        not required.
    """
    if email.strip().lower() in BOOTSTRAP_EMAILS:
        sys.exit(f"REFUSING: {email} is a bootstrap admin and must be preserved.")
    matches = [r for r in aurora_rows if r.get("email") == email]
    if len(matches) == 0:
        sys.exit(f"No Aurora Better Auth user found for {email}; nothing to recover.")
    if len(matches) > 1:
        sys.exit(f"REFUSING: {len(matches)} Aurora users share email {email}; resolve by hand.")

    if matches[0].get("membership_count", 0):
        sys.exit(
            "REFUSING: Paperclip identity has company memberships; recover its credentials instead of deleting it."
        )

    failed = [r["user_id"] for r in ddb_failed_rows if r.get("status") == "failed"]
    if failed and member_user_id is None:
        # Refuse rather than guess: a failed DDB row can't be correlated to
        # the email, so we never delete one without an explicit Clerk id.
        sys.exit(
            f"REFUSING: {len(failed)} failed DDB row(s) in scope but no --user-id given. "
            "A failed paperclip-companies row carries no email and can't be linked to "
            f"{email}, so pass the recovering member's Clerk user_id explicitly. "
            "(Aurora-only recovery, with no DDB delete, runs without --user-id only "
            "when there are zero failed rows in scope.)"
        )
    ddb_user_ids = [uid for uid in failed if uid == member_user_id] if member_user_id is not None else []

    return RecoveryPlan(
        email=email,
        aurora_user_id=matches[0]["id"],
        ddb_user_ids=ddb_user_ids,
    )


def _secret_arn(env: str) -> str:
    # Paginate: list_secrets returns one page + a NextToken. With enough
    # secrets in the account, the target can be on a later page — a single
    # call would falsely report "not found" and block the recovery runbook.
    sm = boto3.client("secretsmanager", region_name=REGION)
    needle = f"lightbulb-{env}-paperclip-db-credentials"
    for page in sm.get_paginator("list_secrets").paginate():
        for s in page.get("SecretList", []):
            if needle in s["Name"]:
                return s["ARN"]
    sys.exit(f"Could not find paperclip-db-credentials secret for env={env}")


def _aurora(env: str):
    return (
        boto3.client("rds-data", region_name=REGION),
        f"arn:aws:rds:{REGION}:{ACCOUNT_ID}:cluster:lightbulb-{env}-paperclip-db",
        _secret_arn(env),
    )


def _aurora_users(client, db_arn, secret_arn) -> list[dict]:
    resp = client.execute_statement(
        resourceArn=db_arn,
        secretArn=secret_arn,
        database="paperclip",
        sql=(
            "SELECT u.id, u.email, (SELECT COUNT(*) FROM company_memberships m "
            "WHERE m.principal_type = 'user' AND m.principal_id = u.id) "
            'FROM "user" u ORDER BY u.email'
        ),
    )
    return [
        {"id": rec[0]["stringValue"], "email": rec[1]["stringValue"], "membership_count": rec[2]["longValue"]}
        for rec in resp.get("records", [])
    ]


def _ddb_failed_rows(env: str, owner_id: str | None) -> list[dict]:
    ddb = boto3.client("dynamodb", region_name=REGION)
    table = f"lightbulb-{env}-paperclip-companies"
    out = []
    # Paginate: scan returns one page + a LastEvaluatedKey. The disambiguation
    # guard in plan_recovery depends on seeing EVERY failed row, so a partial
    # first page could make a multi-failed-row env look single and bypass the
    # --user-id refusal.
    scan_kwargs: dict = {"TableName": table}
    while True:
        resp = ddb.scan(**scan_kwargs)
        for it in resp.get("Items", []):
            row = {
                "user_id": it["user_id"]["S"],
                "status": it.get("status", {}).get("S"),
                "org_id": it.get("org_id", {}).get("S"),
            }
            if row["status"] == "failed" and (owner_id is None or row["org_id"] == owner_id):
                out.append(row)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--email", required=True)
    ap.add_argument("--owner-id", default=None, help="owner id to scope the failed DDB rows")
    ap.add_argument(
        "--user-id",
        default=None,
        help="the recovering member's Clerk user_id; scopes the DDB deletion to that member "
        "(required when the org has more than one failed row)",
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    client, db_arn, secret_arn = _aurora(args.env)
    plan = plan_recovery(
        email=args.email,
        aurora_rows=_aurora_users(client, db_arn, secret_arn),
        ddb_failed_rows=_ddb_failed_rows(args.env, args.owner_id),
        member_user_id=args.user_id,
    )

    print(f"Aurora user to DELETE    : {plan.aurora_user_id} ({plan.email})")
    print(f"DDB failed rows to DELETE: {plan.ddb_user_ids or '(none)'}")
    if not args.apply:
        print("\nDRY RUN — re-run with --apply to execute.")
        return

    deleted = client.execute_statement(
        resourceArn=db_arn,
        secretArn=secret_arn,
        database="paperclip",
        sql=(
            'DELETE FROM "user" WHERE id = :id AND NOT EXISTS '
            "(SELECT 1 FROM company_memberships WHERE principal_type = 'user' AND principal_id = :id)"
        ),
        parameters=[{"name": "id", "value": {"stringValue": plan.aurora_user_id}}],
    )
    if deleted.get("numberOfRecordsUpdated") != 1:
        sys.exit("REFUSING: Aurora identity disappeared or gained membership; DDB rows were preserved.")
    print(f"Deleted Aurora user {plan.aurora_user_id}")

    ddb = boto3.client("dynamodb", region_name=REGION)
    for uid in plan.ddb_user_ids:
        ddb.delete_item(
            TableName=f"lightbulb-{args.env}-paperclip-companies",
            Key={"user_id": {"S": uid}},
        )
        print(f"Deleted DDB row {uid}")

    print("\nDone. Ask the member to reload /teams — member-join re-provisions cleanly.")


if __name__ == "__main__":
    main()
