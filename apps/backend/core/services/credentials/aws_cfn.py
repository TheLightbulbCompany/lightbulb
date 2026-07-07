"""Helpers for the AWS read-only connect flow: mint the STS ExternalId and build
the CloudFormation console quick-create URL the owner clicks to stand up the
cross-account role. The trusted principal is our account root
(``platform_trust_principal_arn``) with the per-owner ExternalId condition as
the lock."""

from __future__ import annotations

import secrets
from urllib.parse import quote

from core.billing import bedrock_aip

_STACK_NAME = bedrock_aip.CONNECTOR_ROLE_NAME


def gen_external_id() -> str:
    """A high-entropy STS ExternalId (≥128-bit, charset-safe for the trust policy)."""
    return secrets.token_urlsafe(32)


def platform_trust_principal_arn() -> str:
    """Account-root principal every connector role trusts — the standard
    cross-account SaaS pattern. Root delegates to our account's IAM; the
    per-owner ExternalId condition (server-side only) is the per-owner lock.
    The root ARN can never drift, so a connected account never needs a trust
    update again."""
    return f"arn:aws:iam::{bedrock_aip.OUR_ACCOUNT_ID}:root"


def quick_create_url(
    *,
    external_id: str,
    principal_arn: str,
    policy_arn: str,
    region: str,
    template_url: str,
) -> str:
    """CloudFormation console quick-create URL pre-filled with the connector stack
    template + parameters. ``template_url`` / ``principal_arn`` / ``policy_arn`` are
    URL-encoded (``safe=""``) since they carry ``:`` / ``/`` that must not leak into
    the query string."""
    return (
        f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"
        f"#/stacks/create/review?templateURL={quote(template_url, safe='')}"
        f"&stackName={_STACK_NAME}"
        f"&param_ExternalId={external_id}"
        f"&param_TrustedPrincipalArn={quote(principal_arn, safe='')}"
        f"&param_ManagedPolicyArn={quote(policy_arn, safe='')}"
    )
