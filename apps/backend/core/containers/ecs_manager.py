"""
ECS Fargate service manager for per-user OpenClaw gateways.

Each subscriber gets a dedicated ECS Service (desiredCount 0/1) that
runs an OpenClaw gateway container with a per-user EFS access point
for data isolation. Task IPs are discovered via the ECS describe_tasks
API.
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
    build_device_paired_json,
    generate_node_device_identity,
    load_node_device_identity,
    write_openclaw_config,
)
from core.containers.workspace import get_workspace
from core.repositories import container_repo

logger = logging.getLogger(__name__)

GATEWAY_PORT = 18789

# Single per-user task size for the flat-fee pivot. Per spec §3.2 / §10.
# Old per-tier sizing (free/starter 0.5/1, pro 1/2, enterprise 2/4) deleted —
# every paying user gets the same 0.5 vCPU / 1 GB box. Kept as constants
# so the resize/register paths pull from a single place.
PER_USER_CPU = "512"
PER_USER_MEMORY_MIB = "2048"


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


class EcsManagerError(Exception):
    """Raised when ECS operations fail."""

    def __init__(self, message: str, user_id: str = ""):
        super().__init__(message)
        self.user_id = user_id


class EcsManager:
    """Manages per-user ECS Fargate services for OpenClaw gateways."""

    # The CDK base task def lives in this family; per-user clones live in
    # `<base>-user`. Splitting the families means `describe_task_definition`
    # on the base family always returns the latest CDK base — no need for
    # SSM-pinned ARNs. See PR #410 for the reasoning (and #299 for the
    # earlier same-family workaround that this supersedes).
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
            taskRoleArn=base.get("taskRoleArn", ""),
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
                deploymentConfiguration={
                    "deploymentCircuitBreaker": {
                        "enable": True,
                        "rollback": False,
                    }
                },
            )
            # Enable ECS Exec for all environments so we can debug per-user
            # OpenClaw containers (skill installs, workspace state, gateway
            # health) without redeploying. Requires the task role to grant
            # ssmmessages:CreateControlChannel/CreateDataChannel/OpenControlChannel/OpenDataChannel.
            create_kwargs["enableExecuteCommand"] = True
            self._ecs.create_service(**create_kwargs)
            await self._update_container(user_id, substatus="service_created")
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

    async def start_user_service(self, user_id: str) -> None:
        """Scale a user's ECS service to 1 (running).

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
        traced in the post-incident review of the 47h-uptime container).

        Args:
            user_id: Clerk user ID.

        Raises:
            EcsManagerError: If the ECS update_service call fails.
        """
        service_name = self._service_name(user_id)
        put_metric("container.lifecycle.state_change", dimensions={"state": "starting"})

        try:
            with timing("container.lifecycle.latency", {"op": "start"}):
                self._ecs.update_service(
                    cluster=self._cluster,
                    service=service_name,
                    desiredCount=1,
                    deploymentConfiguration={
                        "deploymentCircuitBreaker": {
                            "enable": True,
                            "rollback": False,
                        }
                    },
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
            # had (e.g., missing CLAWHUB_WORKDIR — incident 2026-04-17). Per-
            # user state (EFS access point, image, cpu/memory) is layered here.
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
            reg_kwargs = await asyncio.to_thread(
                self._build_register_kwargs_from_base,
                access_point_id,
                new_image=new_image,
                new_cpu=new_cpu,
                new_memory=new_memory,
                hook_token=hook_token,
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
                deploymentConfiguration={
                    "deploymentCircuitBreaker": {
                        "enable": True,
                        "rollback": False,
                    }
                },
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

    async def reprovision_for_user(self, user_id: str) -> dict:
        """Force a fresh deploy: stop then start the ECS service so the
        next task picks up the latest task definition. Less destructive
        than delete_user_service + create_user_service (which would lose
        the gateway token + EFS access point binding). Used by the admin
        dashboard's container-reprovision action.
        """
        await self.stop_user_service(user_id)
        await self.start_user_service(user_id)
        return {"status": "started"}

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
            # recovered if subsequent retries fall through to this block
            # (Codex P1 on PR #309).
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
        """Discover a task's private IP via ECS describe_tasks API.

        Lists running tasks for the given ECS service, then describes
        them to extract the private IPv4 address from the ENI attachment.
        This is more reliable than Cloud Map and ensures per-service
        isolation (each service's tasks are queried independently).

        Args:
            service_name: The ECS service name.

        Returns:
            The task's private IPv4 address, or None if no running task.
        """
        try:
            # List tasks for this specific service
            list_resp = self._ecs.list_tasks(
                cluster=self._cluster,
                serviceName=service_name,
                desiredStatus="RUNNING",
            )
            task_arns = list_resp.get("taskArns", [])
            if not task_arns:
                return None

            # Describe the first running task to get its ENI IP
            desc_resp = self._ecs.describe_tasks(
                cluster=self._cluster,
                tasks=[task_arns[0]],
            )
            tasks = desc_resp.get("tasks", [])
            if not tasks:
                return None

            # Extract private IP from ENI attachment
            for attachment in tasks[0].get("attachments", []):
                if attachment.get("type") == "ElasticNetworkInterface":
                    for detail in attachment.get("details", []):
                        if detail.get("name") == "privateIPv4Address":
                            return detail.get("value")
        except Exception as e:
            logger.error("ECS task discovery failed for %s: %s", service_name, e)
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

        Looks up the api-keys row populated by ``key_service`` (Task 10) for
        ``{user_id, byo_provider}`` and turns its ``secret_arn`` into a single
        ``secrets:`` entry that the per-user task definition references.
        ``service-stack.ts`` already grants the backend
        ``secretsmanager:GetSecretValue`` for ``isol8/{env}/user-keys/*``, so
        the per-user ARN is reachable from the task IAM role.
        """
        if byo_provider not in {"openai", "anthropic"}:
            raise EcsManagerError(
                f"byo_provider must be 'openai' or 'anthropic' for byo_key, got {byo_provider!r}",
                user_id,
            )
        from core.repositories import api_key_repo

        key_row = await api_key_repo.get_key(user_id, byo_provider)
        if not key_row or not key_row.get("secret_arn"):
            raise EcsManagerError(
                f"No saved {byo_provider} key for user {user_id} - caller should "
                "block provisioning until the user adds their key",
                user_id,
            )
        env_var_name = "OPENAI_API_KEY" if byo_provider == "openai" else "ANTHROPIC_API_KEY"
        return [{"name": env_var_name, "valueFrom": key_row["secret_arn"]}]

    async def _pre_stage_oauth_for_user(self, user_id: str) -> None:
        """Pre-stage the auth-profiles.json for a chatgpt_oauth user.

        Fetches the decrypted ChatGPT tokens persisted by ``oauth_service``
        and writes OpenClaw's AuthProfileStore JSON to
        ``<EFS>/users/{user_id}/agents/main/agent/auth-profiles.json``
        BEFORE the ECS service is created so the openai-codex provider
        finds the credentials cold on first boot. Raises if no active
        OAuth row exists — caller should gate provisioning on a completed
        device-code flow.

        See spec at
        ``docs/superpowers/specs/2026-04-29-chatgpt-oauth-auth-profiles-design.md``.
        """
        from core.services.oauth_service import get_decrypted_tokens

        tokens = await get_decrypted_tokens(user_id=user_id)
        if not tokens:
            raise EcsManagerError(
                f"No ChatGPT OAuth tokens for user {user_id} - caller should complete OAuth before provisioning",
                user_id,
            )
        from core.containers.workspace import pre_stage_auth_profile_store

        await pre_stage_auth_profile_store(user_id=user_id, oauth_tokens=tokens)

    async def provision_user_container(
        self,
        user_id: str,
        *,
        provider_choice: str,
        byo_provider: str | None = None,
        owner_type: str = "personal",
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
                ``"bedrock_claude"``. Selects the LLM provider block in
                openclaw.json (Task 12) and drives any pre-task setup
                (codex auth pre-stage for chatgpt_oauth, per-user
                Secrets Manager wiring for byo_key).
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
        if provider_choice == "chatgpt_oauth":
            await self._pre_stage_oauth_for_user(user_id)
            # OpenClaw reads OAuth credentials from
            # <agentDir>/auth-profiles.json. _pre_stage_oauth_for_user above
            # writes that file to EFS. No env var override needed — the
            # path is resolved from agents.list[*].id in openclaw.json.
        elif provider_choice == "byo_key":
            if byo_provider is None:
                raise EcsManagerError(
                    "byo_provider is required when provider_choice == 'byo_key'",
                    user_id,
                )
            secrets_for_task = await self._build_byo_secrets_for_user(user_id, byo_provider)
        elif provider_choice == "bedrock_claude":
            # No extra setup — auth comes from the ECS task IAM role granted
            # bedrock:InvokeModel by the container stack.
            pass
        else:
            raise EcsManagerError(
                f"Unknown provider_choice: {provider_choice!r}",
                user_id,
            )

        service_name = self._service_name(user_id)

        # Check current state
        existing = await container_repo.get_by_owner_id(user_id)
        svc = self._service_exists(service_name)

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

                # Update gateway token and write configs
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
                    )
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
                        new_task_def_arn = await asyncio.to_thread(
                            self._register_task_definition,
                            access_point_id,
                            secrets_for_task=secrets_for_task,
                            environment_for_task=environment_for_task,
                            hook_token=existing_hook_token,
                        )
                        await asyncio.to_thread(
                            self._ecs.update_service,
                            cluster=self._cluster,
                            service=service_name,
                            desiredCount=1,
                            taskDefinition=new_task_def_arn,
                            deploymentConfiguration={
                                "deploymentCircuitBreaker": {
                                    "enable": True,
                                    "rollback": False,
                                }
                            },
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
                    try:
                        await self.start_user_service(user_id)
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
                        deploymentConfiguration={
                            "deploymentCircuitBreaker": {
                                "enable": True,
                                "rollback": False,
                            }
                        },
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
        )

        # Step 3: Write configs to EFS
        try:
            await self.write_user_configs(
                user_id,
                gateway_token,
                provider_choice=provider_choice,
                byo_provider=byo_provider,
                hook_token=hook_token,
                owner_type=owner_type,
            )
        except Exception as e:
            put_metric("container.provision", dimensions={"status": "error"})
            put_metric("container.error_state")
            await self._update_container(user_id, status="error", substatus=None)
            raise EcsManagerError(f"Failed to write configs for user {user_id}: {e}", user_id)

        # Step 4: Start the container
        try:
            await self.start_user_service(user_id)
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
                ``"bedrock_claude"``. Drives which provider block lands in
                openclaw.json (Task 12 contract).
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
        paperclip_api_key: str | None = None
        paperclip_company_id: str | None = None
        try:
            repo = PaperclipRepo(table_name="paperclip-companies")
            admin_client = PaperclipAdminClient(http_client=http)
            paperclip_agents = await fetch_paperclip_agents_for_owner(
                owner_id=owner_id,
                repo=repo,
                admin_client=admin_client,
            )
            # Mint (or read) Friday's Paperclip API key so the MCP server
            # inside her container can call Paperclip's REST API as her
            # rather than as the global admin service account.
            # Self-healing: if the key isn't in api_keys yet, ensure_…
            # mints + stores. If Paperclip is unreachable this run, returns
            # None and write_openclaw_config simply omits the mcp.servers
            # block — the next reprovision picks it up.
            friday = next(
                (a for a in paperclip_agents if a.get("role") == "ceo"),
                None,
            )
            if friday and friday.get("id"):
                from core.services.paperclip_api_key import (
                    ensure_paperclip_api_key,
                )
                from core.services.paperclip_openclaw_pairing import (
                    resolve_company_id_for_owner,
                )

                paperclip_api_key = await ensure_paperclip_api_key(
                    owner_id=owner_id,
                    paperclip_agent_id=friday["id"],
                    admin_client=admin_client,
                )
                paperclip_company_id = await resolve_company_id_for_owner(
                    owner_id=owner_id,
                    repo=repo,
                )
        finally:
            await http.aclose()

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
            paperclip_api_key=paperclip_api_key,
            paperclip_company_id=paperclip_company_id,
            paperclip_api_url=settings.PAPERCLIP_INTERNAL_URL or None,
        )

        # Seed Friday's IDENTITY/SOUL/USER/BOOTSTRAP files on EFS so they're
        # present before the container boots. Best-effort — failure here
        # MUST NOT block provision; Friday boots with upstream defaults if
        # this fails.
        from core.services.friday_identity import pre_stage_friday_identity

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

        paired_json = build_device_paired_json(
            node_identity["device_id"],
            node_identity["public_key_b64"],
            operator_entry=operator_entry,
        )
        workspace.write_file(user_id, "devices/paired.json", paired_json)
