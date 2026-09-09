"""Recoverable, one-owner execution of an immutable OpenClaw release."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import time
import uuid
from datetime import datetime, timezone

from core.config import settings
from core.containers import get_ecs_manager
from core.containers.ecs_manager import SINGLE_WRITER_DEPLOYMENT_CONFIG
from core.repositories import container_repo, update_repo
from core.services import config_schema
from core.services.openclaw_release import (
    OpenClawRelease,
    OwnerReleaseConflict,
    owner_mutation_guard,
    release_by_id,
)

logger = logging.getLogger(__name__)

_HEARTBEAT_S = 30
_STALE_S = 3 * _HEARTBEAT_S
_MAX_RECOVERIES_PER_TICK = 1
_SAFE_OWNER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_LEGACY_SHARED_SCHEMA_VERSION = 15
_LEGACY_AGENT_SCHEMA_VERSION = 19
_EFFECT_FIELDS = ("effect_kind", "effect_claim_id", "effect_executor_id")


class OwnerReleaseSuperseded(RuntimeError):
    pass


class OwnerReleaseValidationError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _new_effect(kind: str, claim_id: str) -> dict[str, str]:
    return {"effect_kind": kind, "effect_claim_id": uuid.uuid4().hex, "effect_executor_id": claim_id}


def _release_filename(update_id: str, suffix: str) -> str:
    if not update_id or any(
        character not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-" for character in update_id
    ):
        raise OwnerReleaseValidationError("release update id is invalid")
    return f".openclaw.release-{update_id}.{suffix}.json"


class _OwnerConfigFiles:
    """Descriptor-relative config I/O that never follows a tenant symlink."""

    def __init__(self, owner_id: str):
        if not _SAFE_OWNER_ID.fullmatch(owner_id):
            raise OwnerReleaseValidationError("owner id is unsafe")
        self.owner_id = owner_id
        self.mount_fd = -1
        self.root_fd = -1

    def __enter__(self) -> "_OwnerConfigFiles":
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.mount_fd = os.open(settings.EFS_MOUNT_PATH, flags)
            self.root_fd = os.open(self.owner_id, flags, dir_fd=self.mount_fd)
            root = os.fstat(self.root_fd)
            if not stat.S_ISDIR(root.st_mode):
                raise OwnerReleaseValidationError("owner EFS root is not a directory")
        except (OSError, OwnerReleaseValidationError) as exc:
            self.close()
            if isinstance(exc, OwnerReleaseValidationError):
                raise
            raise OwnerReleaseValidationError("owner EFS root is unavailable") from exc
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1
        if self.mount_fd >= 0:
            os.close(self.mount_fd)
            self.mount_fd = -1

    def _lstat(self, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=self.root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise OwnerReleaseValidationError("owner config entry could not be inspected") from exc

    def read_regular(self, name: str) -> bytes:
        observed = self._lstat(name)
        if observed is None or not stat.S_ISREG(observed.st_mode):
            raise OwnerReleaseValidationError("owner config entry is not a regular file")
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=self.root_fd)
        except OSError as exc:
            raise OwnerReleaseValidationError("owner config entry could not be opened") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                observed.st_dev,
                observed.st_ino,
            ):
                raise OwnerReleaseValidationError("owner config entry changed while opening")
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(fd)
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise OwnerReleaseValidationError("owner config entry changed while reading")
            data = b"".join(chunks)
            if len(data) != after.st_size:
                raise OwnerReleaseValidationError("owner config entry read was incomplete")
            return data
        finally:
            os.close(fd)

    def atomic_write_regular(self, name: str, data: bytes) -> None:
        target = self._lstat(name)
        if target is not None and not stat.S_ISREG(target.st_mode):
            raise OwnerReleaseValidationError("owner config target is not a regular file")
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        fd = -1
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self.root_fd,
            )
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OwnerReleaseValidationError("owner config write was incomplete")
                view = view[written:]
            os.fsync(fd)
            try:
                os.fchown(fd, 1000, 1000)
            except OSError:
                logger.warning("Could not chown staged owner config", exc_info=True)
            written_stat = os.fstat(fd)
            if not stat.S_ISREG(written_stat.st_mode) or written_stat.st_size != len(data):
                raise OwnerReleaseValidationError("owner config staging file is invalid")
            os.close(fd)
            fd = -1
            current = self._lstat(name)
            if current is not None and not stat.S_ISREG(current.st_mode):
                raise OwnerReleaseValidationError("owner config target changed before replace")
            os.replace(temporary, name, src_dir_fd=self.root_fd, dst_dir_fd=self.root_fd)
            os.fsync(self.root_fd)
        except OSError as exc:
            raise OwnerReleaseValidationError("owner config could not be written safely") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.root_fd)
            except FileNotFoundError:
                pass

    def replace_regular(self, source: str, target: str) -> None:
        source_stat = self._lstat(source)
        target_stat = self._lstat(target)
        if source_stat is None or not stat.S_ISREG(source_stat.st_mode):
            raise OwnerReleaseValidationError("staged owner config is not a regular file")
        if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
            raise OwnerReleaseValidationError("live owner config is not a regular file")
        try:
            os.replace(source, target, src_dir_fd=self.root_fd, dst_dir_fd=self.root_fd)
            os.fsync(self.root_fd)
        except OSError as exc:
            raise OwnerReleaseValidationError("owner config activation failed") from exc


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


async def assert_current_effect(
    owner_id: str,
    update_id: str,
    claim_id: str,
    revision: int,
    *,
    phase: str,
    kind: str,
    effect_id: str,
) -> tuple[dict, dict]:
    """Strongly recheck the exact journal effect immediately before mutation."""

    container, update = await _read_claimed(owner_id, update_id)
    if (
        update.get("apply_claim_id") != claim_id
        or update.get("release_revision") != revision
        or update.get("phase") != phase
        or update.get("effect_kind") != kind
        or update.get("effect_claim_id") != effect_id
        or update.get("effect_executor_id") != claim_id
    ):
        raise OwnerReleaseSuperseded("release effect claim moved")
    if not await update_repo.heartbeat_owner_release(container, update, claim_id):
        raise OwnerReleaseSuperseded("release effect lease moved")
    return container, update


class _ReleaseRuntime:
    def __init__(self):
        self.ecs = get_ecs_manager()
        self._heartbeat_lost: asyncio.Event | None = None

    def bind_heartbeat_loss(self, lost: asyncio.Event) -> None:
        self._heartbeat_lost = lost

    async def assert_effect(self, *args, **kwargs) -> tuple[dict, dict]:
        if self._heartbeat_lost is not None and self._heartbeat_lost.is_set():
            raise OwnerReleaseSuperseded("release heartbeat was lost")
        return await assert_current_effect(*args, **kwargs)

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
            "service_arn": service.get("serviceArn"),
            "task_definition_arn": service.get("taskDefinition"),
            "desired_count": service.get("desiredCount"),
            "running_count": service.get("runningCount"),
            "pending_count": service.get("pendingCount"),
        }

    async def prepare(
        self,
        owner_id: str,
        update_id: str,
        release: OpenClawRelease,
        claim_id: str,
        revision: int,
        effect_id: str,
    ) -> str:
        container = await container_repo.get_by_owner_id(owner_id, consistent=True)
        if not container or not all(
            container.get(field) for field in ("hook_token", "gateway_token", "task_definition_arn")
        ):
            raise OwnerReleaseValidationError("owner container lacks immutable release prerequisites")
        size = await self.ecs.current_container_size(owner_id)
        if size is None:
            raise OwnerReleaseValidationError("owner task definition has no registered size")

        async def before_register() -> None:
            await self.assert_effect(
                owner_id,
                update_id,
                claim_id,
                revision,
                phase="prepare_requested",
                kind="prepare_task_definition",
                effect_id=effect_id,
            )

        task_definition_arn = await self.ecs.resize_user_container(
            owner_id,
            new_cpu=size[0],
            new_memory=size[1],
            new_image=release.digest_image,
            _register_only=True,
            _release_update_id=update_id,
            _effect_fence=before_register,
        )
        task_definition = await self._task_definition(task_definition_arn)
        if self._openclaw_definition(task_definition).get("image") != release.digest_image:
            raise OwnerReleaseValidationError("prepared task definition is not digest-pinned")
        return task_definition_arn

    async def service_tasks(self, owner_id: str, desired_status: str) -> list[dict]:
        if desired_status not in {"RUNNING", "STOPPED"}:
            raise ValueError("unsupported ECS desired status")
        arns: list[str] = []
        next_token = None
        while True:
            kwargs = {
                "cluster": self.ecs._cluster,
                "serviceName": self.ecs._service_name(owner_id),
                "desiredStatus": desired_status,
            }
            if next_token is not None:
                kwargs["nextToken"] = next_token
            page = await asyncio.to_thread(
                self.ecs._ecs.list_tasks,
                **kwargs,
            )
            arns.extend(page.get("taskArns") or [])
            next_token = page.get("nextToken")
            if not next_token:
                break
        if not arns:
            return []
        described = []
        for offset in range(0, len(arns), 100):
            response = await asyncio.to_thread(
                self.ecs._ecs.describe_tasks,
                cluster=self.ecs._cluster,
                tasks=arns[offset : offset + 100],
            )
            if response.get("failures"):
                raise OwnerReleaseValidationError("ECS returned failed listed-task observations")
            described.extend(response.get("tasks") or [])
        observed_arns = {task.get("taskArn") for task in described}
        if any(task_arn not in observed_arns for task_arn in arns):
            raise OwnerReleaseValidationError("ECS omitted a listed task observation")
        tasks = []
        for task in described:
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
                    "exit_code": openclaw.get("exitCode"),
                    "container_reason": openclaw.get("reason"),
                    "stop_code": task.get("stopCode"),
                    "stopped_reason": task.get("stoppedReason"),
                    "ip": address,
                    "created_at": (
                        task.get("createdAt").astimezone(timezone.utc).isoformat()
                        if isinstance(task.get("createdAt"), datetime)
                        else None
                    ),
                }
            )
        return tasks

    async def running_tasks(self, owner_id: str) -> list[dict]:
        return await self.service_tasks(owner_id, "RUNNING")

    async def _describe_tasks_exact(self, task_arns: list[str]) -> list[dict]:
        if not task_arns:
            raise OwnerReleaseValidationError("drain evidence has no captured task")
        described: list[dict] = []
        for offset in range(0, len(task_arns), 100):
            response = await asyncio.to_thread(
                self.ecs._ecs.describe_tasks,
                cluster=self.ecs._cluster,
                tasks=task_arns[offset : offset + 100],
            )
            if response.get("failures"):
                raise OwnerReleaseValidationError("ECS returned failed captured-task observations")
            described.extend(response.get("tasks") or [])
        observed = {task.get("taskArn"): task for task in described}
        if any(task_arn not in observed for task_arn in task_arns):
            raise OwnerReleaseValidationError("ECS omitted a captured task observation")
        return [observed[task_arn] for task_arn in task_arns]

    async def recover(
        self,
        owner_id: str,
        update_id: str,
        release: OpenClawRelease,
        source: dict,
        drain_evidence: dict,
        pre_stop_task_arns: list[str],
        captured_at: str,
        claim_id: str,
        revision: int,
        effect_id: str,
    ) -> dict:
        async def fence_effect() -> None:
            await self.assert_effect(
                owner_id,
                update_id,
                claim_id,
                revision,
                phase="recovery_requested",
                kind="create_recovery_set",
                effect_id=effect_id,
            )

        def fence_effect_sync() -> None:
            asyncio.run(fence_effect())

        await fence_effect()
        reobserved = await self.drain(owner_id, pre_stop_task_arns, captured_at)
        if {key: value for key, value in reobserved.items() if key != "observed_at"} != {
            key: value for key, value in drain_evidence.items() if key != "observed_at"
        }:
            raise OwnerReleaseValidationError("owner quiescence changed before recovery capture")
        from core.services.owner_recovery_set import (
            OwnerRecoverySetUnavailable,
            create_verified_owner_recovery_set,
        )

        try:
            return await create_verified_owner_recovery_set(
                owner_id=owner_id,
                update_id=update_id,
                release={
                    "release_id": release.release_id,
                    "image_digest": release.image_digest,
                    "build_id": release.build_id,
                    "schema_id": release.schema_id,
                },
                source=source,
                drain_evidence=drain_evidence,
                effect_fence=fence_effect,
                effect_fence_sync=fence_effect_sync,
            )
        except OwnerRecoverySetUnavailable as exc:
            raise OwnerReleaseValidationError(str(exc)) from exc

    async def stop(
        self,
        owner_id: str,
        update_id: str,
        claim_id: str,
        revision: int,
        effect_id: str,
    ) -> None:
        await self.assert_effect(
            owner_id,
            update_id,
            claim_id,
            revision,
            phase="stop_requested",
            kind="stop_service",
            effect_id=effect_id,
        )
        await asyncio.to_thread(
            self.ecs._ecs.update_service,
            cluster=self.ecs._cluster,
            service=self.ecs._service_name(owner_id),
            desiredCount=0,
        )

    async def drain(self, owner_id: str, pre_stop_task_arns: list[str], captured_at: str) -> dict:
        try:
            captured_after = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise OwnerReleaseValidationError("pre-stop capture time is invalid") from exc
        captured: set[str] = set(pre_stop_task_arns)
        for _ in range(60):
            service = await self.snapshot(owner_id)
            running = await self.service_tasks(owner_id, "RUNNING")
            stopped = await self.service_tasks(owner_id, "STOPPED")
            captured.update(task["task_arn"] for task in running if task.get("task_arn"))
            for task in stopped:
                created_at = task.get("created_at")
                if not task.get("task_arn") or not created_at:
                    raise OwnerReleaseValidationError("ECS stopped task identity is incomplete")
                try:
                    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise OwnerReleaseValidationError("ECS stopped task has an invalid creation time") from exc
                if created >= captured_after:
                    captured.add(task["task_arn"])
            if (
                service["desired_count"] == 0
                and service["running_count"] == 0
                and service["pending_count"] == 0
                and not running
            ):
                exact = await self._describe_tasks_exact(sorted(captured))
                if all(task.get("lastStatus") == "STOPPED" for task in exact):
                    stopped_tasks = []
                    for task in exact:
                        openclaw = next(
                            (
                                container
                                for container in task.get("containers") or []
                                if container.get("name") == "openclaw"
                            ),
                            {},
                        )
                        evidence = {
                            "task_arn": task.get("taskArn"),
                            "last_status": task.get("lastStatus"),
                            "task_definition_arn": task.get("taskDefinitionArn"),
                            "image_digest": openclaw.get("imageDigest"),
                            "exit_code": openclaw.get("exitCode"),
                            "stop_code": task.get("stopCode"),
                            "stopped_reason": task.get("stoppedReason"),
                        }
                        if any(evidence[field] is None for field in evidence):
                            raise OwnerReleaseValidationError("captured task drain evidence is incomplete")
                        stopped_tasks.append(evidence)
                    return {
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "service_arn": service["service_arn"],
                        "service_desired_count": 0,
                        "service_running_count": 0,
                        "service_pending_count": 0,
                        "stopped_tasks": stopped_tasks,
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
        effect_id: str,
    ) -> tuple[str, str]:
        try:
            with _OwnerConfigFiles(owner_id) as files:
                prior_bytes = files.read_regular("openclaw.json")
            prior_config = json.loads(prior_bytes)
        except (OwnerReleaseValidationError, ValueError) as exc:
            raise OwnerReleaseValidationError("owner live config is unavailable") from exc
        stage_name = _release_filename(update_id, "stage")
        staged_config = json.loads(json.dumps(prior_config))
        meta = staged_config.setdefault("meta", {})
        if not isinstance(meta, dict):
            raise OwnerReleaseValidationError("owner live config meta is invalid")
        meta["lastTouchedVersion"] = release.upstream_version
        staged_config = config_schema.prune_schema_rejected({}, staged_config, schema_id=release.schema_id)
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

        async def fence_effect() -> None:
            await self.assert_effect(
                owner_id,
                update_id,
                claim_id,
                revision,
                phase="stage_requested",
                kind="stage_config",
                effect_id=effect_id,
            )

        async with owner_mutation_guard(
            owner_id,
            release_update_id=update_id,
            effect_fence=fence_effect,
        ):
            await fence_effect()
            with _OwnerConfigFiles(owner_id) as files:
                files.atomic_write_regular(stage_name, staged_bytes)
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
        effect_id: str,
    ) -> None:
        stage_name = _release_filename(update_id, "stage")
        backup_name = _release_filename(update_id, "backup")

        async def fence_effect() -> None:
            await self.assert_effect(
                owner_id,
                update_id,
                claim_id,
                revision,
                phase="activating",
                kind="activate_config",
                effect_id=effect_id,
            )

        async with owner_mutation_guard(
            owner_id,
            release_update_id=update_id,
            effect_fence=fence_effect,
        ):
            await fence_effect()
            with _OwnerConfigFiles(owner_id) as files:
                live_bytes = files.read_regular("openclaw.json")
                if _sha256(live_bytes) == staged_sha256:
                    backup_bytes = files.read_regular(backup_name)
                    if _sha256(backup_bytes) != prior_sha256:
                        raise OwnerReleaseValidationError(
                            "activated release pre-upgrade config backup does not match checkpoint"
                        )
                    return
                if _sha256(live_bytes) != prior_sha256:
                    raise OwnerReleaseValidationError("live config changed after release reservation")
                staged_bytes = files.read_regular(stage_name)
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
                await fence_effect()
                files.atomic_write_regular(backup_name, live_bytes)
                if _sha256(files.read_regular(backup_name)) != prior_sha256:
                    raise OwnerReleaseValidationError("release backup could not be verified")
                await fence_effect()
                files.replace_regular(stage_name, "openclaw.json")

    async def start(
        self,
        owner_id: str,
        update_id: str,
        task_definition_arn: str,
        desired_count: int,
        claim_id: str,
        revision: int,
        effect_id: str,
    ) -> None:
        await self.assert_effect(
            owner_id,
            update_id,
            claim_id,
            revision,
            phase="start_requested",
            kind="start_service",
            effect_id=effect_id,
        )
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
        recovery_set: dict,
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
            "shared_db_schema_version": release.shared_db_schema_version,
            "agent_db_schema_version": release.agent_db_schema_version,
            "config_capabilities": sorted(release.config_capabilities),
            "qualification_id": release.qualification_id,
            "evidence_id": release.evidence_id,
            "runtime_capabilities": sorted(release.runtime_capabilities),
            "qualification_capabilities": sorted(release.qualification_capabilities),
            "task_definition_arn": task_definition_arn,
            "task_arn": task["task_arn"],
            "config_sha256": staged_config_sha256,
            "recovery_set_id": recovery_set["recovery_set_id"],
            "recovery_manifest_sha256": recovery_set["manifest_sha256"],
            "gateway": "accepted",
        }

    async def rollback(
        self,
        owner_id: str,
        update_id: str,
        update: dict,
        gateway_token: str,
        claim_id: str,
        revision: int,
        effect_id: str,
    ) -> dict:
        async def fence_effect() -> None:
            await self.assert_effect(
                owner_id,
                update_id,
                claim_id,
                revision,
                phase="rollback_requested",
                kind="rollback",
                effect_id=effect_id,
            )

        def fence_effect_sync() -> None:
            asyncio.run(fence_effect())

        rollback_captured_at = datetime.now(timezone.utc).isoformat()
        rollback_service = await self.snapshot(owner_id)
        target_tasks = await self.service_tasks(owner_id, "RUNNING")
        target_task_arns = sorted(task["task_arn"] for task in target_tasks if task.get("task_arn"))
        already_stopped = (
            rollback_service.get("desired_count") == 0
            and rollback_service.get("running_count") == 0
            and rollback_service.get("pending_count") == 0
            and not target_tasks
        )
        if already_stopped:
            rollback_drain = {
                "observed_at": rollback_captured_at,
                "service_arn": rollback_service.get("service_arn"),
                "already_stopped": True,
            }
        else:
            if not target_task_arns:
                raise OwnerReleaseValidationError("rollback target task identity is unavailable")
            await fence_effect()
            await asyncio.to_thread(
                self.ecs._ecs.update_service,
                cluster=self.ecs._cluster,
                service=self.ecs._service_name(owner_id),
                desiredCount=0,
            )
            rollback_drain = await self.drain(owner_id, target_task_arns, rollback_captured_at)
        from core.services.owner_recovery_set import (
            OwnerRecoverySetUnavailable,
            restore_verified_owner_recovery_set,
        )

        try:
            restored = await restore_verified_owner_recovery_set(
                owner_id=owner_id,
                update_id=update_id,
                recovery_set=update["recovery_set"],
                effect_fence=fence_effect,
                effect_fence_sync=fence_effect_sync,
            )
        except OwnerRecoverySetUnavailable as exc:
            raise OwnerReleaseValidationError(str(exc)) from exc
        await fence_effect()
        await asyncio.to_thread(
            self.ecs._ecs.update_service,
            cluster=self.ecs._cluster,
            service=self.ecs._service_name(owner_id),
            taskDefinition=update["prior_task_definition_arn"],
            desiredCount=int(update["prior_desired_count"]),
            deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
            **self.ecs._service_registries(),
        )
        waiter = self.ecs._ecs.get_waiter("services_stable")
        await asyncio.to_thread(
            waiter.wait,
            cluster=self.ecs._cluster,
            services=[self.ecs._service_name(owner_id)],
            WaiterConfig={"Delay": 15, "MaxAttempts": 60},
        )
        await fence_effect()
        service = await self.snapshot(owner_id)
        tasks = [task for task in await self.running_tasks(owner_id) if task.get("last_status") == "RUNNING"]
        source = update["source"]
        if (
            service.get("task_definition_arn") != update.get("prior_task_definition_arn")
            or service.get("desired_count") != update.get("prior_desired_count")
            or service.get("running_count") != update.get("prior_desired_count")
            or service.get("pending_count") != 0
            or len(tasks) != 1
            or tasks[0].get("task_definition_arn") != update.get("prior_task_definition_arn")
            or tasks[0].get("image_digest") != source.get("image_digest")
            or not tasks[0].get("ip")
        ):
            raise OwnerReleaseValidationError("rollback source task did not converge")
        from core.gateway.operator_rpc import gateway_hello_oneshot

        hello = await gateway_hello_oneshot(tasks[0]["ip"], owner_id=owner_id, token=gateway_token)
        if (hello.get("server") or {}).get("buildId") != source.get("build_id"):
            raise OwnerReleaseValidationError("rollback source gateway build did not match")
        return {
            **restored,
            "rollback_drain_evidence": rollback_drain,
            "source_task_definition_arn": update["prior_task_definition_arn"],
            "source_image_digest": source["image_digest"],
            "source_task_arn": tasks[0]["task_arn"],
            "source_build_id": source["build_id"],
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


async def _heartbeat(
    owner_id: str,
    update_id: str,
    claim_id: str,
    stop: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_S)
            return
        except asyncio.TimeoutError:
            pass
        try:
            container, update = await _read_claimed(owner_id, update_id)
            if not await update_repo.heartbeat_owner_release(container, update, claim_id):
                lost.set()
                return
        except (OwnerReleaseSuperseded, OwnerReleaseConflict):
            lost.set()
            return
        except Exception:
            logger.exception("Owner release heartbeat failed (owner=%s update=%s)", owner_id, update_id)
            lost.set()
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
    heartbeat_lost = asyncio.Event()
    heartbeat_task = (
        None
        if not isinstance(runtime, _ReleaseRuntime)
        else asyncio.create_task(_heartbeat(owner_id, update_id, claim_id, heartbeat_stop, heartbeat_lost))
    )
    if isinstance(runtime, _ReleaseRuntime):
        runtime.bind_heartbeat_loss(heartbeat_lost)
    try:
        while True:
            if heartbeat_lost.is_set():
                raise OwnerReleaseSuperseded("release heartbeat was lost")
            container, update = await _read_claimed(owner_id, update_id)
            if update.get("apply_claim_id") != claim_id:
                raise OwnerReleaseSuperseded("release claim moved")
            release = release_by_id(update.get("target_release_id"))
            phase = update.get("phase")
            checkpoint = update_repo.checkpoint_owner_release
            if phase == "reserved":
                captured_at = datetime.now(timezone.utc).isoformat()
                service = await runtime.snapshot(owner_id)
                source_tasks = [
                    task for task in await runtime.running_tasks(owner_id) if task.get("last_status") == "RUNNING"
                ]
                if len(source_tasks) != 1:
                    raise OwnerReleaseValidationError("release requires exactly one live founder task before stop")
                source_task = source_tasks[0]
                if (
                    service.get("task_definition_arn") != update.get("prior_task_definition_arn")
                    or service.get("desired_count") != update.get("prior_desired_count")
                    or service.get("running_count") != update.get("prior_desired_count")
                    or service.get("pending_count") != 0
                    or source_task.get("task_definition_arn") != update.get("prior_task_definition_arn")
                    or not source_task.get("task_arn")
                    or not source_task.get("image_digest")
                    or not source_task.get("ip")
                    or not service.get("service_arn")
                    or not container.get("access_point_id")
                    or not settings.EFS_FILE_SYSTEM_ID
                ):
                    raise OwnerReleaseValidationError("release source identity is incomplete")
                from core.gateway.operator_rpc import gateway_hello_oneshot

                source_hello = await gateway_hello_oneshot(
                    source_task["ip"], owner_id=owner_id, token=container["gateway_token"]
                )
                source_build_id = (source_hello.get("server") or {}).get("buildId")
                if type(source_build_id) is not str or not source_build_id:
                    raise OwnerReleaseValidationError("release source gateway build identity is unavailable")
                prior_release = (
                    release_by_id(update["prior_release_id"]) if update.get("prior_release_id") is not None else None
                )
                source = {
                    "service_arn": service["service_arn"],
                    "task_arn": source_task["task_arn"],
                    "task_definition_arn": source_task["task_definition_arn"],
                    "image_digest": source_task["image_digest"],
                    "build_id": source_build_id,
                    "access_point_id": container["access_point_id"],
                    "file_system_id": settings.EFS_FILE_SYSTEM_ID,
                    "efs_root": f"/users/{owner_id}",
                    "shared_schema_version": (
                        prior_release.shared_db_schema_version
                        if prior_release is not None
                        else _LEGACY_SHARED_SCHEMA_VERSION
                    ),
                    "agent_schema_version": (
                        prior_release.agent_db_schema_version
                        if prior_release is not None
                        else _LEGACY_AGENT_SCHEMA_VERSION
                    ),
                }
                fields, next_phase, removals = (
                    {
                        **_new_effect("stop_service", claim_id),
                        "pre_stop_task_arns": [source_task["task_arn"]],
                        "pre_stop_captured_at": captured_at,
                        "source": source,
                    },
                    "stop_requested",
                    (),
                )
            elif phase == "stop_requested":
                await runtime.stop(
                    owner_id,
                    update_id,
                    claim_id,
                    int(update["release_revision"]),
                    update["effect_claim_id"],
                )
                fields, next_phase, removals = (
                    {
                        "drain_evidence": await runtime.drain(
                            owner_id,
                            list(update["pre_stop_task_arns"]),
                            update["pre_stop_captured_at"],
                        )
                    },
                    "drained",
                    _EFFECT_FIELDS,
                )
            elif phase == "drained":
                fields, next_phase, removals = (
                    _new_effect("create_recovery_set", claim_id),
                    "recovery_requested",
                    (),
                )
            elif phase == "recovery_requested":
                recovery_set = await runtime.recover(
                    owner_id,
                    update_id,
                    release,
                    dict(update["source"]),
                    dict(update["drain_evidence"]),
                    list(update["pre_stop_task_arns"]),
                    update["pre_stop_captured_at"],
                    claim_id,
                    int(update["release_revision"]),
                    update["effect_claim_id"],
                )
                fields, next_phase, removals = (
                    {"recovery_set": recovery_set},
                    "recovered",
                    _EFFECT_FIELDS,
                )
            elif phase == "recovered":
                fields, next_phase, removals = (
                    _new_effect("prepare_task_definition", claim_id),
                    "prepare_requested",
                    (),
                )
            elif phase == "prepare_requested":
                target = await runtime.prepare(
                    owner_id,
                    update_id,
                    release,
                    claim_id,
                    int(update["release_revision"]),
                    update["effect_claim_id"],
                )
                fields, next_phase, removals = (
                    {"target_task_definition_arn": target},
                    "prepared",
                    _EFFECT_FIELDS,
                )
            elif phase == "prepared":
                fields, next_phase, removals = (
                    _new_effect("stage_config", claim_id),
                    "stage_requested",
                    (),
                )
            elif phase == "stage_requested":
                prior_hash, staged_hash = await runtime.stage(
                    owner_id,
                    update_id,
                    release,
                    claim_id,
                    int(update["release_revision"]),
                    update["effect_claim_id"],
                )
                fields, next_phase, removals = (
                    {"prior_config_sha256": prior_hash, "staged_config_sha256": staged_hash},
                    "staged",
                    _EFFECT_FIELDS,
                )
            elif phase == "staged":
                fields, next_phase, removals = (
                    _new_effect("activate_config", claim_id),
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
                    update["effect_claim_id"],
                )
                fields, next_phase, removals = (
                    {},
                    "activated",
                    _EFFECT_FIELDS,
                )
            elif phase == "activated":
                fields, next_phase, removals = (
                    _new_effect("start_service", claim_id),
                    "start_requested",
                    (),
                )
            elif phase == "start_requested":
                await runtime.start(
                    owner_id,
                    update_id,
                    update["target_task_definition_arn"],
                    int(update["target_desired_count"]),
                    claim_id,
                    int(update["release_revision"]),
                    update["effect_claim_id"],
                )
                fields, next_phase, removals = (
                    {},
                    "validating",
                    _EFFECT_FIELDS,
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
                            update["recovery_set"],
                        )
                    },
                    "validating",
                    (),
                )
            elif phase == "validating":
                if heartbeat_lost.is_set():
                    raise OwnerReleaseSuperseded("release heartbeat was lost")
                return await update_repo.finish_owner_release(
                    container,
                    update,
                    claim_id,
                    expected_phase="validating",
                )
            elif phase == "rollback_requested":
                evidence = await runtime.rollback(
                    owner_id,
                    update_id,
                    update,
                    container["gateway_token"],
                    claim_id,
                    int(update["release_revision"]),
                    update["effect_claim_id"],
                )
                if heartbeat_lost.is_set():
                    raise OwnerReleaseSuperseded("release heartbeat was lost")
                return await update_repo.finish_owner_release_rollback(
                    container,
                    update,
                    claim_id,
                    rollback_evidence=evidence,
                )
            else:
                raise OwnerReleaseValidationError(f"unsupported owner release phase: {phase!r}")
            if heartbeat_lost.is_set():
                raise OwnerReleaseSuperseded("release heartbeat was lost")
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
    except Exception as exc:
        logger.exception("Owner release effect failed (owner=%s update=%s)", owner_id, update_id)
        try:
            container, update = await _read_claimed(owner_id, update_id)
            if update.get("phase") == "rollback_requested":
                return False
            rollback_effect = uuid.uuid4().hex
            if not await update_repo.begin_owner_release_rollback(
                container,
                update,
                claim_id,
                effect_claim_id=rollback_effect,
                failure_reason=str(exc),
            ):
                return False
            heartbeat_stop.set()
            if heartbeat_task is not None:
                await heartbeat_task
                heartbeat_task = None
            return await execute_claimed_owner_release(owner_id, update_id, claim_id, runtime=runtime)
        except Exception:
            logger.exception("Owner release rollback could not be entered (owner=%s update=%s)", owner_id, update_id)
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
    in_flight = await update_repo.get_in_flight(owner_id)
    if any(
        row.get("update_id") != update_id and row.get("status") == "applying" and row.get("update_type") != "release"
        for row in in_flight
    ):
        return False
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
