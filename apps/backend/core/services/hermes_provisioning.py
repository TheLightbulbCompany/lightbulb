"""Fresh-provision path for a Hermes-on-AgentCore workspace.

NOT a patch of ``agentcore_runtime.ensure_runtime``: that function is a MIGRATION tool and
hard-requires an existing EFS access point and a ``stopped`` ECS row (agentcore_runtime.py:235-239),
which a fresh workspace has never had. Patching it in place would inherit "must come from ECS" into
new-workspace provisioning (report 04, risk 5).

Two steps, in order, both fenced on the SAME claim/attempt-token lease ``provision_runner.run``
already holds (M4-R28/M4-R33):

1. ``prepare_hermes_workspace`` — mints (or reuses) ``hook_token``/``gateway_token``, mints a fresh
   gate bearer, writes the ONE JSON boot secret, and writes the two tokens onto the containers row.
   Runs BEFORE step 0 (the Paperclip seed): the seed's starter-agent loop bakes ``hook_token`` into
   the ``http`` adapter's ``Authorization`` header at seed time
   (``paperclip_adapter_config.build_http_adapter_config``), so a fresh Hermes provision that seeded
   before minting a token shipped every starter agent with an EMPTY bearer — the exact bug M4-R33
   fixes. A NON-terminal, lease-REFRESHING write (``release_lease=False``): the provision is not
   done yet.
2. ``provision_hermes_runtime`` — writes the Hermes profiles to S3, creates the AgentCore runtime,
   waits for it to reach READY, and writes the TERMINAL row fields. Fully idempotent-return only
   once ``status="running"``. The ARN is recorded on the row NON-terminally IMMEDIATELY after
   ``CreateAgentRuntime`` returns, before the READY poll (M4-R35/C1) — runtime names are unique
   per account, so a create that succeeds but is never recorded (a crash, a lost fence, a
   CREATE_FAILED on the poll) would otherwise hit a name conflict on every retry, forever. A retry
   that finds the ARN on the row (or, failing that, by listing AgentCore runtimes for the derived
   name — covers a row write that itself never landed) skips creation and reuses it.

What is reused verbatim: the IAM/naming scaffolding (``runtime_name_for``, ``ensure_agentcore_trust``),
the owner's AIPs and Bedrock role, ``mint_gate_token``, the boot-secret writer, and the lifecycle
constants. What is dropped: ``filesystemConfigurations`` (no EFS — profiles are S3), the OpenClaw env
builder, the SFN watchdog and the long ``wait_for_running`` ECS-boot poll (``CreateAgentRuntime`` is
one synchronous call; the only wait here is the runtime's own short startup to ``READY``).

Fencing (M4-R28): the terminal row write goes through ``container_repo.update_container_leased`` —
the exact fenced primitive ``ecs_manager.wait_for_running`` uses for its own terminal write — so a
straggler from a superseded attempt can never clobber a takeover's row; a lost fence raises
``ProvisionSuperseded`` and writes nothing (``provision_runner.run``'s existing handler for that
exception, from Task 18, already covers both call sites). No ``generation`` fence: that concept is
ECS-incarnation-specific (a container recreate mid-provision) and has no Hermes analogue.

Boot secret (M4-R4/R5): the runtime's ``environmentVariables`` carry NO credential value. One JSON
secret, ``agentcore_runtime.boot_secret_name(owner_id)`` = ``{"hookToken","gatewayToken","bedrockBearer"}``,
is written via ``agentcore_runtime.write_boot_secret`` and only its NAME travels in the env, under
``agentcore_runtime.BOOT_SECRET_ENV_VAR`` — the bridge resolves it at boot with the runtime's own
execution role. ``provision_hermes_runtime`` never re-fetches this name from anywhere: it is a pure
function of ``owner_id`` (``boot_secret_name``), so it is recomputed, not looked up — the only writer
of the secret's CONTENTS is ``prepare_hermes_workspace``, which MUST have already run for this owner
(``provision_runner.run`` always calls it first when ``settings.HERMES_RUNTIME`` is on).

Rotation (M4-R16): this module never rotates an already-provisioned owner's bearer —
``prepare_hermes_workspace`` no-ops once the owner already has an ``agentcore_runtime_arn`` (avoids
rotating a live bearer on every reconcile dispatch against an already-Hermes owner), and
``provision_hermes_runtime`` is idempotent-return on the same field. The ``StopRuntimeSession`` call
the ruling requires before revoking an old token generation is therefore OWED WHEREVER an
already-Hermes owner's gate token is rotated (``core.billing.bedrock_gate_tokens.mint_gate_token`` /
the revoke-restore paths that call it) — not implemented in this module.

BYO key (M4-R32): the plaintext value is read from Secrets Manager at provision time via
``core.services.key_service.read_byo_key`` — the same ``lightbulb/{env}/user-keys/{user}/{provider}``
secret the ECS path references by ARN, but read back directly here (AgentCore has no task-definition
``secrets:`` mechanism to resolve it for us). Only read when ``provider_choice == "byo_key"``.
"""

from __future__ import annotations

import logging

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services.agentcore_runtime import (
    IDLE_TIMEOUT_S,
    MAX_LIFETIME_S,
    BOOT_SECRET_ENV_VAR,
    boot_secret_name,
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

    I1: carries BOTH ``AWS_ENDPOINT_URL_BEDROCK_RUNTIME`` (the boto3-side var; operative for the
    ``converse_stream`` provider) AND ``ANTHROPIC_BEDROCK_BASE_URL`` (the evaluation spec's own
    documented ``AnthropicBedrock(region).base_url`` default for Hermes's Claude route) — the
    spike set both and the two docs disagree about which path Hermes actually takes, so this
    does not silently pick a side. Same value as the endpoint var: both must point at the gate,
    never the real bedrock-runtime endpoint, or that route AccessDenies fleet-wide (the owner
    role carries no ``bedrock:Invoke``).
    """
    return {
        "ISOL8_OWNER_ID": owner_id,
        "ISOL8_API_URL": api_base.rstrip("/"),
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": gate_endpoint,
        "ANTHROPIC_BEDROCK_BASE_URL": gate_endpoint,
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
        # I1: harmless and explicit -- already baked into the image (Dockerfile.spike:22)
        # alongside HERMES_HOME/HERMES_HEADLESS/AGENTCORE_PORT; setting it here too means the
        # runtime request itself documents the full working set rather than half of it.
        "PYTHONUNBUFFERED": "1",
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

    return boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION).create_agent_runtime(**request)[
        "agentRuntimeArn"
    ]


def _find_existing_runtime_arn(name: str) -> str | None:
    """M4-R35: an owner's runtime name is unique per account, so a retry whose row lost the
    ARN (a crash, or a lost fence, between ``CreateAgentRuntime`` and the row write) would
    otherwise hit a name conflict on every future attempt, forever. Sync — call via
    ``run_in_thread``."""
    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)
    for runtime in client.list_agent_runtimes().get("agentRuntimes", []):
        if runtime.get("agentRuntimeName") == name:
            return runtime.get("agentRuntimeArn")
    return None


def _runtime_image_uri(runtime_arn: str) -> str | None:
    """The image URI an existing runtime is currently configured with. Sync — call via
    ``run_in_thread``."""
    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    desc = client.get_agent_runtime(agentRuntimeId=runtime_id)
    return (desc.get("agentRuntimeArtifact") or {}).get("containerConfiguration", {}).get("containerUri")


def _update_runtime_image(runtime_arn: str, image_uri: str) -> None:
    """Bring a reused (not freshly created) runtime onto the currently configured image --
    e.g. a broken tag fixed between retries. Sync — call via ``run_in_thread``."""
    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    client.update_agent_runtime(
        agentRuntimeId=runtime_id,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
    )


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


async def prepare_hermes_workspace(
    owner_id: str, *, claim_id: str, attempt_token: str | None = None, migrate: bool = False
) -> None:
    """M4-R33: mint the owner's hook/gateway tokens + a fresh gate bearer, write the ONE JSON boot
    secret, and persist ``hook_token``/``gateway_token`` onto the row — BEFORE step 0 seeds the
    roster, so the ``http`` adapter's ``Authorization`` header
    (``paperclip_adapter_config.build_http_adapter_config``) bakes in a REAL token instead of the
    empty string a missing row field falls back to.

    Idempotent: a no-op (no AWS call, no row write) once the owner already has an
    ``agentcore_runtime_arn`` — the whole workspace, tokens included, is already provisioned by
    then, so re-running this on every reconcile dispatch would otherwise rotate a LIVE gate bearer
    for no reason. NOT idempotent on the bearer itself while unprovisioned: a retry of a
    still-unprovisioned owner re-mints (``mint_gate_token`` always rotates), which is safe — nothing
    ever presented the prior one, since the runtime was never created.

    I3/M4-R37 freshness guard: ``settings.HERMES_RUNTIME`` is ONE flag for the whole environment,
    and ``provision_runner.run`` branches on it alone — so the first dispatch after a flip reaches
    an owner who is ALREADY a live ECS workspace (a self-heal, a reconcile, any ordinary
    re-dispatch), not just fresh owners. Minting here for one of those would rotate
    ``mint_gate_token``'s bearer into a boot secret their live ECS task never reads (their
    container keeps using its OWN, now-superseded bearer until the liveness predicate expires it)
    and, downstream, ``provision_hermes_runtime`` would create a SECOND runtime beside their
    running container. Mirrors ``agentcore_runtime.ensure_runtime``'s own guard
    (refuses unless ``status == "stopped"``) for the same reason: "two gateways [next to each
    other] on one access point." Detected via ``service_name`` — the same field
    ``_container_already_healthy`` uses to mean "an ECS service was actually created for this
    owner." ``migrate=True`` is the ONE sanctioned bypass, for the M6 cutover job that
    deliberately crosses an existing ECS owner over: it reuses ``hook_token``/``gateway_token``
    off the row exactly as the fresh-provision path does (they already exist, minted at ECS
    provision time) and mints only a fresh bearer, via this same function.

    ``hook_token``/``gateway_token`` are re-minted only when the row does not already carry one —
    mirrors ``ecs_manager``'s own "reuse if present" convention (``container.get("hook_token") or
    secrets.token_urlsafe(32)``) — so a retry never orphans a token some other seam already handed
    out.

    Fenced the same way as the terminal write (M4-R28), but NON-terminal: ``release_lease=False``,
    since the provision is not done yet.
    """
    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    if row.get("agentcore_runtime_arn"):
        return
    if row.get("service_name") and not migrate:
        logger.info(
            "prepare_hermes_workspace: owner %s has a live ECS service (service_name=%s) and "
            "migrate=False -- refusing to mint Hermes credentials for it (I3/M4-R37)",
            owner_id,
            row.get("service_name"),
        )
        return

    import secrets as _secrets

    from core.billing.bedrock_aip import _owner_role_name, ensure_owner_aips, ensure_owner_bedrock_role
    from core.billing.bedrock_gate_tokens import mint_gate_token

    hook_token_reused = bool(row.get("hook_token"))
    gateway_token_reused = bool(row.get("gateway_token"))
    hook_token = row.get("hook_token") or _secrets.token_urlsafe(32)
    gateway_token = row.get("gateway_token") or _secrets.token_urlsafe(32)

    aip_arns = await ensure_owner_aips(owner_id)
    role_arn = await ensure_owner_bedrock_role(owner_id, aip_arns)
    await ensure_agentcore_trust(owner_id, _owner_role_name(owner_id), role_arn.split(":")[4])
    bedrock_bearer = await mint_gate_token(owner_id, aip_arns)
    await write_boot_secret(owner_id, hook_token=hook_token, gateway_token=gateway_token, bedrock_bearer=bedrock_bearer)

    # m2: the one step that mints three credentials and writes a secret was otherwise invisible
    # in the provision log. Never logs a value -- only which tokens were reused vs freshly minted.
    logger.info(
        "hermes prepare owner=%s hook_token=%s gateway_token=%s bedrock_bearer=minted migrate=%s",
        owner_id,
        "reused" if hook_token_reused else "minted",
        "reused" if gateway_token_reused else "minted",
        migrate,
    )

    await container_repo.update_container_leased(
        owner_id,
        claim_id,
        {"hook_token": hook_token, "gateway_token": gateway_token},
        attempt_token=attempt_token,
        release_lease=False,
    )


async def _load_role_and_aips(owner_id: str) -> tuple[str, dict[str, str]]:
    """Idempotently re-derive ``(role_arn, aip_arns)`` — both are stable and deterministic per
    owner (``ensure_owner_aips``/``ensure_owner_bedrock_role`` only ever create-or-reuse), so
    ``provision_hermes_runtime`` can call this independently of ``prepare_hermes_workspace``
    without re-minting anything sensitive (the gate bearer / boot secret are NOT re-derived here —
    only ``prepare_hermes_workspace`` mints those).
    """
    from core.billing.bedrock_aip import ensure_owner_aips, ensure_owner_bedrock_role

    aip_arns = await ensure_owner_aips(owner_id)
    role_arn = await ensure_owner_bedrock_role(owner_id, aip_arns)
    return role_arn, aip_arns


async def _write_profiles(owner_id: str, aip_arns: dict[str, str]) -> None:
    """Step 1's profile fan-out: one Hermes profile (config.yaml + .env + SOUL.md) per starter
    agent, uploaded to S3.

    Roster via ``core.services.messaging.load_agent_roster`` (M4-R10) — the same normalized,
    UUID-validated shape ``write_agent_profile``'s ``agent`` parameter expects.

    BYO key (M4-R32): the plaintext value is read back from Secrets Manager
    (``key_service.read_byo_key``) only when ``provider_choice == "byo_key"`` — every other
    provider choice never touches it.

    Writer failure handling: ``write_agent_profile`` (via ``render_dotenv``) raises ``ValueError``
    on an illegal dotenv value (a newline, an illegal key). Caught PER AGENT and re-raised as a
    named ``RuntimeError`` — the original message already names the offending key — so one bad
    integration/env value fails this provision attempt with a diagnosable error instead of leaving
    a six-agent roster half profiled with no indication which agent or variable broke it.

    I2: ``ensure_paperclip_api_key`` returns ``None`` on ANY failure — its documented contract is
    "skip staging this agent's identity file this provision; try again next time" (a poll-thread
    ECS caller gets exactly that next time, on its next ~3s poll). This path has no next time
    once ``provision_hermes_runtime`` idempotent-returns on the ARN, so a bare ``or ""`` used to
    bake ``PAPERCLIP_API_KEY=`` into the agent's ``.env`` permanently. A ``None`` here now raises
    a named ``RuntimeError`` instead — caught by C2's release-to-error wrapper in
    ``provision_runner.run``, so the provision fails retryably rather than shipping a silently
    unauthenticated agent.
    """
    from core.services.container_provision import resolve_provider_choice
    from core.services.credentials.mcp_server import build_connected_integration_servers
    from core.services.hermes_profiles import upload_profile, write_agent_profile
    from core.services.key_service import read_byo_key
    from core.services.messaging import load_agent_roster
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_api_key import ensure_paperclip_api_key
    from core.services.provisioning_landing import build_provisioned_env

    provider_choice, byo_provider = await resolve_provider_choice(owner_id)
    byo_api_key = await read_byo_key(owner_id, byo_provider or "anthropic") if provider_choice == "byo_key" else None
    integrations = await build_connected_integration_servers(owner_id)
    env_vars = await build_provisioned_env(owner_id)
    admin_client = shared_admin_client()
    for agent in await load_agent_roster(owner_id):
        agent_id = agent["id"]
        paperclip_api_key = await ensure_paperclip_api_key(
            owner_id=owner_id, paperclip_agent_id=agent_id, admin_client=admin_client
        )
        if paperclip_api_key is None:
            # I2: never bake an empty credential. See the module docstring's note above --
            # there is no "next provision" here to retry into, unlike the ECS poll-thread caller.
            raise RuntimeError(
                f"hermes profile for owner={owner_id} agent={agent_id}: paperclip API key mint/read failed"
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
                byo_api_key=byo_api_key,
            )
        except ValueError as exc:
            raise RuntimeError(
                f"hermes profile for owner={owner_id} agent={agent_id} could not be rendered: {exc}"
            ) from exc
        await upload_profile(owner_id, agent_id, files)


async def provision_hermes_runtime(owner_id: str, *, claim_id: str, attempt_token: str | None = None) -> str:
    """Create this workspace's AgentCore runtime and point its container row at it.

    REQUIRES ``prepare_hermes_workspace`` to have already run for this owner — it writes the boot
    secret this reads the NAME of (not the contents) and the ``hook_token``/``gateway_token`` this
    never re-writes. ``provision_runner.run`` always calls it first when ``settings.HERMES_RUNTIME``
    is on.

    Fully idempotent: a row with ``agentcore_runtime_arn`` AND ``status="running"`` returns as-is,
    with no AWS call and no row write. A row with the ARN but NOT yet ``running`` (a retry that
    reached the create but not the terminal write) skips straight to the READY poll instead of
    creating again (M4-R35/C1) — see the ARN-recording comment below for why that field exists.

    ``claim_id``/``attempt_token`` fence every write here (see module docstring, M4-R28) — the
    caller (``provision_runner.run``) always holds a claim; both are threaded straight from there.
    """
    if not settings.AGENTCORE_HERMES_IMAGE:
        raise RuntimeError("AGENTCORE_HERMES_IMAGE is not configured for this environment")
    if not settings.OWNER_STATE_BUCKET:
        # m3: an empty bucket is not a loud failure anywhere downstream -- upload_profile
        # silently no-ops on it (hermes_profiles.py), so the runtime would come up with
        # S3_BUCKET="", restore_profile would no-op on every boot, and every agent would run on
        # Hermes's own DEFAULT_CONFIG (the unpinned auto-chain) with no error anywhere. Same
        # loud guard as the image, one line up.
        raise RuntimeError("OWNER_STATE_BUCKET is not configured for this environment")
    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    arn = row.get("agentcore_runtime_arn")
    if arn and row.get("status") == "running":
        return arn

    role_arn, aip_arns = await _load_role_and_aips(owner_id)
    await _write_profiles(owner_id, aip_arns)

    if not arn:
        # M4-R35: the row has no ARN yet -- but AgentCore itself might, if a prior attempt got
        # as far as CreateAgentRuntime and then died (or lost its fence) before recording it.
        # Runtime names are unique per account, so skipping this check would hit a name
        # conflict on every future attempt, forever.
        arn = await run_in_thread(_find_existing_runtime_arn, runtime_name_for(owner_id))

    if arn:
        # Reusing an already-created runtime (recorded on the row, or found by name). Bring it
        # onto the currently configured image if it drifted (e.g. a broken tag fixed between
        # retries); otherwise just poll it below.
        current_image = await run_in_thread(_runtime_image_uri, arn)
        if current_image and current_image != settings.AGENTCORE_HERMES_IMAGE:
            await run_in_thread(_update_runtime_image, arn, settings.AGENTCORE_HERMES_IMAGE)
        if not row.get("agentcore_runtime_arn"):
            # Found by name lookup only -- the row never got it. Record now, before polling,
            # for the same reason the freshly-created branch below does.
            await container_repo.update_container_leased(
                owner_id, claim_id, {"agentcore_runtime_arn": arn}, attempt_token=attempt_token, release_lease=False
            )
    else:
        from core.billing.bedrock_gate_url import gate_url

        request = build_hermes_runtime_request(
            owner_id=owner_id,
            image_uri=settings.AGENTCORE_HERMES_IMAGE,
            role_arn=role_arn,
            subnets=[s.strip() for s in settings.ECS_SUBNETS.split(",") if s.strip()],
            security_groups=[settings.ECS_SECURITY_GROUP_ID] if settings.ECS_SECURITY_GROUP_ID else [],
            environment=build_hermes_runtime_env(
                owner_id=owner_id,
                api_base=settings.PUBLIC_API_BASE,
                bucket=settings.OWNER_STATE_BUCKET,
                boot_secret_id=boot_secret_name(owner_id),
                gate_endpoint=gate_url(),
                sonnet_aip_arn=aip_arns["sonnet"],
            ),
        )
        arn = await run_in_thread(_create_runtime, request)
        # M4-R35 (C1): record the ARN IMMEDIATELY, non-terminal, fenced -- BEFORE the READY
        # poll. Without this, a CREATE_FAILED, a poll-window timeout, or a lost fence on the
        # terminal write below all lose the ARN with nothing to reconcile it: AgentCore runtime
        # names are unique per account, so every future retry then hits a name conflict on
        # CreateAgentRuntime, forever. A retry that sees this row field takes the `if arn:`
        # branch above instead of creating again.
        await container_repo.update_container_leased(
            owner_id, claim_id, {"agentcore_runtime_arn": arn}, attempt_token=attempt_token, release_lease=False
        )

    await run_in_thread(_wait_for_runtime_ready, arn)

    # `running` from here: AgentCore wakes on invoke, so every "stopped -> start" door must see a
    # live workspace, and the ECS manager's start/stop chokepoints already no-op for this row.
    # Fenced write (M4-R28) — mirrors ecs_manager.wait_for_running's terminal transition. A lost
    # fence raises ProvisionSuperseded here and writes NOTHING (DynamoDB's conditional write is
    # atomic); provision_runner.run's existing handler for that exception covers this call.
    # hook_token/gateway_token are NOT repeated here -- prepare_hermes_workspace already wrote
    # them, and re-asserting them here would gain nothing.
    await container_repo.update_container_leased(
        owner_id,
        claim_id,
        {"runtime_host": "agentcore", "agentcore_runtime_arn": arn, "status": "running"},
        attempt_token=attempt_token,
        release_lease=True,
    )
    logger.info("hermes runtime created owner=%s arn=%s", owner_id, arn)
    return arn
