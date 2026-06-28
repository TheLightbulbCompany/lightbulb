"""Per-owner Bedrock application inference profiles (AIPs) + scoped IAM role.

This module is the Plan-2 enforcement boundary. It mints three per-owner AIPs
(one per tier: opus/sonnet/haiku), each ``copyFrom`` the corresponding system
regional inference profile (which fans out to the foundation model in
us-east-1/us-east-2/us-west-2), and a per-owner IAM role scoped by the
``bedrock:InferenceProfileArn`` condition key to ONLY that owner's AIPs plus the
underlying foundation-model ARNs in every fan-out region. An explicit
``Deny bedrock:CreateInferenceProfile`` stops a tenant from minting a wider
profile to escape the scope.

Everything is a FREE FUNCTION (not a method on EcsManager) so the reverse
lookup (Plan 3) and the kill-switch (Plan 4) can import these without
constructing EcsManager, which eagerly builds boto3 clients in __init__.

Shared contracts other plans import:
  * resolve_owner_and_tier_from_aip_arn(aip_arn) -> tuple[str | None, str | None]
        (Plan 3, meter — single reverse-table read for owner + tier together)
  * resolve_owner_from_aip_arn(aip_arn) -> str | None   (Plan 3, meter)
  * resolve_tier_from_aip_arn(aip_arn) -> str | None     (Plan 3, pricing)
  * revoke_owner_bedrock(owner_id) / restore_owner_bedrock(owner_id)  (Plan 4)
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.repositories import container_repo

logger = logging.getLogger(__name__)

# Our own AWS account (dev + prod share account 877352799272). The owner-aws-sts
# Deny statement blocks the per-owner task role from assuming ANY role in our
# account, so a misconfigured or hostile owner-supplied role ARN can never be
# pointed back at our own infrastructure. Elsewhere the module resolves the live
# account via STS (_caller_account_id) for runtime ARNs, but _build_owner_aws_sts_doc
# is a PURE, synchronous builder that must not make an AWS call — so the account
# is named once here and reused, never inlined twice.
OUR_ACCOUNT_ID: str = "877352799272"

# The three AIP tiers. Order matters only for deterministic iteration.
TIERS: tuple[str, str, str] = ("opus", "sonnet", "haiku")

# Bedrock cross-region inference fans out to these regions; the per-owner role
# must permit the foundation-model ARN in EACH of them.
FAN_OUT_REGIONS: tuple[str, str, str] = ("us-east-1", "us-east-2", "us-west-2")

# Tier -> foundation-model id (LIVE-CONFIRMED). opus carries the "-v1" suffix;
# haiku carries the full date-stamp + ":0". An off-by-one suffix breaks
# invocation. This is the SINGLE authoritative tier->FM map: the IAM policy
# derives the FM ARNs from its values, and core.billing.bedrock_pricing imports
# it to map a resolved tier to its rate-table key (no duplicated FM-id list).
TIER_TO_FM: dict[str, str] = {
    "opus": "anthropic.claude-opus-4-6-v1",
    "sonnet": "anthropic.claude-sonnet-4-6",
    "haiku": "anthropic.claude-haiku-4-5-20251001-v1:0",
}

# Foundation-model ids, tier-ordered. Derived from TIER_TO_FM so the two never
# drift; consumed by _foundation_model_arns to build the per-region FM ARNs.
FM_IDS: tuple[str, ...] = tuple(TIER_TO_FM[tier] for tier in TIERS)

# System (source) regional inference-profile ids to copyFrom, per tier.
SYSTEM_PROFILE_ID: dict[str, str] = {
    "opus": "us.anthropic.claude-opus-4-6-v1",
    "sonnet": "us.anthropic.claude-sonnet-4-6",
    "haiku": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}


def _foundation_model_arns(regions: tuple[str, ...]) -> list[str]:
    """The 9 (len(FM_IDS) x len(regions)) foundation-model ARNs.

    Invoking THROUGH an inference profile requires BOTH the profile ARN AND the
    underlying foundation-model ARN to be permitted — AWS evaluates both. The
    foundation-model ARN omits the account segment (it is AWS-owned):
    arn:aws:bedrock:<region>::foundation-model/<model>.
    """
    return [f"arn:aws:bedrock:{region}::foundation-model/{model}" for region in regions for model in FM_IDS]


def build_owner_bedrock_policy(
    *,
    owner_id: str,
    aip_arns: dict[str, str],
    regions: tuple[str, ...],
) -> dict:
    """Build the per-owner Bedrock inline IAM policy document (PURE — no boto3).

    Invoking THROUGH an application inference profile authorizes TWO resources on
    each call: the profile (AIP) ARN itself, and the underlying foundation-model
    ARN. These need DIFFERENT treatment:

    - The AIP ARN is allowed UNCONDITIONALLY. AWS does NOT populate the
      ``bedrock:InferenceProfileArn`` condition key when authorizing the profile
      RESOURCE itself, so conditioning that statement denies the invoke outright
      (``AccessDeniedException`` — verified live + via simulate-principal-policy).
      Tenant isolation still holds: only THIS owner's AIP ARNs are listed.
    - The foundation-model ARNs ARE gated on
      ``bedrock:InferenceProfileArn == {this owner's AIPs}`` so the raw FMs can be
      reached ONLY via this owner's profile — no direct-FM invoke that would
      bypass the AIP and escape per-owner metering attribution.

    An explicit ``Deny bedrock:CreateInferenceProfile`` stops the tenant from
    minting a wider profile to escape the scope.
    """
    owner_aip_list = sorted(aip_arns.values())
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OwnerScopedProfileInvoke",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": owner_aip_list,
            },
            {
                "Sid": "OwnerScopedFmViaProfile",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": _foundation_model_arns(regions),
                "Condition": {
                    "ArnEquals": {
                        "bedrock:InferenceProfileArn": owner_aip_list,
                    }
                },
            },
            {
                "Sid": "DenyTenantProfileEscalation",
                "Effect": "Deny",
                "Action": "bedrock:CreateInferenceProfile",
                "Resource": "*",
            },
        ],
    }


def _aip_owners_table_name() -> str:
    name = os.environ.get("BEDROCK_AIP_OWNERS_TABLE") or settings.BEDROCK_AIP_OWNERS_TABLE
    if not name:
        raise RuntimeError("BEDROCK_AIP_OWNERS_TABLE is empty — backend is misconfigured.")
    return name


def _aip_owners_table():
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_aip_owners_table_name())


async def persist_owner_aips(
    owner_id: str,
    aip_arns: dict[str, str],
    *,
    role_arn: str | None = None,
) -> None:
    """Persist the per-owner AIP map (and optional role ARN) to BOTH the
    containers row and the bedrock-aip-owners reverse table.

    The reverse table is the read path for resolve_owner_from_aip_arn (Plan 3);
    the containers row is the read path for the config builder + resize/restart.
    """
    fields: dict = {"bedrock_aip_arns": aip_arns}
    if role_arn is not None:
        fields["bedrock_role_arn"] = role_arn
    await container_repo.update_fields(owner_id, fields)

    table = _aip_owners_table()
    for tier, arn in aip_arns.items():
        await _put_reverse_row(table, arn=arn, owner_id=owner_id, tier=tier)


async def _put_reverse_row(table, *, arn: str, owner_id: str, tier: str) -> None:
    from core.dynamodb import run_in_thread

    await run_in_thread(
        table.put_item,
        Item={"aip_arn": arn, "owner_id": owner_id, "tier": tier},
    )


async def read_owner_aips(owner_id: str) -> dict[str, str] | None:
    """Read the persisted per-owner AIP map from the containers row, or None."""
    row = await container_repo.get_by_owner_id(owner_id)
    if not row:
        return None
    arns = row.get("bedrock_aip_arns")
    if not arns:
        return None
    return {str(tier): str(arn) for tier, arn in arns.items()}


async def resolve_owner_and_tier_from_aip_arn(aip_arn: str) -> tuple[str | None, str | None]:
    """SHARED CONTRACT (Plan 3 meter): given an AIP ARN from an invocation-log
    record, return (owner_id, tier) in ONE reverse-table read.

    Both attributes live on the same bedrock-aip-owners row (_put_reverse_row
    writes them together), so the meter resolves owner + tier with a single
    bounded GetItem instead of two — exactly one reverse-table read per metered
    record on the hot path. resolve_owner_from_aip_arn / resolve_tier_from_aip_arn
    delegate here so any other caller keeps its single-value contract.

    Either element is None when the row (or that attribute) is missing.
    """
    from core.dynamodb import run_in_thread

    table = _aip_owners_table()
    resp = await run_in_thread(table.get_item, Key={"aip_arn": aip_arn})
    item = resp.get("Item")
    if not item:
        return None, None
    owner = item.get("owner_id")
    tier = item.get("tier")
    return (str(owner) if owner else None, str(tier) if tier else None)


async def resolve_owner_from_aip_arn(aip_arn: str) -> str | None:
    """SHARED CONTRACT (Plan 3 meter): given an AIP ARN from an invocation-log
    record, return the owning owner_id, or None if unknown.

    Reads the bedrock-aip-owners reverse table by PK — a single strongly
    bounded GetItem, safe on the hot meter path. No name-parsing.
    """
    owner, _ = await resolve_owner_and_tier_from_aip_arn(aip_arn)
    return owner


async def resolve_tier_from_aip_arn(aip_arn: str) -> str | None:
    """SHARED CONTRACT (Plan 3 pricing): given an AIP ARN from an invocation-log
    record, return its model tier ("opus"/"sonnet"/"haiku"), or None if unknown.

    Mirrors resolve_owner_from_aip_arn — same bedrock-aip-owners reverse-table
    PK GetItem (a single bounded read, safe on the hot pricing path) — but
    returns the `tier` attribute that _put_reverse_row stores on every row.
    Plan 3's price_from_aip_arn maps the tier to the per-token rate; the AIP ARN
    alone carries no model name, so the persisted tier is the only reliable key.
    """
    _, tier = await resolve_owner_and_tier_from_aip_arn(aip_arn)
    return tier


def _bedrock_client():
    """Bedrock CONTROL-PLANE client (create/list/tag inference profiles).
    NB: 'bedrock', not 'bedrock-runtime' (which is the invoke client)."""
    return boto3.client("bedrock", region_name=settings.AWS_REGION)


async def _caller_account_id() -> str:
    """The AWS account id, via STS (sts:GetCallerIdentity needs no IAM grant).
    Used to build the system inference-profile ARN for
    CreateInferenceProfile.copyFrom. Resolved on the create-pass only (not the
    idempotent short-circuit), so the common no-op path makes zero AWS calls."""
    from core.dynamodb import run_in_thread

    sts = boto3.client("sts", region_name=settings.AWS_REGION)
    ident = await run_in_thread(sts.get_caller_identity)
    return ident["Account"]


def _system_profile_arn(tier: str, account_id: str) -> str:
    """CreateInferenceProfile.copyFrom requires the FULL inference-profile ARN of
    the source system profile, NOT the bare id (`us.anthropic...`): AWS rejects
    the bare id with a ValidationException (regex demands an
    inference-profile|foundation-model ARN). SYSTEM_DEFINED profile ARNs are
    account-qualified: arn:aws:bedrock:<region>:<account>:inference-profile/<id>."""
    return f"arn:aws:bedrock:{settings.AWS_REGION}:{account_id}:inference-profile/{SYSTEM_PROFILE_ID[tier]}"


def _env() -> str:
    return settings.ENVIRONMENT or "dev"


def _aip_name(owner_id: str, tier: str) -> str:
    """Env-prefixed AIP name so dev/prod never collide on a global name."""
    return f"isol8-{_env()}-{owner_id}-{tier}"


async def ensure_owner_aips(owner_id: str) -> dict[str, str]:
    """Idempotently create the 3 per-owner AIPs (one per tier); return
    {tier: aip_arn}. Persists to the containers row + reverse table.

    Idempotent: if all 3 tiers are already persisted, returns them without any
    Bedrock control-plane call — but STILL re-puts the reverse-index rows
    (review C1). The row update and the reverse PutItems in persist_owner_aips
    are NOT atomic; a prior partial failure (row written, one reverse PutItem
    failed) leaves resolve_owner_from_aip_arn unable to attribute that AIP. The
    reverse PutItems are idempotent (same key, same value), so re-running them
    on the short-circuit path is cheap and self-heals the index.
    """
    from core.dynamodb import run_in_thread

    existing = await read_owner_aips(owner_id)
    if existing and all(tier in existing for tier in TIERS):
        # Self-heal the reverse index in case a prior run's reverse PutItems
        # partially failed after the row was written (C1 inconsistency window).
        table = _aip_owners_table()
        for tier, arn in existing.items():
            await _put_reverse_row(table, arn=arn, owner_id=owner_id, tier=tier)
        return existing

    client = _bedrock_client()
    account_id = await _caller_account_id()
    arns: dict[str, str] = dict(existing or {})
    for tier in TIERS:
        if tier in arns:
            continue
        name = _aip_name(owner_id, tier)
        try:
            resp = await run_in_thread(
                client.create_inference_profile,
                inferenceProfileName=name,
                modelSource={"copyFrom": _system_profile_arn(tier, account_id)},
                tags=[
                    {"key": "owner_id", "value": owner_id},
                    {"key": "tier", "value": tier},
                    {"key": "ManagedBy", "value": "isol8-backend"},
                ],
            )
            arns[tier] = resp["inferenceProfileArn"]
        except ClientError as e:
            # ConflictException == a same-named AIP already exists from a prior
            # crashed run (created in Bedrock but never persisted to the row).
            # Adopt it instead of hard-failing — makes the mint converge rather
            # than 500 a restart/re-provision. NB: boto3's bedrock typed
            # ConflictException is a ClientError subclass, so this one clause
            # covers both real and unit-test (generic ClientError) raises.
            if e.response["Error"]["Code"] != "ConflictException":
                raise
            adopted = await _find_existing_aip_arn(client, name)
            if not adopted:
                raise  # conflict but we can't find it — surface the real error
            arns[tier] = adopted

    await persist_owner_aips(owner_id, arns)
    return arns


async def _find_existing_aip_arn(client, name: str) -> str | None:
    """List application inference profiles and return the ARN of the one whose
    name matches `name`, or None. Used to recover from a ConflictException on a
    re-mint where the AIP exists in Bedrock but the row was never persisted."""
    from core.dynamodb import run_in_thread

    token: str | None = None
    while True:
        kwargs: dict = {"typeEquals": "APPLICATION", "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = await run_in_thread(client.list_inference_profiles, **kwargs)
        for prof in resp.get("inferenceProfileSummaries", []):
            if prof.get("inferenceProfileName") == name:
                return prof.get("inferenceProfileArn")
        token = resp.get("nextToken")
        if not token:
            return None


def _iam_client():
    return boto3.client("iam", region_name=settings.AWS_REGION)


def _owner_role_name(owner_id: str) -> str:
    """Per-owner role name. The CDK PassRole/CreateRole grants scope to the
    'isol8-{env}-owner-bedrock-*' prefix, so this MUST start with it."""
    return f"isol8-{_env()}-owner-bedrock-{owner_id}"


def _fan_out_regions() -> tuple[str, ...]:
    raw = settings.BEDROCK_AIP_FAN_OUT_REGIONS or "us-east-1,us-east-2,us-west-2"
    return tuple(r.strip() for r in raw.split(",") if r.strip())


def _assume_role_doc(account_id: str) -> dict:
    """Trust policy for the per-owner ECS task role.

    Conditions the ``ecs-tasks.amazonaws.com`` assumption on
    ``aws:SourceAccount`` (our account) AND ``aws:SourceArn`` (any ECS task in
    this account/region) so a confused deputy in another account or service
    cannot assume the per-owner role. ECS populates both keys when it assumes
    the task role. The task id is unknown at provision time, so ``SourceArn``
    uses an account-wide task wildcard — this avoids coupling to the cluster
    name (which drifts on stack recreation) while still blocking cross-account
    and any non-ECS-task assumption. ``ExternalId`` is the wrong tool here: it
    is for third-party cross-account principal assumption, not a service
    principal assuming on our behalf.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:ecs:{settings.AWS_REGION}:{account_id}:task/*"},
                },
            }
        ],
    }


async def ensure_owner_bedrock_role(
    owner_id: str,
    aip_arns: dict[str, str],
    *,
    skip_bedrock_grant: bool = False,
) -> str:
    """Idempotently create/update the per-owner IAM role scoped to those AIPs +
    the foundation-model ARNs in every fan-out region; return its ARN.

    Re-running refreshes the scoped inline policy (so a tier-ARN change is
    picked up) but never re-creates an existing role.

    skip_bedrock_grant=True creates/keeps the role + base (EFS/logs/ssm) policy
    but does NOT (re)put the scoped Bedrock inline policy. Used by provision for
    a revoked owner so a re-provision/restart does not silently un-revoke the
    kill-switch (review C6). Plan 4's restore_owner_bedrock is the only path
    that re-grants Bedrock.
    """
    from core.dynamodb import run_in_thread

    client = _iam_client()
    role_name = _owner_role_name(owner_id)

    try:
        got = await run_in_thread(client.get_role, RoleName=role_name)
        role_arn = got["Role"]["Arn"]
        # Backfill: converge a role minted before the trust hardening onto the
        # SourceAccount/SourceArn-conditioned trust policy — exactly ONCE. The
        # `trust_hardened` tag marks convergence so this is not a dead IAM
        # round-trip on every re-provision / container-status poll. Account is
        # parsed from the existing ARN, so this needs no STS call. Best-effort —
        # a transient IAM failure (or the iam:UpdateAssumeRolePolicy grant not
        # yet propagated) must not break provisioning of an otherwise-working
        # role; the old (permissive) trust keeps functioning, and the missing
        # tag means the next provision retries the converge.
        existing_tags = {t["Key"]: t["Value"] for t in got["Role"].get("Tags", [])}
        if existing_tags.get("trust_hardened") != "1":
            account_id = role_arn.split(":")[4]
            try:
                await run_in_thread(
                    client.update_assume_role_policy,
                    RoleName=role_name,
                    PolicyDocument=json.dumps(_assume_role_doc(account_id)),
                )
                await run_in_thread(
                    client.tag_role,
                    RoleName=role_name,
                    Tags=[{"Key": "trust_hardened", "Value": "1"}],
                )
            except ClientError as e:
                logger.warning("bedrock trust-policy backfill failed for %s: %s", role_name, e)
    except ClientError as e:
        # boto3's typed iam NoSuchEntityException IS a ClientError subclass, so
        # one `except ClientError` catches both the real-client typed raise and
        # the unit test's generic ClientError. Do NOT use
        # `except client.exceptions.NoSuchEntityException` — with a MagicMock
        # client that attribute is a MagicMock (not an exception class), and
        # `except <non-class>` raises TypeError at except-evaluation time.
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        role_arn = await _create_owner_role(client, role_name, owner_id)

    # (Re)attach the CDK-managed EFS/logs/ssm base policy — idempotent.
    if settings.BEDROCK_OWNER_BASE_POLICY_ARN:
        await run_in_thread(
            client.attach_role_policy,
            RoleName=role_name,
            PolicyArn=settings.BEDROCK_OWNER_BASE_POLICY_ARN,
        )

    # (Re)put the scoped Bedrock inline policy — refreshes on every call,
    # UNLESS the owner is revoked (kill-switch held; Plan 4 restores it).
    if not skip_bedrock_grant:
        doc = build_owner_bedrock_policy(
            owner_id=owner_id,
            aip_arns=aip_arns,
            regions=_fan_out_regions(),
        )
        await run_in_thread(
            client.put_role_policy,
            RoleName=role_name,
            PolicyName="owner-bedrock",
            PolicyDocument=json.dumps(doc),
        )

    await container_repo.update_fields(owner_id, {"bedrock_role_arn": role_arn})
    return role_arn


async def _create_owner_role(client, role_name: str, owner_id: str) -> str:
    from core.dynamodb import run_in_thread

    # First-mint path only (rare). Resolve the account id for the
    # SourceAccount/SourceArn-conditioned trust policy; one STS call here is
    # negligible alongside CreateRole, and the common existing-role path parses
    # the account from the role ARN instead (no STS).
    account_id = await _caller_account_id()
    create_kwargs: dict = {
        "RoleName": role_name,
        "AssumeRolePolicyDocument": json.dumps(_assume_role_doc(account_id)),
        "Description": f"Isol8 per-owner Bedrock role for {owner_id}",
        "Tags": [
            {"Key": "owner_id", "Value": owner_id},
            {"Key": "ManagedBy", "Value": "isol8-backend"},
            # Born with the conditioned trust policy → mark converged so the
            # existing-role path's backfill skips it on every later call.
            {"Key": "trust_hardened", "Value": "1"},
        ],
    }
    if settings.BEDROCK_OWNER_BOUNDARY_ARN:
        create_kwargs["PermissionsBoundary"] = settings.BEDROCK_OWNER_BOUNDARY_ARN
    resp = await run_in_thread(client.create_role, **create_kwargs)
    return resp["Role"]["Arn"]


async def revoke_owner_bedrock(owner_id: str) -> None:
    """SHARED CONTRACT (Plan 4 kill-switch): remove the per-owner role's scoped
    Bedrock inline policy so the container can no longer invoke any model, and
    stamp bedrock_revoked=True.

    IAM is ~5-15s eventually consistent — Plan 4 pairs this with ECS
    desired-count=0 for the guaranteed instant stop. Idempotent: a missing
    policy (NoSuchEntity) is treated as already-revoked.
    """
    from core.dynamodb import run_in_thread

    client = _iam_client()
    role_name = _owner_role_name(owner_id)
    try:
        await run_in_thread(
            client.delete_role_policy,
            RoleName=role_name,
            PolicyName="owner-bedrock",
        )
    except ClientError as e:
        # NoSuchEntity == already revoked (idempotent). A SINGLE `except
        # ClientError` covers both the real iam typed exception (a ClientError
        # subclass) and the test's generic ClientError. Do NOT use
        # `except getattr(client.exceptions, "NoSuchEntityException", ClientError)`
        # — with a MagicMock client that attribute resolves to a MagicMock, and
        # `except <non-class>` raises TypeError at except-evaluation time before
        # any fallback clause runs.
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
    await container_repo.update_fields(owner_id, {"bedrock_revoked": True})


async def restore_owner_bedrock(owner_id: str) -> None:
    """SHARED CONTRACT (Plan 4): re-attach the per-owner role's scoped Bedrock
    inline policy from the persisted AIP map, and clear bedrock_revoked.

    Raises if the owner has no persisted AIPs (nothing to restore — caller
    must re-provision first).
    """
    arns = await read_owner_aips(owner_id)
    if not arns:
        raise RuntimeError(
            f"Cannot restore Bedrock for {owner_id}: no persisted AIP map (re-provision the container first)."
        )
    await ensure_owner_bedrock_role(owner_id, arns)
    await container_repo.update_fields(owner_id, {"bedrock_revoked": False})


def _build_owner_aws_sts_doc(role_arns: list[str]) -> dict:
    """Build the per-owner inline IAM policy granting sts:AssumeRole on EXACTLY
    the owner-connected read-only role ARN(s), with our own account denied (PURE
    — no boto3, so it is safe to call synchronously under no AWS credentials).

    Two statements:
    - An ``Allow sts:AssumeRole`` whose Resource is the exact owner-supplied ARN
      list — NEVER a wildcard. The task role can assume only the roles the owner
      explicitly connected.
    - A ``Deny sts:AssumeRole`` on ``arn:aws:iam::<our account>:role/*`` that
      fails closed: even if an owner supplies an ARN inside our own account, the
      task role can never assume it (an explicit Deny always beats an Allow).

    This is a SEPARATE inline policy from "owner-bedrock": the Bedrock
    kill-switch (revoke_owner_bedrock) deletes "owner-bedrock", and AWS
    read-only access must survive that.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OwnerScopedAssumeReadonly",
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Resource": list(role_arns),
            },
            {
                "Sid": "DenyAssumeIntoOurAccount",
                "Effect": "Deny",
                "Action": "sts:AssumeRole",
                "Resource": f"arn:aws:iam::{OUR_ACCOUNT_ID}:role/*",
            },
        ],
    }


async def put_owner_aws_sts_policy(owner_id: str, role_arns: list[str]) -> None:
    """Put the per-owner "owner-aws-sts" inline policy granting the task role
    sts:AssumeRole on exactly ``role_arns`` (our account denied) onto the
    per-owner role. A SEPARATE policy from "owner-bedrock" so the Bedrock
    kill-switch never strips AWS access. Mirrors the scoped-Bedrock put_role_policy
    pattern (same IAM client + run_in_thread)."""
    from core.dynamodb import run_in_thread

    client = _iam_client()
    await run_in_thread(
        client.put_role_policy,
        RoleName=_owner_role_name(owner_id),
        PolicyName="owner-aws-sts",
        PolicyDocument=json.dumps(_build_owner_aws_sts_doc(role_arns)),
    )


async def delete_owner_aws_sts_policy(owner_id: str) -> None:
    """Delete the per-owner "owner-aws-sts" inline policy (disconnect AWS access)
    from the per-owner role. Idempotent: a missing policy (NoSuchEntity) is
    treated as already-deleted, mirroring revoke_owner_bedrock's delete_role_policy
    handling — one ``except ClientError`` covers both the real iam typed
    exception (a ClientError subclass) and a unit test's generic ClientError."""
    from core.dynamodb import run_in_thread

    client = _iam_client()
    try:
        await run_in_thread(
            client.delete_role_policy,
            RoleName=_owner_role_name(owner_id),
            PolicyName="owner-aws-sts",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
