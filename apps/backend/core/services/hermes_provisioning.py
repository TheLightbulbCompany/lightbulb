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
from dataclasses import dataclass

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


class AgentCoreRuntimeCreateFailed(RuntimeError):
    """F1 (predeploy review, M6-R34): the runtime reached a TERMINAL failure status
    (``CREATE_FAILED``/``UPDATE_FAILED``) -- dead, and AWS will never accept an
    ``UpdateAgentRuntime`` against it. Distinguished from ``_wait_for_runtime_ready``'s other
    ``RuntimeError`` (the poll window merely ran out while the runtime was still
    creating/updating -- it may yet succeed) so ``provision_hermes_runtime`` knows to delete
    the dead runtime and clear the row's ARN instead of leaving a retry stuck reusing it
    forever."""


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
    ``run_in_thread``.

    N3/M4-R41: paginates. A single unpaginated call only ever saw page 1 of
    ``list_agent_runtimes`` (``maxResults`` <= 100) -- one runtime per owner means the fleet
    outgrows page 1 eventually, and this recovery lookup would then silently miss every runtime
    past it, forever hitting a name conflict on create instead of finding the existing one.
    """
    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)
    paginator = client.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for runtime in page.get("agentRuntimes", []):
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


def _update_runtime_image(runtime_arn: str, request: dict) -> None:
    """Bring a reused (not freshly created) runtime onto the currently configured image --
    e.g. a broken tag fixed between retries. Sync — call via ``run_in_thread``.

    N2/M4-R40: ``UpdateAgentRuntime`` is a FULL REPLACE and requires ``roleArn`` (verified
    against the installed botocore service model: it is a REQUIRED member, alongside
    ``agentRuntimeId``/``agentRuntimeArtifact`` -- the previous call sent only the latter two and
    raised ``ParamValidationError`` on every invocation). Omitting ``networkConfiguration``/
    ``environmentVariables``/``protocolConfiguration``/``lifecycleConfiguration`` on a full replace
    would also silently drop the VPC mode, every env var, and the 4h/8h lifecycle. Reuses
    ``request`` -- the SAME dict ``build_hermes_runtime_request`` produced for create -- so update
    and create can never drift apart; strips ``agentRuntimeName``/``tags``, which the model does
    NOT accept on update (confirmed: neither is a member of ``UpdateAgentRuntime``'s input shape).
    """
    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    update_kwargs = {k: v for k, v in request.items() if k not in ("agentRuntimeName", "tags")}
    client.update_agent_runtime(agentRuntimeId=runtime_id, **update_kwargs)


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
            raise AgentCoreRuntimeCreateFailed(f"AgentCore runtime {runtime_arn} failed to start: {status}")
        time.sleep(_READY_POLL_INTERVAL_S)
    raise RuntimeError(f"AgentCore runtime {runtime_arn} did not reach READY within the poll window")


def _agentcore_data_client():
    import boto3

    return boto3.client("bedrock-agentcore", region_name=settings.AWS_REGION)


def _delete_runtime(runtime_arn: str) -> None:
    """``DeleteAgentRuntime``. Sync — call via ``run_in_thread``. Shared by F1's terminal-runtime
    cleanup (below) and ``teardown_hermes_runtime`` (F5)."""
    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)
    client.delete_agent_runtime(agentRuntimeId=runtime_arn.rsplit("/", 1)[-1])


def _stop_runtime_session(runtime_arn: str, session_id: str) -> None:
    """``StopRuntimeSession`` (M4-R16) — must run BEFORE the old bearer generation is revoked: a
    warm AgentCore session caches the bearer it read from the boot secret at cold-start, so
    denying/deleting the token row alone leaves an already-warm session presenting a now-dead
    credential until AWS reaps it on its own. Sync — call via ``run_in_thread``."""
    _agentcore_data_client().stop_runtime_session(agentRuntimeArn=runtime_arn, runtimeSessionId=session_id)


def _delete_boot_secret(owner_id: str) -> None:
    """Force-delete the owner's Hermes boot secret (no recovery window — a torn-down owner's
    credentials are dead the moment the runtime they were minted for is gone). Sync — call via
    ``run_in_thread``."""
    import boto3

    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    try:
        sm.delete_secret(SecretId=boot_secret_name(owner_id), ForceDeleteWithoutRecovery=True)
    except sm.exceptions.ResourceNotFoundException:
        pass


def _delete_profile_prefix_objects(owner_id: str) -> None:
    """Delete every S3 object under ``<owner>/hermes/`` -- the whole Hermes home, every agent's
    profile plus the root ``auth.json``. Only called by ``teardown_hermes_runtime`` when
    ``delete_profiles=True`` (an account wipe, never a rollback). Paginated (S3's own 1000-key
    batch cap), sync — call via ``run_in_thread``. Mirrors ``hermes_profiles.delete_profile``'s
    own pagination shape."""
    if not settings.OWNER_STATE_BUCKET:
        return
    import boto3

    from core.services.hermes_profiles import root_home_prefix

    client = boto3.client("s3")
    prefix = root_home_prefix(owner_id)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.OWNER_STATE_BUCKET, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=settings.OWNER_STATE_BUCKET, Delete={"Objects": keys})


async def teardown_hermes_runtime(owner_id: str, *, container_row: dict, delete_profiles: bool) -> None:
    """The AgentCore-side teardown (F5, predeploy review, M6-R38) -- the ONE place that undoes
    what ``prepare_hermes_workspace``/``provision_hermes_runtime`` created, extracted from
    ``migrate_workspace_to_hermes.rollback``'s own AgentCore half so every caller that needs to
    tear an AgentCore workspace down shares it instead of re-deriving the AWS call sequence.

    Steps, each best-effort -- an already-gone runtime/secret/hash/prefix must never stop the
    caller's own teardown (a rollback flipping the row back to ECS, an account wipe deleting the
    containers row):

      1. ``StopRuntimeSession`` (M4-R16) -- a warm session caches the bearer it read at
         cold-start; skipping this can leave it presenting a dead credential until AWS reaps it.
      2. ``DeleteAgentRuntime``.
      3. Revoke the row's own ``bedrock_gate_token_hash`` (the live bearer generation).
      4. Delete the ONE JSON boot secret.
      5. (``delete_profiles`` only) delete every object under ``<owner>/hermes/`` in S3.

    ``delete_profiles=False`` is ``rollback``'s own choice -- S3 profiles are cheap and the next
    ``migrate()`` attempt overwrites them anyway. ``delete_profiles=True`` is for a caller after
    which NOTHING will ever provision this owner's Hermes workspace again (an account wipe /
    ``ecs_manager.delete_user_service`` on an agentcore row) -- otherwise the whole
    ``<owner>/hermes/`` prefix (every profile, the root ``auth.json``) orphans indefinitely in
    S3 with no row left to even name it.

    ``container_row`` supplies ``agentcore_runtime_arn`` (the runtime to stop+delete) and
    ``bedrock_gate_token_hash`` (the bearer generation to revoke) -- the caller's OWN
    already-read row, never re-read here (a wipe's caller already holds the authoritative
    ``ALL_OLD`` image; a rollback's caller already read it strongly consistent).
    """
    from core.billing.bedrock_gate_tokens import delete_gate_token_rows
    from core.services.runtime_transport import session_id_for

    arn = container_row.get("agentcore_runtime_arn")
    if arn:
        try:
            await run_in_thread(_stop_runtime_session, arn, session_id_for(owner_id))
        except Exception:  # noqa: BLE001 — best-effort: an already-cold/gone session is not fatal
            logger.warning(
                "hermes teardown: StopRuntimeSession failed for owner=%s arn=%s", owner_id, arn, exc_info=True
            )
        try:
            await run_in_thread(_delete_runtime, arn)
        except Exception:  # noqa: BLE001 — best-effort: the runtime may already be gone
            logger.warning(
                "hermes teardown: DeleteAgentRuntime failed for owner=%s arn=%s", owner_id, arn, exc_info=True
            )

    current_hash = container_row.get("bedrock_gate_token_hash")
    if current_hash:
        try:
            await delete_gate_token_rows([current_hash])
        except Exception:  # noqa: BLE001 — best-effort: a stranded row is a known, accepted risk elsewhere too
            logger.warning("hermes teardown: could not revoke gate token for owner=%s", owner_id, exc_info=True)

    try:
        await run_in_thread(_delete_boot_secret, owner_id)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        logger.warning("hermes teardown: could not delete boot secret for owner=%s", owner_id, exc_info=True)

    if delete_profiles:
        try:
            await run_in_thread(_delete_profile_prefix_objects, owner_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning("hermes teardown: could not delete S3 profiles for owner=%s", owner_id, exc_info=True)


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


class HermesProfileError(RuntimeError):
    """A Hermes profile's owner-level inputs, or a per-agent Paperclip API key
    mint, failed hard (fix round 3, M6-R13). A profile that boots but cannot
    authenticate -- to its own LLM provider or to Paperclip -- is a failed
    install, not a degrade, so this is raised instead of shipping an empty
    credential.

    Subclasses ``RuntimeError`` (not a bare ``Exception``) so
    ``container_repo.classify_provision_error``'s treatment of the
    provisioning-runner path is UNCHANGED: none of that function's branches
    single out ``RuntimeError`` by exact type, so a ``HermesProfileError``
    classifies identically to a plain ``RuntimeError`` (retryable, per its
    "everything else" default) — asserted directly in
    ``test_classify_provision_error_treats_hermes_profile_error_like_runtime_error``.
    Also lets every existing ``pytest.raises(RuntimeError, ...)`` /
    ``except RuntimeError`` in this module and its callers keep working
    unchanged.
    """


@dataclass(frozen=True)
class ProfileInputs:
    """The owner-level Hermes profile writer inputs, sourced IDENTICALLY by
    every caller of ``hermes_profiles.write_agent_profile`` (fix round 3,
    M6-R10). Before this, ``catalog_service._build_and_upload_agent_profile``
    hand-rolled its own version of this sourcing that diverged from
    ``_write_profiles``'s in three ways: it skipped
    ``provider_choice_from_row``'s ``trial_local`` branch (a card-less trial
    owner's marketplace deploy would land the full paid model ladder instead
    of ``BEDROCK_TRIAL_CHOICE``), it returned ``None`` for an unset
    ``byo_provider`` on a ``byo_key`` owner instead of the ``"anthropic"``
    default this module reads, and it degraded a roster-read failure to a
    manifest-derived stand-in agent row instead of hard-failing.

    Per-agent inputs (the Paperclip API key mint, the agent row itself) are
    deliberately NOT here — both callers resolve those per agent, not per
    owner, and mint/lookup them differently (a six-agent fan-out here vs. a
    single freshly-deployed agent in catalog_service).
    """

    provider_choice: str
    byo_provider: str | None
    byo_api_key: str | None
    aip_arns: dict[str, str]
    integrations: dict[str, dict]
    env_vars: dict[str, str]


async def resolve_profile_inputs(owner_id: str, *, container_row: dict) -> ProfileInputs:
    """The six owner-level Hermes profile writer inputs (fix round 3, M6-R10)
    — the ONE function every caller of ``write_agent_profile`` sources them
    through, so they can never diverge again.

    ``container_row`` supplies ``aip_arns`` (the containers table's
    ``bedrock_aip_arns`` field) — the same source ``catalog_service`` always
    read directly. ``_write_profiles``'s own caller
    (``provision_hermes_runtime``) already has a real ``aip_arns`` value from
    its own ``_load_role_and_aips`` call by the time it calls this (that
    call's ``ensure_owner_aips`` is what persists ``bedrock_aip_arns`` onto
    the row in the first place), so it passes a lightweight
    ``{"bedrock_aip_arns": aip_arns}`` here rather than re-reading the row —
    the row it read at its own entry was captured BEFORE that write landed,
    so trusting it here would silently reintroduce a stale/empty AIP map.

    Raises ``HermesProfileError`` when ``provider_choice == "byo_key"`` and
    no readable key exists for ``byo_provider or "anthropic"`` — a profile
    with no LLM credential at all is a failed install, not a degrade
    (M6-R11, folded into this shared helper by M6-R10).
    """
    from core.services.container_provision import resolve_provider_choice
    from core.services.credentials.mcp_server import build_connected_integration_servers
    from core.services.key_service import read_byo_key
    from core.services.provisioning_landing import build_provisioned_env

    provider_choice, byo_provider = await resolve_provider_choice(owner_id)
    byo_api_key = await read_byo_key(owner_id, byo_provider or "anthropic") if provider_choice == "byo_key" else None
    if provider_choice == "byo_key" and not byo_api_key:
        raise HermesProfileError(f"hermes profile for owner={owner_id}: byo api key read failed")
    integrations = await build_connected_integration_servers(owner_id)
    env_vars = await build_provisioned_env(owner_id)
    return ProfileInputs(
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        byo_api_key=byo_api_key,
        aip_arns=container_row.get("bedrock_aip_arns") or {},
        integrations=integrations,
        env_vars=env_vars,
    )


async def write_profile_for_agent(
    owner_id: str,
    agent_row: dict,
    *,
    container_row: dict,
    soul_md_override: str | None = None,
    inputs: ProfileInputs | None = None,
) -> None:
    """The per-agent Hermes profile write (M6-R23): owner-level inputs (``resolve_profile_inputs``)
    plus this one agent's own Paperclip API key -> ``write_agent_profile`` -> ``upload_profile``.

    Moved, unchanged in behaviour, from ``catalog_service._build_and_upload_agent_profile`` --
    this is now the ONE per-agent "write this agent's profile" seam every caller shares: the
    roster fan-out below (``_write_profiles``), a single marketplace-deploy install
    (``catalog_service._build_and_upload_agent_profile``, now a thin resolve-then-delegate
    wrapper), and ``runtime_rpc``'s ``agents.create`` door for an AgentCore owner (Q9a row #1).

    ``agent_row`` is the caller's OWN already-resolved Paperclip roster entry (the shape
    ``write_agent_profile`` documents: ``{"id","name","role","title","capabilities","icon"}``) --
    this function does no roster read of its own, so a caller iterating a roster it already holds
    (``_write_profiles``, below) never re-reads it per agent.

    Raises ``HermesProfileError`` when ``resolve_profile_inputs`` hard-fails (a missing BYO key)
    or this agent's own Paperclip API key mint/read fails (I2/M6-R13) -- a profile that boots but
    cannot authenticate is a failed install, not a degrade. A ``ValueError`` from
    ``write_agent_profile`` (an illegal dotenv value) is re-raised as a named ``RuntimeError`` so a
    bad integration/env value fails with a diagnosable, agent-scoped message instead of leaving a
    roster half profiled with no indication which agent or variable broke it.

    ``soul_md_override``, when given, replaces the derived SOUL.md (``write_agent_profile``'s own
    ``build_soul_md(agent_row)``) verbatim -- for a caller staging identity text that isn't purely
    roster-derived. ``None`` (the default) keeps today's behaviour for every existing caller.

    ``inputs``, when given, is used AS-IS instead of calling ``resolve_profile_inputs`` again --
    Q9a fix round 1, I3: ``_write_profiles``'s six-agent roster fan-out resolves the SAME
    owner-level inputs ONCE and passes them to every agent, rather than re-reading billing/
    Secrets/integrations/env six times (and risking them diverging mid-fan-out if a value
    changes between agents). Every other caller (a single freshly-created/deployed agent) leaves
    this ``None`` and gets its own fresh resolve, unchanged.
    """
    from core.services.hermes_profiles import upload_profile, write_agent_profile
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_api_key import ensure_paperclip_api_key

    agent_id = agent_row["id"]
    if inputs is None:
        inputs = await resolve_profile_inputs(owner_id, container_row=container_row)
    paperclip_api_key = await ensure_paperclip_api_key(
        owner_id=owner_id, paperclip_agent_id=agent_id, admin_client=shared_admin_client()
    )
    if paperclip_api_key is None:
        # I2/M6-R13: never bake an empty credential. See this function's own docstring above --
        # there is no "next provision" here to retry into, unlike the ECS poll-thread caller.
        raise HermesProfileError(
            f"hermes profile for owner={owner_id} agent={agent_id}: paperclip API key mint/read failed"
        )
    try:
        files = write_agent_profile(
            owner_id,
            agent_row,
            provider_choice=inputs.provider_choice,
            aip_arns=inputs.aip_arns,
            paperclip_api_key=paperclip_api_key,
            integrations=inputs.integrations,
            env_vars=inputs.env_vars,
            byo_provider=inputs.byo_provider,
            byo_api_key=inputs.byo_api_key,
        )
    except ValueError as exc:
        raise RuntimeError(
            f"hermes profile for owner={owner_id} agent={agent_id} could not be rendered: {exc}"
        ) from exc
    if soul_md_override is not None:
        files.soul_md = soul_md_override
    await upload_profile(owner_id, agent_id, files)


async def _write_profiles(owner_id: str, aip_arns: dict[str, str]) -> None:
    """Step 1's profile fan-out: one Hermes profile (config.yaml + .env + SOUL.md) per starter
    agent, uploaded to S3.

    Roster via ``core.services.messaging.load_agent_roster`` (M4-R10) — the same normalized,
    UUID-validated shape ``write_agent_profile``'s ``agent`` parameter expects.

    M6-R23: the per-agent gate/write/upload now lives in ``write_profile_for_agent`` — the same
    function ``catalog_service``'s single-agent install and ``runtime_rpc``'s ``agents.create``
    door for an AgentCore owner call, so there is exactly one place that mints an agent's
    Paperclip key, writes its profile, and uploads it.

    Q9a fix round 1, I3: owner-level inputs are resolved ONCE here and threaded into every
    agent's ``write_profile_for_agent`` call via its ``inputs`` parameter — not once per agent
    (six billing/Secrets/integrations/env reads for a six-agent starter roster, and a risk that
    the roster diverges on those inputs mid-fan-out).

    Fix round 1, Important 2: after every agent has its profile, ``_stage_owner_facts`` uploads
    ``lightbulb-facts.json`` to each one too — see its own docstring for why this must happen
    HERE rather than by simply calling ``owner_facts.write_owner_facts``.
    """
    from core.services.messaging import load_agent_roster

    container_row = {"bedrock_aip_arns": aip_arns}
    inputs = await resolve_profile_inputs(owner_id, container_row=container_row)
    roster = await load_agent_roster(owner_id)
    for agent in roster:
        await write_profile_for_agent(owner_id, agent, container_row=container_row, inputs=inputs)
    await _stage_owner_facts(owner_id, [agent["id"] for agent in roster], inputs)


async def _stage_owner_facts(owner_id: str, agent_ids: list[str], inputs: ProfileInputs) -> None:
    """Fix round 1, Important 2: a freshly provisioned AgentCore workspace never runs
    ``ecs_manager.write_user_configs`` (the ONLY caller of ``owner_facts.write_owner_facts``
    today), so it had NO ``lightbulb-facts.json`` at all while the bundled skill tells every
    agent to read it. Stages the SAME facts file directly into every roster profile instead of
    calling ``write_owner_facts`` itself: at this point in provisioning the row's
    ``runtime_host`` has NOT yet flipped to ``"agentcore"`` (``_write_profiles`` runs BEFORE
    ``CreateAgentRuntime`` records the ARN -- N4/M4-R42, above), so ``write_owner_facts``'s own
    ``is_agentcore_owner`` check would take the wrong (EFS) branch here and write nothing
    anyone will ever read.

    Sources plan/provider the same way ``ecs_manager.write_user_configs``'s own
    ``write_owner_facts`` call does (as ``account_overrides``): this provision's OWN
    just-resolved ``inputs.provider_choice``/``inputs.byo_provider``, not a re-read that could
    race an in-flight billing write. Email + provisioning-workflow lookups reuse
    ``owner_facts``'s own private helpers so this can never diverge from what the ECS path
    renders. Best-effort, matching ``write_owner_facts``'s own contract -- a facts-file hiccup
    must never fail provisioning."""
    import json

    from core.repositories import billing_repo
    from core.services import hermes_profiles
    from core.services.owner_facts import _lookup_email_address, _lookup_provisioning, build_owner_facts

    try:
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
        account = {**account, "provider_choice": inputs.provider_choice, "byo_provider": inputs.byo_provider}
        facts = build_owner_facts(
            owner_id,
            account,
            email_address=await _lookup_email_address(owner_id),
            provisioning=await _lookup_provisioning(owner_id),
        )
        facts_json = json.dumps(facts, indent=2) + "\n"
        await run_in_thread(hermes_profiles.write_facts_to_profiles, owner_id, agent_ids, facts_json)
    except Exception:  # noqa: BLE001 — a facts-file hiccup must never fail provisioning
        logger.exception("hermes profile facts staging failed for owner=%s", owner_id)


async def provision_hermes_runtime(
    owner_id: str, *, claim_id: str, attempt_token: str | None = None, migrate: bool = False
) -> str:
    """Create this workspace's AgentCore runtime and point its container row at it.

    REQUIRES ``prepare_hermes_workspace`` to have already run for this owner — it writes the boot
    secret this reads the NAME of (not the contents) and the ``hook_token``/``gateway_token`` this
    never re-writes. ``provision_runner.run`` always calls it first when ``settings.HERMES_RUNTIME``
    is on.

    Fully idempotent: a row with ``agentcore_runtime_arn`` AND ``status="running"`` returns as-is,
    with no AWS call and no row write. A row with the ARN but NOT yet ``running`` (a retry that
    reached the create but not the terminal write) skips straight to the READY poll instead of
    creating again (M4-R35/C1) — see the ARN-recording comment below for why that field exists.

    N1/M4-R39 freshness guard: the SAME ``service_name``/``migrate`` check ``prepare_hermes_workspace``
    already makes (see its docstring) — this function is the OTHER half of that guard. Without it, a
    dispatch that reached here for an owner with a live ECS service would still create a runtime and
    flip the row to ``runtime_host="agentcore"``/``status="running"`` — pointing
    ``runtime_transport.host_for`` at a runtime whose boot secret was never written (``prepare``
    already refused to mint it), while the ECS task keeps serving traffic underneath it. Raises
    ``ValueError`` (not a bare ``RuntimeError``) so ``classify_provision_error`` maps it to
    deterministic/``EXIT_DETERMINISTIC`` unconditionally, running C2's release-to-error path instead
    of looping forever. ``migrate=True`` is the sanctioned bypass for the M6 cutover job, which calls
    both this and ``prepare_hermes_workspace`` with ``migrate=True`` and stops the ECS service itself
    once this reaches READY — not this module's job.

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
    if row.get("service_name") and not migrate:
        logger.info(
            "provision_hermes_runtime: owner %s has a live ECS service (service_name=%s) and "
            "migrate=False -- refusing to create a second runtime for it (N1/M4-R39)",
            owner_id,
            row.get("service_name"),
        )
        raise ValueError(
            f"provision_hermes_runtime refused for owner={owner_id}: live ECS service "
            f"{row.get('service_name')!r} present and migrate=False (N1/M4-R39)"
        )

    role_arn, aip_arns = await _load_role_and_aips(owner_id)
    if not arn:
        # N4/M4-R42: _write_profiles runs BEFORE CreateAgentRuntime, so a row that already
        # carries an ARN has its profiles in S3 already. Idempotence now keys on
        # status == "running" (M4-R35/C1), so an ARN row sitting in error/stopped -- which C2's
        # release-to-error path now produces routinely -- must skip straight to recovering the
        # runtime (poll READY / update on image drift / fenced terminal write) instead of
        # re-running the fan-out and overwriting owner-edited profiles + bumping .revision.
        await _write_profiles(owner_id, aip_arns)

    if not arn:
        # M4-R35: the row has no ARN yet -- but AgentCore itself might, if a prior attempt got
        # as far as CreateAgentRuntime and then died (or lost its fence) before recording it.
        # Runtime names are unique per account, so skipping this check would hit a name
        # conflict on every future attempt, forever.
        arn = await run_in_thread(_find_existing_runtime_arn, runtime_name_for(owner_id))

    def _build_request() -> dict:
        # N2/M4-R40: the ONE spec-building path for BOTH CreateAgentRuntime and (minus its
        # create-only fields, plus agentRuntimeId -- see _update_runtime_image) UpdateAgentRuntime
        # -- so a reused runtime whose image drifted gets updated with the exact same
        # roleArn/network/env/lifecycle a fresh create would have gotten, never a hand-built
        # partial replace. Only called from the two branches that actually need it (create, or
        # update-on-drift) -- gate_url() is a real (cached) SSM read, not free to call on every
        # idempotent poll-and-reuse pass.
        from core.billing.bedrock_gate_url import gate_url

        return build_hermes_runtime_request(
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

    if arn:
        # Reusing an already-created runtime (recorded on the row, or found by name). Bring it
        # onto the currently configured image if it drifted (e.g. a broken tag fixed between
        # retries); otherwise just poll it below.
        current_image = await run_in_thread(_runtime_image_uri, arn)
        if current_image and current_image != settings.AGENTCORE_HERMES_IMAGE:
            await run_in_thread(_update_runtime_image, arn, _build_request())
        if not row.get("agentcore_runtime_arn"):
            # Found by name lookup only -- the row never got it. Record now, before polling,
            # for the same reason the freshly-created branch below does.
            await container_repo.update_container_leased(
                owner_id, claim_id, {"agentcore_runtime_arn": arn}, attempt_token=attempt_token, release_lease=False
            )
    else:
        arn = await run_in_thread(_create_runtime, _build_request())
        # M4-R35 (C1): record the ARN IMMEDIATELY, non-terminal, fenced -- BEFORE the READY
        # poll. Without this, a CREATE_FAILED, a poll-window timeout, or a lost fence on the
        # terminal write below all lose the ARN with nothing to reconcile it: AgentCore runtime
        # names are unique per account, so every future retry then hits a name conflict on
        # CreateAgentRuntime, forever. A retry that sees this row field takes the `if arn:`
        # branch above instead of creating again.
        await container_repo.update_container_leased(
            owner_id, claim_id, {"agentcore_runtime_arn": arn}, attempt_token=attempt_token, release_lease=False
        )

    try:
        await run_in_thread(_wait_for_runtime_ready, arn)
    except AgentCoreRuntimeCreateFailed:
        # F1 (predeploy review, M6-R34): CREATE_FAILED/UPDATE_FAILED is a DEAD runtime -- AWS
        # will refuse an UpdateAgentRuntime against it, so every retry that found this ARN on
        # the row via the `if arn:` branch above would otherwise call Update on a terminal
        # runtime forever. Delete it (best-effort) and clear the row's ARN with the SAME fenced
        # write the ARN record used, THEN raise -- a retry after the underlying cause (e.g. an
        # unpushed image tag) is fixed then takes the CREATE path instead.
        logger.warning(
            "hermes provision: runtime create/update failed for owner=%s arn=%s -- deleting and "
            "clearing the row's ARN so the next attempt creates fresh",
            owner_id,
            arn,
        )
        try:
            await run_in_thread(_delete_runtime, arn)
        except Exception:  # noqa: BLE001 — best-effort: the runtime may already be gone
            logger.warning(
                "hermes provision: could not delete the failed runtime owner=%s arn=%s", owner_id, arn, exc_info=True
            )
        # A lost fence here means a concurrent takeover now owns the row -- ProvisionSuperseded
        # propagates in place of the runtime failure, which matches every other fenced write in
        # this module (provision_runner.run's existing handler already covers both).
        await container_repo.update_container_leased(
            owner_id, claim_id, {"agentcore_runtime_arn": None}, attempt_token=attempt_token, release_lease=False
        )
        raise

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
