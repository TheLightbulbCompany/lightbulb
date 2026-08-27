"""Per-owner SES tenant — reputation isolation for agent-sent mail.

SES computes bounce/complaint findings per TENANT and (with the managed
``standard`` policy) pauses only that tenant when it goes bad, instead of the
whole account. One tenant per owner, created lazily on the first send, named
deterministically so nothing is stored. Mirrors how per-owner Bedrock AIPs are
created by the backend rather than CDK.

Never raises: a tenant hiccup must not block a send — the send simply goes
out untenanted (still on the configuration set, still observable).
"""

import logging
import re

import anyio
import boto3
from botocore.exceptions import ClientError

from core.config import apex_domain, settings

logger = logging.getLogger(__name__)

_ses_client = None
_sts_account: str | None = None
# ponytail: per-process memo; after a restart the idempotent calls simply run again.
_ensured: set[str] = set()

# Bound on the synchronous SES/STS calls in _ensure_sync — a hung boto3 call
# (dead network, throttled retries) must not stall the send path indefinitely.
_ENSURE_TIMEOUT_S = 10.0


def _client():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("sesv2", region_name=settings.AWS_REGION)
    return _ses_client


def _account_id() -> str:
    global _sts_account
    if _sts_account is None:
        _sts_account = boto3.client("sts", region_name=settings.AWS_REGION).get_caller_identity()["Account"]
    return _sts_account


def tenant_name_for(owner_id: str) -> str:
    return "owner-" + re.sub(r"[^A-Za-z0-9_-]", "-", owner_id)[:58]


def _identity_domain() -> str:
    """The SES domain identity every env sends under.

    A non-prod ``AGENT_EMAIL_DOMAIN`` is a mail SUBDOMAIN of the one verified
    identity (``dev.isol8.co`` under ``isol8.co``, see ses-inbound-stack.ts) —
    SES authorizes subdomain sends via the parent identity, so the association
    target is always the APEX of the configured mail domain, computed rather
    than string-matched against a brand literal.
    """
    return apex_domain(settings.AGENT_EMAIL_DOMAIN)


def _ignore_exists(fn, **kwargs) -> None:
    try:
        fn(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "AlreadyExistsException":
            raise


def _ensure_sync(owner_id: str) -> str:
    name = tenant_name_for(owner_id)
    region, account = settings.AWS_REGION, _account_id()
    ses = _client()
    # The real tenant ARN is `tenant/{name}/{tenant-id}` — a hand-built
    # `tenant/{name}` doesn't match anything, so UpdateReputationEntityPolicy
    # needs the ARN AWS actually assigned. create_tenant returns it directly;
    # on a second call (AlreadyExistsException) get_tenant fetches it back.
    try:
        tenant_arn = ses.create_tenant(TenantName=name)["TenantArn"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "AlreadyExistsException":
            raise
        tenant_arn = ses.get_tenant(TenantName=name)["Tenant"]["TenantArn"]
    for arn in (
        f"arn:aws:ses:{region}:{account}:identity/{_identity_domain()}",
        f"arn:aws:ses:{region}:{account}:configuration-set/{settings.AGENT_MAIL_CONFIGURATION_SET.strip()}",
    ):
        _ignore_exists(ses.create_tenant_resource_association, TenantName=name, ResourceArn=arn)
    ses.update_reputation_entity_policy(
        ReputationEntityType="RESOURCE",
        ReputationEntityReference=tenant_arn,
        ReputationEntityPolicy=f"arn:aws:ses:{region}:aws:reputation-policy/standard",
    )
    return name


async def ensure_tenant(owner_id: str) -> str | None:
    if not (settings.AGENT_MAIL_CONFIGURATION_SET or "").strip():
        return None
    name = tenant_name_for(owner_id)
    if name in _ensured:
        return name
    try:
        # abandon_on_cancel=True: anyio's default (False) waits for the worker
        # thread to finish before delivering the deadline's cancellation, which
        # defeats fail_after as a wall-clock bound. Abandoning just means we
        # stop waiting on it — the thread (boto3 calls) keeps running in the
        # background, which is safe: _ensure_sync is idempotent and _ensured
        # is only updated on a return we actually waited for.
        with anyio.fail_after(_ENSURE_TIMEOUT_S):
            await anyio.to_thread.run_sync(_ensure_sync, owner_id, abandon_on_cancel=True)
        _ensured.add(name)
        return name
    except Exception:  # noqa: BLE001 — a tenant hiccup must never block a send
        logger.warning("agent_mail_tenant: ensure failed for %s", owner_id, exc_info=True)
        return None
