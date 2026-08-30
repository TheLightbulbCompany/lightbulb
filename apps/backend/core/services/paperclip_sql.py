"""Read-only SQL against the Paperclip Aurora cluster via the RDS Data API.

The backend task role already carries ``rds-data:ExecuteStatement`` on the
cluster and ``secretsmanager:GetSecretValue`` on the DASH-named
``lightbulb-{env}-paperclip-db-credentials`` secret (service-stack.ts
``AccountWipePaperclipAurora*``), granted for the account-wipe user delete.
This module is the shared wiring for that grant: ``paperclip_user_delete``
imports the ARN/client helpers from here, and fleet aggregates
(``core/services/ops/growth.py``) use ``select_rows``.

Why SQL and not ``PaperclipAdminClient``: every Paperclip REST read is
company-scoped, so a fleet aggregate over N tenants is 3-4 calls per tenant;
one SQL statement covers all tenants and returns an aggregate that fits the
Data API's 1 MiB response cap (10 MB with ``formatRecordsAs=JSON``). Column
names come from the deployed fork's drizzle schema
(``~/dev/paperclip-fork/packages/db/src/schema``) — verify against the pinned
tag in ``paperclip-version.json`` before adding a statement.
"""

from __future__ import annotations

import json

import boto3

from core.config import settings
from core.dynamodb import run_in_thread

# Same AWS account both envs share (see core.billing.bedrock_aip.OUR_ACCOUNT_ID).
_ACCOUNT_ID = "877352799272"

# env -> secret ARN. ListSecrets has no resource-level scoping and paginates;
# resolve once per process.
_secret_arn_cache: dict[str, str] = {}


def rds_data_client():
    return boto3.client("rds-data", region_name=settings.AWS_REGION)


def secretsmanager_client():
    return boto3.client("secretsmanager", region_name=settings.AWS_REGION)


def cluster_arn(env: str) -> str:
    return f"arn:aws:rds:{settings.AWS_REGION}:{_ACCOUNT_ID}:cluster:lightbulb-{env}-paperclip-db"


def find_secret_arn(sm_client, env: str) -> str:
    """Paginated lookup: with enough secrets in the account the target can be
    on a later page, so a single unpaginated call falsely reports not-found."""
    needle = f"lightbulb-{env}-paperclip-db-credentials"
    for page in sm_client.get_paginator("list_secrets").paginate():
        for s in page.get("SecretList", []):
            if needle in s["Name"]:
                return s["ARN"]
    raise RuntimeError(f"Could not find paperclip-db-credentials secret for env={env}")


async def select_rows(
    sql: str,
    *,
    env: str | None = None,
    parameters: list[dict] | None = None,
) -> list[dict]:
    """Run one SELECT/WITH statement and return rows as dicts keyed by column alias.

    Refuses anything else — the master credentials can write, this door cannot.
    ``formatRecordsAs=JSON`` renders timestamps/uuids/jsonb as strings and
    ints/floats/bools natively (AWS data-api-json docs); duplicate column
    labels are a Data API error, so alias every expression.
    """
    head = sql.lstrip()[:6].upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise ValueError("select_rows only runs SELECT/WITH statements")
    env = env or settings.ENVIRONMENT
    if env not in _secret_arn_cache:
        _secret_arn_cache[env] = await run_in_thread(find_secret_arn, secretsmanager_client(), env)
    resp = await run_in_thread(
        rds_data_client().execute_statement,
        resourceArn=cluster_arn(env),
        secretArn=_secret_arn_cache[env],
        database="paperclip",
        sql=sql,
        parameters=parameters or [],
        formatRecordsAs="JSON",
    )
    return json.loads(resp.get("formattedRecords") or "[]")
