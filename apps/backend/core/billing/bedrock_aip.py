"""Per-owner Bedrock application inference profiles (AIPs) + scoped IAM role.

This module is the Plan-2 enforcement boundary. It mints three per-owner AIPs
(one per tier: opus/sonnet/haiku), each ``copyFrom`` the corresponding system
regional inference profile (which fans out to the foundation model in
us-east-1/us-east-2/us-west-2), and a per-owner IAM role scoped by the
``bedrock:InferenceProfileArn`` condition key to ONLY that owner's AIPs plus the
underlying foundation-model ARNs in every fan-out region. An explicit
``Deny bedrock:CreateInferenceProfile`` stops a tenant from minting a wider
profile to escape the scope.

Everything is a FREE FUNCTION (not a method on EcsManager) so the
kill-switch (Plan 4) can import these without constructing EcsManager,
which eagerly builds boto3 clients in __init__.

The bedrock-aip-owners reverse table (``persist_owner_aips`` /
``_put_reverse_row`` below) is read by the bedrock-usage-meter Lambda via its
own boto3 GetItem — not through this module. There is no in-backend reverse
lookup: the Plan-3 ARN -> owner/tier resolvers that used to live here
(``resolve_owner_and_tier_from_aip_arn``, ``resolve_owner_from_aip_arn``,
``resolve_tier_from_aip_arn``) had zero production callers and were deleted.

Shared contracts other plans import:
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

# Our own AWS account (dev + prod share account 877352799272). Named once here
# so aws_cfn / connections can reference it without an STS call; runtime ARNs
# elsewhere in the module use _caller_account_id() instead.
OUR_ACCOUNT_ID: str = "877352799272"

# The IAM role name of the read-only connector Lightbulb stands up in its OWN
# account for the AWS scope-tiers rail. Lives here (the base module both aws_cfn
# and connections import) so the literal has a single home — aws_cfn already
# imports bedrock_aip, so the constant can't live there without a cycle.
# Also the role an owner creates in THEIR OWN account: the name is pinned by the
# quick-create CloudFormation template (apps/infra/aws/aws-connector.yaml
# RoleName), the service-stack iam:GetRole / sts:AssumeRole resource ARNs, and
# the baked aws-exec-profile plugin's AWS_CONNECTOR_PROFILE. Renaming it means
# renaming all four in lockstep and relaunching every quick-create stack.
CONNECTOR_ROLE_NAME = "lightbulb-connector"

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


def build_owner_litestream_s3_policy(owner_id: str, bucket: str) -> dict:
    """Per-owner Litestream S3 grant, scoped to the owner's replica prefix.

    The shared OwnerBedrockBoundary caps this bucket-wide; this inline policy
    is the per-owner prefix fence (same shape as the EFS access-point fence).
    ListBucket is prefix-conditioned; GetBucketLocation carries no prefix
    semantics so it is granted unconditioned on the bucket. DeleteObject is
    required — litestream retention/compaction prunes old LTX files.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReplicaBucketLocation",
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation"],
                "Resource": f"arn:aws:s3:::{bucket}",
            },
            {
                "Sid": "ReplicaBucketList",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": [f"{owner_id}/*"]}},
            },
            {
                "Sid": "ReplicaObjects",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": f"arn:aws:s3:::{bucket}/{owner_id}/*",
            },
        ],
    }


def build_owner_bedrock_policy(
    *,
    owner_id: str,
    aip_arns: dict[str, str],
    regions: tuple[str, ...],
) -> dict:
    """Build the per-owner Bedrock inline IAM policy document (PURE — no boto3).

    THE FENCE: the owner role carries NO ``bedrock:Invoke*`` — every inference
    call goes through the bedrock-gate (the container authenticates to the gate
    with its bearer token; the GATE's role invokes the owner's AIP). This is
    what closes all three direct-spend routes at once: the OpenClaw provider
    and the ACP harness are repointed at the gate by config/env, and the
    ``exec: full`` shell path — which no proxy can see — simply AccessDenies.

    What remains:
    - ``Deny bedrock:CreateInferenceProfile`` — belt-and-suspenders against a
      tenant minting a profile (nothing grants Bedrock anyway, but an explicit
      Deny survives any future accidental Allow via base policy drift).

    The PRE-GATEWAY direct-invoke shape lives on ONLY as
    ``build_owner_bedrock_direct_policy`` for the break-glass script — an
    emergency where the gate is down and the fleet must temporarily invoke
    Bedrock directly (see docs/runbooks/bedrock-gate-break-glass.md).
    """
    del aip_arns, regions  # shape kept for call-site stability; unused now
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyTenantProfileEscalation",
                "Effect": "Deny",
                "Action": "bedrock:CreateInferenceProfile",
                "Resource": "*",
            },
        ],
    }


def build_owner_bedrock_direct_policy(
    *,
    owner_id: str,
    aip_arns: dict[str, str],
    regions: tuple[str, ...],
) -> dict:
    """BREAK-GLASS ONLY: the pre-gateway direct-invoke policy document.

    Used exclusively by ``scripts/bedrock_gate_break_glass.py`` to restore
    direct container→Bedrock invocation when the gate is down. Invoking THROUGH
    an application inference profile authorizes TWO resources on each call: the
    profile (AIP) ARN itself, and the underlying foundation-model ARN:

    - The AIP ARN is allowed UNCONDITIONALLY. AWS does NOT populate the
      ``bedrock:InferenceProfileArn`` condition key when authorizing the profile
      RESOURCE itself, so conditioning that statement denies the invoke outright
      (``AccessDeniedException`` — verified live + via simulate-principal-policy).
      Tenant isolation still holds: only THIS owner's AIP ARNs are listed.
    - The foundation-model ARNs ARE gated on
      ``bedrock:InferenceProfileArn == {this owner's AIPs}`` so the raw FMs can be
      reached ONLY via this owner's profile — no direct-FM invoke that would
      bypass the AIP and escape per-owner metering attribution.
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
    plan: str = "paid",
) -> None:
    """Persist the per-owner AIP map (and optional role ARN) to BOTH the
    containers row and the bedrock-aip-owners reverse table.

    The reverse table is the read path for the bedrock-usage-meter Lambda
    (its own boto3 GetItem, not any function in this module); the containers
    row is the read path for the config builder + resize/restart.

    `plan` ("paid" or "trial") is stamped onto every reverse row. The meter
    Lambda already does exactly one GetItem on this table per invocation to
    attribute owner + tier — `plan` rides along for free in that same read,
    letting the Lambda skip Metronome ingest for trial owners (who have no
    Stripe customer / Metronome contract) without a second lookup.
    """
    fields: dict = {"bedrock_aip_arns": aip_arns}
    if role_arn is not None:
        fields["bedrock_role_arn"] = role_arn
    await container_repo.update_fields(owner_id, fields)

    table = _aip_owners_table()
    for tier, arn in aip_arns.items():
        await _put_reverse_row(table, arn=arn, owner_id=owner_id, tier=tier, plan=plan)


async def _put_reverse_row(table, *, arn: str, owner_id: str, tier: str, plan: str = "paid") -> None:
    from core.dynamodb import run_in_thread

    await run_in_thread(
        table.put_item,
        Item={"aip_arn": arn, "owner_id": owner_id, "tier": tier, "plan": plan},
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
    """Env-prefixed AIP name so dev/prod never collide on a global name.

    Changing this does NOT rename anything for an existing owner. Bedrock has no
    update/rename operation for application inference profiles — only create,
    delete, get, list — and ``ensure_owner_aips`` skips any tier already present
    in the owner's stored ARN map (``if tier in arns: continue``). So an owner
    who already holds a sonnet ARN keeps their old-named profile forever; only a
    fresh owner, or one whose map has been cleared, mints under this name.

    Migrating an existing owner therefore means: clear their stored map, let a
    provision mint fresh profiles, and repoint every place the OLD ARN was
    persisted — the containers row, the reverse table (whose PRIMARY KEY is the
    arn), the gate-token row's aip_tiers, the task-def model env, and
    openclaw.json on EFS. Handled by the one-shot rename script (deleted
    2026-09-11; see git history).
    """
    return f"lightbulb-{_env()}-{owner_id}-{tier}"


async def ensure_owner_aips(owner_id: str, *, tiers: list[str] | None = None) -> dict[str, str]:
    """Idempotently create the per-owner AIPs for `tiers` (one per tier);
    return {tier: aip_arn}. Persists to the containers row + reverse table.

    `tiers=None` (the default) mints all 3 tiers (opus/sonnet/haiku) — today's
    paid-owner behavior, unchanged in call shape or effect. `tiers=["sonnet"]`
    mints ONLY that tier — the free-trial shape. This is the security
    boundary, not just a cost optimization: build_owner_bedrock_policy scopes
    the owner's IAM role to exactly the AIP ARNs it is given, so a
    single-tier mint means the resulting role can invoke only that one model
    — a trial owner cannot reach Opus even by rewriting their own
    openclaw.json, because the container has no credential path to it.

    The reverse-row `plan` attribute mirrors the same split: "paid" when
    `tiers` is None, "trial" otherwise. See persist_owner_aips for why this
    is free for the meter Lambda to read.

    Idempotent: if all requested tiers are already persisted, returns them
    without any Bedrock control-plane call — but STILL re-puts the
    reverse-index rows (review C1). The row update and the reverse PutItems in
    persist_owner_aips are NOT atomic; a prior partial failure (row written,
    one reverse PutItem failed) leaves the meter Lambda's reverse-table read
    unable to attribute that AIP. The reverse PutItems are idempotent (same
    key, same value), so re-running them on the short-circuit path is cheap
    and self-heals the index.
    """
    from core.dynamodb import run_in_thread

    target_tiers: tuple[str, ...] = tuple(tiers) if tiers is not None else TIERS
    plan = "paid" if tiers is None else "trial"

    existing = await read_owner_aips(owner_id)
    if existing and all(tier in existing for tier in target_tiers):
        # Self-heal the reverse index in case a prior run's reverse PutItems
        # partially failed after the row was written (C1 inconsistency window).
        table = _aip_owners_table()
        for tier, arn in existing.items():
            await _put_reverse_row(table, arn=arn, owner_id=owner_id, tier=tier, plan=plan)
        return existing

    client = _bedrock_client()
    account_id = await _caller_account_id()
    arns: dict[str, str] = dict(existing or {})

    # Re-stamp the plan on the tiers we ALREADY hold, BEFORE minting the rest.
    #
    # ``plan`` is a property of the OWNER — of which tier-set the caller asked
    # for — not of whether the mint below succeeds. persist_owner_aips runs only
    # at the END of this function, so without this, a raise from any
    # create_inference_profile would leave a CONVERTED owner's pre-existing
    # sonnet row still stamped "trial". The meter Lambda reads exactly that row
    # to decide whether to apply the $3/day cap and the IAM-revoke + ECS
    # desiredCount=0 kill. One transient haiku mint failure during a
    # trial->paid upgrade would therefore cut a PAYING customer off from Bedrock
    # and stop their container. Stamping first makes the plan correct the moment
    # we know it, and the reverse PutItems are idempotent so this is cheap.
    if arns:
        table = _aip_owners_table()
        for tier, arn in arns.items():
            await _put_reverse_row(table, arn=arn, owner_id=owner_id, tier=tier, plan=plan)

    for tier in target_tiers:
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
                    {"key": "ManagedBy", "value": "lightbulb-backend"},
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

    await persist_owner_aips(owner_id, arns, plan=plan)
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
    'lightbulb-{env}-owner-bedrock-*' prefix, so this MUST start with it."""
    return f"lightbulb-{_env()}-owner-bedrock-{owner_id}"


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
    + permissions boundary but does NOT (re)put the scoped Bedrock inline policy.
    Two callers pass it:
      * provision for a NON-Bedrock owner (chatgpt_oauth / byo_key) so every
        owner gets a unique per-owner task-role principal (the AWS read-only
        integration pins each owner's cross-account trust to it) WITHOUT being
        granted Bedrock — such an owner has no AIPs, so ``aip_arns`` is ``{}``
        and goes unread here.
      * provision for a REVOKED Bedrock owner so a re-provision/restart does not
        silently un-revoke the kill-switch (review C6). Plan 4's
        restore_owner_bedrock is the only path that re-grants Bedrock.
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

    # local-sqlite layout: per-owner Litestream S3 grant. UNCONDITIONAL —
    # unlike the Bedrock grant above, every provider ships replicas and a
    # revoked/trial-capped owner keeps durability (skip_bedrock_grant governs
    # inference, not storage). A SEPARATE PolicyName: the break-glass script
    # swaps the "owner-bedrock" document wholesale, and folding S3 into it
    # would strip replication mid-incident. Inert while LITESTREAM_BUCKET is
    # unset (EFS layout). Existing owners provisioned before the env flipped
    # are backfilled by scripts/ensure_litestream_s3_policies.py — run it
    # BEFORE any fleet force-apply onto the local-sqlite boot chain, or litestream (the
    # boot supervisor) AccessDenies and the task crash-loops.
    if settings.LITESTREAM_BUCKET:
        await run_in_thread(
            client.put_role_policy,
            RoleName=role_name,
            PolicyName="owner-litestream-s3",
            PolicyDocument=json.dumps(build_owner_litestream_s3_policy(owner_id, settings.LITESTREAM_BUCKET)),
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
            {"Key": "ManagedBy", "Value": "lightbulb-backend"},
            # Born with the conditioned trust policy → mark converged so the
            # existing-role path's backfill skips it on every later call.
            {"Key": "trust_hardened", "Value": "1"},
        ],
    }
    if settings.BEDROCK_OWNER_BOUNDARY_ARN:
        create_kwargs["PermissionsBoundary"] = settings.BEDROCK_OWNER_BOUNDARY_ARN
    resp = await run_in_thread(client.create_role, **create_kwargs)
    return resp["Role"]["Arn"]


async def put_owner_bedrock_policy(owner_id: str, policy_doc: dict) -> None:
    """BREAK-GLASS seam: put an EXPLICIT inline policy document on the owner
    role. Used only by ``scripts/bedrock_gate_break_glass.py`` to swap between
    the fence policy (``build_owner_bedrock_policy``, no invoke) and the
    emergency direct-invoke policy (``build_owner_bedrock_direct_policy``).
    Normal provisioning never calls this — ``ensure_owner_bedrock_role`` always
    writes the fence policy.
    """
    import json as _json

    from core.dynamodb import run_in_thread

    client = _iam_client()
    await run_in_thread(
        client.put_role_policy,
        RoleName=_owner_role_name(owner_id),
        PolicyName="owner-bedrock",
        PolicyDocument=_json.dumps(policy_doc),
    )


def aip_fan_out_regions() -> tuple[str, ...]:
    """Public alias of the configured AIP fan-out regions (break-glass script)."""
    return _fan_out_regions()


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

    # bedrock-gate transition: ALSO flip the deny flag on the owner's gate-token
    # row so a gateway'd owner is stopped at the gate too (the gateway invokes
    # under its OWN role, so the IAM revoke above does not reach it). Both fences
    # are held during the transition phase. Best-effort — a deny-token failure
    # must NOT break the IAM revoke, which is the primary fence; no token row
    # (non-gateway owner) is a silent no-op inside deny_gate_token.
    from core.billing.bedrock_gate_tokens import deny_gate_token

    try:
        await deny_gate_token(owner_id, True)
    except Exception as e:  # noqa: BLE001 — deny is a backstop, not the fence
        logger.warning("deny_gate_token(True) failed for %s: %s", owner_id, e)


async def delete_owner_aips(owner_id: str, *, aip_arns: list[str] | None = None) -> list[str]:
    """SHARED CONTRACT (Admin Account-Wipe, teardown step 6-7): delete every
    persisted AIP for `owner_id` from Bedrock, plus its bedrock-aip-owners
    reverse row, and return the list of ARNs removed.

    ``aip_arns``, if provided (not None), is used DIRECTLY instead of
    re-reading the containers row via read_owner_aips. This matters because
    the wipe orchestrator's `ecs_service` step (which runs before this one)
    deletes the containers row as a side effect of
    ``ecs_manager.delete_user_service`` — a live re-read at this point would
    see no row and silently no-op, orphaning both the Bedrock inference
    profile and the reverse-table row while still reporting success. Callers
    that already hold the ARNs (e.g. the wipe orchestrator's upfront
    identifier snapshot) MUST pass them here instead of relying on the
    containers row still existing.

    When ``aip_arns`` is None (the default), falls back to the original
    behavior: read the containers-row map via read_owner_aips (the module's
    single source of truth for an owner's AIPs outside of a wipe). Returns
    `[]` (no Bedrock/DDB calls) when there is nothing to delete, so callers
    can invoke this unconditionally.

    Idempotent: a missing AIP (ResourceNotFoundException — already deleted by
    a prior partial wipe, or never actually minted) is treated as
    already-gone; the reverse row is still deleted (DynamoDB DeleteItem is a
    no-op on a missing key) and the ARN still counts as removed.
    """
    from core.dynamodb import run_in_thread

    if aip_arns is not None:
        arns_to_delete = list(aip_arns)
    else:
        arns = await read_owner_aips(owner_id)
        if not arns:
            return []
        arns_to_delete = list(arns.values())

    if not arns_to_delete:
        return []

    client = _bedrock_client()
    table = _aip_owners_table()
    removed: list[str] = []
    failure: Exception | None = None
    for arn in arns_to_delete:
        try:
            try:
                await run_in_thread(client.delete_inference_profile, inferenceProfileIdentifier=arn)
            except ClientError as e:
                # See revoke_owner_bedrock for why a single `except ClientError`
                # covers both the real typed exception and the test's generic one.
                if e.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
            await run_in_thread(table.delete_item, Key={"aip_arn": arn})
            removed.append(arn)
        except Exception as e:  # noqa: BLE001 — re-raised below, after every arn is tried
            # EVERY arn is attempted before giving up, mirroring
            # delete_gate_token_rows. By the time teardown calls this the
            # containers row that named these is already gone, so aborting the
            # loop on a transient throttle at arn #2 strands arn #3 forever —
            # nothing can ever name it again. The reverse row is deliberately
            # LEFT for a failed arn: it is then the only surviving record that
            # the profile is still out there.
            failure = failure or e
    if failure is not None:
        raise failure
    return removed


async def delete_owner_bedrock_role(owner_id: str) -> None:
    """SHARED CONTRACT (Admin Account-Wipe, teardown step 8): tear down the
    per-owner IAM role in the same order it was built up — inline scoped
    policy, then the (CDK-managed, shared) base policy attachment, then the
    role itself.

    Detaches the base managed policy (`settings.BEDROCK_OWNER_BASE_POLICY_ARN`
    — the same constant ensure_owner_bedrock_role attaches) but NEVER deletes
    it; it is a shared CDK-managed resource used by every owner's role.

    Idempotent: each step swallows NoSuchEntity (role/policy/attachment
    already gone), matching revoke_owner_bedrock's pattern — safe to re-run
    after a partial wipe failure.
    """
    from core.dynamodb import run_in_thread

    client = _iam_client()
    role_name = _owner_role_name(owner_id)

    # BOTH inline policies must go: IAM refuses DeleteRole while any inline
    # policy remains, so leaving owner-litestream-s3 behind would make the
    # account-wipe bedrock_role step fail for every local-sqlite owner and leak the role.
    for policy_name in ("owner-bedrock", "owner-litestream-s3"):
        try:
            await run_in_thread(
                client.delete_role_policy,
                RoleName=role_name,
                PolicyName=policy_name,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise

    # Detach whatever is ACTUALLY attached, not the ARN the current setting
    # names. A role minted before a policy-ARN change still carries the OLD
    # attachment; detaching the new ARN returns NoSuchEntity, which is swallowed
    # -- so the old attachment survives, DeleteRole below fails DeleteConflict,
    # the role leaks, and the old policy's AttachmentCount can never reach zero,
    # meaning it can never be deleted either. Enumerating removes the class.
    try:
        attached = await run_in_thread(client.list_attached_role_policies, RoleName=role_name)
        for policy in attached.get("AttachedPolicies", []):
            await run_in_thread(
                client.detach_role_policy,
                RoleName=role_name,
                PolicyArn=policy["PolicyArn"],
            )
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    try:
        await run_in_thread(client.delete_role, RoleName=role_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise


async def reap_owner_bedrock_resources(owner_id: str, row_image: dict | None) -> str:
    """SHARED CONTRACT (every teardown path): destroy the per-owner Bedrock
    footprint that the containers row was the only handle on.

    Called from ``ecs_manager.delete_user_service`` — the seam EVERY teardown
    crosses (entitlement cancellation, admin account-wipe, ``DELETE
    /debug/provision``, ``DELETE /debug/user-data``) — so no path can forget it
    and none needs its own copy. It runs immediately after the containers row is
    deleted, against that row's own ALL_OLD image, because that image is the
    last thing in the system that names any of this:

    * **gate-token rows** — the tokens table has NO owner index, so the row's
      ``bedrock_gate_token_hash`` plus its superseded list is the only handle
      that exists. Left behind they are unreachable, un-denyable and
      un-expirable forever: a captured bearer hash would stay valid against the
      gate with no revocation path (four were stranded exactly this way in
      prod, all at ``denied=False``).
    * **AIPs** — named only by ``bedrock_aip_arns``.
    * **the IAM role** — the one resource with a deterministic name
      (``lightbulb-{env}-owner-bedrock-{owner_id}``), so it is reaped
      UNCONDITIONALLY rather than gated on ``bedrock_role_arn`` being present
      on the row. Gating on that field is precisely what let legacy rows skip
      it silently.

    Dev, staging and prod share ONE AWS account, so a leak on any path spends
    the same account-wide IAM role quota (1,000) this exists to protect.

    Best-effort and idempotent per resource, but every failure is collected and
    the first re-raised, so an audited caller reports a partial reap rather
    than claiming success.
    """
    from core.billing.bedrock_gate_tokens import delete_gate_token_rows, owner_gate_token_hashes
    from core.observability.metrics import put_metric

    row = row_image if isinstance(row_image, dict) else {}
    aip_map = row.get("bedrock_aip_arns")
    aip_arns = list(aip_map.values()) if isinstance(aip_map, dict) else []
    gate_hashes = owner_gate_token_hashes(row)

    # A ChatGPT-OAuth or BYOK owner never had any of this. IAM write TPS is low,
    # and four no-op calls for every cancelling owner is enough to throttle IAM
    # account-wide during a bulk-churn event and stall legitimate provisioning.
    # No evidence on the row means nothing was ever minted: skip without
    # touching AWS at all.
    if not (aip_arns or gate_hashes or row.get("bedrock_role_arn")):
        return "skipped (no Bedrock footprint on the row)"

    # FENCE — do not reap resources that belong to a LIVE container.
    #
    # Every identifier here is deterministic or re-minted under the same owner,
    # so a resubscribe's provision re-creates the very things this reap would
    # delete. A cancellation webhook redelivered after that resubscribe (both
    # Stripe and Apple retry on 5xx) would otherwise strip a paying owner's
    # running container of its Bedrock access — and the AIPs have NO self-heal,
    # because the provision that re-created them already returned success, so
    # nothing retries and the owner's chat just hard-fails on Bedrock.
    #
    # The containers row was deleted moments ago. If one exists again, a
    # provision has run since and these are ITS resources now, not the dead
    # generation's. Strongly consistent because a stale empty read here is
    # exactly the wrong answer.
    live = await container_repo.get_by_owner_id(owner_id, consistent=True)
    if live is not None:
        put_metric("bedrock.reap", dimensions={"outcome": "fenced"})
        logger.warning(
            "bedrock reap FENCED for owner %s: a containers row exists again (generation=%s) — "
            "a provision raced this teardown, so its Bedrock resources are left alone",
            owner_id,
            live.get("provision_generation"),
        )
        return "skipped (fenced: reprovisioned mid-teardown)"

    failure: Exception | None = None
    done: list[str] = []

    # Credential first: it is the security-critical one, and the only one whose
    # residue is a still-valid bearer token rather than a quota row.
    if gate_hashes:
        try:
            done.append(f"{await delete_gate_token_rows(gate_hashes)} gate-token row(s)")
        except Exception as e:  # noqa: BLE001 — re-raised below
            failure = failure or e
            put_metric("bedrock.reap", dimensions={"outcome": "failed", "resource": "gate_tokens"})
            logger.exception("bedrock reap: gate-token rows failed for owner %s", owner_id)

    if aip_arns:
        try:
            done.append(f"{len(await delete_owner_aips(owner_id, aip_arns=aip_arns))} AIP(s)")
        except Exception as e:  # noqa: BLE001 — re-raised below
            failure = failure or e
            put_metric("bedrock.reap", dimensions={"outcome": "failed", "resource": "aips"})
            logger.exception("bedrock reap: AIPs failed for owner %s", owner_id)

    try:
        await delete_owner_bedrock_role(owner_id)
        done.append("IAM role")
    except Exception as e:  # noqa: BLE001 — re-raised below
        failure = failure or e
        put_metric("bedrock.reap", dimensions={"outcome": "failed", "resource": "role"})
        logger.exception("bedrock reap: IAM role failed for owner %s", owner_id)

    if failure is not None:
        raise failure
    put_metric("bedrock.reap", dimensions={"outcome": "ok"})
    return "reaped " + ", ".join(done)


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

    # Mirror revoke_owner_bedrock: clear the deny flag on the owner's gate-token
    # row so a gateway'd owner is un-blocked at the gate alongside the IAM
    # re-grant. Best-effort (no token row is a silent no-op).
    from core.billing.bedrock_gate_tokens import deny_gate_token

    try:
        await deny_gate_token(owner_id, False)
    except Exception as e:  # noqa: BLE001 — deny is a backstop, not the fence
        logger.warning("deny_gate_token(False) failed for %s: %s", owner_id, e)
