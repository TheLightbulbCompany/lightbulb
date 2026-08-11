"""Audit (and optionally neutralize) non-gateway Paperclip adapters.

The Paperclip proxy adapter filter (PR #1673) blocks any NEW request that would
persist a host-executing adapter (``process`` / ``http`` / ``*_local`` / ``cursor``).
But rows written BEFORE that fence deployed — while the proxy forwarded
everything — can still name a dangerous adapter, and agent ``wakeup`` /
``heartbeat/invoke`` execute the stored adapter on the shared Paperclip host.
This is the §3.6 data-cleanup the audit doc pairs with the filter: the RCE is not
fully closed for pre-existing data until this runs and reports clean.

Isol8 only ever seeds/hires ``openclaw_gateway`` agents, so a dangerous row is
either corruption or an attacker's plant. Both are neutralized by rewriting
``adapter_type`` to ``openclaw_gateway``: the agent can no longer exec on the
host (a gateway adapter connects out to the tenant's own container), even if its
stored adapterConfig is now inert.

Read-only AUDIT by default; ``--apply`` performs the rewrite. Requires AWS SSO
(``aws sso login --profile isol8-admin``).

    cd apps/backend && uv run python -m scripts.audit_paperclip_adapter_types --env prod
    cd apps/backend && uv run python -m scripts.audit_paperclip_adapter_types --env prod --apply
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import boto3

ACCOUNT_ID = "877352799272"
REGION = "us-east-1"

# The only host-SAFE adapters: both are remote gateways (the agent runs in the
# tenant's own container / Hermes install, never on the Paperclip host). Keep in
# lockstep with the proxy filter's allowlist reasoning; hermes_gateway is
# excluded from the proxy allowlist (Isol8 is OpenClaw-only) but is still
# host-safe, so it is NOT something this cleanup needs to rewrite.
SAFE_ADAPTER_TYPES = frozenset({"openclaw_gateway", "hermes_gateway"})
# What we rewrite a dangerous adapter to (host-exec-neutralizing).
NEUTRAL_ADAPTER_TYPE = "openclaw_gateway"


def is_dangerous(adapter_type: str | None) -> bool:
    """True if this adapter can execute on / reach out from the Paperclip host.

    ``None`` counts as dangerous: the agents schema defaults ``adapter_type`` to
    ``"process"``, and a null join-request adapter falls back to ``process`` on
    approval.
    """
    return adapter_type not in SAFE_ADAPTER_TYPES


@dataclass
class Finding:
    table: str
    row_id: str
    company_id: str
    label: str
    adapter_type: str | None


def classify_agents(rows: list[dict]) -> list[Finding]:
    """rows: [{id, company_id, name, adapter_type}]."""
    return [
        Finding("agents", r["id"], r.get("company_id", ""), r.get("name", ""), r.get("adapter_type"))
        for r in rows
        if is_dangerous(r.get("adapter_type"))
    ]


def classify_join_requests(rows: list[dict]) -> list[Finding]:
    """rows: [{id, company_id, request_type, adapter_type, status}] — agent joins only."""
    return [
        Finding("join_requests", r["id"], r.get("company_id", ""), r.get("status", ""), r.get("adapter_type"))
        for r in rows
        if r.get("request_type") == "agent" and is_dangerous(r.get("adapter_type"))
    ]


# --------------------------------------------------------------------------- AWS


def _secret_arn(env: str) -> str:
    sm = boto3.client("secretsmanager", region_name=REGION)
    needle = f"isol8-{env}-paperclip-db-credentials"
    for page in sm.get_paginator("list_secrets").paginate():
        for s in page.get("SecretList", []):
            if needle in s["Name"]:
                return s["ARN"]
    sys.exit(f"Could not find paperclip-db-credentials secret for env={env}")


def _aurora(env: str):
    return (
        boto3.client("rds-data", region_name=REGION),
        f"arn:aws:rds:{REGION}:{ACCOUNT_ID}:cluster:isol8-{env}-paperclip-db",
        _secret_arn(env),
    )


def _rows(resp: dict, cols: list[str]) -> list[dict]:
    out = []
    for rec in resp.get("records", []):
        row = {}
        for col, field in zip(cols, rec):
            row[col] = None if field.get("isNull") else field.get("stringValue")
        out.append(row)
    return out


def _query_agents(client, db_arn, secret_arn) -> list[dict]:
    resp = client.execute_statement(
        resourceArn=db_arn,
        secretArn=secret_arn,
        database="paperclip",
        sql=(
            "SELECT id, company_id, name, adapter_type FROM agents "
            "WHERE adapter_type NOT IN ('openclaw_gateway', 'hermes_gateway')"
        ),
    )
    return _rows(resp, ["id", "company_id", "name", "adapter_type"])


def _query_join_requests(client, db_arn, secret_arn) -> list[dict]:
    resp = client.execute_statement(
        resourceArn=db_arn,
        secretArn=secret_arn,
        database="paperclip",
        sql=(
            "SELECT id, company_id, request_type, adapter_type, status FROM join_requests "
            "WHERE request_type = 'agent' AND status = 'pending_approval' "
            "AND (adapter_type IS NULL OR adapter_type NOT IN ('openclaw_gateway', 'hermes_gateway'))"
        ),
    )
    return _rows(resp, ["id", "company_id", "request_type", "adapter_type", "status"])


def _count_dangerous_revisions(client, db_arn, secret_arn) -> int:
    # Config revisions are neutralized by the proxy's rollback block; audit-only
    # so the operator knows how many exist. after_config carries the adapterType.
    resp = client.execute_statement(
        resourceArn=db_arn,
        secretArn=secret_arn,
        database="paperclip",
        sql=(
            "SELECT count(*) FROM agent_config_revisions "
            "WHERE (after_config->>'adapterType') IS NOT NULL "
            "AND (after_config->>'adapterType') NOT IN ('openclaw_gateway', 'hermes_gateway')"
        ),
    )
    recs = resp.get("records", [])
    return int(recs[0][0].get("longValue", 0)) if recs else 0


def _neutralize(client, db_arn, secret_arn, table: str, row_id: str) -> None:
    client.execute_statement(
        resourceArn=db_arn,
        secretArn=secret_arn,
        database="paperclip",
        sql=f"UPDATE {table} SET adapter_type = :neutral WHERE id = :id",  # noqa: S608 — table is a fixed literal
        parameters=[
            {"name": "neutral", "value": {"stringValue": NEUTRAL_ADAPTER_TYPE}},
            {"name": "id", "value": {"stringValue": row_id}, "typeHint": "UUID"},
        ],
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", required=True, choices=["dev", "staging", "prod"])
    ap.add_argument("--apply", action="store_true", help="Rewrite dangerous adapters (default: audit only)")
    args = ap.parse_args()

    client, db_arn, secret_arn = _aurora(args.env)

    findings = classify_agents(_query_agents(client, db_arn, secret_arn)) + classify_join_requests(
        _query_join_requests(client, db_arn, secret_arn)
    )
    rev_count = _count_dangerous_revisions(client, db_arn, secret_arn)

    print(f"\n=== Paperclip adapter audit — env={args.env} ===")
    if not findings:
        print("✓ No live agents or pending join-requests name a host-executing adapter.")
    else:
        print(f"⚠ {len(findings)} row(s) name a host-executing adapter (RCE surface):\n")
        for f in findings:
            print(f"  [{f.table}] id={f.row_id} company={f.company_id} adapter_type={f.adapter_type!r} ({f.label})")

    if rev_count:
        print(
            f"\nℹ {rev_count} agent_config_revision(s) carry a non-gateway adapterType. "
            "These are inert (the proxy blocks rollback) — clean by hand before ever un-blocking it."
        )

    if not findings:
        return
    if not args.apply:
        print("\nDry run. Re-run with --apply to rewrite these to openclaw_gateway (neutralizes host exec).")
        return

    print(f"\nRewriting {len(findings)} row(s) to adapter_type={NEUTRAL_ADAPTER_TYPE!r}...")
    for f in findings:
        _neutralize(client, db_arn, secret_arn, f.table, f.row_id)
        print(f"  fixed [{f.table}] id={f.row_id}")
    print("Done. Re-run without --apply to confirm the audit is clean.")


if __name__ == "__main__":
    main()
