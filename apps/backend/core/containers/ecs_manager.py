"""
ECS Fargate service manager for per-user OpenClaw gateways.

Each subscriber gets a dedicated ECS Service (desiredCount 0/1) that
runs an OpenClaw gateway container with a per-user EFS access point
for data isolation. Task IPs are discovered via AWS Cloud Map
(``DiscoverInstances``).
"""

import asyncio
import hashlib
import logging
import re
import secrets
import socket
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import utc_now_iso
from core.observability.metrics import put_metric, timing
from core.containers.config import (
    BEDROCK_PROVIDER_CHOICES,
    BEDROCK_TRIAL_CHOICE,
    CHANNEL_PROVIDER_IDS,
    build_device_paired_json,
    generate_node_device_identity,
    load_node_device_identity,
    read_openclaw_config_from_efs,
    write_openclaw_config,
)
from core.containers.workspace import get_workspace
from core.repositories import container_repo
from core.repositories.container_repo import ProvisionSuperseded

logger = logging.getLogger(__name__)

GATEWAY_PORT = 18789

# Single per-user task size for the flat-fee pivot. Per spec §3.2 / §10.
# Old per-tier sizing (free/starter 0.5/1, pro 1/2, enterprise 2/4) deleted —
# every paying user gets the same 0.5 vCPU / 1 GB box. Kept as constants
# so the resize/register paths pull from a single place.
PER_USER_CPU = "512"
PER_USER_MEMORY_MIB = "2048"

# The AIP tiers minted for a `bedrock_trial` (no-card free tier) owner. ONE
# tier, and it must be sonnet: config.py makes sonnet the trial's
# agents.defaults.model.primary and hard-fails a Bedrock config without it.
#
# This list IS the free tier's security boundary, not a cost optimization.
# build_owner_bedrock_policy scopes the per-owner task role to exactly the AIPs
# it is handed and explicitly denies bedrock:CreateInferenceProfile, so a
# one-tier mint leaves the container with no credential path to Opus/Haiku —
# the owner cannot reach them even by rewriting their own openclaw.json (which
# they can do: agents run with `exec: full`).
BEDROCK_TRIAL_TIERS = ["sonnet"]

# Per-user services run exactly one task whose OpenClaw process writes SQLite
# state (chat history, task runs, memory) into a shared per-owner EFS dir in
# WAL mode. ECS's default rolling deploy (200% / 100%) briefly runs the old
# and new task at the same time; two OpenClaw processes on different Fargate
# hosts opening the same WAL database over NFS corrupts it, because WAL's -shm
# coordination only works between processes on a single host. Pinning
# maximumPercent=100 (never two tasks) and minimumHealthyPercent=0 (allowed to
# drop to zero between them) makes ECS stop the old task before starting the
# new one, so the EFS state is only ever touched by a single writer.
SINGLE_WRITER_DEPLOYMENT_CONFIG = {
    "deploymentCircuitBreaker": {"enable": True, "rollback": False},
    "minimumHealthyPercent": 0,
    "maximumPercent": 100,
}


def _user_owned_mcp_servers(existing_servers: dict, integration_provider_keys: set[str]) -> dict:
    """User-added MCP servers to carry across a full openclaw.json regenerate.

    The reprovision path rebuilds openclaw.json from structured inputs and would
    otherwise drop anything added through the manual MCP panel — those servers
    live only in the on-disk file, not in a DDB store the generator reads back.
    Preserve every existing server whose key is neither a registry integration
    key (re-injected from the credential system) nor the legacy ``paperclip``
    block — i.e. the ones the user added themselves. The ``paperclip`` MCP
    server is no longer seeded (each agent authenticates with its own per-agent
    key via the bundled skill), so excluding it here PURGES any stale on-disk
    copy on the next reprovision instead of carrying it forward.
    """
    reserved = integration_provider_keys | {"paperclip"}
    return {name: block for name, block in existing_servers.items() if name not in reserved and isinstance(block, dict)}


async def _provision_mcp_servers(owner_id: str, connected_servers: dict) -> dict:
    """Servers to inject into a regenerated openclaw.json: the user's own
    on-disk MCP servers plus the connected integration servers.

    The reprovision path fully overwrites openclaw.json, so without this a
    manually-added MCP server (which lives only on disk) is lost. Read the
    current on-disk servers, keep the user-owned ones (not a registry
    integration key, not the seeded paperclip block), and merge — connected
    integrations win on the (impossible) key collision since user-owned keys
    exclude integration keys.

    Fail-safe: any read/registry hiccup falls back to ``connected_servers`` so
    provisioning never breaks (worst case the user re-adds the server).
    """
    try:
        from core.services.credentials import registry

        existing_cfg = await read_openclaw_config_from_efs(owner_id) or {}
        existing_servers = (existing_cfg.get("mcp") or {}).get("servers") or {}
        if not isinstance(existing_servers, dict):
            existing_servers = {}
        integration_keys = {
            p.wiring.mcp_server.key for p in registry.all_providers().values() if p.wiring.mcp_server is not None
        }
        user_servers = _user_owned_mcp_servers(existing_servers, integration_keys)
        return {**user_servers, **connected_servers}
    except Exception:  # noqa: BLE001 — best-effort; never fail provision over this.
        logger.exception("preserving user MCP servers failed for %s; continuing", owner_id)
        return connected_servers


async def _provision_channel_accounts(owner_id: str) -> dict[str, dict[str, dict]]:
    """Channel bot accounts to re-inject into a regenerated openclaw.json.

    The reprovision path fully overwrites openclaw.json, so without this every
    configured Telegram/Slack/Discord account — bot token, allowFrom pairings
    and all — is lost, exactly like the MCP-server case that
    ``_provision_mcp_servers`` handles. Read the live on-disk accounts and
    return them keyed ``{provider: {accountId: account}}``;
    ``build_openclaw_config_dict`` merges them back and recreates the sibling
    route bindings.

    Fail-safe: any read/parse hiccup returns ``{}`` so provisioning never breaks
    (worst case the user re-runs the channel wizard).
    """
    try:
        existing_cfg = await read_openclaw_config_from_efs(owner_id) or {}
        channels = existing_cfg.get("channels") or {}
        out: dict[str, dict[str, dict]] = {}
        for provider in CHANNEL_PROVIDER_IDS:
            entry = channels.get(provider)
            if not isinstance(entry, dict):
                continue
            accounts = entry.get("accounts")
            if isinstance(accounts, dict) and accounts:
                out[provider] = accounts
        return out
    except Exception:  # noqa: BLE001 — best-effort; never fail provision over this.
        logger.exception("preserving channel accounts failed for %s; continuing", owner_id)
        return {}


def _build_per_user_environment(
    *,
    hook_token: str,
    existing: list[dict],
) -> list[dict]:
    """Return the per-user ``environment`` list with OPENCLAW_HOOK_TOKEN injected.

    ``existing`` is the caller-supplied list of per-user env entries (e.g. an
    earlier flow may layer in CODEX_HOME or other markers). We preserve every
    entry verbatim except we replace any pre-existing ``OPENCLAW_HOOK_TOKEN``
    with the freshly generated value (matters for the backfill: the old
    task-def carried no token, but a half-complete prior backfill could have
    left a stale one).
    """
    out: list[dict] = [e for e in existing if e.get("name") != "OPENCLAW_HOOK_TOKEN"]
    out.append({"name": "OPENCLAW_HOOK_TOKEN", "value": hook_token})
    return out


def _build_bedrock_acp_harness_env(
    aip_arns: dict[str, str],
    *,
    gate_token: str | None = None,
    gate_url: str | None = None,
) -> list[dict]:
    """Env that points the ACP coding harness (Claude Code) at the owner's
    per-owner Bedrock Application Inference Profiles.

    Claude Code — spawned by acpx for ``sessions_spawn({runtime:"acp"})`` — reads
    these to route its own Bedrock calls. ``ANTHROPIC_MODEL`` /
    ``ANTHROPIC_SMALL_FAST_MODEL`` MUST be the owner's AIP ARNs, never base model
    ids: Isol8 attributes Bedrock billing by AIP (the bedrock-usage-meter Lambda
    keys on the ``bedrock-aip-owners`` table), and the per-user task role grants
    Bedrock only on ``application-inference-profile/*`` — so a base-model call is
    BOTH unbilled (the meter's log filter misses it) AND IAM-denied. Pointing the
    harness at the owner's AIP is what makes "Claude Code on Bedrock draws the
    owner's credits" true. ``AWS_REGION`` is set explicitly because, unlike
    OpenClaw's native provider (which bakes the region into its baseUrl), Claude
    Code's AWS SDK resolves the region from the environment, and the base task
    def does not set it.

    Tier mapping mirrors Claude Code's own defaults: ``sonnet`` is the main
    coding model, ``haiku`` the small/fast background model.

    The bedrock-gate is the standard path: with a ``gate_url``/``gate_token``
    the harness routes its Bedrock client through the gate with the SAME
    per-owner bearer token as the OpenClaw provider. Claude Code binds to
    ``ANTHROPIC_BEDROCK_BASE_URL`` (verified against the 2.1.156 binary — plain
    AWS-SDK endpoint vars do NOT work), skips its own SigV4 signing
    (``CLAUDE_CODE_SKIP_BEDROCK_AUTH``), and presents the token as
    ``ANTHROPIC_AUTH_TOKEN`` — so no ACP-specific IAM carve-out is needed and
    the same gate enforces its spend. An absent ``gate_url`` (bootstrap /
    break-glass) leaves the harness on direct SigV4, which post-cutover also
    requires the break-glass IAM re-grant.
    """
    env = [
        {"name": "CLAUDE_CODE_USE_BEDROCK", "value": "1"},
        {"name": "AWS_REGION", "value": settings.AWS_REGION},
        {"name": "ANTHROPIC_MODEL", "value": aip_arns["sonnet"]},
        {"name": "ANTHROPIC_SMALL_FAST_MODEL", "value": aip_arns["haiku"]},
    ]
    if gate_url and gate_token:
        env.extend(
            [
                {"name": "ANTHROPIC_BEDROCK_BASE_URL", "value": gate_url},
                {"name": "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "value": "1"},
                {"name": "ANTHROPIC_AUTH_TOKEN", "value": gate_token},
            ]
        )
    return env


class EcsManagerError(Exception):
    """Raised when ECS operations fail."""

    def __init__(self, message: str, user_id: str = ""):
        super().__init__(message)
        self.user_id = user_id


# Why ``start_user_service`` may refuse to start a container. Both are POLICY
# refusals, not failures — see ContainerStartRefused.
#
# START_REFUSED_CAP / START_REFUSED_EXPIRED are deliberately the *same strings*
# as the provision gate's ``trial_daily_cap`` / ``trial_expired`` codes, so a
# router can hand either straight to the matching gate's ``.to_payload()`` and
# the owner sees the identical 402 card whichever door they knocked on.
# (Literals, not imports of trial_cap.DAILY_CAP/TRIAL_EXPIRED: trial_cap is
# imported lazily inside the guard to avoid a module cycle. A lockstep test
# pins these to it so they cannot drift.)
START_REFUSED_CAP = "trial_daily_cap"
START_REFUSED_EXPIRED = "trial_expired"
START_REFUSED_CONVERTING = "conversion_in_flight"

# Copy for each trial refusal. Keyed by the reason ``trial_cap.trial_block_reason``
# returns, so a new reason cannot be added without giving the owner words.
_TRIAL_REFUSAL_COPY = {
    START_REFUSED_CAP: "You've used today's free allowance — resets tomorrow.",
    START_REFUSED_EXPIRED: "Your 3-day free trial has ended.",
}


class ContainerStartRefused(Exception):
    """A guard refused to start a container. NOTHING FAILED.

    Deliberately NOT an ``EcsManagerError``: every ``except EcsManagerError`` in
    this codebase means "provisioning broke" and reacts accordingly — 503 to the
    caller, ``status="error"`` on the container row, rollback of AWS resources.
    A refusal is none of those things: the container is fine, we are declining to
    start it. Making this a subclass would silently route every refusal into
    those handlers and mark healthy rows broken.

    ``reason`` is one of the ``START_REFUSED_*`` constants above.
    """

    def __init__(self, reason: str, user_id: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.user_id = user_id


class EcsManager:
    """Manages per-user ECS Fargate services for OpenClaw gateways."""

    # The CDK base task def lives in this family; per-user clones live in
    # `<base>-user`. Splitting the families means `describe_task_definition`
    # on the base family always returns the latest CDK base — no need for
    # SSM-pinned ARNs.
    _BASE_TASK_DEF_FAMILY = "isol8-{env}-openclaw"
    _PER_USER_FAMILY_SUFFIX = "-user"

    def __init__(self):
        self._ecs = boto3.client("ecs", region_name=settings.AWS_REGION)
        self._efs = boto3.client("efs", region_name=settings.AWS_REGION)
        self._cluster = settings.ECS_CLUSTER_ARN
        self._base_task_def_family = self._BASE_TASK_DEF_FAMILY.format(
            env=settings.ENVIRONMENT or "dev",
        )
        self._subnets = [s.strip() for s in settings.ECS_SUBNETS.split(",") if s.strip()]
        self._security_groups = [settings.ECS_SECURITY_GROUP_ID]
        self._cloud_map_service_arn = settings.CLOUD_MAP_SERVICE_ARN
        self._servicediscovery = boto3.client("servicediscovery", region_name=settings.AWS_REGION)
        self._cloud_map_namespace_name = settings.CLOUD_MAP_NAMESPACE_NAME
        self._cloud_map_service_name = settings.CLOUD_MAP_SERVICE_NAME
        self._efs_file_system_id = settings.EFS_FILE_SYSTEM_ID

    def _service_name(self, user_id: str) -> str:
        """Generate deterministic, collision-resistant service name from user_id.

        ECS service names must be <= 255 chars. Format: openclaw-{user_id}-{hash}
        so services are identifiable in the ECS console. The hash suffix
        guarantees uniqueness even if user_id is truncated.
        """
        uid_hash = hashlib.sha256(user_id.encode()).hexdigest()[:12]
        # Sanitize user_id for ECS naming (alphanumeric, hyphens, underscores only)
        safe_uid = re.sub(r"[^a-zA-Z0-9_-]", "", user_id)[:40]
        return f"openclaw-{safe_uid}-{uid_hash}"

    # ------------------------------------------------------------------
    # Per-user EFS access points
    # ------------------------------------------------------------------

    def _create_access_point(self, user_id: str, owner_type: str = "personal") -> str:
        """Create a per-user EFS access point rooted at /users/{user_id}.

        The access point enforces POSIX UID/GID 0 (matching the
        container's root user) and creates the root directory
        with 0755 permissions if it doesn't exist.

        Args:
            user_id: Clerk user ID.

        Returns:
            The access point ID (e.g. "fsap-0abc123...").

        Raises:
            EcsManagerError: If the EFS API call fails.
        """
        try:
            resp = self._efs.create_access_point(
                FileSystemId=self._efs_file_system_id,
                PosixUser={"Uid": 1000, "Gid": 1000},
                RootDirectory={
                    "Path": f"/users/{user_id}",
                    "CreationInfo": {
                        "OwnerUid": 1000,
                        "OwnerGid": 1000,
                        "Permissions": "0755",
                    },
                },
                Tags=[
                    {"Key": "Name", "Value": f"isol8-{user_id}"},
                    {"Key": "owner_id", "Value": user_id},
                    {"Key": "owner_type", "Value": owner_type},
                    {"Key": "ManagedBy", "Value": "isol8-backend"},
                ],
            )
            access_point_id = resp["AccessPointId"]
            put_metric("container.efs.access_point", dimensions={"op": "create", "status": "ok"})
            logger.info("Created EFS access point %s for user %s", access_point_id, user_id)
            return access_point_id
        except Exception as e:
            put_metric("container.efs.access_point", dimensions={"op": "create", "status": "error"})
            raise EcsManagerError(
                f"Failed to create EFS access point for user {user_id}: {e}",
                user_id,
            )

    def _delete_access_point(self, access_point_id: str) -> None:
        """Delete an EFS access point. Idempotent (ignores not-found)."""
        try:
            self._efs.delete_access_point(AccessPointId=access_point_id)
            put_metric("container.efs.access_point", dimensions={"op": "delete", "status": "ok"})
            logger.info("Deleted EFS access point %s", access_point_id)
        except self._efs.exceptions.AccessPointNotFound:
            logger.warning("Access point %s already deleted", access_point_id)
        except Exception as e:
            put_metric("container.efs.access_point", dimensions={"op": "delete", "status": "error"})
            logger.error("Failed to delete access point %s: %s", access_point_id, e)

    # ------------------------------------------------------------------
    # Per-user task definition revisions
    # ------------------------------------------------------------------

    def _build_register_kwargs_from_base(
        self,
        access_point_id: str,
        new_image: str | None = None,
        new_cpu: str | None = None,
        new_memory: str | None = None,
        secrets_for_task: list[dict] | None = None,
        environment_for_task: list[dict] | None = None,
        hook_token: str | None = None,
        task_role_arn: str | None = None,
    ) -> dict:
        """Build register_task_definition kwargs by cloning the CDK-managed base.

        Reads the latest revision from the base family (e.g. ``isol8-dev-openclaw``)
        live on every call — that family contains ONLY CDK-managed base
        revisions because per-user clones register into ``<base>-user`` (see
        the family below). This means the running backend always picks up
        new CDK base deploys without a backend restart.

        Per-user state layered on top:
          - access_point_id  (per-user EFS access point, replaces the volume's)
          - new_image        (image_update flow only — None preserves base image)
          - new_cpu/memory   (tier-resize flow only — None preserves base values)
          - hook_token       (per-user OpenClaw webhook auth token; injected
                              as ``OPENCLAW_HOOK_TOKEN`` env var on the
                              primary container so the gateway can verify
                              inbound webhooks). Optional today — the
                              backfill-only callers (image-update, resize)
                              that re-register without a hook_token leave
                              the env var off and rely on a separate backfill
                              pass to land it.
        """
        desc_resp = self._ecs.describe_task_definition(
            taskDefinition=self._base_task_def_family,
        )
        base = desc_resp["taskDefinition"]

        volumes = []
        for vol in base.get("volumes", []):
            vol_copy = dict(vol)
            efs_config = vol_copy.get("efsVolumeConfiguration")
            if efs_config:
                efs_copy = dict(efs_config)
                auth_config = dict(efs_copy.get("authorizationConfig", {}))
                auth_config["accessPointId"] = access_point_id
                efs_copy["authorizationConfig"] = auth_config
                vol_copy["efsVolumeConfiguration"] = efs_copy
            volumes.append(vol_copy)

        # Deep-copy containerDefinitions because we may mutate `image`,
        # `secrets`, and `environment`. Each entry is itself a dict — we
        # shallow-copy each before mutation. Per-user `secrets:` (BYOK provider
        # key ARN) and `environment:` (chatgpt_oauth CODEX_HOME pointing at
        # the EFS-staged auth.json dir) are layered onto the primary container.
        container_defs = []
        for idx, cd in enumerate(base["containerDefinitions"]):
            cd_copy = dict(cd)
            if new_image:
                cd_copy["image"] = new_image
            # SIGTERM→SIGKILL grace. The ECS default (30s) truncates
            # OpenClaw's up-to-5-minute graceful drain on every Track-2
            # update, scale-to-zero stop, and deploy — killing in-flight
            # autonomous runs that restart-recovery then re-spends tokens
            # replaying. 120s is the Fargate maximum.
            cd_copy["stopTimeout"] = 120
            if idx == 0 and secrets_for_task:
                # Merge with any baseline `secrets` list the CDK base may have
                # set so we don't drop platform secrets while injecting per-user
                # provider keys.
                base_secrets = list(cd_copy.get("secrets") or [])
                cd_copy["secrets"] = base_secrets + list(secrets_for_task)
            if idx == 0:
                # Layer per-user env entries on top of the CDK base (which carries
                # CHOKIDAR_USEPOLLING, CLAWHUB_WORKDIR, etc.). When a hook_token
                # is provided, inject it as OPENCLAW_HOOK_TOKEN — the helper
                # also de-dupes any stale entry the caller supplied (matters
                # for the backfill story).
                base_env = list(cd_copy.get("environment") or [])
                per_user_env = list(environment_for_task or [])
                if hook_token:
                    per_user_env = _build_per_user_environment(
                        hook_token=hook_token,
                        existing=per_user_env,
                    )
                if per_user_env:
                    cd_copy["environment"] = base_env + per_user_env
            container_defs.append(cd_copy)

        reg_kwargs = dict(
            # Per-user clones go to a SEPARATE family so future
            # describe_task_definition(base_family) always returns a CDK base
            # revision, never a per-user clone. Keeps the cross-stack story
            # simple and removes the SSM-pin dance that #299 had to add when
            # the two kinds of revisions co-mingled.
            family=f"{base['family']}{self._PER_USER_FAMILY_SUFFIX}",
            taskRoleArn=task_role_arn or base.get("taskRoleArn", ""),
            executionRoleArn=base.get("executionRoleArn", ""),
            networkMode=base.get("networkMode", "awsvpc"),
            containerDefinitions=container_defs,
            volumes=volumes,
            requiresCompatibilities=base.get("requiresCompatibilities", ["FARGATE"]),
            # Single per-user task size (flat-fee pivot). Explicit `new_cpu`/
            # `new_memory` (used by the resize flow) win; otherwise fall back to
            # the canonical PER_USER_* constants instead of whatever the CDK base
            # task def carries — a stale base value would leak into per-user
            # revisions.
            cpu=new_cpu or PER_USER_CPU,
            memory=new_memory or PER_USER_MEMORY_MIB,
        )
        if base.get("runtimePlatform"):
            reg_kwargs["runtimePlatform"] = base["runtimePlatform"]

        return reg_kwargs

    def _register_task_definition(
        self,
        access_point_id: str,
        secrets_for_task: list[dict] | None = None,
        environment_for_task: list[dict] | None = None,
        hook_token: str | None = None,
        task_role_arn: str | None = None,
    ) -> str:
        """Clone the CDK base for a new per-user container.

        Args:
            access_point_id: The per-user EFS access point ID.
            secrets_for_task: Optional ECS-task ``secrets:`` entries to layer
                onto the primary container. Each entry is a dict like
                ``{"name": "OPENAI_API_KEY", "valueFrom": "<secrets-arn>"}``.
            environment_for_task: Optional plain ``environment:`` entries
                (``{"name": "...", "value": "..."}``). Used for the
                ``chatgpt_oauth`` path which injects ``CODEX_HOME`` so
                OpenClaw reads its auth.json from EFS.
            hook_token: Optional per-user OpenClaw webhook auth token. When
                provided, injected as ``OPENCLAW_HOOK_TOKEN`` env var on the
                primary container.

        Returns:
            The ARN of the newly registered task definition revision.

        Raises:
            EcsManagerError: If the ECS API calls fail.
        """
        try:
            reg_kwargs = self._build_register_kwargs_from_base(
                access_point_id,
                secrets_for_task=secrets_for_task,
                environment_for_task=environment_for_task,
                hook_token=hook_token,
                task_role_arn=task_role_arn,
            )
            reg_resp = self._ecs.register_task_definition(**reg_kwargs)
            task_def_arn = reg_resp["taskDefinition"]["taskDefinitionArn"]
            put_metric("container.task_def.register", dimensions={"status": "ok"})
            logger.info("Registered per-user task definition %s", task_def_arn)
            return task_def_arn
        except Exception as e:
            put_metric("container.task_def.register", dimensions={"status": "error"})
            raise EcsManagerError(
                f"Failed to register per-user task definition: {e}",
                user_id="",
            )

    def _deregister_task_definition(self, task_definition_arn: str) -> None:
        """Deregister a per-user task definition revision. Idempotent."""
        try:
            self._ecs.deregister_task_definition(taskDefinition=task_definition_arn)
            logger.info("Deregistered task definition %s", task_definition_arn)
        except Exception as e:
            logger.error("Failed to deregister task definition %s: %s", task_definition_arn, e)

    async def refresh_task_definition(self, owner_id: str) -> str | None:
        """Re-register a user's ECS task definition with the current env list,
        then roll the running task onto the new revision.

        Used by the hook-token backfill: existing per-user task-defs were
        registered before OPENCLAW_HOOK_TOKEN existed; just patching
        openclaw.json on EFS leaves the env var undefined inside the running
        container, so the gateway's ${VAR} substitution resolves to empty.
        This helper rebuilds the env list (now including the freshly-set
        hook_token from the containers row) and updates the service to
        deploy a new task-def revision.

        Unlike ``_register_task_definition`` (which clones from the CDK base
        family via ``_build_register_kwargs_from_base``), the backfill path
        must preserve every per-user mutation already on the live revision
        (BYOK provider secrets, chatgpt_oauth ``CODEX_HOME``, etc.). So we
        describe the user's CURRENT task-def, swap its env list, and
        re-register that — the base-cloning seam doesn't fit here.

        The EFS access point and DDB row are untouched — losing agent
        state would be a backfill regression.

        Returns the new task definition ARN on success, or None if the user
        has no provisioned container or the row is missing task_definition_arn.
        Callers need the ARN so they can wait specifically for the PRIMARY
        deployment to roll onto this revision (waiting for any RUNNING task
        races against the OLD task during ``forceNewDeployment``).
        """
        container = await container_repo.get_by_owner_id(owner_id)
        if not container or not container.get("task_definition_arn"):
            return None

        # Read the current task-def to pull family/volumes/secrets/networkMode/etc.
        current = self._ecs.describe_task_definition(taskDefinition=container["task_definition_arn"])
        td = current["taskDefinition"]

        # Rebuild the openclaw container-def with a fresh environment list
        # that includes the freshly-set OPENCLAW_HOOK_TOKEN.
        existing_env = next(c for c in td["containerDefinitions"] if c["name"] == "openclaw")["environment"]
        # Drop any container-wide PAPERCLIP_* env carried by task-defs registered
        # before per-agent Paperclip identity. That shared env let every agent's
        # curl resolve the seeded Friday identity; identity is now per-agent
        # (each agent reads its own claimed-key file), so the legacy shared env
        # must not survive a refresh and re-seed Friday's credentials fleet-wide.
        existing_env = [e for e in existing_env if not e.get("name", "").startswith("PAPERCLIP_")]
        new_env = _build_per_user_environment(
            hook_token=container.get("hook_token") or "",
            existing=existing_env,
        )
        for cd in td["containerDefinitions"]:
            if cd["name"] == "openclaw":
                cd["environment"] = new_env

        register_kwargs = {
            "family": td["family"],
            "containerDefinitions": td["containerDefinitions"],
            "volumes": td.get("volumes", []),
            "networkMode": td["networkMode"],
            "requiresCompatibilities": td.get("requiresCompatibilities", []),
            "cpu": td.get("cpu"),
            "memory": td.get("memory"),
            "executionRoleArn": td.get("executionRoleArn"),
            "taskRoleArn": td.get("taskRoleArn"),
        }
        register_kwargs = {k: v for k, v in register_kwargs.items() if v is not None}
        new_td = self._ecs.register_task_definition(**register_kwargs)
        new_arn = new_td["taskDefinition"]["taskDefinitionArn"]

        self._ecs.update_service(
            cluster=settings.ECS_CLUSTER_ARN,
            service=container["service_name"],
            taskDefinition=new_arn,
            forceNewDeployment=True,
        )
        # Persist the new ARN so subsequent operations see it.
        await container_repo.update_fields(owner_id, {"task_definition_arn": new_arn})
        return new_arn

    # ------------------------------------------------------------------
    # Safe DB helpers
    # ------------------------------------------------------------------

    async def _update_container(self, user_id: str, **fields) -> None:
        """Update container fields via the DynamoDB container repo."""
        await container_repo.update_fields(user_id, fields)

    # ------------------------------------------------------------------
    # Service lifecycle
    # ------------------------------------------------------------------

    async def create_user_service(
        self,
        user_id: str,
        gateway_token: str,
        owner_type: str = "personal",
        secrets_for_task: list[dict] | None = None,
        environment_for_task: list[dict] | None = None,
        hook_token: str | None = None,
        task_role_arn: str | None = None,
        bedrock_aip_arns: dict[str, str] | None = None,
        bedrock_gate_token_hash: str | None = None,
        claim_id: str | None = None,
    ) -> str:
        """Create an ECS Service for a user with per-user EFS isolation.

        1. Upserts the Container DB record (status=provisioning) so frontend can poll
        2. Creates a per-user EFS access point -> substatus=efs_created
        3. Registers a per-user task definition revision -> substatus=task_registered
        4. Creates the ECS service -> substatus=service_created

        On failure, sets status=error, substatus=None, and rolls back AWS resources.

        Args:
            user_id: Clerk user ID.
            gateway_token: Auth token for the OpenClaw gateway HTTP API.
            owner_type: "personal" or "org" for tagging the EFS access point.
            secrets_for_task: Optional per-user ECS ``secrets:`` entries to
                inject into the primary container (e.g. the BYOK
                OPENAI_API_KEY/ANTHROPIC_API_KEY ARN). Empty for the
                ``chatgpt_oauth`` and ``bedrock_claude`` paths.
            environment_for_task: Optional plain ``environment:`` entries.
                Used by the ``chatgpt_oauth`` path to set ``CODEX_HOME``
                so OpenClaw reads its auth.json from the EFS-staged dir.
            hook_token: Optional per-user OpenClaw webhook auth token. When
                provided, persisted onto the containers row by the upsert
                below (atomically with the rest of the row, so brand-new
                rows land with the token already set) and injected as
                ``OPENCLAW_HOOK_TOKEN`` on the primary container.
            task_role_arn: Optional per-owner ECS task-role ARN override (the
                scoped per-owner Bedrock role for the ``bedrock_claude`` path).
                Threaded into ``_register_task_definition`` AND, when present,
                persisted onto the containers row as ``bedrock_role_arn`` by the
                upsert below — see the bedrock-fields note there.
            bedrock_aip_arns: Optional per-owner AIP map ({tier: aip_arn}) for
                the ``bedrock_claude`` path. Persisted onto the containers row
                as ``bedrock_aip_arns`` by the upsert below.

        Returns:
            The ECS service name.

        Raises:
            EcsManagerError: If any step fails.
        """
        service_name = self._service_name(user_id)
        access_point_id = None
        task_def_arn = None

        # Step 0: Upsert container record EARLY so frontend can start polling.
        # Persist hook_token here so brand-new rows land with it set in the
        # same write that creates the row — folding it into a separate
        # set_hook_token call would no-op for first-time provisioning (the
        # row doesn't exist until this upsert).
        upsert_fields: dict = {
            "service_name": service_name,
            "gateway_token": gateway_token,
            "status": "provisioning",
            "substatus": None,
            "owner_type": owner_type,
        }
        if hook_token:
            upsert_fields["hook_token"] = hook_token
            upsert_fields["hook_token_rotated_at"] = utc_now_iso()
            # Stamp the backfill-style "this user is in the desired end-state"
            # marker so a re-run of the backfill script's idempotency guard
            # (which reads hook_token_provisioned_at) doesn't rotate native
            # users' tokens and silently break their external integrations.
            # Native provisioning achieves the desired state atomically here
            # (token in DDB + env on task def + openclaw.json rendered with
            # the hooks block); backfill achieves it after refresh + config
            # patch. Both should stamp.
            upsert_fields["hook_token_provisioned_at"] = utc_now_iso()
        # Persist the per-owner Bedrock role + AIP map here too, for the SAME
        # reason as hook_token: the mint (ensure_owner_aips/ensure_owner_bedrock_role)
        # ran BEFORE this upsert, so its container_repo.update_fields calls
        # no-opped against a not-yet-existing row (update_fields returns None and
        # writes nothing when the row is absent). Only the reverse table got
        # written. Without folding them into this row-creating upsert, the row
        # would carry no bedrock_role_arn, and the next resize/restart re-register
        # (which reads bedrock_role_arn off the row) would silently revert the
        # container to the shared all-models task role — which no longer carries
        # any Bedrock grant — AccessDenying every subsequent chat. restore_owner_bedrock
        # would also break (it reads the AIP map off the row).
        if task_role_arn:
            upsert_fields["bedrock_role_arn"] = task_role_arn
        if bedrock_aip_arns:
            upsert_fields["bedrock_aip_arns"] = bedrock_aip_arns
        # Same fold-in for the bedrock-gate token hash: mint_gate_token ran before
        # this row exists, so its own bedrock_gate_token_hash update_fields no-op'd
        # (writes nothing on an absent row). Persist it here so the row carries the
        # rotation pointer + deny-flag lookup key from first boot; without it,
        # deny_gate_token/rotation would silently no-op until the next reprovision.
        if bedrock_gate_token_hash:
            upsert_fields["bedrock_gate_token_hash"] = bedrock_gate_token_hash
        # CHECKPOINT before the row write: a run superseded during the mint must
        # not deposit its gateway_token / AIP map / role onto the row — the
        # upsert deliberately preserves the NEW owner's lease fields, so without
        # this fence the zombie's write would sail straight through.
        if claim_id is not None:
            await self._provision_checkpoint(user_id, claim_id)
        await container_repo.upsert(user_id, upsert_fields)

        try:
            # Step 1: Create per-user EFS access point
            access_point_id = self._create_access_point(user_id, owner_type=owner_type)
            await self._update_container(
                user_id,
                access_point_id=access_point_id,
                substatus="efs_created",
            )

            # Step 2: Register per-user task definition with that access point.
            # Inject any per-user secrets (e.g. byo_key OPENAI/ANTHROPIC ARN),
            # per-user env vars, and the per-user OPENCLAW_HOOK_TOKEN (so the
            # OpenClaw gateway can verify inbound webhook calls) before the
            # service is created so the first task pulls them.
            task_def_arn = self._register_task_definition(
                access_point_id,
                secrets_for_task=secrets_for_task,
                environment_for_task=environment_for_task,
                hook_token=hook_token,
                task_role_arn=task_role_arn,
            )
            await self._update_container(
                user_id,
                task_definition_arn=task_def_arn,
                substatus="task_registered",
            )

            # Step 3: Create ECS service with desiredCount=0 so the container
            # does NOT start yet -- the caller writes config files (openclaw.json)
            # to EFS before calling start_user_service().
            create_kwargs = dict(
                cluster=self._cluster,
                serviceName=service_name,
                taskDefinition=task_def_arn,
                desiredCount=0,
                launchType="FARGATE",
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": self._subnets,
                        "securityGroups": self._security_groups,
                        "assignPublicIp": "DISABLED",
                    }
                },
                serviceRegistries=[{"registryArn": self._cloud_map_service_arn}],
                deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
            )
            # Enable ECS Exec for all environments so we can debug per-user
            # OpenClaw containers (skill installs, workspace state, gateway
            # health) without redeploying. Requires the task role to grant
            # ssmmessages:CreateControlChannel/CreateDataChannel/OpenControlChannel/OpenDataChannel.
            create_kwargs["enableExecuteCommand"] = True
            # CHECKPOINT before the ECS service create — the last unfenced
            # window where a superseded run could stand up a service built from
            # its own task definition/role.
            if claim_id is not None:
                await self._provision_checkpoint(user_id, claim_id)
            self._ecs.create_service(**create_kwargs)
            await self._update_container(user_id, substatus="service_created")
        except ProvisionSuperseded:
            raise
        except EcsManagerError:
            put_metric("container.error_state")
            # Mark container as error in DB
            try:
                await self._update_container(user_id, status="error", substatus=None)
            except Exception:
                logger.error("Failed to mark container as error for user %s", user_id)
            # Rollback already-created AWS resources
            if task_def_arn:
                self._deregister_task_definition(task_def_arn)
            if access_point_id:
                self._delete_access_point(access_point_id)
            raise
        except Exception as e:
            put_metric("container.error_state")
            # Mark container as error in DB
            try:
                await self._update_container(user_id, status="error", substatus=None)
            except Exception:
                logger.error("Failed to mark container as error for user %s", user_id)
            # Rollback already-created AWS resources
            if task_def_arn:
                self._deregister_task_definition(task_def_arn)
            if access_point_id:
                self._delete_access_point(access_point_id)
            logger.error(
                "Failed to create ECS service %s for user %s: %s",
                service_name,
                user_id,
                e,
            )
            raise EcsManagerError(f"Failed to create ECS service: {e}", user_id)

        logger.info("Created ECS service %s for user %s", service_name, user_id)
        return service_name

    async def stop_user_service(self, user_id: str) -> None:
        """Scale a user's ECS service to 0 (stopped).

        Args:
            user_id: Clerk user ID.

        Raises:
            EcsManagerError: If the ECS update_service call fails.
        """
        service_name = self._service_name(user_id)
        put_metric("container.lifecycle.state_change", dimensions={"state": "stopping"})

        try:
            with timing("container.lifecycle.latency", {"op": "stop"}):
                self._ecs.update_service(
                    cluster=self._cluster,
                    service=service_name,
                    desiredCount=0,
                )
        except Exception as e:
            logger.error(
                "Failed to stop ECS service %s for user %s: %s",
                service_name,
                user_id,
                e,
            )
            raise EcsManagerError(f"Failed to stop ECS service: {e}", user_id)

        container = await container_repo.get_by_owner_id(user_id)
        if container:
            await container_repo.update_status(user_id, "stopped")

        logger.info("Stopped ECS service %s for user %s", service_name, user_id)

    async def kill_owner_bedrock(self, owner_id: str) -> None:
        """ADMIN BREAK-GLASS — NOT balance enforcement.

        Balance enforcement is revoke-only (the $50 subscription pays for the
        container; only Bedrock inference draws credits) — see
        `core/services/bedrock_enforcement.py`. This combined kill exists for
        genuine runaways (a container looping Bedrock calls hard enough that
        even the ~5-15s IAM-propagation window matters, or an incident where
        an operator wants the box DOWN), invoked by an operator, never by the
        low-balance webhook.

        Fires BOTH:
          1. IAM revoke (`revoke_owner_bedrock` from
             `core.billing.bedrock_aip`, Plan 2's primitive) — detaches/denies
             the per-owner Bedrock policy. Eventually-consistent (~5-15s), so it
             is fired first to start the propagation clock.
          2. ECS `desiredCount=0` (via `stop_user_service`) — the guaranteed
             instant stop; the running container is the only live spend source.

        The IAM revoke is best-effort: if it raises, the ECS stop MUST still
        run (the container keeps invoking Bedrock until desired=0 lands).
        """
        from core.billing.bedrock_aip import revoke_owner_bedrock
        from core.observability.metrics import put_metric

        put_metric("container.killswitch.invoke")
        try:
            await revoke_owner_bedrock(owner_id)
        except Exception:
            put_metric("container.killswitch.iam_revoke_error")
            logger.exception(
                "IAM revoke failed for owner %s during kill-switch — proceeding to ECS desired=0 (the guaranteed stop)",
                owner_id,
            )
        await self.stop_user_service(owner_id)
        logger.warning("Bedrock kill-switch completed for owner %s", owner_id)

    async def _assert_start_allowed(self, user_id: str) -> None:
        """The two guards every customer-driven container start must pass.

        This is the chokepoint. Both checks used to live ONLY on the
        ``GET /container/status`` auto-retry branch, so ``POST
        /container/provision`` — which restarts a stopped container directly —
        bypassed both. Duplicating them into a second router branch is how they
        drifted apart in the first place; they live here instead, where no
        caller can forget them.

        1. FREE-TIER DAILY CAP. A ``trial_local`` owner over the $3/day cap had
           their container scaled to zero *by* the cap. Waking it is exactly
           what the cap forbids, so the container stays down until the counter
           resets (UTC midnight) or the owner upgrades. Same predicate the
           status path uses (``trial_cap.trial_block_reason``), so the two can
           never disagree.

        2. TRIAL->PAID CONVERSION IN FLIGHT. ``reprovision_for_user`` scales the
           trial container to zero and then BLOCKS in a ~90s drain wait — the
           per-owner state is WAL-mode SQLite on EFS and a second live task
           holding it open is our documented corruption mode. A start inside
           that window IS that second task. ``conversion_guard_active`` is the
           same predicate the status path stands down on; it keys off the
           ``conversion_started_at`` TIMESTAMP rather than the ``converting``
           substatus, because ``stop_user_service`` writes
           ``update_status(owner, "stopped")`` — substatus defaults to None —
           and so clobbers the label the moment the drain begins.

        Raises:
            ContainerStartRefused: If either guard is up. Never EcsManagerError:
                nothing failed, so the row must not be marked "error".
        """
        # Lazy imports: both modules sit above core.containers in the import
        # graph (stripe_webhook_handlers reaches back into it via
        # ``from core.containers import get_ecs_manager``), so importing either
        # at module scope would close a cycle.
        from core.services import trial_cap
        from core.services.stripe_webhook_handlers import conversion_guard_active

        container = await container_repo.get_by_owner_id(user_id)
        if conversion_guard_active(container):
            put_metric("container.start.refused", dimensions={"reason": START_REFUSED_CONVERTING})
            raise ContainerStartRefused(
                START_REFUSED_CONVERTING,
                user_id,
                "A subscription upgrade is reprovisioning this container; it will come back on its own.",
            )

        # ONE read covering both ways a free-tier owner runs out of runway.
        # Checking only the cap here would let an EXPIRED trial wake its own
        # container through this path — the gate refuses it, this would not.
        reason = await trial_cap.trial_block_reason(user_id)
        if reason:
            put_metric("container.start.refused", dimensions={"reason": reason})
            raise ContainerStartRefused(reason, user_id, _TRIAL_REFUSAL_COPY[reason])

    async def start_user_service(self, user_id: str, *, force: bool = False) -> None:
        """Scale a user's ECS service to 1 (running).

        THE START CHOKEPOINT. Unless ``force``, every start runs
        ``_assert_start_allowed`` first — the free-tier daily cap and the
        trial->paid conversion guard.

        Fires _await_running_transition afterwards so the provisioning -> running
        transition happens eventually even if the user disconnects before the
        ECS task finishes warming up. Without this, cold-start restart rows
        get stuck at status=provisioning forever.

        Does NOT pass forceNewDeployment=True. The historical use case here was
        a stopped service (desiredCount=0) — `desiredCount=1` alone is enough
        to launch a new task. Forcing a new deployment was unintentionally
        destructive when called in racy contexts: if `stop_user_service` had
        just set `desiredCount=0` but ECS hadn't yet stopped the old task,
        a follow-up `start_user_service` with forceNewDeployment would
        terminate that still-running task and replace it with a fresh one,
        producing a ~30s outage right after a user logged in (the cascade
        traced in the post-incident review).

        Args:
            user_id: Clerk user ID.
            force: Skip the start guards. SYSTEM and OPERATOR callers only —
                never a customer-reachable path:
                  - ``provision_user_container``'s own start. It is the
                    conversion's reprovision (and the auto-retry's), dispatched
                    *after* the row is stamped — guarding it would deadlock the
                    conversion against its own marker. Its callers gate
                    themselves upstream (``evaluate_provision_gate``, which
                    already returns the same ``trial_daily_cap`` gate).
                  - the audited admin start (``POST /admin/users/{id}
                    /container/start``) — the operator break-glass, and the only
                    manual lever to un-wedge a container carrying a stranded
                    ``converting`` substatus.
                  - the dev-only ``/debug/provision`` routes (403 in prod).

        Raises:
            ContainerStartRefused: If a start guard is up (and not ``force``).
            EcsManagerError: If the ECS update_service call fails.
        """
        if not force:
            await self._assert_start_allowed(user_id)

        service_name = self._service_name(user_id)
        put_metric("container.lifecycle.state_change", dimensions={"state": "starting"})

        try:
            with timing("container.lifecycle.latency", {"op": "start"}):
                self._ecs.update_service(
                    cluster=self._cluster,
                    service=service_name,
                    desiredCount=1,
                    deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
                )
        except Exception as e:
            logger.error(
                "Failed to start ECS service %s for user %s: %s",
                service_name,
                user_id,
                e,
            )
            raise EcsManagerError(f"Failed to start ECS service: {e}", user_id)

        container = await container_repo.get_by_owner_id(user_id)
        if container:
            await container_repo.update_status(user_id, "provisioning")

        logger.info("Started ECS service %s for user %s", service_name, user_id)

        # Fire the durable poller. Any code path that sets status=provisioning
        # MUST ensure a transition poller is running, otherwise a slow ECS
        # cold-start leaves the row stuck.
        asyncio.create_task(self._await_running_transition(user_id))

    async def resize_user_container(
        self,
        user_id: str,
        new_cpu: str | None = None,
        new_memory: str | None = None,
        new_image: str | None = None,
    ) -> str:
        """Update a user's container with new CPU/memory/image and force redeploy.

        Registers a new task definition revision with the updated values,
        then updates the ECS service to use it with forceNewDeployment.

        Args:
            user_id: Owner ID (user or org).
            new_cpu: New CPU value (e.g. "1024"). None = keep current.
            new_memory: New memory value (e.g. "2048"). None = keep current.
            new_image: New container image. None = keep current.

        Returns:
            The ARN of the new task definition revision.
        """
        container = await container_repo.get_by_owner_id(user_id)
        if not container:
            raise EcsManagerError(f"No container found for user {user_id}", user_id)

        access_point_id = container.get("access_point_id")
        if not access_point_id:
            raise EcsManagerError(f"No EFS access point for user {user_id}", user_id)

        service_name = self._service_name(user_id)

        try:
            # Always read containerDefinitions/env/command/mounts from the
            # CDK-managed base, NEVER from the user's prior per-user revision.
            # Reading from a prior per-user revision propagates any drift it
            # had (e.g., missing CLAWHUB_WORKDIR). Per-user state (EFS access
            # point, image, cpu/memory) is layered here.
            # The hook_token is per-user state stored on the containers row;
            # we must thread it through so the new task def keeps OPENCLAW_HOOK_TOKEN
            # set (otherwise resize would silently strip the env var).
            #
            # Self-heal legacy rows that pre-date hook-token provisioning:
            # build_openclaw_config_dict renders the hooks block unconditionally
            # with the "${OPENCLAW_HOOK_TOKEN}" placeholder, but
            # _build_register_kwargs_from_base only injects the env var when
            # hook_token is truthy. Letting None pass through would leave the
            # new revision without OPENCLAW_HOOK_TOKEN, so the container would
            # boot with the unsubstituted placeholder string and 401 every
            # webhook. Mint+persist here so the row, the env var, and the
            # rendered config stay consistent.
            hook_token = container.get("hook_token") or secrets.token_urlsafe(32)
            if not container.get("hook_token"):
                await container_repo.update_fields(
                    user_id,
                    {
                        "hook_token": hook_token,
                        "hook_token_rotated_at": utc_now_iso(),
                        "hook_token_provisioned_at": utc_now_iso(),
                    },
                )
            # Per-owner Bedrock role: a resize that re-registers from the CDK
            # base would otherwise revert the container to the shared
            # all-models task role (same self-heal class as hook_token above).
            bedrock_task_role_arn = container.get("bedrock_role_arn")
            reg_kwargs = await asyncio.to_thread(
                self._build_register_kwargs_from_base,
                access_point_id,
                new_image=new_image,
                new_cpu=new_cpu,
                new_memory=new_memory,
                hook_token=hook_token,
                task_role_arn=bedrock_task_role_arn,
            )

            reg_resp = await asyncio.to_thread(self._ecs.register_task_definition, **reg_kwargs)
            new_task_def_arn = reg_resp["taskDefinition"]["taskDefinitionArn"]
            logger.info("Registered resized task definition %s for user %s", new_task_def_arn, user_id)

            # Update service to use new task def + force redeploy
            await asyncio.to_thread(
                self._ecs.update_service,
                cluster=self._cluster,
                service=service_name,
                taskDefinition=new_task_def_arn,
                forceNewDeployment=True,
                deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
            )

            # Decide whether to flip status=provisioning + fire the poller.
            # resize never touches desiredCount -- if the service is currently
            # scaled to zero (e.g. a free-tier container stopped by the reaper),
            # update_service just registers the new task definition for later
            # use. No tasks will start as a result of resize alone, so flipping
            # status=provisioning would leave an orphan poller spinning forever
            # (rolloutState won't go to FAILED; the deployment "completes" with
            # zero tasks). The new task def will take effect on the next
            # start_user_service call, which has its own poller firing.
            desc_resp = await asyncio.to_thread(
                self._ecs.describe_services,
                cluster=self._cluster,
                services=[service_name],
            )
            services = desc_resp.get("services", [])
            current_desired = services[0].get("desiredCount", 0) if services else 0

            if current_desired > 0:
                # Update DB record
                await self._update_container(
                    user_id,
                    task_definition_arn=new_task_def_arn,
                    status="provisioning",
                )
                logger.info(
                    "Resized running container for user %s: cpu=%s memory=%s image=%s",
                    user_id,
                    new_cpu,
                    new_memory,
                    new_image,
                )
                # Fire the durable transition poller. resize sets
                # status=provisioning via update_fields above and forces a new
                # ECS deployment; without the poller, the row stays stuck at
                # provisioning until the next backend restart's startup
                # reconciler catches it.
                asyncio.create_task(self._await_running_transition(user_id))
            else:
                # Scaled to zero: only update task_definition_arn so the new
                # def takes effect on next start. Do NOT flip status or fire
                # the poller -- there are no tasks to become healthy.
                await self._update_container(
                    user_id,
                    task_definition_arn=new_task_def_arn,
                )
                logger.info(
                    "Resized stopped container for user %s: cpu=%s memory=%s image=%s (new task def applies on next start)",
                    user_id,
                    new_cpu,
                    new_memory,
                    new_image,
                )

            return new_task_def_arn

        except EcsManagerError:
            raise
        except Exception as e:
            raise EcsManagerError(f"Failed to resize container: {e}", user_id)

    async def _wait_for_service_drained(self, user_id: str, timeout_s: float = 90.0) -> bool:
        """Poll until the user's ECS service has no running or pending tasks.

        Returns True once the service is drained, or False on timeout (the
        caller proceeds anyway — a stuck describe is not worth wedging an
        admin reprovision). Enforces the single-writer invariant on the
        reprovision path, which scales to 0 and then re-creates the task; the
        new task must not start until the old one is fully stopped, or both
        would hold the EFS WAL database open at once.
        """
        service_name = self._service_name(user_id)
        interval_s = 3.0
        attempts = max(1, int(timeout_s / interval_s))
        for _ in range(attempts):
            try:
                resp = await asyncio.to_thread(
                    self._ecs.describe_services,
                    cluster=self._cluster,
                    services=[service_name],
                )
                services = resp.get("services", [])
                svc = services[0] if services else {}
                if svc.get("runningCount", 0) == 0 and svc.get("pendingCount", 0) == 0:
                    return True
            except Exception:
                logger.warning("describe_services failed while draining %s; will retry", service_name)
            await asyncio.sleep(interval_s)
        logger.warning(
            "Service %s did not drain within %.0fs; proceeding with reprovision",
            service_name,
            timeout_s,
        )
        return False

    async def reprovision_for_user(self, user_id: str) -> dict:
        """Full reprovision: re-register the task def from the current CDK
        base (picks up the latest pinned image) AND rewrite the EFS
        openclaw.json from the owner's current provider settings, then roll
        the service onto the new revision. Used by the admin dashboard's
        container-reprovision action.

        This replaces the old stop+start, which only scaled the existing
        service — it left the container on its stale task-def AND its stale
        EFS config, so it could neither pick up a new image nor apply a
        backend config-builder change. Routing through
        ``provision_user_container`` (via ``background_provision``) is the
        only path that re-runs ``write_user_configs`` + the chatgpt OAuth
        pre-stage, so an admin reprovision now genuinely re-applies config.

        Dispatched as a background task: a full reprovision waits on the
        image pull + running transition (minutes), which would blow the
        admin request's HTTP timeout. The container row flips to
        provisioning -> running on the usual status-poll path; admins watch
        the Container card. Preserves EFS data, gateway token, and the EFS
        access point (provision_user_container reuses the existing row).

        Scale to 0 FIRST: provision_user_container only re-registers the task
        def + rewrites the EFS config on its scale-FROM-zero branch. Its
        already-running branch just force-redeploys the SAME (stale)
        task-def + config — so without dropping to 0 first, an admin
        reprovision on a live container would apply neither an image bump nor
        a config-builder change. The new task is only started once the old one
        has fully drained (single EFS SQLite writer — see the drain-wait
        below); EFS, gateway token, and access point are all preserved.
        """
        from core.services.container_provision import background_provision

        # Drop to desiredCount=0 so the dispatched provision takes the
        # scale-from-zero branch (re-register + write_user_configs), not the
        # running force-redeploy branch.
        await self.stop_user_service(user_id)

        # Block until the old task is fully stopped before provisioning the new
        # one. stop_user_service only issues desiredCount=0 and returns at once;
        # ECS then takes up to the task's stop-timeout to actually kill it. The
        # per-owner state lives in WAL-mode SQLite on EFS, which corrupts if the
        # draining old task and the new task hold it open together (the same
        # reason SINGLE_WRITER_DEPLOYMENT_CONFIG pins max=100). Waiting here
        # guarantees a single writer across the reprovision.
        await self._wait_for_service_drained(user_id)

        # clerk_user_id is unused by the reprovision path: provision reads
        # provider_choice off the billing row by owner_id, and chatgpt OAuth
        # resolves the connector via resolve_token_owner(owner_id). Pass
        # user_id for both to satisfy the signature.
        asyncio.create_task(background_provision(user_id, user_id))
        return {"status": "reprovisioning"}

    async def reapply_standard_sizing(self, user_id: str) -> dict:
        """Roll the user's task definition with the canonical flat-fee
        size. Admin recovery action for containers wedged on a stale
        task def. No tier arg — every user gets the same 0.5 vCPU /
        2 GB box post flat-fee cutover.
        """
        await self.resize_user_container(
            user_id,
            new_cpu=PER_USER_CPU,
            new_memory=PER_USER_MEMORY_MIB,
        )
        return {
            "status": "resized",
            "cpu": PER_USER_CPU,
            "memory": PER_USER_MEMORY_MIB,
        }

    async def delete_user_service(self, user_id: str) -> None:
        """Remove a user's ECS service and per-user resources entirely.

        1. Scale to 0 and delete ECS service
        2. Deregister per-user task definition (if present)
        3. Delete per-user EFS access point (if present)
        4. Delete Container DB record

        Args:
            user_id: Clerk user ID.

        Raises:
            EcsManagerError: If the ECS API calls fail.
        """
        service_name = self._service_name(user_id)

        try:
            # Scale to 0 before deleting
            self._ecs.update_service(
                cluster=self._cluster,
                service=service_name,
                desiredCount=0,
            )
            self._ecs.delete_service(
                cluster=self._cluster,
                service=service_name,
                force=True,
            )
        except ClientError as e:
            # Service-not-found is idempotent success — the e2e teardown can
            # legitimately retry against a state where the service was already
            # deleted (e.g. previous partial run). Treating "doesn't exist" as
            # an error breaks retry semantics. All other ClientErrors propagate.
            #
            # NB: we do NOT early-return here — the per-user resources below
            # (task-def revision, EFS access point, DDB container row) may
            # still exist even after the service is gone, and a partial
            # cleanup that left the access point orphaned can only be
            # recovered if subsequent retries fall through to this block.
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ServiceNotFoundException", "ServiceNotActiveException"):
                logger.info(
                    "delete_user_service: service %s for user %s already gone — continuing per-user cleanup",
                    service_name,
                    user_id,
                )
            else:
                logger.error(
                    "Failed to delete ECS service %s for user %s: %s",
                    service_name,
                    user_id,
                    e,
                )
                raise EcsManagerError(f"Failed to delete ECS service: {e}", user_id)
        except Exception as e:
            logger.error(
                "Failed to delete ECS service %s for user %s: %s",
                service_name,
                user_id,
                e,
            )
            raise EcsManagerError(f"Failed to delete ECS service: {e}", user_id)

        # Clean up per-user resources and delete container record from DB
        container = await container_repo.get_by_owner_id(user_id)
        if container:
            # Deregister per-user task definition
            if container.get("task_definition_arn"):
                self._deregister_task_definition(container["task_definition_arn"])

            # Delete per-user EFS access point
            if container.get("access_point_id"):
                self._delete_access_point(container["access_point_id"])

            await container_repo.delete(user_id)

        # Sweep channel_links rows for this owner
        try:
            from core.repositories import channel_link_repo

            link_count = await channel_link_repo.sweep_by_owner(user_id)
            if link_count:
                logger.info(
                    "Swept %d channel_link rows for deleted container (owner=%s)",
                    link_count,
                    user_id,
                )
        except Exception:
            # Non-fatal — the container is already gone, orphan rows are
            # cheap to keep. Log and continue.
            logger.exception(
                "Failed to sweep channel_link rows for owner %s after container delete",
                user_id,
            )

        logger.info("Deleted ECS service %s for user %s", service_name, user_id)

    # ------------------------------------------------------------------
    # Discovery and health
    # ------------------------------------------------------------------

    def discover_ip(self, service_name: str) -> str | None:
        """Discover a task's private IP via AWS Cloud Map.

        Every per-user OpenClaw ECS service registers its task into one shared
        Cloud Map service (see apps/infra/lib/stacks/container-stack.ts), each
        instance carrying an ``ECS_SERVICE_NAME`` attribute. We scope the
        lookup to a single tenant by filtering on that attribute, so the IP
        reflects the *current* task and can never be stale across task
        replacements — unlike a cached address.

        ``HealthStatus="HEALTHY_OR_ELSE_ALL"``: the shared Cloud Map service has
        no HealthCheckCustomConfig, so ECS-registered instances report health
        ``UNKNOWN``. This mode returns the current task regardless of that, and
        automatically prefers healthy tasks if health-checking is ever enabled.

        Args:
            service_name: The per-user ECS service name (the ``ECS_SERVICE_NAME``
                attribute value to match).

        Returns:
            The current task's private IPv4 address, or None.
        """
        if not self._cloud_map_namespace_name or not self._cloud_map_service_name:
            logger.error(
                "Cloud Map names not configured (namespace=%r service=%r); cannot discover %s",
                self._cloud_map_namespace_name,
                self._cloud_map_service_name,
                service_name,
            )
            return None
        try:
            resp = self._servicediscovery.discover_instances(
                NamespaceName=self._cloud_map_namespace_name,
                ServiceName=self._cloud_map_service_name,
                QueryParameters={"ECS_SERVICE_NAME": service_name},
                HealthStatus="HEALTHY_OR_ELSE_ALL",
            )
        except Exception as e:
            logger.error("Cloud Map discovery failed for %s: %s", service_name, e)
            return None
        for inst in resp.get("Instances", []):
            attrs = inst.get("Attributes", {})
            if attrs.get("ECS_SERVICE_NAME") == service_name and attrs.get("AWS_INSTANCE_IPV4"):
                return attrs["AWS_INSTANCE_IPV4"]
        return None

    def is_healthy(self, ip: str) -> bool:
        """Check if a gateway at the given IP is responding.

        Opens a TCP connection to the gateway port. If the connection
        succeeds, the gateway is listening and healthy.

        Args:
            ip: The task's private IPv4 address.

        Returns:
            True if healthy, False otherwise.
        """
        try:
            with socket.create_connection((ip, GATEWAY_PORT), timeout=5):
                return True
        except Exception:
            return False

    async def resolve_running_container(self, user_id: str) -> tuple[dict | None, str | None]:
        """Look up a user's container and discover its task IP.

        Accepts containers in "provisioning" or "running" status.
        If a provisioning container's task is healthy, automatically
        transitions the status to "running".

        Args:
            user_id: Clerk user ID.

        Returns:
            Tuple of (container_dict, task_ip) or (None, None) if no active container.
        """
        container = await container_repo.get_by_owner_id(user_id)
        if not container or container.get("status") not in ("provisioning", "running"):
            return None, None

        ip = self.discover_ip(container["service_name"])
        if not ip:
            # DDB row says provisioning/running but ECS has no running task
            # for the service. Either the task is still pulling the image
            # (provisioning) or it died and is being replaced (running →
            # zombie row). Logged so we can correlate against the
            # /isol8/<env>/ecs-task-events log group to see whether the
            # task was stopped, by whom, and why.
            logger.warning(
                "resolve_running_container ip=None user=%s service=%s ddb_status=%s ddb_substatus=%s last_handshake=%s",
                user_id,
                container.get("service_name"),
                container.get("status"),
                container.get("substatus"),
                container.get("last_gateway_handshake_at") or "never",
            )
            return container, None
        # Stale-row detection: DDB says running but TCP probe fails. This
        # is the wedge-class signature — kernel listener bound but gateway
        # process is in NFS deadlock. Log loudly so the next iteration of
        # this bug shows up immediately in CloudWatch Insights:
        #   filter ip_unhealthy=true and ddb_status="running"
        if container.get("status") == "running" and not self.is_healthy(ip):
            logger.warning(
                "resolve_running_container ip_unhealthy=true user=%s service=%s ip=%s last_handshake=%s",
                user_id,
                container.get("service_name"),
                ip,
                container.get("last_gateway_handshake_at") or "never",
            )

        # Auto-transition provisioning -> running once the task is reachable.
        # Also opportunistically capture the task ARN if we don't already have
        # one on the row — previously this field was silently missing on every
        # container row because no code path wrote it.
        if container.get("status") == "provisioning" and self.is_healthy(ip):
            try:
                fields = {
                    "substatus": "gateway_healthy",
                    "status": "running",
                }
                if not container.get("task_arn"):
                    try:
                        list_resp = self._ecs.list_tasks(
                            cluster=self._cluster,
                            serviceName=container["service_name"],
                            desiredStatus="RUNNING",
                        )
                        task_arns = list_resp.get("taskArns", [])
                        if task_arns:
                            fields["task_arn"] = task_arns[0]
                    except Exception:
                        # Task ARN is a nice-to-have for observability,
                        # not load-bearing for the transition itself.
                        pass
                await container_repo.update_fields(user_id, fields)
                logger.info(
                    "Container %s for user %s transitioned to running",
                    container["service_name"],
                    user_id,
                )
                # Update local dict to reflect changes
                container["status"] = "running"
                container["substatus"] = "gateway_healthy"
                if "task_arn" in fields:
                    container["task_arn"] = fields["task_arn"]
            except Exception:
                logger.warning(
                    "Failed to persist running status for user %s, will retry on next poll",
                    user_id,
                )

        return container, ip

    async def get_service_status(self, user_id: str) -> dict | None:
        """Get the Container record for a user.

        Args:
            user_id: Clerk user ID.

        Returns:
            The container dict, or None if not found.
        """
        return await container_repo.get_by_owner_id(user_id)

    # ------------------------------------------------------------------
    # Full provisioning flow
    # ------------------------------------------------------------------

    def _service_exists(self, service_name: str) -> dict | None:
        """Check if an ECS service exists and return its description, or None."""
        try:
            resp = self._ecs.describe_services(
                cluster=self._cluster,
                services=[service_name],
            )
            services = resp.get("services", [])
            if services and services[0].get("status") != "INACTIVE":
                return services[0]
        except Exception:
            pass
        return None

    async def _build_byo_secrets_for_user(
        self,
        user_id: str,
        byo_provider: str,
    ) -> list[dict]:
        """Build the ECS task ``secrets:`` block for a BYOK provisioning.

        Resolves the per-user LLM-key secret ARN from Secrets Manager (via
        ``key_service.get_secret_arn``, keyed deterministically on
        ``{user_id, byo_provider}``) and turns it into a single ``secrets:``
        entry that the per-user task definition references.
        ``service-stack.ts`` already grants the backend
        ``secretsmanager:GetSecretValue`` for ``isol8/{env}/user-keys/*``, so
        the per-user ARN is reachable from the task IAM role.
        """
        if byo_provider not in {"openai", "anthropic"}:
            raise EcsManagerError(
                f"byo_provider must be 'openai' or 'anthropic' for byo_key, got {byo_provider!r}",
                user_id,
            )
        from core.services.key_service import get_secret_arn

        secret_arn = await get_secret_arn(user_id, byo_provider)
        if not secret_arn:
            raise EcsManagerError(
                f"No saved {byo_provider} key for user {user_id} - caller should "
                "block provisioning until the user adds their key",
                user_id,
            )
        env_var_name = "OPENAI_API_KEY" if byo_provider == "openai" else "ANTHROPIC_API_KEY"
        return [{"name": env_var_name, "valueFrom": secret_arn}]

    async def _pre_stage_oauth_for_user(
        self,
        user_id: str,
        *,
        token_owner_id: str | None = None,
    ) -> None:
        """Pre-stage the container-global ``credentials/oauth.json`` for a
        chatgpt_oauth owner.

        Fetches the decrypted ChatGPT tokens persisted by ``oauth_service``
        and writes ``<EFS>/users/{user_id}/credentials/oauth.json`` (a single
        container-global file, NOT a per-agent fan-out) BEFORE the ECS service
        is created, so OpenClaw imports it into every agent's auth store on
        first boot. See workspace.pre_stage_auth_profile_store.

        ``user_id`` is the CONTAINER OWNER (org_id for orgs) — it keys the
        EFS path the container mounts. ``token_owner_id`` is the Clerk user
        whose OAuth row holds the tokens (the org's workspace owner);
        defaults to ``user_id``, which is correct for personal owners.
        Raises if no active OAuth row exists — caller should gate
        provisioning on a completed device-code flow.
        """
        from core.services.oauth_service import get_decrypted_tokens

        token_owner = token_owner_id or user_id
        # ``user_id`` is the CONTAINER OWNER — pass it as ``bound_to`` so we only
        # stage a row the connector claimed for THIS workspace. A row bound to a
        # different owner reads as missing and raises below, rather than staging
        # one ChatGPT account into a second container.
        tokens = await get_decrypted_tokens(user_id=token_owner, bound_to=user_id)
        if not tokens:
            raise EcsManagerError(
                f"No ChatGPT OAuth tokens for connector {token_owner} (owner {user_id}) - "
                "caller should complete OAuth before provisioning",
                user_id,
            )
        from core.containers.workspace import pre_stage_auth_profile_store

        # Stage the container-global oauth.json seed. We do NOT clear the
        # default agent's auth row here: the container's per-agent SQLite auth
        # store is the source of truth for the LIVE (rotating) token, and this
        # DDB-sourced seed is a stale snapshot the instant the container's first
        # refresh rotates it. The container's store-row-absent import gate
        # already consumes this seed ONLY for a container with no credential yet
        # (fresh provision) and ignores it on a populated store — so re-staging
        # on a reprovision/recover/config-rewrite never clobbers a live token.
        # Force-reimport (clear_default_agent_auth_profile_row) happens ONLY on a
        # genuine fresh-token event: reconnect (routers/oauth.py /poll) and
        # provider switch (_hot_swap_provider). See
        # docs/superpowers/specs/2026-06-24-oauth-reprovision-stale-token-design.md.
        await pre_stage_auth_profile_store(user_id=user_id, oauth_tokens=tokens)

    async def provision_user_container(
        self,
        user_id: str,
        *,
        provider_choice: str,
        byo_provider: str | None = None,
        owner_type: str = "personal",
        claim_id: str | None = None,
    ) -> str:
        """Fenced entry point for every container provision.

        ``claim_id`` is the provision lease's fencing token
        (container_repo.try_claim_provision). The poll-driven path
        (background_provision) claims before calling and passes its token here.
        A DIRECT call with no token is, by definition, a DELIBERATE reprovision
        (trial->paid conversion, /container/recover, admin force-apply, provider
        change, debug) — it takes the lease over unconditionally, which fences
        any in-flight poll provision out at that run's next checkpoint. So
        deliberate doors are never blocked, and two provisions can never
        interleave their writes: at most one holds the lease, and the loser
        stops before its next side effect (ProvisionSuperseded).

        On any failure the lease is released to ``error`` (token- and
        status-conditioned, see release_provision_to_error), which the status
        poll's existing auto-retry re-claims on the next ~3s tick — a failed
        provision self-heals with no operator.
        """
        # Lazy module lookup for the LEASE primitives (only): several tests
        # wholesale-replace this module's `container_repo` binding with a
        # MagicMock to stub row I/O; the lease has its own moto-backed suite
        # and is stubbed at the source module instead.
        from core.repositories import container_repo as _lease_repo

        if claim_id is None:
            claim_id = await _lease_repo.takeover_provision(user_id)
            logger.info("Provision lease takeover for owner %s (deliberate reprovision)", user_id)
        try:
            return await self._provision_user_container_inner(
                user_id,
                provider_choice=provider_choice,
                byo_provider=byo_provider,
                owner_type=owner_type,
                claim_id=claim_id,
            )
        except ProvisionSuperseded:
            # A newer run owns the row now; write nothing over its state.
            put_metric("container.provision", dimensions={"status": "superseded"})
            raise
        except Exception:
            try:
                await _lease_repo.release_provision_to_error(user_id, claim_id)
            except Exception:  # noqa: BLE001 — release is best-effort
                logger.exception("Failed to release provision lease for owner %s", user_id)
            raise

    async def has_live_service(self, user_id: str) -> bool:
        """True when the owner has an ACTIVE ECS service with desiredCount > 0.

        The guard for the status poll's stale-lease recovery: a provision that
        already reached start_user_service stops refreshing its lease when it
        RETURNS, while the container boots for up to ~12 minutes — so a stale
        lease alone does not mean the provision is dead. If ECS shows a live
        service, the right move is to keep waiting for the running transition,
        not to re-provision (which would force-restart a healthy boot)."""
        container = await container_repo.get_by_owner_id(user_id)
        service_name = (container or {}).get("service_name")
        if not service_name:
            return False
        try:
            resp = await asyncio.to_thread(
                self._ecs.describe_services,
                cluster=self._cluster,
                services=[service_name],
            )
        except Exception:  # noqa: BLE001 — liveness probe must not 500 the poll
            logger.warning("has_live_service: describe_services failed for %s", user_id, exc_info=True)
            return True  # fail SAFE: assume live, do not re-provision on a blind read
        services = [svc for svc in resp.get("services", []) if svc.get("status") == "ACTIVE"]
        if not services or services[0].get("desiredCount", 0) == 0:
            return False
        svc = services[0]
        # desiredCount alone is NOT liveness: a crash-looping deployment sits at
        # ACTIVE + desired=1 + running=0 + rolloutState=FAILED forever, and
        # treating that as live would suppress stale-lease recovery permanently
        # (the in-process transition poller that would mark it failed is exactly
        # what died). Live = a task actually running, or a rollout still making
        # progress.
        if svc.get("runningCount", 0) > 0:
            return True
        primary = next((d for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"), None)
        return bool(primary and primary.get("rolloutState") == "IN_PROGRESS")

    async def _provision_checkpoint(self, user_id: str, claim_id: str) -> None:
        """Verify this run still holds the provision lease and refresh its
        stamp. Placed immediately before non-idempotent writes (service create,
        config write) so a superseded run stops BEFORE poisoning the winner's
        artifacts — this is what makes the fencing token real."""
        from core.repositories import container_repo as _lease_repo

        await _lease_repo.update_container_leased(user_id, claim_id)

    async def _provision_user_container_inner(
        self,
        user_id: str,
        *,
        provider_choice: str,
        byo_provider: str | None = None,
        owner_type: str = "personal",
        claim_id: str,
    ) -> str:
        """Provision or recover a user's container.

        Handles all scenarios:
        1. No service exists -> full provisioning (create service, write configs, start)
        2. Service exists with 0 desired -> write configs, scale up to 1
        3. Service exists and running -> force new deployment (restart)
        4. Container in error state -> clean up and re-provision

        Preserves EFS data (agent files) across all scenarios.

        Args:
            user_id: Clerk user ID.
            provider_choice: One of ``"chatgpt_oauth"``, ``"byo_key"``,
                ``"bedrock_claude"``, ``"bedrock_trial"``. Selects the LLM
                provider block in openclaw.json and drives any pre-task setup
                (codex auth pre-stage for chatgpt_oauth, per-user Secrets
                Manager wiring for byo_key, per-owner AIP + IAM mint for both
                Bedrock modes). ``bedrock_trial`` is a config-path MODE
                resolved from the owner's ``trial_local`` subscription_status
                (container_provision.resolve_provider_choice) — it is never a
                stored provider_choice.
            byo_provider: Required when ``provider_choice == "byo_key"``;
                one of ``"openai"`` or ``"anthropic"``.
            owner_type: "personal" or "org" (for EFS access-point tagging).

        Returns:
            The ECS service name.

        Raises:
            EcsManagerError: If any provisioning step fails.
        """
        # --- Card-specific pre-task setup (must run BEFORE ECS service create
        # so the first task starts with auth/keys already in place). ---
        secrets_for_task: list[dict] = []
        environment_for_task: list[dict] = []
        # Give the container a base URL to curl the public platform-info catalog
        # ($ISOL8_API_URL/api/v1/platform-info). The isol8 skill reads the live
        # connectable integrations/channels from there instead of a stale baked
        # list. Reuse atlas_seed._api_base_url so the per-env host convention has
        # exactly one definition. Public route -> no auth, no owner_id.
        from core.services import atlas_seed

        environment_for_task.append({"name": "ISOL8_API_URL", "value": atlas_seed._api_base_url(settings.ENVIRONMENT)})
        if provider_choice == "chatgpt_oauth":
            from core.services.oauth_service import resolve_token_owner

            await self._pre_stage_oauth_for_user(user_id, token_owner_id=await resolve_token_owner(owner_id=user_id))
            # OpenClaw 6.6 imports the container-global credentials/oauth.json
            # into each agent's auth store at boot. _pre_stage_oauth_for_user
            # above writes that single file to EFS. No env var override needed.
        elif provider_choice == "byo_key":
            if byo_provider is None:
                raise EcsManagerError(
                    "byo_provider is required when provider_choice == 'byo_key'",
                    user_id,
                )
            secrets_for_task = await self._build_byo_secrets_for_user(user_id, byo_provider)

        if provider_choice not in ("chatgpt_oauth", "byo_key", *BEDROCK_PROVIDER_CHOICES):
            raise EcsManagerError(
                f"Unknown provider_choice: {provider_choice!r}",
                user_id,
            )

        # Read the row ONCE up front. Reused for the bedrock kill-switch check
        # AND the state-machine routing below — no second round-trip. The mint
        # block's update_fields calls do not change bedrock_revoked (only
        # revoke_owner_bedrock does), so this pre-mint read is authoritative for
        # the revoke guard; the freshly-minted bedrock_role_arn is carried in
        # the in-memory `bedrock_role_arn` local (not re-read off the row), so a
        # stale `existing` here cannot drop it.
        existing = await container_repo.get_by_owner_id(user_id)

        # Every owner — regardless of provider_choice — runs under a UNIQUE
        # per-owner ECS task role (isol8-<env>-owner-bedrock-<owner_id>), minted
        # SYNCHRONOUSLY here before the service starts. The role NAME keeps the
        # historical "owner-bedrock" prefix because the CDK
        # PassRole/PutRolePolicy/permissions-boundary grants are scoped to it
        # (renaming silently breaks them). A unique per-owner IAM principal is
        # what the AWS read-only integration pins each owner's cross-account
        # trust to — the shared base task role is a cross-tenant hole. Lazy
        # import to avoid pulling bedrock_aip's boto3 clients into module load.
        from core.billing.bedrock_aip import (
            ensure_owner_aips,
            ensure_owner_bedrock_role,
        )

        bedrock_aip_arns: dict[str, str] | None = None
        bedrock_role_arn: str | None = None
        bedrock_gate_token_hash: str | None = None
        if provider_choice in BEDROCK_PROVIDER_CHOICES:
            # Bedrock owner: mint the per-owner AIPs + scoped IAM role. The
            # container's openclaw.json model ids become the AIP ARNs; if the
            # AIPs were absent at first boot the container would AccessDenied on
            # the AIP-scoped role.
            #
            # A `bedrock_trial` (no-card free tier) owner is a REAL Bedrock owner
            # — the grant is NOT skipped. What bounds them is the SCOPE of the
            # mint: one tier (sonnet), which pins their task role to one model
            # (see BEDROCK_TRIAL_TIERS). The paid call shape is left exactly as
            # it was — no `tiers` kwarg — so ensure_owner_aips keeps stamping
            # plan="paid" on the reverse rows; the trial mint stamps
            # plan="trial", which is what arms the meter Lambda's daily-cap kill
            # switch (a trial owner has no Stripe customer to meter).
            if provider_choice == BEDROCK_TRIAL_CHOICE:
                bedrock_aip_arns = await ensure_owner_aips(user_id, tiers=BEDROCK_TRIAL_TIERS)
            else:
                bedrock_aip_arns = await ensure_owner_aips(user_id)

            # KILL-SWITCH GUARD: ensure_owner_bedrock_role always re-puts the
            # scoped inline policy, so calling it for a revoked owner would
            # silently RESTORE Bedrock access — defeating the kill-switch on
            # any re-provision/restart. Mint the AIPs + role SHELL (so the task
            # role exists + carries EFS/logs/ssm), but do NOT re-grant the
            # Bedrock inline policy if the owner is revoked. Plan 4 owns
            # un-revoking (restore_owner_bedrock), not provision. This matters
            # doubly on the free tier: the revoke IS the daily-cap enforcement,
            # so a re-provision that re-granted it would hand a capped trial
            # owner an unlimited reset.
            is_revoked = bool(existing and existing.get("bedrock_revoked"))
            bedrock_role_arn = await ensure_owner_bedrock_role(user_id, bedrock_aip_arns, skip_bedrock_grant=is_revoked)

            # Mint (rotate) the per-owner bedrock-gate bearer token for EVERY
            # Bedrock owner — trial AND paid. The gate is the standard Bedrock
            # path, and the token row's deny flag is the kill switch
            # (revoke_owner_bedrock). mint_gate_token rotates: it grace-expires
            # any prior token row this owner pointed at. Lazy import keeps
            # boto3 out of module load.
            from core.billing.bedrock_gate_tokens import (
                deny_gate_token,
                hash_token,
                mint_gate_token,
            )

            gate_token = await mint_gate_token(user_id, bedrock_aip_arns)
            # First-provision persistence: mint_gate_token's own update_fields
            # no-ops against a not-yet-created row, so carry the hash to
            # create_user_service's row-creating upsert (same C-1 fold-in as
            # bedrock_role_arn / bedrock_aip_arns).
            bedrock_gate_token_hash = hash_token(gate_token)
            # Keep a revoked owner DENIED across the reprovision: mint wrote a
            # fresh row with denied=False, so re-assert the kill switch (mirrors
            # skip_bedrock_grant on the IAM half). Best-effort — a deny hiccup
            # must not break provisioning (IAM revoke is the primary fence).
            if is_revoked:
                try:
                    await deny_gate_token(user_id, True)
                except Exception:  # noqa: BLE001 — deny is a backstop, not the fence
                    logger.warning("deny_gate_token(True) failed for revoked owner %s", user_id)

            # Bearer env — the SAME resolver predicate build_provider_block
            # uses for the baseUrl, so token and URL always travel together:
            # the bearer token forces httpBearerAuth in the amazon-bedrock
            # plugin regardless of baseUrl (stream.runtime.ts), so injecting it
            # while baseUrl targets real Bedrock (break-glass/bootstrap direct
            # mode) would 403 direct invocation.
            from core.billing.bedrock_gate_url import gate_url as _resolve_gate_url

            resolved_gate_url = _resolve_gate_url()
            if resolved_gate_url:
                environment_for_task.append({"name": "AWS_BEARER_TOKEN_BEDROCK", "value": gate_token})
                environment_for_task.append({"name": "AWS_BEDROCK_FORCE_HTTP1", "value": "1"})

            # ACP coding-harness (Claude Code) Bedrock wiring. Gated by the
            # ACP_ENABLED feature flag (dark by default, same gate as the
            # openclaw.json acp block) and skipped for revoked owners — their
            # Bedrock grant is withheld above, so pointing the harness at it
            # would only AccessDeny. Points Claude Code at the owner's AIP ARNs
            # so its inference is billed to the owner (see helper docstring).
            #
            # PAID ONLY. derive_enabled_harnesses returns [] for bedrock_trial
            # (no ACP on the free tier), so a trial container gets no acp block
            # to drive — and the harness env could not be built for one anyway:
            # it needs a haiku AIP the trial owner deliberately does not have.
            if provider_choice == "bedrock_claude" and settings.ACP_ENABLED and not is_revoked and bedrock_aip_arns:
                environment_for_task.extend(
                    _build_bedrock_acp_harness_env(
                        bedrock_aip_arns,
                        gate_token=gate_token,
                        gate_url=resolved_gate_url,
                    )
                )
        else:
            # Non-Bedrock owner (chatgpt_oauth / byo_key): mint/keep the
            # per-owner role SHELL — base EFS/logs/ssm policy + permissions
            # boundary — but pass skip_bedrock_grant=True so it carries NO
            # Bedrock inline policy, and DO NOT mint Bedrock AIPs (the owner
            # never uses Bedrock inference). The empty aip_arns map is unread
            # under skip_bedrock_grant=True. This gives every owner a unique
            # task-role principal without granting Bedrock to non-Bedrock owners.
            bedrock_role_arn = await ensure_owner_bedrock_role(user_id, {}, skip_bedrock_grant=True)

        # Paperclip auth is no longer a container-wide task env. Each agent
        # authenticates as ITSELF via its own per-agent identity file
        # (paperclip-claimed-api-key.json staged by
        # friday_identity.pre_stage_paperclip_identities), so there is nothing
        # to inject here.

        service_name = self._service_name(user_id)

        # Check current state (`existing` was read above, before the mint).
        svc = self._service_exists(service_name)

        # CHECKPOINT: the AIP/role mint above is done. Verify the lease is
        # still ours (and refresh it) before any ECS/EFS side effect — a run
        # superseded during the mint stops here.
        await self._provision_checkpoint(user_id, claim_id)

        # --- Scenario: Service exists (ACTIVE or DRAINING) ---
        if svc and svc.get("status") == "ACTIVE":
            desired = svc.get("desiredCount", 0)
            running = svc.get("runningCount", 0)

            if desired == 0:
                # Service exists but stopped -- write fresh configs and scale up
                logger.info("Service %s exists with 0 desired, scaling up", service_name)
                # Legacy rows pre-dating gateway_token would yield None here;
                # fall through to a fresh token so write_openclaw_config doesn't
                # silently emit a null auth.token (see config.py validation).
                gateway_token = (existing.get("gateway_token") if existing else None) or secrets.token_urlsafe(32)

                # Update gateway token and write configs.
                if existing:
                    await container_repo.update_fields(
                        user_id,
                        {
                            "status": "provisioning",
                            "substatus": "restarting",
                        },
                    )

                # Self-heal hook_token for legacy rows that pre-date hook-token
                # provisioning. Without this, a legacy user hitting the restart
                # path passes None for hook_token through write_user_configs and
                # _register_task_definition. Because build_openclaw_config_dict
                # renders the hooks block UNCONDITIONALLY with the literal
                # "${OPENCLAW_HOOK_TOKEN}" placeholder while
                # _build_register_kwargs_from_base gates env injection on
                # `if hook_token:`, the new task-def would lack
                # OPENCLAW_HOOK_TOKEN and the container would boot with the
                # unsubstituted placeholder string as its hooks.token — every
                # webhook 401s indefinitely. Mint+persist here so the row, the
                # env var, and the rendered config stay consistent.
                existing_hook_token = (existing.get("hook_token") if existing else None) or secrets.token_urlsafe(32)
                if existing and not existing.get("hook_token"):
                    await container_repo.update_fields(
                        user_id,
                        {
                            "hook_token": existing_hook_token,
                            "hook_token_rotated_at": utc_now_iso(),
                            "hook_token_provisioned_at": utc_now_iso(),
                        },
                    )

                try:
                    # CHECKPOINT before the config write — the poison write in
                    # the double-provision incident. A superseded run must not
                    # overwrite the winner's openclaw.json.
                    await self._provision_checkpoint(user_id, claim_id)
                    # Reuse the existing (or freshly-minted) hook_token so the
                    # rendered openclaw.json placeholder resolves at boot
                    # against the env var we layer back onto the new task-def
                    # below.
                    await self.write_user_configs(
                        user_id,
                        gateway_token,
                        provider_choice=provider_choice,
                        byo_provider=byo_provider,
                        hook_token=existing_hook_token,
                        owner_type=owner_type,
                        aip_arns=bedrock_aip_arns,
                    )
                except ProvisionSuperseded:
                    raise
                except Exception as e:
                    logger.warning("Config write failed during restart: %s (continuing anyway)", e)

                # Re-register the task definition with the freshly-computed
                # secrets_for_task. Without this, a BYOK user whose container
                # scaled to zero would restart on the prior revision and skip
                # the per-user OPENAI/ANTHROPIC secrets injection (the path
                # that flows in via _build_byo_secrets_for_user above).
                # We always re-register so the path is uniform across
                # providers; oauth/bedrock_claude have empty secrets_for_task.
                # Use the same existing/self-healed hook_token computed above
                # so the env var on the new revision matches what
                # write_user_configs just rendered.
                access_point_id = existing.get("access_point_id") if existing else None
                if access_point_id:
                    try:
                        # Prefer the role just minted in THIS call (in-memory)
                        # over the row's value: `existing` was read before the
                        # mint, so for a re-provision its bedrock_role_arn may be
                        # stale/absent. The mint above runs for ALL providers now,
                        # so bedrock_role_arn is normally set here; the row
                        # fallback only covers a degenerate path where the mint
                        # returned nothing — it must still resolve to a per-owner
                        # role, never the shared base.
                        restart_task_role_arn = bedrock_role_arn or (
                            existing.get("bedrock_role_arn") if existing else None
                        )
                        new_task_def_arn = await asyncio.to_thread(
                            self._register_task_definition,
                            access_point_id,
                            secrets_for_task=secrets_for_task,
                            environment_for_task=environment_for_task,
                            hook_token=existing_hook_token,
                            task_role_arn=restart_task_role_arn,
                        )
                        await asyncio.to_thread(
                            self._ecs.update_service,
                            cluster=self._cluster,
                            service=service_name,
                            desiredCount=1,
                            taskDefinition=new_task_def_arn,
                            deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
                        )
                        old_task_def_arn = existing.get("task_definition_arn") if existing else None
                        await container_repo.update_fields(
                            user_id,
                            {"task_definition_arn": new_task_def_arn},
                        )
                        if old_task_def_arn and old_task_def_arn != new_task_def_arn:
                            try:
                                await asyncio.to_thread(
                                    self._deregister_task_definition,
                                    old_task_def_arn,
                                )
                            except Exception as e:
                                logger.warning(
                                    "Failed to deregister stale task def %s: %s",
                                    old_task_def_arn,
                                    e,
                                )
                        # start_user_service normally fires the running-transition
                        # poller; we bypassed it, so do it here.
                        asyncio.create_task(self._await_running_transition(user_id))
                    except EcsManagerError:
                        put_metric("container.provision", dimensions={"status": "error"})
                        put_metric("container.error_state")
                        await self._update_container(user_id, status="error", substatus=None)
                        raise
                    except Exception as e:
                        put_metric("container.provision", dimensions={"status": "error"})
                        put_metric("container.error_state")
                        await self._update_container(user_id, status="error", substatus=None)
                        raise EcsManagerError(
                            f"Failed to restart service with refreshed task def: {e}",
                            user_id,
                        )
                else:
                    # No access point on record (shouldn't happen for an
                    # ACTIVE service but fall back to the simple start path).
                    # force: this IS a provision — see the Step 4 comment below.
                    try:
                        await self.start_user_service(user_id, force=True)
                    except EcsManagerError:
                        put_metric("container.provision", dimensions={"status": "error"})
                        put_metric("container.error_state")
                        await self._update_container(user_id, status="error", substatus=None)
                        raise

                put_metric("container.provision", dimensions={"status": "ok"})
                return service_name

            elif running > 0:
                # Service exists and running -- force new deployment
                logger.info(
                    "Service %s running (%d tasks), forcing new deployment",
                    service_name,
                    running,
                )
                if existing:
                    await container_repo.update_fields(
                        user_id,
                        {
                            "status": "provisioning",
                            "substatus": "redeploying",
                        },
                    )

                try:
                    self._ecs.update_service(
                        cluster=self._cluster,
                        service=service_name,
                        forceNewDeployment=True,
                        deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
                    )
                except Exception as e:
                    put_metric("container.provision", dimensions={"status": "error"})
                    put_metric("container.error_state")
                    await self._update_container(user_id, status="error", substatus=None)
                    raise EcsManagerError(f"Failed to force redeploy: {e}", user_id)

                put_metric("container.provision", dimensions={"status": "ok"})
                # New deployment forces a fresh task; fire the poller so the
                # row transitions back to status=running once the new task is
                # healthy.
                asyncio.create_task(self._await_running_transition(user_id))
                return service_name

            else:
                # Desired > 0 but not running yet -- ECS is working on it, just wait
                logger.info(
                    "Service %s has %d desired, %d running -- ECS is starting",
                    service_name,
                    desired,
                    running,
                )
                if existing and existing.get("status") != "provisioning":
                    await container_repo.update_fields(
                        user_id,
                        {
                            "status": "provisioning",
                            "substatus": "starting",
                        },
                    )
                    # We just flipped status to provisioning -- fire the poller.
                    # If status was already provisioning, an earlier poller is
                    # already running (or the startup reconciler will pick it up
                    # on next deploy), so firing another would just double the
                    # ECS API traffic for no gain.
                    asyncio.create_task(self._await_running_transition(user_id))
                return service_name

        # --- Scenario: No service exists -- full provisioning ---
        logger.info("No active service for user %s, full provisioning", user_id)

        # Clean up stale DB record if in error state
        if existing and existing.get("status") == "error":
            if existing.get("task_definition_arn"):
                try:
                    self._deregister_task_definition(existing["task_definition_arn"])
                except Exception:
                    pass
            # Don't delete access point -- preserves user's EFS data
            await container_repo.update_fields(
                user_id,
                {
                    "task_definition_arn": None,
                    "status": "provisioning",
                    "substatus": None,
                },
            )

        # Step 1: Generate gateway token + hook token. The gateway token
        # authorizes our backend's RPC calls into the OpenClaw HTTP API; the
        # hook token authorizes inbound webhooks the container needs to
        # verify (channel ingress, etc.). They are independent secrets.
        # hook_token is persisted atomically with the rest of the row by
        # create_user_service's upsert below — passing it as a separate
        # set_hook_token call before the upsert would no-op for first-time
        # provisioning since the row doesn't exist yet.
        gateway_token = secrets.token_urlsafe(32)
        hook_token = secrets.token_urlsafe(32)

        # Step 2: Create ECS service (desiredCount=0). Per-user secrets (only
        # set for byo_key) and per-user env (only set for chatgpt_oauth, to
        # point CODEX_HOME at the EFS-staged auth.json dir) are layered onto
        # the per-user task def so the first task ECS launches has them.
        # The hook_token is injected as OPENCLAW_HOOK_TOKEN on the primary
        # container by create_user_service -> _register_task_definition.
        service_name = await self.create_user_service(
            user_id,
            gateway_token,
            owner_type=owner_type,
            secrets_for_task=secrets_for_task,
            environment_for_task=environment_for_task,
            hook_token=hook_token,
            task_role_arn=bedrock_role_arn,
            bedrock_aip_arns=bedrock_aip_arns,
            bedrock_gate_token_hash=bedrock_gate_token_hash,
            claim_id=claim_id,
        )

        # Step 3: Write configs to EFS
        try:
            # CHECKPOINT before the config write — the poison write in the
            # double-provision incident: this is exactly where the losing run
            # historically overwrote the winner's openclaw.json with an AIP ARN
            # the IAM policy did not allow.
            await self._provision_checkpoint(user_id, claim_id)
            await self.write_user_configs(
                user_id,
                gateway_token,
                provider_choice=provider_choice,
                byo_provider=byo_provider,
                hook_token=hook_token,
                owner_type=owner_type,
                aip_arns=bedrock_aip_arns,
            )
        except ProvisionSuperseded:
            raise
        except Exception as e:
            put_metric("container.provision", dimensions={"status": "error"})
            put_metric("container.error_state")
            await self._update_container(user_id, status="error", substatus=None)
            raise EcsManagerError(f"Failed to write configs for user {user_id}: {e}", user_id)

        # Step 4: Start the container.
        #
        # force: this call IS the provision, not a wake. Two reasons it must not
        # re-run the start guards:
        #   - The trial->paid conversion STAMPS the container row and then
        #     dispatches this provision. Guarding here would make the conversion
        #     refuse to start the container it just reprovisioned — a deadlock
        #     against its own marker, and the paying customer is left with
        #     nothing running.
        #   - Every caller that reaches provision_user_container has already
        #     passed evaluate_provision_gate (which returns the same
        #     trial_daily_cap gate for an over-cap trial owner) or is a
        #     deliberate system/operator reprovision.
        try:
            await self.start_user_service(user_id, force=True)
        except EcsManagerError:
            put_metric("container.provision", dimensions={"status": "error"})
            put_metric("container.error_state")
            await self._update_container(user_id, status="error", substatus=None)
            raise

        put_metric("container.provision", dimensions={"status": "ok"})
        logger.info("Provisioned container %s for user %s", service_name, user_id)

        # No poller fired here: start_user_service above already fired one.
        # Firing again would double list_tasks/describe_services traffic and
        # race two tasks to write the provisioning -> running transition.

        return service_name

    async def _await_running_transition(
        self,
        user_id: str,
        *,
        interval_s: float = 10.0,
    ) -> None:
        """Durable background poller that drives provisioning -> running.

        Exits on one of four conditions:

        1. Container is reachable (ECS task from the PRIMARY deployment is
           RUNNING + gateway port open): write status=running, substatus=
           gateway_healthy, store task_arn, return.
        2. DDB status is no longer "provisioning" (external state change --
           admin stop, re-provision, reaper, etc.): return silently.
        3. PRIMARY deployment rolloutState=FAILED (circuit breaker tripped --
           the provision will never succeed): write status=error, return.
        4. asyncio.CancelledError (backend shutdown): propagate.

        No fixed timeout. The container can take 10s or 10 minutes to become
        healthy -- the poller keeps going until one of the above happens.

        Forced-redeploy paths (resize, forceNewDeployment) have an OLD healthy
        task while the NEW deployment rolls out. The poller filters tasks to
        only those from the PRIMARY deployment so an old task can't cause a
        premature status=running write that masks a failing new rollout.
        """
        while True:
            try:
                container = await container_repo.get_by_owner_id(user_id)
                if not container or container.get("status") != "provisioning":
                    # External state change or row gone -- nothing to do.
                    return

                service_name = container["service_name"]

                # One describe_services per iteration covers both concerns:
                # 1. primary deployment id -> startedBy filter for list_tasks
                # 2. primary deployment rolloutState -> failure detection
                primary = await self._get_primary_deployment(service_name)
                if primary is None:
                    # Transient state -- no PRIMARY deployment visible. Retry.
                    await asyncio.sleep(interval_s)
                    continue

                if primary.get("rolloutState") == "FAILED":
                    await container_repo.update_fields(
                        user_id,
                        {
                            "status": "error",
                            "substatus": "deployment_failed",
                        },
                    )
                    logger.error(
                        "Deployment circuit breaker tripped for container %s (user=%s); marking error",
                        service_name,
                        user_id,
                    )
                    return

                deployment_id = primary.get("id")
                task_arn, ip = await self._poll_running_task(service_name, deployment_id)
                if task_arn and ip and self.is_healthy(ip):
                    await container_repo.update_fields(
                        user_id,
                        {
                            "status": "running",
                            "substatus": "gateway_healthy",
                            "task_arn": task_arn,
                        },
                    )
                    logger.info(
                        "Transitioned container %s to running (user=%s, task=%s)",
                        service_name,
                        user_id,
                        task_arn.split("/")[-1],
                    )
                    return
                if task_arn and ip and container.get("substatus") != "task_running":
                    # Task is RUNNING with an IP but the gateway port isn't
                    # open yet — image pulled, OpenClaw still booting. This
                    # window is minutes long; surface it so the frontend
                    # stepper can render a real phase for it instead of a
                    # time-based guess. Guarded on the fetched row so it
                    # writes once, not once per poll iteration.
                    await container_repo.update_fields(
                        user_id,
                        {"substatus": "task_running"},
                    )

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Unexpected error in _await_running_transition for %s; will retry",
                    user_id,
                )

            await asyncio.sleep(interval_s)

    async def _get_primary_deployment(self, service_name: str) -> dict | None:
        """Return the PRIMARY deployment dict for the service, or None if
        unavailable. Primary is the deployment currently being rolled out or
        most-recently-succeeded; there is at most one at any time.

        Returns None on transient ECS errors so the caller retries on the
        next poll tick rather than flipping to error on a describe_services
        hiccup.
        """
        try:
            resp = self._ecs.describe_services(
                cluster=self._cluster,
                services=[service_name],
            )
        except Exception:
            logger.warning("describe_services failed for %s; will retry next tick", service_name)
            return None

        services = resp.get("services", [])
        if not services:
            return None
        for deployment in services[0].get("deployments", []):
            if deployment.get("status") == "PRIMARY":
                return deployment
        return None

    async def _poll_running_task(self, service_name: str, deployment_id: str | None) -> tuple[str | None, str | None]:
        """Find a RUNNING task from the given deployment and return (task_arn, ip).

        Lists ALL running tasks for the service, then filters in-code to
        tasks whose `startedBy` matches the primary deployment's id. ECS
        tags service-launched tasks with startedBy=ecs-svc/<deployment-id>.
        We can't filter via the ListTasks API because ECS rejects startedBy
        combined with serviceName (InvalidParameterException).

        Returns (None, None) when no qualifying task has an IP yet.
        """
        if not deployment_id:
            return None, None

        list_resp = self._ecs.list_tasks(
            cluster=self._cluster,
            serviceName=service_name,
            desiredStatus="RUNNING",
        )
        task_arns = list_resp.get("taskArns", [])
        if not task_arns:
            return None, None

        desc_resp = self._ecs.describe_tasks(
            cluster=self._cluster,
            tasks=task_arns,
        )
        for task in desc_resp.get("tasks", []):
            if task.get("startedBy") != deployment_id:
                continue
            if task.get("lastStatus") != "RUNNING":
                continue
            for attachment in task.get("attachments", []):
                if attachment.get("type") != "ElasticNetworkInterface":
                    continue
                for detail in attachment.get("details", []):
                    if detail.get("name") == "privateIPv4Address":
                        task_arn = task.get("taskArn") or task_arns[0]
                        return task_arn, detail.get("value")
        return None, None

    async def write_user_configs(
        self,
        user_id: str,
        gateway_token: str,
        *,
        provider_choice: str,
        byo_provider: str | None = None,
        hook_token: str | None = None,
        owner_type: str = "personal",
        aip_arns: dict[str, str] | None = None,
    ) -> None:
        """Write OpenClaw config files + pre-paired device trust store to EFS.

        Writes three files to the user's EFS workspace:

        - `openclaw.json` — the gateway config (provider block driven by
          ``provider_choice``: chatgpt_oauth / byo_key / bedrock_claude).
          ``agents.list`` is now seeded from the owner's live Paperclip
          agent list so /chat and /teams operate on the same agent
          population. If the owner has no Paperclip company yet or
          Paperclip is unreachable, ``fetch_paperclip_agents_for_owner``
          returns ``[]`` and ``build_openclaw_config_dict`` falls back to
          the legacy ``main`` agent so provision still succeeds.
        - `devices/.node-device-key.pem` — the node role's private key, read
          by the in-container agent for loopback authentication
        - `devices/paired.json` — the combined trust store (node + operator)

        Also ensures the operator Ed25519 private key is present in the
        container's DynamoDB row (KMS-encrypted).

        Callers that only need to refresh the device trust store (e.g. after
        a config patch that updated openclaw.json in-place) should call
        :meth:`ensure_device_identities` instead to avoid overwriting the
        openclaw.json that was just patched.

        Args:
            user_id: Container OWNER id (org_id for orgs, user_id for
                personal users). The parameter name is preserved from the
                pre-flat-fee API surface — pre-cutover containers were
                strictly per-user, but post-cutover everything is
                per-owner. Treated as ``owner_id`` internally.
            gateway_token: Auth token for the OpenClaw gateway HTTP API.
            provider_choice: One of ``"chatgpt_oauth"``, ``"byo_key"``,
                ``"bedrock_claude"``, ``"bedrock_trial"``. Drives which
                provider block lands in openclaw.json (both Bedrock modes emit
                the same provider shape; ``bedrock_trial`` emits a one-model
                catalog and the free-tier lockdown).
            byo_provider: ``"openai"`` or ``"anthropic"``. Required when
                ``provider_choice == "byo_key"`` so the right model block is
                emitted; ignored otherwise.
            hook_token: Optional per-user OpenClaw webhook auth token. Not
                interpolated into the rendered openclaw.json (it carries the
                literal ``${OPENCLAW_HOOK_TOKEN}`` placeholder which OpenClaw
                expands at boot from the per-user ECS task-def env var). The
                arg is threaded through for signature symmetry with the env
                injection in :meth:`_register_task_definition` and to leave
                a hook for the rotate flow to land here without rewiring.
        """
        # Treat the inbound parameter as owner_id internally. Naming
        # mismatch with the API surface is intentional — preserves
        # callers but makes the multi-tenant intent clear.
        owner_id = user_id

        # Lazy imports to avoid pulling these into module load (they
        # transitively touch httpx + DDB clients we don't want at import
        # time for unit tests).
        import httpx

        from core.repositories.paperclip_repo import PaperclipRepo
        from core.services.paperclip_admin_client import PaperclipAdminClient
        from core.services.paperclip_openclaw_pairing import (
            fetch_paperclip_agents_for_owner,
        )

        # Match the construction pattern used elsewhere
        # (routers/webhooks.py, routers/paperclip_proxy.py): create an
        # httpx.AsyncClient bound to PAPERCLIP_INTERNAL_URL, hand it to
        # PaperclipAdminClient, and aclose() the underlying client when
        # done. PaperclipAdminClient itself has no aclose() — the http
        # client owns the connection pool lifecycle. Short table name
        # only; core.dynamodb.get_table prepends the env prefix.
        http = httpx.AsyncClient(
            base_url=settings.PAPERCLIP_INTERNAL_URL,
            timeout=15.0,
        )
        # {paperclip_agent_id: plaintext key} for EVERY agent in the owner's
        # company — each agent authenticates to Paperclip as ITSELF (not as
        # Friday) via its own per-agent identity file. Empty when Paperclip is
        # unreachable; the next reprovision self-heals.
        paperclip_agent_keys: dict[str, str] = {}
        paperclip_company_id: str | None = None
        try:
            repo = PaperclipRepo(table_name="paperclip-companies")
            admin_client = PaperclipAdminClient(http_client=http)
            paperclip_agents = await fetch_paperclip_agents_for_owner(
                owner_id=owner_id,
                repo=repo,
                admin_client=admin_client,
            )
            # Mint (or read) a per-agent Paperclip API key for EACH agent so
            # actions in Paperclip's audit log attribute to the acting agent,
            # not to Friday or the global admin service account. Self-healing:
            # ensure_paperclip_api_key reads the stored key or mints + stores
            # one. Best-effort PER AGENT — one agent's mint failure (returns
            # None) is logged + skipped so the rest still get keys; if Paperclip
            # is unreachable the whole map is empty and the next reprovision
            # picks it up.
            if paperclip_agents:
                from core.services.paperclip_api_key import (
                    ensure_paperclip_api_key,
                )
                from core.services.paperclip_openclaw_pairing import (
                    resolve_company_id_for_owner,
                )

                paperclip_company_id = await resolve_company_id_for_owner(
                    owner_id=owner_id,
                    repo=repo,
                )
                for agent in paperclip_agents:
                    agent_id = agent.get("id")
                    if not agent_id:
                        continue
                    key = await ensure_paperclip_api_key(
                        owner_id=owner_id,
                        paperclip_agent_id=agent_id,
                        admin_client=admin_client,
                    )
                    if key:
                        paperclip_agent_keys[agent_id] = key
                    else:
                        logger.warning(
                            "write_user_configs: paperclip key mint skipped for owner=%s agent=%s",
                            owner_id,
                            agent_id,
                        )
        finally:
            await http.aclose()

        # Re-inject the owner's connected integration MCP servers (Stripe, …)
        # so this full openclaw.json overwrite doesn't drop them on a
        # reprovision. Best-effort: a Secrets-Manager hiccup while building the
        # blocks must not fail the whole provision — fall back to no integration
        # servers and let the next reconcile/reprovision self-heal.
        try:
            from core.services.credentials.mcp_server import (
                build_connected_integration_servers,
            )

            integration_mcp_servers = await build_connected_integration_servers(owner_id)
        except Exception:  # noqa: BLE001 — best-effort.
            logger.exception(
                "build_connected_integration_servers failed for %s; provisioning without them",
                owner_id,
            )
            integration_mcp_servers = {}

        # Carry the user's own on-disk MCP servers across this full overwrite
        # (the manual MCP panel writes them only into openclaw.json), merged with
        # the connected integration servers.
        integration_mcp_servers = await _provision_mcp_servers(owner_id, integration_mcp_servers)

        # Carry the user's configured channel bot accounts across this full
        # overwrite. A reprovision rewrites openclaw.json wholesale, so without
        # this every Telegram/Slack/Discord bot token + pairing is dropped.
        channel_accounts = await _provision_channel_accounts(owner_id)

        config_path = Path(settings.EFS_MOUNT_PATH) / owner_id / "openclaw.json"
        await write_openclaw_config(
            config_path=config_path,
            gateway_token=gateway_token,
            provider_choice=provider_choice,
            user_id=owner_id,
            byo_provider=byo_provider,
            paperclip_agents=paperclip_agents,
            hook_token=hook_token,
            owner_type=owner_type,
            integration_mcp_servers=integration_mcp_servers,
            aip_arns=aip_arns,
            channel_accounts=channel_accounts,
        )

        # Seed Friday's IDENTITY/SOUL/USER/BOOTSTRAP files on EFS so they're
        # present before the container boots. Best-effort — failure here
        # MUST NOT block provision; Friday boots with upstream defaults if
        # this fails.
        from core.services.friday_identity import (
            pre_stage_friday_identity,
            pre_stage_paperclip_identities,
        )

        try:
            await pre_stage_friday_identity(
                owner_id=owner_id,
                paperclip_agents=paperclip_agents,
                workspace=get_workspace(),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort.
            put_metric("friday_identity.seed.error")
            logger.warning(
                "write_user_configs: pre_stage_friday_identity failed for owner=%s: %s",
                owner_id,
                exc,
            )

        # Stage each agent's per-agent Paperclip identity file
        # (paperclip-claimed-api-key.json) into its own workspace so it can
        # authenticate to Paperclip AS ITSELF via the bundled `paperclip` skill.
        # Best-effort — failure here MUST NOT block provision; the next
        # reprovision re-stages (write-if-missing is idempotent).
        try:
            await pre_stage_paperclip_identities(
                owner_id=owner_id,
                paperclip_agents=paperclip_agents,
                agent_keys=paperclip_agent_keys,
                company_id=paperclip_company_id,
                api_url=settings.PAPERCLIP_INTERNAL_URL or None,
                workspace=get_workspace(),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort.
            put_metric("paperclip_identity.seed.error")
            logger.warning(
                "write_user_configs: pre_stage_paperclip_identities failed for owner=%s: %s",
                owner_id,
                exc,
            )

        # Seed Atlas's recruiter workspace (files + skill bundles) on EFS,
        # mirroring Friday. Best-effort — an Atlas failure MUST NOT block
        # provision; Atlas boots with an empty workspace if this fails.
        from core.services.atlas_seed import pre_stage_atlas

        try:
            await pre_stage_atlas(
                owner_id=owner_id,
                paperclip_agents=paperclip_agents,
                workspace=get_workspace(),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort.
            # Distinct from the seeder's per-file `atlas_seed.error`: this fires
            # only on a catastrophic failure (e.g. import error) where the seeder
            # never ran, vs a single bad file the seeder logged and skipped.
            put_metric("atlas_seed.invoke_error")
            logger.warning(
                "write_user_configs: pre_stage_atlas failed for owner=%s: %s",
                owner_id,
                exc,
            )

        # Stage the container-global credentials/oauth.json after the config
        # write, so it's present for any reprovision / config-rewrite path that
        # reaches write_user_configs WITHOUT the provision pre-task (debug
        # PATCH, the reapply script). For a fresh provision the pre-task already
        # wrote it; this overwrite is idempotent. One shared file authenticates
        # every agent (OpenClaw imports it per-agent on boot), so there is no
        # per-agent fan-out. Best-effort + loud: a transient failure self-heals
        # on the next reprovision rather than blocking the config write.
        if provider_choice == "chatgpt_oauth":
            from core.services.oauth_service import resolve_token_owner

            try:
                await self._pre_stage_oauth_for_user(
                    owner_id,
                    token_owner_id=await resolve_token_owner(owner_id=owner_id),
                )
            except Exception as exc:  # noqa: BLE001 — best-effort.
                put_metric("chatgpt_oauth.oauth_json_stage.error")
                logger.warning(
                    "write_user_configs: oauth.json stage failed for owner=%s: %s",
                    owner_id,
                    exc,
                )

        # NOTE: the per-agent Google-account instruction (`gog -a <email>`) is
        # deliberately NOT seeded here. That managed-block edit to IDENTITY.md
        # is user-action-driven — the assign API and the disconnect path own
        # it. IDENTITY.md is a workspace file that survives a config-rewrite
        # reprovision, so a seeded agent stays seeded with no provision-time
        # re-seed; an unassigned agent needs nothing.

        # Refresh the owner-facts file (plan/provider snapshot the bundled
        # isol8 skill points agents at). The provider this config was JUST
        # rendered with rides along as an override: on the provider-switch
        # cold path this write can land right after the billing-row commit,
        # and an eventually-consistent row re-read could otherwise roll the
        # file back to the OLD provider. Internally best-effort — never
        # blocks the config write.
        from core.services.owner_facts import write_owner_facts

        await write_owner_facts(
            owner_id,
            account_overrides={"provider_choice": provider_choice, "byo_provider": byo_provider},
        )

        await self.ensure_device_identities(owner_id, gateway_token)

    async def ensure_device_identities(self, user_id: str, gateway_token: str) -> None:
        """Idempotently provision node + operator device identities.

        Separated from :meth:`write_user_configs` so callers can refresh the
        device trust store without touching openclaw.json — important for
        the redeploy path that patches openclaw.json in-place and just
        needs the operator entry backfilled after an OpenClaw 4.5 upgrade.

        What this writes:

        - `devices/.node-device-key.pem` (if not already on EFS)
        - `devices/paired.json` (always overwritten with the latest node +
          operator entries)
        - DynamoDB `containers` row `operator_device_id` + `operator_priv_key_enc`
          (latter is KMS-encrypted with encryption-context bound to owner_id)

        Safe to call repeatedly: reuses existing node PEM and existing
        KMS-encrypted operator seed when present. Only regenerates when the
        ciphertext is missing or undecryptable.
        """
        from core.crypto import kms_secrets
        from core.crypto.operator_device import (
            BACKEND_OPERATOR_SCOPES,
            build_paired_operator_entry,
            generate_operator_device,
            load_operator_device_from_seed,
        )

        workspace = get_workspace()

        # --- Node device identity -------------------------------------------
        # Reuse the existing key if the PEM is already on EFS (so the node's
        # in-container agent keeps the same identity across restarts),
        # otherwise generate fresh.
        try:
            existing_pem = workspace.read_file(user_id, "devices/.node-device-key.pem")
            node_identity = load_node_device_identity(existing_pem)
            logger.info("Reusing existing node device key for user %s", user_id)
        except Exception:
            node_identity = generate_node_device_identity()
            workspace.write_file(
                user_id,
                "devices/.node-device-key.pem",
                node_identity["private_key_pem"],
            )
            logger.info("Generated new node device key for user %s", user_id)

        # --- Operator device identity (OpenClaw 4.5 scoped-auth requirement) -
        # Bind the ciphertext to the owner_id via KMS encryption context — a
        # stolen row can't be replayed against a different container.
        kms_ctx = {"owner_id": user_id, "purpose": "operator-device-seed"}
        existing_container = await container_repo.get_by_owner_id(user_id)
        existing_enc_seed = existing_container.get("operator_priv_key_enc") if existing_container else None

        if existing_enc_seed:
            try:
                seed_bytes = kms_secrets.decrypt_bytes(existing_enc_seed, encryption_context=kms_ctx)
                operator_identity = load_operator_device_from_seed(seed_bytes)
                logger.info("Reusing existing operator device for user %s", user_id)
            except Exception as exc:
                # KMS decrypt failure or corrupted ciphertext — regenerate
                # rather than wedging the container. The old encrypted blob is
                # replaced below before the container boots, so the mismatch
                # never becomes visible to the gateway.
                logger.warning(
                    "Failed to decrypt existing operator device for user %s (%s); regenerating",
                    user_id,
                    exc,
                )
                operator_identity = generate_operator_device()
        else:
            operator_identity = generate_operator_device()
            logger.info("Generated new operator device for user %s", user_id)

        # Always re-encrypt and persist. This covers both "first provision"
        # and "we just regenerated after a decrypt failure" — the DynamoDB row
        # becomes the source of truth alongside the paired.json on EFS.
        enc_seed_b64 = kms_secrets.encrypt_bytes(operator_identity.private_key_seed, encryption_context=kms_ctx)
        await container_repo.update_fields(
            user_id,
            {
                "operator_device_id": operator_identity.device_id,
                "operator_priv_key_enc": enc_seed_b64,
            },
        )

        operator_entry = build_paired_operator_entry(
            operator_identity,
            gateway_token=gateway_token,
            scopes=BACKEND_OPERATOR_SCOPES,
        )

        # Pre-pair each Paperclip agent's device so its relayed operator connect
        # clears the remote-pairing wall, and bind each agent's adapterConfig
        # authToken to this container's gateway_token. Best-effort and enumerated
        # at provision so a reprovision (which overwrites paired.json) re-includes
        # them. Paperclip unavailability must never block container provision.
        paperclip_entries: list[dict] = []
        try:
            import httpx

            from core.repositories.paperclip_repo import PaperclipRepo
            from core.services.paperclip_admin_client import PaperclipAdminClient
            from core.services.paperclip_openclaw_pairing import (
                register_paperclip_agent_devices,
            )

            http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
            try:
                paperclip_entries = await register_paperclip_agent_devices(
                    owner_id=user_id,
                    gateway_token=gateway_token,
                    repo=PaperclipRepo(table_name="paperclip-companies"),
                    admin_client=PaperclipAdminClient(http_client=http),
                )
            finally:
                await http.aclose()
        except Exception as exc:  # noqa: BLE001 — pairing must not block provision
            logger.warning(
                "ensure_device_identities: Paperclip device pairing failed for owner=%s: %s",
                user_id,
                exc,
            )

        paired_json = build_device_paired_json(
            node_identity["device_id"],
            node_identity["public_key_b64"],
            operator_entry=operator_entry,
            extra_operator_entries=paperclip_entries,
        )
        workspace.write_file(user_id, "devices/paired.json", paired_json)
