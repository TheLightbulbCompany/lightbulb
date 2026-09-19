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


def build_runtime_env(
    *,
    owner_id: str,
    api_base: str,
    hook_token: str,
    litestream_bucket: str,
    gate_bearer: str | None = None,
    gate_url: str | None = None,
    sonnet_aip_arn: str | None = None,
) -> dict[str, str]:
    """The gateway's environment on AgentCore.

    On ECS this arrives from two places: a static block in the CDK task definition and a
    per-owner list built in ecs_manager. A runtime has no task definition, so both sets are
    supplied here or the gateway boots unable to call back, publish, or reach a model. The
    first dev flip answered a turn only after ``AWS_BEARER_TOKEN_BEDROCK`` /
    ``AWS_BEDROCK_FORCE_HTTP1`` were patched in by hand: the bedrock-gate is a Lambda function
    URL (HTTP/1.1 only) and the SDK defaults to HTTP/2, producing a bare "Protocol error".

    Deliberately NOT carried over: DISPLAY and BROWSER_EGRESS_PROXY_PORT (the browser stack is
    stripped from this image).

    ⚠️ SECRETS. ``gate_bearer`` (also carried as ``ANTHROPIC_AUTH_TOKEN``) is a bearer token and
    AgentCore environment variables are plaintext on a control-plane resource, readable by any
    principal with ``bedrock-agentcore:GetAgentRuntime`` — a wider audience than an ECS task
    definition. This matches today's posture (the gate bearer is already plaintext in the task
    definition's ``environment``, not ``secrets``) so it is not a regression, but the spec's P6
    calls for fetching it from Secrets Manager at boot instead. Do that before prod: a rotation
    also only reaches new sessions, so env is the wrong home for it twice over.
    """
    env: dict[str, str] = {
        # Per-owner identity and callback path.
        "ISOL8_OWNER_ID": owner_id,
        "ISOL8_API_URL": api_base.rstrip("/"),
        "OPENCLAW_HOOK_TOKEN": hook_token,
        "LITESTREAM_S3_BUCKET": litestream_bucket,
        # The static block the CDK task definition supplies on ECS.
        "HOME": "/home/node",
        "XDG_CONFIG_HOME": "/home/node/.openclaw/.config",
        "XDG_DATA_HOME": "/home/node/.openclaw/.local/share",
        "AWS_CONFIG_FILE": "/home/node/.openclaw/.aws/config",
        "CLAWHUB_WORKDIR": "/home/node/.openclaw",
        "GOG_KEYRING_BACKEND": "file",
        "OPENCLAW_DISABLE_BONJOUR": "true",
        "OPENCLAW_GATEWAY_STARTUP_TRACE": "1",
        # A routine in-process restart must not end the microVM: the update-restart path
        # spawns a detached child and exits, which would kill litestream -exec with it.
        "OPENCLAW_NO_RESPAWN": "1",
        # EFS/NFS workaround for the config watcher -- openclaw.json is still on EFS.
        "CHOKIDAR_USEPOLLING": "true",
        # Tells boot `doctor --fix` that litestream owns the gateway process, so it skips its
        # native service-ownership inspection (see ecs_manager / CLAUDE.md "Config Patching
        # Notes") -- without it every ChatGPT-OAuth connect throws on every turn.
        "OPENCLAW_SERVICE_REPAIR_POLICY": "external",
        "AWS_REGION": settings.AWS_REGION,
        "CLAUDE_CODE_USE_BEDROCK": "1",
    }
    if sonnet_aip_arn:
        env["ANTHROPIC_MODEL"] = sonnet_aip_arn
    if gate_bearer:
        env["AWS_BEARER_TOKEN_BEDROCK"] = gate_bearer
        env["AWS_BEDROCK_FORCE_HTTP1"] = "1"
    if gate_url and gate_bearer:
        env["ANTHROPIC_BEDROCK_BASE_URL"] = gate_url
        env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] = "1"
        env["ANTHROPIC_AUTH_TOKEN"] = gate_bearer
    return env


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
    environment: dict[str, str] | None = None,
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
        "environmentVariables": environment or {},
        "tags": {"owner_id": owner_id, "Environment": _env(), "ManagedBy": "lightbulb-backend"},
    }


#: Owner ruling 2026-09-16: 4h idle. Idle memory is ~$0.014/h, so a generous timeout costs a
#: couple of dollars a month and removes nearly every cold start inside a working day.
IDLE_TIMEOUT_S = 4 * 60 * 60
#: AWS's hard ceiling for a microVM session; the backend rolls sessions at a quiet point before it.
MAX_LIFETIME_S = 8 * 60 * 60


async def ensure_runtime(owner_id: str) -> dict:
    """Give this workspace an AgentCore runtime and point its container row at it.

    Idempotent: a row that already carries ``agentcore_runtime_arn`` is returned as-is. The
    steps reuse the owner's existing resources rather than minting parallel ones — the EFS
    access point from the container row, the per-owner Bedrock role (re-trusted for AgentCore),
    and the owner's application-inference profiles for the gate bearer.

    Note on the gate bearer: ``mint_gate_token`` ROTATES the owner's bearer, so the ECS task
    that served this workspace stops being able to reach Bedrock the moment this runs. That is
    the intended one-way cutover shape (spec §5.5); do not call this for a workspace you mean
    to keep on ECS.
    """
    import boto3

    from core.billing.bedrock_aip import _owner_role_name, ensure_owner_aips, ensure_owner_bedrock_role
    from core.billing.bedrock_gate_tokens import mint_gate_token
    from core.repositories import container_repo

    if not settings.AGENTCORE_GATEWAY_IMAGE:
        raise RuntimeError("AGENTCORE_GATEWAY_IMAGE is not configured for this environment")
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    if not row:
        raise RuntimeError(f"no container row for {owner_id}")
    if row.get("agentcore_runtime_arn"):
        return {"owner_id": owner_id, "runtime_arn": row["agentcore_runtime_arn"], "created": False}
    access_point_id = row.get("access_point_id")
    hook_token = row.get("hook_token")
    if not access_point_id or not hook_token:
        raise RuntimeError(f"container row for {owner_id} lacks access_point_id/hook_token; provision on ECS first")
    if row.get("status") != "stopped":
        # A live ECS task next to the new microVM is two gateways on one access point.
        raise RuntimeError(f"{owner_id} is {row.get('status')!r} on ECS; stop it before moving it")

    aip_arns = await ensure_owner_aips(owner_id)
    role_arn = await ensure_owner_bedrock_role(owner_id, aip_arns)
    account_id = role_arn.split(":")[4]
    await ensure_agentcore_trust(owner_id, _owner_role_name(owner_id), account_id)
    gate_bearer = await mint_gate_token(owner_id, aip_arns)

    # Token and URL travel together -- same resolver every other harness env uses.
    from core.billing.bedrock_gate_url import gate_url as _resolve_gate_url

    resolved_gate_url = _resolve_gate_url()

    env = build_runtime_env(
        owner_id=owner_id,
        api_base=settings.PUBLIC_API_BASE,
        hook_token=hook_token,
        litestream_bucket=settings.OWNER_STATE_BUCKET,
        gate_bearer=gate_bearer,
        gate_url=resolved_gate_url,
        sonnet_aip_arn=aip_arns["sonnet"],
    )
    request = build_runtime_request(
        owner_id=owner_id,
        image_uri=settings.AGENTCORE_GATEWAY_IMAGE,
        role_arn=role_arn,
        efs_access_point_arn=(
            f"arn:aws:elasticfilesystem:{settings.AWS_REGION}:{account_id}:access-point/{access_point_id}"
        ),
        subnets=[s.strip() for s in settings.ECS_SUBNETS.split(",") if s.strip()],
        security_groups=[settings.ECS_SECURITY_GROUP_ID] if settings.ECS_SECURITY_GROUP_ID else [],
        idle_timeout_s=IDLE_TIMEOUT_S,
        max_lifetime_s=MAX_LIFETIME_S,
        environment=env,
    )
    control = boto3.client("bedrock-agentcore-control")
    created = await run_in_thread(control.create_agent_runtime, **request)
    arn = created["agentRuntimeArn"]
    # ``running`` from here on: AgentCore wakes on invoke, so every "stopped → start ECS" door
    # must see a live workspace. The ECS manager's start/stop chokepoints no-op for this row.
    await container_repo.update_fields(
        owner_id, {"runtime_host": "agentcore", "agentcore_runtime_arn": arn, "status": "running"}
    )
    logger.info("agentcore runtime created owner=%s arn=%s", owner_id, arn)
    return {"owner_id": owner_id, "runtime_arn": arn, "created": True}


# --- Boot secret (M4-R4/R5) -------------------------------------------------
#
# One JSON secret per owner, ``lightbulb/{env}/hermes/{owner_id}/boot`` =
# ``{"hookToken", "gatewayToken", "bedrockBearer"}``, named by the ONE env var
# below in a runtime's ``environmentVariables`` — never a plaintext credential
# value. The bridge (``apps/infra/hermes/bridge/sync.py::load_boot_secrets``,
# wired in ``apps/infra/hermes/bridge/__init__.py``) resolves it at boot with
# the runtime's own execution role; ``core.billing.bedrock_gate_tokens``'s
# ``_agentcore_presentable_hashes`` reads the SAME secret to drive gate-token
# liveness. A per-credential secret-name scheme was considered and rejected —
# there is exactly one boot-secret mechanism, not three.
#
# Duplicated here (not imported) from ``bedrock_gate_tokens._BOOT_SECRET_ENV``:
# both modules treat it as a stable, external contract string shared with the
# bridge, not an implementation detail either module owns.
#
# NOTE: ``ensure_runtime``/``build_runtime_env`` above still embed the plaintext
# ``AWS_BEARER_TOKEN_BEDROCK``/``OPENCLAW_HOOK_TOKEN`` — that is the OTHER stream's
# OpenClaw-gateway migration path (ECS -> AgentCore for an EXISTING owner), not this one.
# The Hermes fresh-provision path (``hermes_provisioning.py``) uses this boot secret +
# ``BOOT_SECRET_ENV_VAR`` exclusively. ``ensure_runtime``'s plaintext path is scheduled for
# deletion at cutover (M6), not retrofitted onto the boot secret.
BOOT_SECRET_ENV_VAR = "LIGHTBULB_SECRET_ID"


def boot_secret_name(owner_id: str) -> str:
    """The name of this owner's one JSON boot secret."""
    return f"lightbulb/{_env()}/hermes/{owner_id}/boot"


async def write_boot_secret(owner_id: str, *, hook_token: str, gateway_token: str, bedrock_bearer: str) -> str:
    """Create or update the owner's boot secret; returns its name (the value a runtime's
    ``BOOT_SECRET_ENV_VAR`` carries). Plaintext lives in exactly one place: this secret."""
    import boto3

    from core.secrets_manager import create_or_update_secret

    name = boot_secret_name(owner_id)
    value = json.dumps({"hookToken": hook_token, "gatewayToken": gateway_token, "bedrockBearer": bedrock_bearer})
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    await run_in_thread(create_or_update_secret, sm, name, value)
    return name
