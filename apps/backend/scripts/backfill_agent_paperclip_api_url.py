"""One-time backfill: set the per-agent ``adapterConfig.paperclipApiUrl`` override
on Paperclip agents that are missing it.

WHY. The bundled ``paperclip`` skill curls ``$PAPERCLIP_API_URL`` at wake. The
openclaw-gateway adapter injects that from ``adapterConfig.paperclipApiUrl`` when
the per-agent override is present, and otherwise falls back to its default —
``origin(PAPERCLIP_PUBLIC_URL)`` = ``company.isol8.co``, which is NXDOMAIN (the
public host was retired; the string survives only as Better Auth's origin
identity). Agents created BEFORE the override shipped (``paperclip_adapter_config``
began stamping ``paperclipApiUrl`` = ``PAPERCLIP_INTERNAL_URL``) have the field
NULL, so their skill's curl path dies on DNS. Agents provisioned since already
carry it, so this is a legacy-data gap, not an ongoing one. Fixes issue #1276.

WHAT. Sets ``adapterConfig.paperclipApiUrl`` to the in-VPC Cloud Map host every
healthy agent already uses (``http://paperclip.isol8-{env}.local:3100``) on every
row where it is NULL. The value is read from an existing agent that HAS it (the
modal non-null value), so the backfill can only ever write the exact string the
deployment is already using — never a guessed one. Idempotent (only touches NULL
rows). A direct Data-API write is used deliberately: the proper ``patch_agent``
path talks to the in-VPC Paperclip API, which is unreachable from an operator
laptop, whereas the RDS Data API is reachable and this is a pure JSON-column
data migration with no server-side side effects (the adapter reads the field per
wake, so the fix takes effect on each agent's next run — no reprovision, no
restart).

USAGE (requires AWS SSO: ``aws sso login --profile isol8-admin``):
    python scripts/backfill_agent_paperclip_api_url.py --env prod            # dry-run
    python scripts/backfill_agent_paperclip_api_url.py --env prod --apply    # execute
"""

from __future__ import annotations

import argparse
import sys

import boto3

ACCOUNT = "877352799272"
REGION = "us-east-1"
PROFILE = "isol8-admin"


def _secret_arn(session, env: str) -> str:
    sm = session.client("secretsmanager")
    name = f"isol8-{env}-paperclip-db-credentials"
    for s in sm.list_secrets(Filters=[{"Key": "name", "Values": [name]}])["SecretList"]:
        if s["Name"] == name:
            return s["ARN"]
    raise SystemExit(f"secret not found: {name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "prod"])
    ap.add_argument("--apply", action="store_true", help="execute (default is dry-run)")
    args = ap.parse_args()

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    data = session.client("rds-data")
    cluster = f"arn:aws:rds:{REGION}:{ACCOUNT}:cluster:isol8-{args.env}-paperclip-db"
    secret = _secret_arn(session, args.env)

    def q(sql: str):
        return data.execute_statement(resourceArn=cluster, secretArn=secret, database="paperclip", sql=sql)

    # The exact value every healthy agent already uses — read it, never guess it.
    rows = q(
        "SELECT adapter_config->>'paperclipApiUrl' AS url, count(*) c "
        "FROM agents WHERE adapter_config->>'paperclipApiUrl' IS NOT NULL "
        "GROUP BY 1 ORDER BY c DESC LIMIT 1"
    )["records"]
    if not rows:
        print(
            f"[{args.env}] no agent has paperclipApiUrl set — cannot derive the "
            "canonical value; aborting (nothing safe to backfill)."
        )
        return 1
    canonical = rows[0][0]["stringValue"]

    missing = q("SELECT count(*) FROM agents WHERE adapter_config->>'paperclipApiUrl' IS NULL")["records"][0][0][
        "longValue"
    ]

    print(f"[{args.env}] canonical paperclipApiUrl = {canonical}")
    print(f"[{args.env}] agents missing the override: {missing}")
    if missing == 0:
        print(f"[{args.env}] nothing to do.")
        return 0

    if not args.apply:
        print(
            f"[{args.env}] DRY-RUN: would set paperclipApiUrl={canonical} on "
            f"{missing} agents. Re-run with --apply to execute."
        )
        return 0

    # jsonb_set adds the key on rows where it is absent. Guarded by the same
    # IS NULL predicate so a re-run is a no-op.
    res = q(
        "UPDATE agents SET adapter_config = "
        f"jsonb_set(adapter_config, '{{paperclipApiUrl}}', '\"{canonical}\"'::jsonb) "
        "WHERE adapter_config->>'paperclipApiUrl' IS NULL"
    )
    print(f"[{args.env}] APPLIED: updated {res.get('numberOfRecordsUpdated', '?')} agents.")

    remaining = q("SELECT count(*) FROM agents WHERE adapter_config->>'paperclipApiUrl' IS NULL")["records"][0][0][
        "longValue"
    ]
    print(f"[{args.env}] remaining missing after backfill: {remaining}")
    return 0 if remaining == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
