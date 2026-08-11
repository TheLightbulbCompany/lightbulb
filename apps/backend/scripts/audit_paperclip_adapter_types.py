"""Audit (and optionally neutralize) non-gateway Paperclip adapters.

The Paperclip proxy adapter filter (PR #1673) blocks any NEW request that would
persist a host-executing adapter (``process`` / ``http`` / ``*_local`` / ``cursor``).
But rows written BEFORE that fence deployed — while the proxy forwarded
everything — can still name a dangerous adapter, and several ALLOWED routes act
on that stored value:

  * agent ``wakeup`` / ``heartbeat/invoke`` execute the stored adapter on the
    shared Paperclip host (``agents``);
  * ``/approvals/:id/approve`` creates an agent from ``payload.adapterType``,
    defaulting to ``process`` when absent (``approvals``, type=hire_agent).

This is the §3.6 data-cleanup the audit doc pairs with the filter: the RCE is not
fully closed for pre-existing data until this reports clean.

Isol8 only ever seeds/hires ``openclaw_gateway`` agents, so a dangerous row is
either corruption or an attacker's plant. Neutralizing by only rewriting
``adapter_type`` is NOT enough — a stale ``http`` row keeps an attacker-controlled
``adapter_config.url`` that a gateway adapter would then dial from the host. So we
QUARANTINE instead:

  * agents  -> status='terminated' (wakeup/scheduling skip terminated agents),
               and adapter_type/adapter_config/runtime_config scrubbed so a later
               un-terminate can't resurrect the payload;
  * hire approvals -> status='rejected' (no longer approvable).

Join-requests (approve route) and config-revisions (rollback route) are already
route-blocked by #1673, so they are audited (report-only), including BOTH sides
of a revision (a repaired agent's ``before_config`` is the rollback target).

Read-only AUDIT by default; ``--apply`` performs the quarantine. Requires AWS SSO
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
# tenant's own container / Hermes install, never on the Paperclip host).
SAFE_ADAPTER_TYPES = frozenset({"openclaw_gateway", "hermes_gateway"})
_SAFE_SQL = "('openclaw_gateway', 'hermes_gateway')"


def is_dangerous(adapter_type: str | None) -> bool:
    """True if this adapter can execute on / reach out from the Paperclip host.

    ``None`` counts as dangerous: the agents schema defaults ``adapter_type`` to
    ``"process"``, and a null approval/join adapter falls back to ``process``.
    """
    return adapter_type not in SAFE_ADAPTER_TYPES


@dataclass
class Finding:
    table: str
    row_id: str
    company_id: str
    label: str
    adapter_type: str | None
    remediable: bool  # False => already route-blocked, audit-only


def classify_agents(rows: list[dict]) -> list[Finding]:
    """rows: [{id, company_id, name, adapter_type}] — live agents (wakeup surface)."""
    return [
        Finding("agents", r["id"], r.get("company_id", ""), r.get("name", ""), r.get("adapter_type"), True)
        for r in rows
        if is_dangerous(r.get("adapter_type"))
    ]


def classify_approvals(rows: list[dict]) -> list[Finding]:
    """rows: [{id, company_id, type, status, adapter_type}] — pending hire approvals."""
    return [
        Finding("approvals", r["id"], r.get("company_id", ""), r.get("status", ""), r.get("adapter_type"), True)
        for r in rows
        if r.get("type") == "hire_agent"
        and r.get("status") in ("pending", "revision_requested")
        and is_dangerous(r.get("adapter_type"))
    ]


def classify_join_requests(rows: list[dict]) -> list[Finding]:
    """rows: [{id, company_id, request_type, adapter_type, status}]. Route-blocked (#1673)."""
    return [
        Finding("join_requests", r["id"], r.get("company_id", ""), r.get("status", ""), r.get("adapter_type"), False)
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
        out.append({col: (None if f.get("isNull") else f.get("stringValue")) for col, f in zip(cols, rec)})
    return out


def _sql(client, db_arn, secret_arn, sql: str, params: list | None = None) -> dict:
    return client.execute_statement(
        resourceArn=db_arn,
        secretArn=secret_arn,
        database="paperclip",
        sql=sql,
        parameters=params or [],
    )


def _query_agents(client, db_arn, secret_arn) -> list[dict]:
    resp = _sql(
        client,
        db_arn,
        secret_arn,
        f"SELECT id, company_id, name, adapter_type FROM agents WHERE adapter_type NOT IN {_SAFE_SQL}",
    )
    return _rows(resp, ["id", "company_id", "name", "adapter_type"])


def _query_approvals(client, db_arn, secret_arn) -> list[dict]:
    resp = _sql(
        client,
        db_arn,
        secret_arn,
        "SELECT id, company_id, type, status, payload->>'adapterType' AS adapter_type FROM approvals "
        "WHERE type = 'hire_agent' AND status IN ('pending', 'revision_requested') "
        f"AND (payload->>'adapterType' IS NULL OR payload->>'adapterType' NOT IN {_SAFE_SQL})",
    )
    return _rows(resp, ["id", "company_id", "type", "status", "adapter_type"])


def _query_join_requests(client, db_arn, secret_arn) -> list[dict]:
    resp = _sql(
        client,
        db_arn,
        secret_arn,
        "SELECT id, company_id, request_type, adapter_type, status FROM join_requests "
        "WHERE request_type = 'agent' AND status = 'pending_approval' "
        f"AND (adapter_type IS NULL OR adapter_type NOT IN {_SAFE_SQL})",
    )
    return _rows(resp, ["id", "company_id", "request_type", "adapter_type", "status"])


def _count_dangerous_revisions(client, db_arn, secret_arn) -> int:
    # BOTH sides: a repaired agent's before_config is the rollback target, so a
    # gateway-safe after_config can still hide a dangerous before_config.
    resp = _sql(
        client,
        db_arn,
        secret_arn,
        "SELECT count(*) FROM agent_config_revisions WHERE "
        f"(before_config->>'adapterType' IS NOT NULL AND before_config->>'adapterType' NOT IN {_SAFE_SQL}) "
        f"OR (after_config->>'adapterType' IS NOT NULL AND after_config->>'adapterType' NOT IN {_SAFE_SQL})",
    )
    recs = resp.get("records", [])
    return int(recs[0][0].get("longValue", 0)) if recs else 0


def _quarantine(client, db_arn, secret_arn, finding: Finding) -> None:
    uid = [{"name": "id", "value": {"stringValue": finding.row_id}, "typeHint": "UUID"}]
    if finding.table == "agents":
        # Terminate (non-invokable) AND scrub type+configs so an un-terminate
        # can't resurrect the host-exec adapter or its stored url/command.
        _sql(
            client,
            db_arn,
            secret_arn,
            "UPDATE agents SET status = 'terminated', adapter_type = 'openclaw_gateway', "
            "adapter_config = '{}'::jsonb, runtime_config = '{}'::jsonb WHERE id = :id",
            uid,
        )
    elif finding.table == "approvals":
        _sql(client, db_arn, secret_arn, "UPDATE approvals SET status = 'rejected' WHERE id = :id", uid)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", required=True, choices=["dev", "staging", "prod"])
    ap.add_argument("--apply", action="store_true", help="Quarantine dangerous rows (default: audit only)")
    args = ap.parse_args()

    client, db_arn, secret_arn = _aurora(args.env)

    findings = (
        classify_agents(_query_agents(client, db_arn, secret_arn))
        + classify_approvals(_query_approvals(client, db_arn, secret_arn))
        + classify_join_requests(_query_join_requests(client, db_arn, secret_arn))
    )
    rev_count = _count_dangerous_revisions(client, db_arn, secret_arn)

    remediable = [f for f in findings if f.remediable]
    blocked = [f for f in findings if not f.remediable]

    print(f"\n=== Paperclip adapter audit — env={args.env} ===")
    if not findings and not rev_count:
        print("✓ Clean: no live agents, pending approvals, join-requests, or revisions name a host-exec adapter.")
        return

    if remediable:
        print(f"\n⚠ {len(remediable)} ACTIVE host-exec surface(s) (wakeup / approve):")
        for f in remediable:
            print(f"  [{f.table}] id={f.row_id} company={f.company_id} adapter_type={f.adapter_type!r} ({f.label})")
    if blocked:
        print(f"\nℹ {len(blocked)} route-blocked row(s) (#1673 blocks their approve/rollback — inert, clean by hand):")
        for f in blocked:
            print(f"  [{f.table}] id={f.row_id} company={f.company_id} adapter_type={f.adapter_type!r}")
    if rev_count:
        print(f"\nℹ {rev_count} agent_config_revision(s) carry a non-gateway adapterType (either side). Rollback is")
        print("  blocked (#1673) so they are inert — scrub by hand before ever un-blocking rollback.")

    if not remediable:
        return
    if not args.apply:
        print(
            "\nDry run. Re-run with --apply to quarantine the ACTIVE surfaces (agents->terminated, approvals->rejected)."
        )
        return

    print(f"\nQuarantining {len(remediable)} active surface(s)...")
    for f in remediable:
        _quarantine(client, db_arn, secret_arn, f)
        print(f"  quarantined [{f.table}] id={f.row_id}")
    print("Done. Re-run without --apply to confirm the audit is clean.")


if __name__ == "__main__":
    main()
