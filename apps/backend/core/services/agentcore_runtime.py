"""Per-workspace AgentCore Runtime naming, trust and boot secret.

One runtime per workspace. That gives each workspace its own execution role, which is AWS's own
recommended multi-tenant control — the docs are explicit that AgentCore does not enforce that a
sessionId belongs to the calling user, and name per-tenant IAM principals as the fix.

**The role is the per-owner Bedrock role, re-trusted, not a new one.**
``lightbulb-{env}-owner-bedrock-{owner}`` already carries what a runtime needs: the owner's
Bedrock application-inference profile, the owner-state S3 prefix and AppSync publish. Minting a
second role would duplicate all of it and drift.

The request/environment builders live in ``hermes_provisioning``
(``build_hermes_runtime_request`` / ``build_hermes_runtime_env``) — this module holds only what
is true of any runtime regardless of the image inside it.
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
    # m5: an unset ENVIRONMENT (empty string is Settings' own default) must never silently
    # produce a double-slash name/secret path (e.g. "lightbulb//hermes/<owner>/boot") -- that
    # falls outside every environment-scoped IAM grant. Matches the fallback the original brief
    # snippet already had for boot_secret_name; centralized here since runtime_name_for shares
    # this same helper.
    return settings.ENVIRONMENT or "dev"


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
        principal_block = statement.get("Principal")
        # A bare-string Principal ("*" or an ARN) is valid IAM JSON; it names no service.
        principal = principal_block.get("Service") if isinstance(principal_block, dict) else None
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


#: How long AgentCore keeps an idle session's microVM warm before reaping it (the owner's ruling:
#: long enough that a working day does not pay a cold start twice).
IDLE_TIMEOUT_S = 4 * 60 * 60
#: AWS's hard ceiling for a microVM session; the backend rolls sessions at a quiet point before it.
MAX_LIFETIME_S = 8 * 60 * 60


BOOT_SECRET_ENV_VAR = "LIGHTBULB_SECRET_ID"


def boot_secret_name(owner_id: str) -> str:
    """The name of this owner's one JSON boot secret."""
    return f"lightbulb/{_env()}/hermes/{owner_id}/boot"


async def write_boot_secret(
    owner_id: str,
    *,
    hook_token: str,
    gateway_token: str,
    bedrock_bearer: str,
    provider_choice: str,
) -> bool:
    """Converge the owner's boot secret (named ``boot_secret_name(owner_id)``, the value a
    runtime's ``BOOT_SECRET_ENV_VAR`` carries). Plaintext lives in exactly one place: this secret.

    Returns whether a BOOT-TIME input changed -- anything but ``providerChoice``, which no
    current bridge reads (it decides the route from the profile it pulls each turn) and which
    is still written only for N-1 bridges. A warm session reads the rest once, at cold start,
    so a True answer is the caller's cue to restart the idle session. Identical bytes are not
    rewritten, so concurrent runs converge without churning secret versions.

    ``providerSwitchFrozen: False`` stays for the same N-1 bridges, which refuse every turn when
    it is absent-and-truthy; ``providerSwitchGeneration`` is dropped -- those bridges admit a
    turn only when its (now never sent) generation header matches it."""
    import boto3

    from core.secrets_manager import create_or_update_secret

    name = boot_secret_name(owner_id)
    payload = {
        "hookToken": hook_token,
        "gatewayToken": gateway_token,
        "bedrockBearer": bedrock_bearer,
        "providerChoice": provider_choice,
        "providerSwitchFrozen": False,
    }
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)

    def _converge() -> bool:
        try:
            prior = json.loads(sm.get_secret_value(SecretId=name)["SecretString"])
        except (sm.exceptions.ResourceNotFoundException, sm.exceptions.InvalidRequestException):
            prior = None  # absent, or scheduled for deletion (create_or_update_secret retries that)
        if prior == payload:
            return False
        create_or_update_secret(sm, name, json.dumps(payload))

        def boot_inputs(value):
            return {k: v for k, v in (value or {}).items() if k != "providerChoice"}

        return boot_inputs(prior) != boot_inputs(payload)

    return await run_in_thread(_converge)
