"""Per-workspace AgentCore Runtime lifecycle.

One runtime per workspace, which AWS forces rather than us choosing it:
``filesystemConfigurations`` are set on the RUNTIME and mount into every session, so an
owner's EFS access point cannot be shared across a multi-tenant runtime.

That same shape gives each workspace its own execution role, which is AWS's own recommended
multi-tenant control — the docs are explicit that AgentCore does not enforce that a sessionId
belongs to the calling user, and name per-tenant IAM principals as the fix.

**The role is the EXISTING per-owner ECS task role, re-trusted, not a new one.**
``lightbulb-{env}-owner-bedrock-{owner}`` already carries what a gateway needs: the owner's
Bedrock application-inference profile, the litestream S3 prefix, EFS mount rights and AppSync
publish. Minting a second role would duplicate all of it and drift. During the migration the
role trusts BOTH ``ecs-tasks.amazonaws.com`` and ``bedrock-agentcore.amazonaws.com`` so a
workspace can be flipped without a race; the ECS statement is dropped once the fleet has moved.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from core.config import settings
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)

AGENTCORE_SERVICE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"

#: AgentCore runtime names take letters, digits and underscores, and must start with a letter.
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]")
#: Keep clear of the service's name ceiling while staying unique per owner.
_MAX_NAME_LEN = 48


def _env() -> str:
    return settings.ENVIRONMENT


def runtime_name_for(owner_id: str) -> str:
    """A stable, AWS-legal runtime name for this workspace.

    Owner ids carry characters AgentCore rejects and can exceed the name ceiling, so the name
    is a sanitised prefix plus a hash of the FULL id. The hash is what keeps two owners whose
    ids share a sanitised prefix from colliding onto one runtime — which would put two
    workspaces on one EFS mount.
    """
    digest = hashlib.sha256(owner_id.encode()).hexdigest()[:12]
    stem = _NAME_SAFE.sub("_", owner_id)
    prefix = f"lb_{_env()}_"
    room = _MAX_NAME_LEN - len(prefix) - len(digest) - 1
    return f"{prefix}{stem[: max(room, 0)]}_{digest}"


def trust_policy_with_agentcore(existing: dict | None, account_id: str) -> dict:
    """Add the AgentCore service principal to an owner role's trust policy, idempotently.

    Additive on purpose: during the migration a workspace may still be served by its ECS task,
    and removing that statement while the task is live would break it mid-turn. The ECS
    statement is dropped separately, after the fleet has moved.
    """
    doc = json.loads(json.dumps(existing)) if existing else {"Version": "2012-10-17", "Statement": []}
    statements = doc.setdefault("Statement", [])
    if not isinstance(statements, list):
        statements = [statements]
        doc["Statement"] = statements
    for statement in statements:
        principal = (statement.get("Principal") or {}).get("Service")
        services = principal if isinstance(principal, list) else [principal]
        if AGENTCORE_SERVICE_PRINCIPAL in services:
            return doc  # already trusted; do not append a duplicate
    statements.append(
        {
            "Effect": "Allow",
            "Principal": {"Service": AGENTCORE_SERVICE_PRINCIPAL},
            "Action": "sts:AssumeRole",
            "Condition": {
                # Same confused-deputy fence the ECS statement uses: our account, and only
                # AgentCore resources within it.
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{settings.AWS_REGION}:{account_id}:*"},
            },
        }
    )
    return doc


async def ensure_agentcore_trust(owner_id: str, role_name: str, account_id: str) -> bool:
    """Make the owner's role assumable by AgentCore. Returns True when it changed anything."""
    import boto3

    iam = boto3.client("iam")
    got = await run_in_thread(iam.get_role, RoleName=role_name)
    current = got["Role"].get("AssumeRolePolicyDocument")
    updated = trust_policy_with_agentcore(current, account_id)
    if updated == current:
        return False
    await run_in_thread(iam.update_assume_role_policy, RoleName=role_name, PolicyDocument=json.dumps(updated))
    logger.info("agentcore trust added owner=%s role=%s", owner_id, role_name)
    return True


def build_runtime_request(
    *,
    owner_id: str,
    image_uri: str,
    role_arn: str,
    efs_access_point_arn: str,
    subnets: list[str],
    security_groups: list[str],
    idle_timeout_s: int,
    max_lifetime_s: int,
) -> dict:
    """The CreateAgentRuntime request for one workspace.

    VPC mode is required, not preferred: the gateway must reach Paperclip over Cloud Map
    private DNS and mount the owner's EFS access point. Both were verified working from a
    microVM in dev on 2026-09-16.
    """
    return {
        "agentRuntimeName": runtime_name_for(owner_id),
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role_arn,
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {"securityGroups": security_groups, "subnets": subnets},
        },
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        # Sleep is not optional on this platform: there is no always-on. The idle timeout is
        # the product decision (4h), maxLifetime is AWS's hard ceiling.
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": idle_timeout_s,
            "maxLifetime": max_lifetime_s,
        },
        "filesystemConfigurations": [
            {"efsAccessPoint": {"accessPointArn": efs_access_point_arn, "mountPath": "/mnt/oc"}}
        ],
        "tags": {"owner_id": owner_id, "Environment": _env(), "ManagedBy": "lightbulb-backend"},
    }
