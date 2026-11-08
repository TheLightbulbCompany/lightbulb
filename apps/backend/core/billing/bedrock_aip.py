"""The per-owner IAM role (each workspace's AgentCore execution role) and its policies.

What lives here: the role itself (``ensure_owner_bedrock_role`` / ``delete_owner_bedrock_role``),
its S3, CloudWatch Logs, AgentCore Memory and AgentCore Browser inline policies, the teardown reap
(``reap_owner_bedrock_resources``), and ``delete_owner_aips`` -- kept only so teardown and
``scripts/reap_owner_aips.py`` can remove the Bedrock application inference profiles an older
backend minted. Nothing mints AIPs any more. The module name is a scar from when the role's job
was scoping Bedrock access.

Everything is a FREE FUNCTION so callers can import these without constructing a client class.
"""

from __future__ import annotations

import json
import logging

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


def build_owner_session_policy(owner_id: str, bucket: str, memory_arn: str = "", browsers: list[str] = ()) -> dict:
    """The session policy on one owner's runtime credentials (runtime_transport): only that
    owner's ``<owner>/*`` objects and, with a memory, its own memory events and records. AWS
    intersects it with the vending role, so everything this does not allow is denied. No
    Sids: an inline session policy is capped at 2,048 characters.

    ``browsers`` (``browser_arns``): the screen driver's ``InvokeBrowser`` on the session the
    backend started (bridge ``browser_control.invoke``), as the per-owner ``owner-browser`` grant
    has it. That action authorizes on the browser, not a profile, so it is no narrower there
    either: sessions are found only by id, and only the backend may list them."""
    statements = build_owner_litestream_s3_policy(owner_id, bucket)["Statement"]
    if memory_arn:
        statements += _owner_memory_allows(owner_id, memory_arn)
    if browsers:
        statements.append(
            {"Effect": "Allow", "Action": ["bedrock-agentcore:InvokeBrowser"], "Resource": list(browsers)}
        )
    return {"Version": "2012-10-17", "Statement": [{k: v for k, v in st.items() if k != "Sid"} for st in statements]}


#: AWS's built-in managed browser. Its ARN sits under the literal ``aws`` pseudo-account
#: (research 01 §5, from a live AccessDenied), not ours — see the resources below.
BUILTIN_BROWSER_ID = "aws.browser.v1"
#: The browser every workspace session starts on: the env's VPC-mode custom browser, which
#: leaves from the NAT's fixed address (the built-in one takes a new AWS address per session,
#: and sites such as Google sign a moved session out). The built-in one where unset.
BROWSER_ID = settings.AGENTCORE_BROWSER_ID or BUILTIN_BROWSER_ID


def browser_arns(region: str, account_id: str) -> list[str]:
    """The workspace browser's ARN, plus the built-in one: a session started there before the
    move must still be reachable until it ends. A custom browser's ARN is ours, under
    ``browser-custom/``."""
    builtin = f"arn:aws:bedrock-agentcore:{region}:aws:browser/{BUILTIN_BROWSER_ID}"
    if BROWSER_ID == BUILTIN_BROWSER_ID:
        return [builtin]
    return [f"arn:aws:bedrock-agentcore:{region}:{account_id}:browser-custom/{BROWSER_ID}", builtin]


def build_owner_logs_policy(owner_id: str, account_id: str) -> dict:
    """CloudWatch Logs for this workspace's OWN runtime log group only.

    AgentCore names it ``/aws/bedrock-agentcore/runtimes/<runtimeId>-<endpoint>`` and the
    runtime id is ``<runtime_name_for(owner)>-<suffix>``, so ``<name>-*`` is the group plus its
    streams (``*`` crosses the ``:log-stream:`` colon). The name carries a hash of the owner id,
    which an IAM policy variable cannot compute -- hence per-owner inline, not the shared base
    policy. The agent's shell can reach this role over the metadata service, so a shared
    ``logs:*`` on ``*`` let it write into any log group in the account (the backend's included).
    """
    from core.services.agentcore_runtime import runtime_name_for

    group = f"/aws/bedrock-agentcore/runtimes/{runtime_name_for(owner_id)}-*"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OwnRuntimeLogs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{settings.AWS_REGION}:{account_id}:log-group:{group}",
            }
        ],
    }


async def ensure_owner_browser_policy(owner_id: str, profile_id: str) -> None:
    """Per-owner AgentCore Browser grant, inline on the owner's runtime role.

    Its OWN ``PolicyName``, like ``owner-litestream-s3``: one grant per document, so each can
    be put and removed on its own.

    Two statements. The first is the built-in browser, whose ARN lives under the literal
    ``aws`` pseudo-account; the second is **this workspace's own profile**, and it repeats
    ``StartBrowserSession`` deliberately — a start carrying ``profileConfiguration``
    authorizes against the PROFILE ARN as well as the browser ARN (a live 403 named it).

    Deliberately absent: ``ListBrowserSessions`` (the fence that stops a rogue runtime
    learning another tenant's session ids — only the backend discovers sessions) and every
    control-plane action (a runtime minting profiles would eat the 100/account quota).

    A no-op for an empty ``profile_id``: ``browser_sessions.ensure_browser_profile``
    returns ``""`` when AWS refused, and an unscoped grant must not be written in its place.
    """
    from core.dynamodb import run_in_thread

    if not profile_id or settings.HERMES_SHARED_RUNTIME_ARN:
        return  # on the shared runtime the session's scoped credentials carry InvokeBrowser
    region = settings.AWS_REGION
    account_id = await _caller_account_id()
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AgentCoreBrowserSessions",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:StartBrowserSession",
                    "bedrock-agentcore:GetBrowserSession",
                    "bedrock-agentcore:StopBrowserSession",
                    "bedrock-agentcore:ConnectBrowserAutomationStream",
                    # computer_use (cua_driver.py): real mouse/keyboard/screenshots (Chrome's own UI).
                    "bedrock-agentcore:InvokeBrowser",
                ],
                # ONLY the workspace browser (and the built-in one): both, and nothing wider,
                # sit inside the CDK permissions boundary.
                "Resource": browser_arns(region, account_id),
            },
            {
                "Sid": "AgentCoreBrowserOwnProfile",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:StartBrowserSession",
                    "bedrock-agentcore:SaveBrowserSessionProfile",
                ],
                "Resource": f"arn:aws:bedrock-agentcore:{region}:{account_id}:browser-profile/{profile_id}",
            },
        ],
    }
    await run_in_thread(
        _iam_client().put_role_policy,
        RoleName=_owner_role_name(owner_id),
        PolicyName="owner-browser",
        PolicyDocument=json.dumps(doc),
    )


def _owner_memory_allows(owner_id: str, memory_arn: str) -> list[dict]:
    """This owner's events and records on the shared memory (see ensure_owner_memory_policy)."""
    from core.services.workspace_memory import owner_namespace

    return [
        {
            "Sid": "WorkspaceMemoryOwnEvents",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:CreateEvent",
                "bedrock-agentcore:ListEvents",
                "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListSessions",
            ],
            "Resource": memory_arn,
            "Condition": {"StringEquals": {"bedrock-agentcore:actorId": owner_id}},
        },
        {
            "Sid": "WorkspaceMemoryMemberEvents",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:CreateEvent",
                "bedrock-agentcore:ListEvents",
                "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListSessions",
            ],
            "Resource": memory_arn,
            "Condition": {"StringLike": {"bedrock-agentcore:actorId": f"{owner_id}:user_*"}},
        },
        {
            "Sid": "WorkspaceMemoryOwnRecords",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:ListMemoryRecords"],
            "Resource": memory_arn,
            "Condition": {"StringEquals": {"bedrock-agentcore:namespace": owner_namespace(owner_id)}},
        },
        {
            "Sid": "WorkspaceMemoryMemberRecords",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:ListMemoryRecords"],
            "Resource": memory_arn,
            "Condition": {"StringLike": {"bedrock-agentcore:namespace": f"/owners/{owner_id}:user_*/"}},
        },
    ]


async def ensure_owner_memory_policy(owner_id: str) -> None:
    """Per-owner workspace-memory grant, inline on the owner's runtime role, as its own
    ``PolicyName`` (same reason as ``owner-browser``).

    The memory resource is SHARED by every tenant in the env, so this document IS the isolation --
    exactly the shape the 2026-09-22 dev spike proved (a second owner was AccessDenied on every
    cross-owner call, including ``CreateEvent`` under the first owner's actor id):

    - events only under ``actorId`` = this owner (team scope) or ``<owner>:user_*`` (one
      member's private scope);
    - records only in ``/owners/<owner>/`` or ``/owners/<owner>:user_*/`` (there is no
      ``namespacePath`` condition key; a prefix call fails closed under these Allows). Owner ids
      never contain ``:`` (``owner_namespace`` refuses one), so ``<owner>:`` cannot reach
      another owner. The private scope is a product boundary between members of ONE workspace,
      not a security boundary: every agent of the workspace runs under this one role.

    The private statements are separate Allows (OR), so the proven StringEquals pair keeps team
    memory working even if the StringLike pair were not honoured.
    - an explicit Deny on the actions that carry no condition keys at all -- allowed anywhere,
      they reach any tenant's record by id. The CDK boundary omits them too; this is the belt.

    A no-op when the env has no memory configured."""
    from core.dynamodb import run_in_thread

    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return
    memory_arn = f"arn:aws:bedrock-agentcore:{settings.AWS_REGION}:{await _caller_account_id()}:memory/{memory_id}"
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            *_owner_memory_allows(owner_id, memory_arn),
            {
                "Sid": "WorkspaceMemoryNoUnconditionable",
                "Effect": "Deny",
                "Action": [
                    "bedrock-agentcore:GetMemoryRecord",
                    "bedrock-agentcore:DeleteMemoryRecord",
                    "bedrock-agentcore:BatchCreateMemoryRecords",
                    "bedrock-agentcore:BatchUpdateMemoryRecords",
                    "bedrock-agentcore:BatchDeleteMemoryRecords",
                    "bedrock-agentcore:DeleteEvent",
                    "bedrock-agentcore:ListActors",
                ],
                "Resource": "*",
            },
        ],
    }
    await run_in_thread(
        _iam_client().put_role_policy,
        RoleName=_owner_role_name(owner_id),
        PolicyName="owner-memory",
        PolicyDocument=json.dumps(doc),
    )


def _bedrock_client():
    """Bedrock CONTROL-PLANE client (list/delete inference profiles for the AIP reap)."""
    return boto3.client("bedrock", region_name=settings.AWS_REGION)


async def _caller_account_id() -> str:
    """The AWS account id, via STS (sts:GetCallerIdentity needs no IAM grant). Resolved on the
    role's first-mint path only, so the common existing-role path makes no STS call."""
    from core.dynamodb import run_in_thread

    sts = boto3.client("sts", region_name=settings.AWS_REGION)
    ident = await run_in_thread(sts.get_caller_identity)
    return ident["Account"]


def _env() -> str:
    return settings.ENVIRONMENT or "dev"


def _iam_client():
    return boto3.client("iam", region_name=settings.AWS_REGION)


def _owner_role_name(owner_id: str) -> str:
    """Per-owner role name. The CDK PassRole/CreateRole grants scope to the
    'lightbulb-{env}-owner-bedrock-*' prefix, so this MUST start with it."""
    return f"lightbulb-{_env()}-owner-bedrock-{owner_id}"


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


async def ensure_owner_bedrock_role(owner_id: str) -> str:
    """Idempotently create/update the per-owner IAM role (the workspace's AgentCore execution
    role) and its base + S3 policies; return its ARN. Never re-creates an existing role.

    The role carries no Bedrock grant: every owner gets a unique principal (the AWS read-only
    integration pins each owner's cross-account trust to it). Roles minted by an older backend
    may still hold a deny-only ``owner-bedrock`` inline policy; it is harmless, and teardown
    enumerates inline policies so it never blocks ``delete_owner_bedrock_role``.
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
            from core.services.agentcore_runtime import trust_policy_with_agentcore

            account_id = role_arn.split(":")[4]
            try:
                # Superset, not ECS-only: a concurrent ensure_agentcore_trust may have
                # already added the AgentCore statement, and overwriting with the
                # ECS-only document here would silently clobber it back off.
                hardened_doc = trust_policy_with_agentcore(_assume_role_doc(account_id), account_id)
                await run_in_thread(
                    client.update_assume_role_policy,
                    RoleName=role_name,
                    PolicyDocument=json.dumps(hardened_doc),
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

    await run_in_thread(
        client.put_role_policy,
        RoleName=role_name,
        PolicyName="owner-logs",
        PolicyDocument=json.dumps(build_owner_logs_policy(owner_id, role_arn.split(":")[4])),
    )

    # (Re)attach the CDK-managed base policy — idempotent.
    if settings.BEDROCK_OWNER_BASE_POLICY_ARN:
        await run_in_thread(
            client.attach_role_policy,
            RoleName=role_name,
            PolicyArn=settings.BEDROCK_OWNER_BASE_POLICY_ARN,
        )

    # Per-owner S3 grant on the owner-state bucket: every owner's runtime reads and saves its own
    # profiles there.
    if settings.OWNER_STATE_BUCKET:
        await run_in_thread(
            client.put_role_policy,
            RoleName=role_name,
            PolicyName="owner-litestream-s3",
            PolicyDocument=json.dumps(build_owner_litestream_s3_policy(owner_id, settings.OWNER_STATE_BUCKET)),
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
    try:
        resp = await run_in_thread(client.create_role, **create_kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        # A concurrent provision run won the create race; adopt its role
        # instead of failing (the owner ruling: no leases, both runs converge).
        got = await run_in_thread(client.get_role, RoleName=role_name)
        return got["Role"]["Arn"]
    return resp["Role"]["Arn"]


async def delete_owner_aips(aip_arns: list[str]) -> list[str]:
    """Delete these Bedrock application inference profiles; return the ARNs removed.

    Nothing mints AIPs any more. Callers are teardown (``reap_owner_bedrock_resources``, with the
    ARNs from the deleted containers row's image) and ``scripts/reap_owner_aips.py``.

    Idempotent: a missing AIP (ResourceNotFoundException) counts as removed. EVERY arn is
    attempted before the first failure is re-raised, so a throttle on one cannot strand the rest.
    """
    from core.dynamodb import run_in_thread

    if not aip_arns:
        return []
    client = _bedrock_client()
    removed: list[str] = []
    failure: Exception | None = None
    for arn in aip_arns:
        try:
            await run_in_thread(client.delete_inference_profile, inferenceProfileIdentifier=arn)
        except ClientError as e:
            # boto3's typed exceptions ARE ClientError subclasses, so one clause covers both.
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                failure = failure or e
                continue
        except Exception as e:  # noqa: BLE001 — re-raised below, after every arn is tried
            failure = failure or e
            continue
        removed.append(arn)
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
    already gone) — safe to re-run
    after a partial wipe failure.
    """
    from core.dynamodb import run_in_thread

    client = _iam_client()
    role_name = _owner_role_name(owner_id)

    # ENUMERATE, never name. IAM refuses DeleteRole while ANY inline policy
    # remains, so a policy this list forgot leaks the role silently — and this
    # list had already been bolted onto twice (owner-litestream-s3, then
    # owner-browser). Same reasoning, and the same fix, as the managed-policy
    # detach below: "Enumerating removes the class."
    try:
        inline = await run_in_thread(client.list_role_policies, RoleName=role_name)
        policy_names = inline.get("PolicyNames", [])
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        policy_names = []
    for policy_name in policy_names:
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


async def _owner_role_exists(owner_id: str) -> bool:
    from core.dynamodb import run_in_thread

    try:
        await run_in_thread(_iam_client().get_role, RoleName=_owner_role_name(owner_id))
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            return False
        raise
    return True


async def reap_owner_bedrock_resources(
    owner_id: str, row_image: dict | None, *, delete_browser_profile: bool = True
) -> str:
    """SHARED CONTRACT (every teardown path): destroy the per-owner Bedrock
    footprint that the containers row was the only handle on.

    Called from ``hermes_provisioning.delete_workspace_runtime`` — the seam EVERY teardown
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
    # and a handful of no-op calls for every cancelling owner is enough to throttle
    # IAM account-wide during a bulk-churn event and stall legitimate provisioning.
    # No evidence on the row means nothing was ever minted: skip without
    # touching AWS at all.
    if not (aip_arns or gate_hashes or row.get("bedrock_role_arn")):
        # No row at all means an earlier teardown already deleted it, possibly after its
        # role delete failed. The role is the one resource with a deterministic name, so
        # one IAM read tells whether a re-run still owes it; without this a failed first
        # reap orphans the role forever. A row that exists but names nothing still costs
        # no AWS calls.
        if row_image is not None or not await _owner_role_exists(owner_id):
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
            done.append(f"{len(await delete_owner_aips(aip_arns))} AIP(s)")
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

    # The AgentCore Browser profile. Deterministic name (== owner_id), same as the role, so
    # it needs nothing from the row image. It holds the workspace's COOKIES and local
    # storage, which makes leaving it behind a data-deletion gap rather than a quota one.
    # Never contributes to `failure`: a wipe must not fail on the browser leg, whatever
    # the reason. The task role's DeleteBrowserProfile grant is #2794 (merged), so a
    # failure here is a real one and logs as such.
    from core.services.browser_sessions import delete_browser_profiles

    deleted_profiles = await delete_browser_profiles(owner_id) if delete_browser_profile else 0
    if deleted_profiles:
        done.append(f"{deleted_profiles} browser profile(s)")

    if failure is not None:
        raise failure
    put_metric("bedrock.reap", dimensions={"outcome": "ok"})
    return "reaped " + ", ".join(done)
