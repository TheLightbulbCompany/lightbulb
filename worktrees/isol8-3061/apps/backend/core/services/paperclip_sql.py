"""Addressing for the Paperclip Aurora cluster (RDS Data API).

The backend task role carries ``rds-data:ExecuteStatement`` on the cluster and
``secretsmanager:GetSecretValue`` on the DASH-named
``lightbulb-{env}-paperclip-db-credentials`` secret (service-stack.ts
``AccountWipePaperclipAurora*``), granted for the account-wipe user delete.
This module is the shared wiring for that grant, and ``paperclip_user_delete``
— which imports the ARN/client helpers below — is its only consumer.

It also held ``select_rows``, a read-only SELECT door for fleet aggregates. That
went with the /admin/growth rebuild (2026-09-12): the growth page is computed
from the messaging table and billing rows now, and nothing else read Paperclip
over SQL. If a fleet aggregate ever needs SQL again, recover it from the history
of this file rather than reaching for ``PaperclipAdminClient`` — every REST read
there is company-scoped, so an aggregate over N tenants is 3-4 calls per tenant.
Column names come from the deployed fork's drizzle schema
(``~/dev/paperclip-fork/packages/db/src/schema``); verify against the pinned tag
in ``paperclip-version.json``.
"""

from __future__ import annotations

import boto3

from core.config import settings

# Same AWS account both envs share (see core.billing.bedrock_aip.OUR_ACCOUNT_ID).
_ACCOUNT_ID = "877352799272"


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
