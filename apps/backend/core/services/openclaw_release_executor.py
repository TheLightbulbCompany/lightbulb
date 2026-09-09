"""Recoverable, one-owner execution of an immutable OpenClaw release."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from core.containers import get_ecs_manager
from core.containers.ecs_manager import SINGLE_WRITER_DEPLOYMENT_CONFIG
from core.repositories import container_repo, update_repo
from core.services import config_schema
from core.services.efs_file_lock import chown_uid_1000_best_effort
from core.services.openclaw_release import (
    OpenClawRelease,
    OwnerReleaseConflict,
    assert_release_claim_sync,
    owner_mutation_guard,
    release_by_id,
)

logger = logging.getLogger(__name__)

_HEARTBEAT_S = 30
_STALE_S = 3 * _HEARTBEAT_S
_MAX_RECOVERIES_PER_TICK = 1


class OwnerReleaseSuperseded(RuntimeError):
    pass


class OwnerReleaseValidationError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stage_path(owner_id: str, update_id: str) -> Path:
    if not update_id or any(
        character not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-" for character in update_id
    ):
        raise OwnerReleaseValidationError("release update id is invalid")
    return Path(settings.EFS_MOUNT_PATH) / owner_id / f".openclaw.release-{update_id}.json"


def _config_has(config: dict, capability: str) -> bool:
    value: object = config
    for segment in capability.split("."):
        if not isinstance(value, dict) or segment not in value:
            return False
        value = value[segment]
    return True


async def _read_claimed(owner_id: str, update_id: str) -> tuple[dict, dict]:
    container, update = await asyncio.gather(
        container_repo.get_by_owner_id(owner_id, consistent=True),
        update_repo.get_by_id(owner_id, update_id, consistent=True),
    )
    if not container or not update:
        raise OwnerReleaseSuperseded("release state disappeared")
    claim_id = update.get("apply_claim_id")
    if (
        update.get("status") != "applying"
        or update.get("update_type") != "release"
        or container.get("release_update_id") != update_id
        or container.get("release_revision") != update.get("release_revision")
        or container.get("provision_claim_id") != claim_id
        or container.get("provision_attempt_token") != claim_id
    ):
        raise OwnerReleaseSuperseded("release claim moved")
    return container, update


class _ReleaseRuntime:
    def __init__(self):
        self.ecs = get_ecs_manager()

    async def snapshot(self, owner_id: str) -> dict:
        response = await asyncio.to_thread(
            self.ecs._ecs.describe_services,
            cluster=self.ecs._cluster,
            services=[self.ecs._service_name(owner_id)],
        )
        services = response.get("services") or []
        if len(services) != 1 or services[0].get("status") != "ACTIVE":
            raise OwnerReleaseValidationError("owner ECS service is unavailable")
        service = services[0]
        return {
            "task_definition_arn": service.get("taskDefinition"),
            "desired_count": service.get("desiredCount"),
            "running_count": service.get("runningCount"),
            "pending_count": service.get("pendingCount"),
        }

    async def prepare(self, owner_id: str, release: OpenClawRelease) -> str:
        container = await container_repo.get_by_owner_id(owner_id, consistent=True)
        if not container or not all(
            container.get(field) for field in ("hook_token", "gateway_token", "task_definition_arn")
        ):
            raise OwnerReleaseValidationError("owner container lacks immutable release prerequisites")
        size = await self.ecs.current_container_size(owner_id)
        if size is None:
            raise OwnerReleaseValidationError("owner task definition has no registered size")
        task_definition_arn = await self.ecs.resize_user_container(
            owner_id,
            new_cpu=size[0],
            new_memory=size[1],
            new_image=release.digest_image,
            _register_only=True,
        )
        task_definition = await self._task_definition(task_definition_arn)
        if self._openclaw_definition(task_definition).get("image") != release.digest_image:
            raise OwnerReleaseValidationError("prepared task definition is not digest-pinned")
        return task_definition_arn

    async def running_tasks(self, owner_id: str) -> list[dict]:
        arns = (
            await asyncio.to_thread(
                self.ecs._ecs.list_tasks,
                cluster=self.ecs._cluster,
                serviceName=self.ecs._service_name(owner_id),
                desiredStatus="RUNNING",
            )
        ).get("taskArns", [])
        if not arns:
            return []
        response = await asyncio.to_thread(
            self.ecs._ecs.describe_tasks,
            cluster=self.ecs._cluster,
            tasks=arns,
        )
        tasks = []
        for task in response.get("tasks") or []:
            address = None
            for attachment in task.get("attachments") or []:
                if attachment.get("type") != "ElasticNetworkInterface":
                    continue
                address = next(
                    (
                        detail.get("value")
                        for detail in attachment.get("details") or []
                        if detail.get("name") == "privateIPv4Address"
                    ),
                    None,
                )
            openclaw = next(
                (container for container in task.get("containers") or [] if container.get("name") == "openclaw"),
                {},
            )
            tasks.append(
                {
                    "task_arn": task.get("taskArn"),
                    "task_definition_arn": task.get("taskDefinitionArn"),
                    "last_status": task.get("lastStatus"),
                    "image_digest": openclaw.get("imageDigest"),
                    "ip": address,
                }
            )
        return tasks

    async def stop(self, owner_id: str) -> None:
        await asyncio.to_thread(
            self.ecs._ecs.update_service,
            cluster=self.ecs._cluster,
            service=self.ecs._service_name(owner_id),
            desiredCount=0,
        )

    async def drain(self, owner_id: str, pre_stop_task_arns: list[str]) -> dict:
        for _ in range(60):
            service = await self.snapshot(owner_id)
            if (
                service["desired_count"] == 0
                and service["running_count"] == 0
                and service["pending_count"] == 0
                and not await self.running_tasks(owner_id)
            ):
                described = await asyncio.to_thread(
                    self.ecs._ecs.describe_tasks,
                    cluster=self.ecs._cluster,
                    tasks=pre_stop_task_arns,
                )
                if all(task.get("lastStatus") == "STOPPED" for task in described.get("tasks") or []):
                    return {
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "service_desired_count": 0,
                        "service_running_count": 0,
                        "service_pending_count": 0,
                        "stopped_task_arns": pre_stop_task_arns,
                    }
            await asyncio.sleep(3)
        raise TimeoutError("owner ECS service did not drain")

    async def stage(
        self,
        owner_id: str,
        update_id: str,
        release: OpenClawRelease,
        claim_id: str,
        revision: int,
    ) -> tuple[str, str]:
        from core.containers.config import BEDROCK_TRIAL_CHOICE
        from core.services.config_patcher import _deep_merge
        from core.services.container_provision import resolve_provider_choice
        from core.services.update_service import _runtime_only_delta

        container = await container_repo.get_by_owner_id(owner_id, consistent=True)
        if not container or not container.get("gateway_token"):
            raise OwnerReleaseValidationError("owner gateway token is unavailable")
        live_path = Path(settings.EFS_MOUNT_PATH) / owner_id / "openclaw.json"
        try:
            prior_bytes = live_path.read_bytes()
            prior_config = json.loads(prior_bytes)
        except (OSError, ValueError) as exc:
            raise OwnerReleaseValidationError("owner live config is unavailable") from exc
        stage_path = _stage_path(owner_id, update_id)
        provider_choice, byo_provider = await resolve_provider_choice(owner_id)
        await self.ecs.write_user_configs(
            owner_id,
            container["gateway_token"],
            provider_choice=provider_choice,
            byo_provider=byo_provider,
            hook_token=container.get("hook_token"),
            owner_type=container.get("owner_type") or "personal",
            aip_arns=container.get("bedrock_aip_arns"),
            deployed_tag=release.image.rpartition(":")[2],
            config_path=stage_path,
            schema_id=release.schema_id,
            release_update_id=update_id,
            release_claim_id=claim_id,
            release_revision=revision,
        )
        try:
            staged_config = json.loads(stage_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise OwnerReleaseValidationError("staged release config is unavailable") from exc
        restore = _runtime_only_delta(prior_config, staged_config)
        if provider_choice == BEDROCK_TRIAL_CHOICE:
            restore.pop("channels", None)
            restore.pop("bindings", None)
        restore = config_schema.prune_schema_rejected(staged_config, restore, schema_id=release.schema_id)
        staged_config = _deep_merge(staged_config, restore)
        missing = sorted(
            capability for capability in release.config_capabilities if not _config_has(staged_config, capability)
        )
        if missing:
            raise OwnerReleaseValidationError(f"staged release config lacks capabilities: {missing}")
        config_schema.gate_config(
            staged_config,
            owner_id=owner_id,
            context="owner release stage",
            enforce=True,
            schema_id=release.schema_id,
        )
        staged_bytes = json.dumps(staged_config, indent=2).encode()
        assert_release_claim_sync(owner_id, update_id, claim_id, revision)
        self._atomic_write(stage_path, staged_bytes)
        assert_release_claim_sync(owner_id, update_id, claim_id, revision)
        return _sha256(prior_bytes), _sha256(staged_bytes)

    async def activate(
        self,
        owner_id: str,
        update_id: str,
        prior_sha256: str,
        staged_sha256: str,
        release: OpenClawRelease,
        claim_id: str,
        revision: int,
    ) -> None:
        live_path = Path(settings.EFS_MOUNT_PATH) / owner_id / "openclaw.json"
        stage_path = _stage_path(owner_id, update_id)
        backup_path = live_path.with_name(f".openclaw.release-{update_id}.backup.json")
        async with owner_mutation_guard(owner_id, release_update_id=update_id):
            assert_release_claim_sync(owner_id, update_id, claim_id, revision)
            live_bytes = live_path.read_bytes()
            if _sha256(live_bytes) == staged_sha256:
                try:
                    backup_bytes = backup_path.read_bytes()
                except OSError as exc:
                    raise OwnerReleaseValidationError(
                        "activated release has no verified pre-upgrade config backup"
                    ) from exc
                if _sha256(backup_bytes) != prior_sha256:
                    raise OwnerReleaseValidationError(
                        "activated release pre-upgrade config backup does not match checkpoint"
                    )
                return
            if _sha256(live_bytes) != prior_sha256:
                raise OwnerReleaseValidationError("live config changed after release reservation")
            staged_bytes = stage_path.read_bytes()
            if _sha256(staged_bytes) != staged_sha256:
                raise OwnerReleaseValidationError("staged config digest does not match checkpoint")
            try:
                staged_config = json.loads(staged_bytes)
            except ValueError as exc:
                raise OwnerReleaseValidationError("staged config is invalid JSON") from exc
            config_schema.gate_config(
                staged_config,
                owner_id=owner_id,
                context="owner release activation",
                enforce=True,
                schema_id=release.schema_id,
            )
            self._atomic_write(backup_path, live_bytes)
            if _sha256(backup_path.read_bytes()) != prior_sha256:
                raise OwnerReleaseValidationError("release backup could not be verified")
            os.replace(stage_path, live_path)
            chown_uid_1000_best_effort(str(live_path))

    async def start(self, owner_id: str, task_definition_arn: str, desired_count: int) -> None:
        await asyncio.to_thread(
            self.ecs._ecs.update_service,
            cluster=self.ecs._cluster,
            service=self.ecs._service_name(owner_id),
            taskDefinition=task_definition_arn,
            desiredCount=desired_count,
            deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
            **self.ecs._service_registries(),
        )

    async def accept(
        self,
        owner_id: str,
        task_definition_arn: str,
        staged_config_sha256: str,
        release: OpenClawRelease,
        gateway_token: str,
    ) -> dict:
        waiter = self.ecs._ecs.get_waiter("services_stable")
        await asyncio.to_thread(
            waiter.wait,
            cluster=self.ecs._cluster,
            services=[self.ecs._service_name(owner_id)],
            WaiterConfig={"Delay": 15, "MaxAttempts": 60},
        )
        service = await self.snapshot(owner_id)
        if (
            service["task_definition_arn"] != task_definition_arn
            or service["desired_count"] != 1
            or service["running_count"] != 1
            or service["pending_count"] != 0
        ):
            raise OwnerReleaseValidationError("ECS service did not converge on the qualified target")
        task_definition = await self._task_definition(task_definition_arn)
        if self._openclaw_definition(task_definition).get("image") != release.digest_image:
            raise OwnerReleaseValidationError("live task definition image is not digest-pinned")
        tasks = [task for task in await self.running_tasks(owner_id) if task.get("last_status") == "RUNNING"]
        if len(tasks) != 1:
            raise OwnerReleaseValidationError("qualified release does not have exactly one running task")
        task = tasks[0]
        if (
            task.get("task_definition_arn") != task_definition_arn
            or task.get("image_digest") != release.image_digest
            or not task.get("task_arn")
            or not task.get("ip")
        ):
            raise OwnerReleaseValidationError("live task identity does not match the qualified release")
        from core.gateway.operator_rpc import gateway_hello_oneshot

        hello = await gateway_hello_oneshot(task["ip"], owner_id=owner_id, token=gateway_token)
        methods = set((hello.get("features") or {}).get("methods") or [])
        if hello.get("server", {}).get("buildId") != release.build_id or not release.runtime_capabilities <= methods:
            raise OwnerReleaseValidationError("gateway hello does not match qualified build capabilities")
        return {
            "release_id": release.release_id,
            "image": release.image,
            "image_digest": release.image_digest,
            "build_id": release.build_id,
            "upstream_version": release.upstream_version,
            "fork_tag": release.fork_tag,
            "fork_commit": release.fork_commit,
            "schema_id": release.schema_id,
            "schema_sha256": release.schema_sha256,
            "config_capabilities": sorted(release.config_capabilities),
            "qualification_id": release.qualification_id,
            "evidence_id": release.evidence_id,
            "runtime_capabilities": sorted(release.runtime_capabilities),
            "qualification_capabilities": sorted(release.qualification_capabilities),
            "task_definition_arn": task_definition_arn,
            "task_arn": task["task_arn"],
            "config_sha256": staged_config_sha256,
            "gateway": "accepted",
        }

    async def _task_definition(self, task_definition_arn: str) -> dict:
        response = await asyncio.to_thread(
            self.ecs._ecs.describe_task_definition,
            taskDefinition=task_definition_arn,
        )
        return response.get("taskDefinition") or {}

    @staticmethod
    def _openclaw_definition(task_definition: dict) -> dict:
        return next(
            (
                definition
                for definition in task_definition.get("containerDefinitions") or []
                if definition.get("name") == "openclaw"
            ),
            {},
        )

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            chown_uid_1000_best_effort(temporary)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


async def _heartbeat(owner_id: str, update_id: str, claim_id: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_S)
            return
        except asyncio.TimeoutError:
            pass
        try:
            container, update = await _read_claimed(owner_id, update_id)
            if not await update_repo.heartbeat_owner_release(container, update, claim_id):
                return
        except (OwnerReleaseSuperseded, OwnerReleaseConflict):
            return
        except Exception:
            logger.exception("Owner release heartbeat failed (owner=%s update=%s)", owner_id, update_id)
            return


async def execute_claimed_owner_release(
    owner_id: str,
    update_id: str,
    claim_id: str,
    *,
    runtime=None,
) -> bool:
    runtime = runtime or _ReleaseRuntime()
    heartbeat_stop = asyncio.Event()
    heartbeat_task = (
        None
        if not isinstance(runtime, _ReleaseRuntime)
        else asyncio.create_task(_heartbeat(owner_id, update_id, claim_id, heartbeat_stop))
    )
    try:
        while True:
            container, update = await _read_claimed(owner_id, update_id)
            if update.get("apply_claim_id") != claim_id:
                raise OwnerReleaseSuperseded("release claim moved")
            release = release_by_id(update.get("target_release_id"))
            phase = update.get("phase")
            checkpoint = update_repo.checkpoint_owner_release
            if phase == "reserved":
                target = await runtime.prepare(owner_id, release)
                fields, next_phase, removals = {"target_task_definition_arn": target}, "prepared", ()
            elif phase == "prepared":
                pre_stop = [task["task_arn"] for task in await runtime.running_tasks(owner_id) if task.get("task_arn")]
                if not pre_stop:
                    raise OwnerReleaseValidationError("release requires a live founder task before stop")
                fields, next_phase, removals = (
                    {
                        "effect_kind": "stop_service",
                        "effect_claim_id": uuid.uuid4().hex,
                        "effect_executor_id": claim_id,
                        "pre_stop_task_arns": pre_stop,
                    },
                    "stop_requested",
                    (),
                )
            elif phase == "stop_requested":
                await runtime.stop(owner_id)
                fields, next_phase, removals = (
                    {"drain_evidence": await runtime.drain(owner_id, list(update["pre_stop_task_arns"]))},
                    "drained",
                    ("effect_kind", "effect_claim_id", "effect_executor_id"),
                )
            elif phase == "drained":
                prior_hash, staged_hash = await runtime.stage(
                    owner_id,
                    update_id,
                    release,
                    claim_id,
                    int(update["release_revision"]),
                )
                fields, next_phase, removals = (
                    {"prior_config_sha256": prior_hash, "staged_config_sha256": staged_hash},
                    "staged",
                    (),
                )
            elif phase == "staged":
                fields, next_phase, removals = (
                    {
                        "effect_kind": "activate_config",
                        "effect_claim_id": uuid.uuid4().hex,
                        "effect_executor_id": claim_id,
                    },
                    "activating",
                    (),
                )
            elif phase == "activating":
                await runtime.activate(
                    owner_id,
                    update_id,
                    update["prior_config_sha256"],
                    update["staged_config_sha256"],
                    release,
                    claim_id,
                    int(update["release_revision"]),
                )
                fields, next_phase, removals = (
                    {},
                    "activated",
                    (
                        "effect_kind",
                        "effect_claim_id",
                        "effect_executor_id",
                    ),
                )
            elif phase == "activated":
                fields, next_phase, removals = (
                    {
                        "effect_kind": "start_service",
                        "effect_claim_id": uuid.uuid4().hex,
                        "effect_executor_id": claim_id,
                    },
                    "start_requested",
                    (),
                )
            elif phase == "start_requested":
                await runtime.start(owner_id, update["target_task_definition_arn"], int(update["target_desired_count"]))
                fields, next_phase, removals = (
                    {},
                    "validating",
                    (
                        "effect_kind",
                        "effect_claim_id",
                        "effect_executor_id",
                    ),
                )
            elif phase == "validating" and "acceptance" not in update:
                fields, next_phase, removals = (
                    {
                        "acceptance": await runtime.accept(
                            owner_id,
                            update["target_task_definition_arn"],
                            update["staged_config_sha256"],
                            release,
                            container["gateway_token"],
                        )
                    },
                    "validating",
                    (),
                )
            elif phase == "validating":
                return await update_repo.finish_owner_release(
                    container,
                    update,
                    claim_id,
                    expected_phase="validating",
                )
            else:
                raise OwnerReleaseValidationError(f"unsupported owner release phase: {phase!r}")
            if not await checkpoint(
                container,
                update,
                claim_id,
                expected_phase=phase,
                next_phase=next_phase,
                fields=fields,
                remove_fields=removals,
            ):
                raise OwnerReleaseSuperseded("release checkpoint lost its claim")
    except OwnerReleaseSuperseded:
        return False
    except OwnerReleaseValidationError:
        logger.exception("Owner release validation stopped (owner=%s update=%s)", owner_id, update_id)
        return False
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            await heartbeat_task


async def apply_owner_release(owner_id: str, update_id: str) -> bool:
    update = await update_repo.get_by_id(owner_id, update_id, consistent=True)
    if not update or update.get("update_type") != "release" or update.get("status") not in {"pending", "scheduled"}:
        return False
    changes = update.get("changes")
    if not isinstance(changes, dict):
        return False
    release = release_by_id(changes.get("target_release_id"))
    runtime = _ReleaseRuntime()
    snapshot = await runtime.snapshot(owner_id)
    container = await container_repo.get_by_owner_id(owner_id, consistent=True)
    if (
        not container
        or snapshot.get("task_definition_arn") != container.get("task_definition_arn")
        or type(snapshot.get("desired_count")) is not int
        or snapshot["desired_count"] != 1
    ):
        return False
    claim_id = uuid.uuid4().hex
    if not await update_repo.reserve_owner_apply(
        container,
        update,
        claim_id,
        target_release_id=release.release_id,
        prior_task_definition_arn=snapshot["task_definition_arn"],
        prior_desired_count=snapshot["desired_count"],
        target_desired_count=snapshot["desired_count"],
    ):
        return False
    try:
        return await execute_claimed_owner_release(owner_id, update_id, claim_id, runtime=runtime)
    except Exception:
        logger.exception("Owner release remains reserved for recovery (owner=%s update=%s)", owner_id, update_id)
        return False


async def recover_stale_owner_releases() -> None:
    stale_before = int(time.time()) - _STALE_S
    rows = await update_repo.list_stale_applying("release", stale_before_epoch=stale_before)
    for stale_update in rows[:_MAX_RECOVERIES_PER_TICK]:
        owner_id = stale_update.get("owner_id")
        update_id = stale_update.get("update_id")
        if not owner_id or not update_id:
            continue
        container = await container_repo.get_by_owner_id(owner_id, consistent=True)
        update = await update_repo.get_by_id(owner_id, update_id, consistent=True)
        if not container or not update:
            continue
        claim_id = uuid.uuid4().hex
        if not await update_repo.takeover_owner_release(
            container,
            update,
            claim_id,
            stale_before_epoch=stale_before,
        ):
            continue
        try:
            await execute_claimed_owner_release(owner_id, update_id, claim_id)
        except Exception:
            logger.exception("Recovered owner release remains resumable (owner=%s update=%s)", owner_id, update_id)
