"""Fresh-provision path for a Hermes-on-AgentCore workspace.

NOT a patch of ``agentcore_runtime.ensure_runtime``: that function is a MIGRATION tool and
hard-requires an existing EFS access point and a ``stopped`` ECS row (agentcore_runtime.py:235-239),
which a fresh workspace has never had. Patching it in place would inherit "must come from ECS" into
new-workspace provisioning (report 04, risk 5).

What is reused verbatim: the IAM/naming scaffolding (``runtime_name_for``, ``ensure_agentcore_trust``),
the owner's AIPs and Bedrock role, ``mint_gate_token``, the boot-secret writer, and the lifecycle
constants. What is dropped: ``filesystemConfigurations`` (no EFS — profiles are S3), the OpenClaw env
builder, the SFN watchdog and the long ``wait_for_running`` ECS-boot poll (``CreateAgentRuntime`` is
one synchronous call; the only wait here is the runtime's own short startup to ``READY``).

Fencing (M4-R28): called from inside ``provision_runner.run()``'s single outer ``try``, under the
SAME claim/attempt-token lease step 0 already runs under. The terminal row write goes through
``container_repo.update_container_leased`` — the exact fenced primitive ``ecs_manager.wait_for_running``
uses for its own terminal write — so a straggler from a superseded attempt can never clobber a
takeover's row; a lost fence raises ``ProvisionSuperseded`` and writes nothing (the caller's existing
handler for that exception, from Task 18, already covers this branch). No ``generation`` fence: that
concept is ECS-incarnation-specific (a container recreate mid-provision) and has no Hermes analogue —
a fresh runtime has nothing to fence a generation against.

Boot secret (M4-R4/R5): the runtime's ``environmentVariables`` carry NO credential value. One JSON
secret, ``agentcore_runtime.boot_secret_name(owner_id)`` = ``{"hookToken","gatewayToken","bedrockBearer"}``,
is written via ``agentcore_runtime.write_boot_secret`` and only its NAME travels in the env, under
``agentcore_runtime.BOOT_SECRET_ENV_VAR`` — the bridge resolves it at boot with the runtime's own
execution role.

Rotation (M4-R16): this module never rotates an already-provisioned owner's bearer — it is
idempotent-return on an existing ``agentcore_runtime_arn`` and mints fresh tokens only for a
never-provisioned row. The ``StopRuntimeSession`` call the ruling requires before revoking an old
token generation is therefore OWED WHEREVER an already-Hermes owner's gate token is rotated
(``core.billing.bedrock_gate_tokens.mint_gate_token`` / the revoke-restore paths that call it) — not
implemented in this task.

BYO key (known gap, not solved here): ``write_agent_profile`` accepts an owner-level ``byo_api_key``
plaintext value, but Secrets Manager has never stored a BYO key's plaintext for backend retrieval —
only its ARN, which ECS resolves via a task-definition ``secrets:`` reference the backend never reads
back. A BYO-provider Hermes owner's profile therefore ships today WITHOUT a baked
``ANTHROPIC_API_KEY``/``OPENAI_API_KEY`` until a follow-up adds a plaintext-fetch path.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services.agentcore_runtime import (
    IDLE_TIMEOUT_S,
    MAX_LIFETIME_S,
    BOOT_SECRET_ENV_VAR,
    ensure_agentcore_trust,
    runtime_name_for,
    write_boot_secret,
)

logger = logging.getLogger(__name__)

#: Bounded poll for AgentCore's OWN startup — CreateAgentRuntime returns immediately, but the
#: runtime itself takes some seconds to reach READY. This is NOT a substitute for
#: ecs_manager.wait_for_running's SFN-watched, many-minutes ECS boot wait: it lives entirely
#: inside the single-shot provision runner's own execution (mirrors the dev spike's own
#: 40 x 10s ceiling — a real flip there resolved in well under that window).
_READY_POLL_ATTEMPTS = 40
_READY_POLL_INTERVAL_S = 10.0
_TERMINAL_STATUSES = ("CREATE_FAILED", "UPDATE_FAILED")


def build_hermes_runtime_env(
    *,
    owner_id: str,
    api_base: str,
    bucket: str,
    boot_secret_id: str,
    gate_endpoint: str,
    sonnet_aip_arn: str,
) -> dict[str, str]:
    """The bridge's environment (M4-R19). Identifiers ONLY — every credential lives in the ONE
    Secrets Manager boot secret ``boot_secret_id`` names; nothing here is a bearer, a hook token,
    or a gateway token. Mirrors the working dev-spike set minus the three plaintext values it
    hand-patched in before this boot secret existed.
    """
    return {
        "ISOL8_OWNER_ID": owner_id,
        "ISOL8_API_URL": api_base.rstrip("/"),
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": gate_endpoint,
        "AWS_REGION": settings.AWS_REGION,
        "AWS_BEDROCK_FORCE_HTTP1": "1",
        "ANTHROPIC_MODEL": sonnet_aip_arn,
        "S3_BUCKET": bucket,
        # Owner FIRST: the per-owner S3 grant is <bucket>/<owner>/*
        # (bedrock_aip.build_owner_litestream_s3_policy). Any other order is outside every
        # owner's grant and AccessDenies on the first restore.
        "S3_PREFIX": f"{owner_id}/hermes",
        "HERMES_HOME": "/opt/data",
        "HERMES_HEADLESS": "1",
        BOOT_SECRET_ENV_VAR: boot_secret_id,
    }


def build_hermes_runtime_request(
    *,
    owner_id: str,
    image_uri: str,
    role_arn: str,
    subnets: list[str],
    security_groups: list[str],
    environment: dict[str, str],
) -> dict:
    """The CreateAgentRuntime request for a fresh Hermes workspace. No
    ``filesystemConfigurations`` — a Hermes profile is a complete S3-resident ``$HERMES_HOME``,
    not an EFS mount."""
    return {
        "agentRuntimeName": runtime_name_for(owner_id),
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role_arn,
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {"securityGroups": security_groups, "subnets": subnets},
        },
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": IDLE_TIMEOUT_S,
            "maxLifetime": MAX_LIFETIME_S,
        },
        "environmentVariables": environment,
        "tags": {"owner_id": owner_id, "Environment": settings.ENVIRONMENT, "ManagedBy": "lightbulb-backend"},
    }


def _create_runtime(request: dict) -> str:
    import boto3

    return boto3.client("bedrock-agentcore-control").create_agent_runtime(**request)["agentRuntimeArn"]


def _wait_for_runtime_ready(runtime_arn: str) -> None:
    """Poll ``GetAgentRuntime`` until READY (or a terminal failure). Sync — call via
    ``run_in_thread``. Bounded per ``_READY_POLL_ATTEMPTS``/``_READY_POLL_INTERVAL_S`` above."""
    import time

    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    for _ in range(_READY_POLL_ATTEMPTS):
        status = client.get_agent_runtime(agentRuntimeId=runtime_id)["status"]
        if status == "READY":
            return
        if status in _TERMINAL_STATUSES:
            raise RuntimeError(f"AgentCore runtime {runtime_arn} failed to start: {status}")
        time.sleep(_READY_POLL_INTERVAL_S)
    raise RuntimeError(f"AgentCore runtime {runtime_arn} did not reach READY within the poll window")


class _OwnerPrep(NamedTuple):
    role_arn: str
    aip_arns: dict[str, str]
    hook_token: str
    gateway_token: str
    boot_secret_id: str


async def _prepare_owner_aws(owner_id: str) -> _OwnerPrep:
    """AIPs, the per-owner Bedrock role, AgentCore trust, fresh hook/gateway tokens, and the ONE
    JSON boot secret naming them plus a freshly minted gate bearer (M4-R4/R5).

    ``hook_token``/``gateway_token`` are re-minted only when the row does not already carry one —
    mirrors ``ecs_manager``'s own "reuse if present" convention (``container.get("hook_token") or
    secrets.token_urlsafe(32)``) — so a retry of a partially-failed provision never orphans a token
    some other seam already handed out.
    """
    import secrets as _secrets

    from core.billing.bedrock_aip import _owner_role_name, ensure_owner_aips, ensure_owner_bedrock_role
    from core.billing.bedrock_gate_tokens import mint_gate_token

    row = await container_repo.get_by_owner_id(owner_id) or {}
    hook_token = row.get("hook_token") or _secrets.token_urlsafe(32)
    gateway_token = row.get("gateway_token") or _secrets.token_urlsafe(32)

    aip_arns = await ensure_owner_aips(owner_id)
    role_arn = await ensure_owner_bedrock_role(owner_id, aip_arns)
    await ensure_agentcore_trust(owner_id, _owner_role_name(owner_id), role_arn.split(":")[4])
    bedrock_bearer = await mint_gate_token(owner_id, aip_arns)
    boot_secret_id = await write_boot_secret(
        owner_id, hook_token=hook_token, gateway_token=gateway_token, bedrock_bearer=bedrock_bearer
    )
    return _OwnerPrep(
        role_arn=role_arn,
        aip_arns=aip_arns,
        hook_token=hook_token,
        gateway_token=gateway_token,
        boot_secret_id=boot_secret_id,
    )


async def _write_profiles(owner_id: str, aip_arns: dict[str, str]) -> None:
    """Step 1's profile fan-out: one Hermes profile (config.yaml + .env + SOUL.md) per starter
    agent, uploaded to S3.

    Roster via ``core.services.messaging.load_agent_roster`` (M4-R10) — the same normalized,
    UUID-validated shape ``write_agent_profile``'s ``agent`` parameter expects.

    Writer failure handling: ``write_agent_profile`` (via ``render_dotenv``) raises ``ValueError``
    on an illegal dotenv value (a newline, an illegal key). Caught PER AGENT and re-raised as a
    named ``RuntimeError`` — the original message already names the offending key — so one bad
    integration/env value fails this provision attempt with a diagnosable error instead of leaving
    a six-agent roster half profiled with no indication which agent or variable broke it.
    """
    from core.services.container_provision import resolve_provider_choice
    from core.services.credentials.mcp_server import build_connected_integration_servers
    from core.services.hermes_profiles import upload_profile, write_agent_profile
    from core.services.messaging import load_agent_roster
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_api_key import ensure_paperclip_api_key
    from core.services.provisioning_landing import build_provisioned_env

    provider_choice, byo_provider = await resolve_provider_choice(owner_id)
    integrations = await build_connected_integration_servers(owner_id)
    env_vars = await build_provisioned_env(owner_id)
    admin_client = shared_admin_client()
    for agent in await load_agent_roster(owner_id):
        agent_id = agent["id"]
        paperclip_api_key = (
            await ensure_paperclip_api_key(owner_id=owner_id, paperclip_agent_id=agent_id, admin_client=admin_client)
            or ""
        )
        try:
            files = write_agent_profile(
                owner_id,
                agent,
                provider_choice=provider_choice,
                aip_arns=aip_arns,
                paperclip_api_key=paperclip_api_key,
                integrations=integrations,
                env_vars=env_vars,
                byo_provider=byo_provider,
                # byo_api_key intentionally omitted — see module docstring's "BYO key" note.
            )
        except ValueError as exc:
            raise RuntimeError(
                f"hermes profile for owner={owner_id} agent={agent_id} could not be rendered: {exc}"
            ) from exc
        await upload_profile(owner_id, agent_id, files)


async def provision_hermes_runtime(owner_id: str, *, claim_id: str, attempt_token: str | None = None) -> str:
    """Create this workspace's AgentCore runtime and point its container row at it.

    Idempotent: a row that already carries ``agentcore_runtime_arn`` is returned as-is, with no
    AWS call and no row write.

    ``claim_id``/``attempt_token`` fence the terminal write (see module docstring, M4-R28) — the
    caller (``provision_runner.run``) always holds a claim; both are threaded straight from there.
    """
    if not settings.AGENTCORE_HERMES_IMAGE:
        raise RuntimeError("AGENTCORE_HERMES_IMAGE is not configured for this environment")
    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    existing = row.get("agentcore_runtime_arn")
    if existing:
        return existing

    prep = await _prepare_owner_aws(owner_id)
    await _write_profiles(owner_id, prep.aip_arns)

    from core.billing.bedrock_gate_url import gate_url

    request = build_hermes_runtime_request(
        owner_id=owner_id,
        image_uri=settings.AGENTCORE_HERMES_IMAGE,
        role_arn=prep.role_arn,
        subnets=[s.strip() for s in settings.ECS_SUBNETS.split(",") if s.strip()],
        security_groups=[settings.ECS_SECURITY_GROUP_ID] if settings.ECS_SECURITY_GROUP_ID else [],
        environment=build_hermes_runtime_env(
            owner_id=owner_id,
            api_base=settings.PUBLIC_API_BASE,
            bucket=settings.OWNER_STATE_BUCKET,
            boot_secret_id=prep.boot_secret_id,
            gate_endpoint=gate_url(),
            sonnet_aip_arn=prep.aip_arns["sonnet"],
        ),
    )
    arn = await run_in_thread(_create_runtime, request)
    await run_in_thread(_wait_for_runtime_ready, arn)

    # `running` from here: AgentCore wakes on invoke, so every "stopped -> start" door must see a
    # live workspace, and the ECS manager's start/stop chokepoints already no-op for this row.
    # Fenced write (M4-R28) — mirrors ecs_manager.wait_for_running's terminal transition. A lost
    # fence raises ProvisionSuperseded here and writes NOTHING (DynamoDB's conditional write is
    # atomic); provision_runner.run's existing handler for that exception covers this call.
    await container_repo.update_container_leased(
        owner_id,
        claim_id,
        {
            "runtime_host": "agentcore",
            "agentcore_runtime_arn": arn,
            "status": "running",
            "hook_token": prep.hook_token,
            "gateway_token": prep.gateway_token,
        },
        attempt_token=attempt_token,
        release_lease=True,
    )
    logger.info("hermes runtime created owner=%s arn=%s", owner_id, arn)
    return arn
