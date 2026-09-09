"""Immutable OpenClaw releases and the shared owner-mutation fence."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator

from core.config import settings
from core.dynamodb import get_table
from core.repositories import container_repo
from core.services import config_schema
from core.services.efs_file_lock import get_file_lock, openclaw_config_recovery_guard

_REGISTRY_PATH = Path(__file__).with_name("openclaw_releases.json")
_ECR_PREFIX = "877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/openclaw-extended:"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")
_REQUIRED_QUALIFICATION_CAPABILITIES = frozenset({"legacy-privacy", "machine-coexistence", "native-human-auth"})


class OpenClawReleaseError(ValueError):
    pass


class OwnerReleaseConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenClawRelease:
    release_id: str
    image: str
    image_digest: str
    build_id: str
    upstream_version: str
    fork_tag: str
    fork_commit: str
    schema_id: str
    schema_sha256: str
    config_capabilities: frozenset[str]
    qualification_id: str
    evidence_id: str
    runtime_capabilities: frozenset[str]
    qualification_capabilities: frozenset[str]

    @property
    def digest_image(self) -> str:
        repository, _separator, _tag = self.image.rpartition(":")
        return f"{repository}@{self.image_digest}"


@dataclass(frozen=True)
class OwnerConfigContext:
    owner_id: str
    container_id: str
    release_revision: int
    active_release_id: str | None


def _required_string(row: dict, field: str) -> str:
    value = row.get(field)
    if type(value) is not str or not value.strip():
        raise OpenClawReleaseError(f"release registry field {field} is invalid")
    return value


def _parse_release(row: object) -> OpenClawRelease:
    if not isinstance(row, dict):
        raise OpenClawReleaseError("release registry row is invalid")
    release_id = _required_string(row, "release_id")
    image = _required_string(row, "image")
    image_digest = _required_string(row, "image_digest")
    build_id = _required_string(row, "build_id")
    upstream_version = _required_string(row, "upstream_version")
    fork_tag = _required_string(row, "fork_tag")
    fork_commit = _required_string(row, "fork_commit")
    schema_id = _required_string(row, "schema_id")
    schema_sha256 = _required_string(row, "schema_sha256")
    capabilities = row.get("config_capabilities")
    qualification_id = _required_string(row, "qualification_id")
    evidence_id = _required_string(row, "evidence_id")
    runtime_capabilities = row.get("runtime_capabilities")
    qualification_capabilities = row.get("qualification_capabilities")
    if (
        not _ID_RE.fullmatch(release_id)
        or not image.startswith(_ECR_PREFIX)
        or image.endswith(":latest")
        or "@" in image
        or not _DIGEST_RE.fullmatch(image_digest)
        or not _ID_RE.fullmatch(build_id)
        or not _COMMIT_RE.fullmatch(fork_commit)
        or not _DIGEST_RE.fullmatch(f"sha256:{schema_sha256}")
        or not isinstance(capabilities, list)
        or any(type(capability) is not str or not capability for capability in capabilities)
        or len(capabilities) != len(set(capabilities))
        or not _ID_RE.fullmatch(qualification_id)
        or not _DIGEST_RE.fullmatch(evidence_id)
        or not isinstance(runtime_capabilities, list)
        or not runtime_capabilities
        or any(type(capability) is not str or not capability for capability in runtime_capabilities)
        or len(runtime_capabilities) != len(set(runtime_capabilities))
        or not isinstance(qualification_capabilities, list)
        or any(type(capability) is not str or not capability for capability in qualification_capabilities)
        or len(qualification_capabilities) != len(set(qualification_capabilities))
        or not _REQUIRED_QUALIFICATION_CAPABILITIES <= set(qualification_capabilities)
    ):
        raise OpenClawReleaseError(f"release registry row {release_id!r} is not immutable and complete")
    try:
        if config_schema.schema_artifact_sha256(schema_id) != schema_sha256:
            raise OpenClawReleaseError(f"release registry row {release_id!r} has the wrong schema digest")
        # Validation errors for an empty document are expected; the call proves
        # the explicitly selected registered artifact loads.
        config_schema.schema_errors({}, schema_id=schema_id)
    except config_schema.ConfigSchemaError as exc:
        raise OpenClawReleaseError(f"release registry row {release_id!r} has no usable schema") from exc
    return OpenClawRelease(
        release_id=release_id,
        image=image,
        image_digest=image_digest,
        build_id=build_id,
        upstream_version=upstream_version,
        fork_tag=fork_tag,
        fork_commit=fork_commit,
        schema_id=schema_id,
        schema_sha256=schema_sha256,
        config_capabilities=frozenset(capabilities),
        qualification_id=qualification_id,
        evidence_id=evidence_id,
        runtime_capabilities=frozenset(runtime_capabilities),
        qualification_capabilities=frozenset(qualification_capabilities),
    )


@lru_cache(maxsize=1)
def _load_releases() -> dict[str, OpenClawRelease]:
    try:
        raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OpenClawReleaseError("OpenClaw release registry is unavailable") from exc
    rows = raw.get("releases") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise OpenClawReleaseError("OpenClaw release registry is invalid")
    releases: dict[str, OpenClawRelease] = {}
    for row in rows:
        release = _parse_release(row)
        if release.release_id in releases:
            raise OpenClawReleaseError("OpenClaw release registry contains a duplicate id")
        releases[release.release_id] = release
    return releases


def available_releases() -> tuple[OpenClawRelease, ...]:
    return tuple(_load_releases().values())


def release_by_id(release_id: str) -> OpenClawRelease:
    if type(release_id) is not str or not release_id.strip():
        raise OpenClawReleaseError("release_id is invalid")
    release = _load_releases().get(release_id)
    if release is None:
        raise OpenClawReleaseError("OpenClaw release is not registered")
    return release


def _release_ids(container: dict) -> tuple[str | None, str | None, str | None]:
    desired = container.get("desired_release_id")
    active = container.get("active_release_id")
    pointer = container.get("release_update_id")
    for field, value in (
        ("desired_release_id", desired),
        ("active_release_id", active),
        ("release_update_id", pointer),
    ):
        if value is not None and (type(value) is not str or not value.strip()):
            raise OpenClawReleaseError(f"{field} is invalid")
    if desired is None and active is None:
        return None, None, pointer
    if desired is None or (pointer is None and desired != active):
        raise OpenClawReleaseError("owner release assignment is inconsistent")
    return desired, active, pointer


def resolve_desired_release(container: dict) -> OpenClawRelease | None:
    desired, _active, _pointer = _release_ids(container)
    return release_by_id(desired) if desired is not None else None


def resolve_active_release(container: dict) -> OpenClawRelease | None:
    _desired, active, _pointer = _release_ids(container)
    return release_by_id(active) if active is not None else None


def registered_acceptance(
    release_id: str,
    acceptance: object,
    *,
    task_definition_arn: object,
    config_sha256: object,
) -> dict | None:
    """Return exact registry-derived acceptance evidence, never request claims."""

    if not isinstance(acceptance, dict):
        return None
    try:
        release = release_by_id(release_id)
    except (TypeError, ValueError):
        return None
    expected = {
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
        "config_sha256": config_sha256,
        "gateway": "accepted",
    }
    if set(acceptance) != set(expected) | {"task_arn"}:
        return None
    if any(acceptance.get(field) != value for field, value in expected.items()):
        return None
    if type(acceptance.get("task_arn")) is not str or not acceptance["task_arn"].strip():
        return None
    return acceptance


def stable_active_release_evidence(container: dict, update: dict) -> dict | None:
    """Resolve qualification only from a finished stable assignment."""

    active = container.get("active_release_id")
    if (
        type(active) is not str
        or not active
        or container.get("desired_release_id") != active
        or "release_update_id" in container
        or update.get("status") != "applied"
        or update.get("phase") != "healthy"
        or update.get("target_release_id") != active
        or container.get("release_revision") != update.get("release_revision")
        or container.get("task_definition_arn") != update.get("target_task_definition_arn")
    ):
        return None
    return registered_acceptance(
        active,
        update.get("acceptance"),
        task_definition_arn=update.get("target_task_definition_arn"),
        config_sha256=update.get("staged_config_sha256"),
    )


def _revision(row: dict) -> int:
    value = row.get("release_revision", 0)
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, Decimal) and value.is_finite() and value >= 0 and value == value.to_integral_value():
        return int(value)
    raise OwnerReleaseConflict("owner release revision is invalid")


def _context_from_row(owner_id: str, row: dict) -> OwnerConfigContext:
    if row.get("owner_id") != owner_id:
        raise OwnerReleaseConflict("owner container identity is unavailable")
    desired, active, pointer = _release_ids(row)
    if pointer is not None:
        raise OwnerReleaseConflict("owner release transaction is active")
    if desired != active:
        raise OwnerReleaseConflict("owner release assignment is not stable")
    container_id = row.get("id")
    if type(container_id) is not str or not container_id:
        raise OwnerReleaseConflict("owner container identity is unavailable")
    if active is not None:
        release_by_id(active)
    return OwnerConfigContext(
        owner_id=owner_id,
        container_id=container_id,
        release_revision=_revision(row),
        active_release_id=active,
    )


async def capture_config_context(owner_id: str) -> OwnerConfigContext:
    if type(owner_id) is not str or not owner_id:
        raise OwnerReleaseConflict("owner_id is invalid")
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    if not row:
        raise OwnerReleaseConflict("owner container is unavailable")
    return _context_from_row(owner_id, row)


async def capture_config_context_if_enabled(owner_id: str) -> OwnerConfigContext | None:
    """Capture a writer fence once an immutable release has been registered.

    The shipped empty registry keeps this foundation dormant until the build
    pipeline injects a reviewed image, digest, commit, and matching schema.
    """

    if not available_releases():
        return None
    return await capture_config_context(owner_id)


def assert_config_context_sync(context: OwnerConfigContext) -> None:
    response = get_table("containers").get_item(
        Key={"owner_id": context.owner_id},
        ConsistentRead=True,
    )
    row = response.get("Item")
    if not row or _context_from_row(context.owner_id, row) != context:
        raise OwnerReleaseConflict("owner config context moved before write")


def assert_release_claim_sync(
    owner_id: str,
    update_id: str,
    claim_id: str,
    release_revision: int,
) -> dict:
    """Recheck the durable release owner immediately before an EFS write."""

    response = get_table("containers").get_item(Key={"owner_id": owner_id}, ConsistentRead=True)
    row = response.get("Item")
    if (
        not row
        or row.get("release_update_id") != update_id
        or row.get("provision_claim_id") != claim_id
        or row.get("provision_attempt_token") != claim_id
        or _revision(row) != release_revision
    ):
        raise OwnerReleaseConflict("owner release claim moved before write")
    return row


def active_schema_id(context: OwnerConfigContext) -> str | None:
    if context.active_release_id is None:
        return None
    return release_by_id(context.active_release_id).schema_id


def _owner_paths(owner_id: str) -> tuple[str, str]:
    config_dir = str(Path(settings.EFS_MOUNT_PATH) / owner_id)
    return config_dir, str(Path(config_dir) / "openclaw.json.lock")


@asynccontextmanager
async def owner_mutation_guard(
    owner_id: str,
    *,
    release_update_id: str | None = None,
) -> AsyncIterator[dict]:
    """Serialize owner effects with config writes and release reservation."""

    config_dir, lock_path = _owner_paths(owner_id)
    local_lock = get_file_lock(lock_path)
    await asyncio.to_thread(local_lock.acquire)
    guard = openclaw_config_recovery_guard(config_dir, lock_path)
    entered = False
    try:
        await asyncio.to_thread(guard.__enter__)
        entered = True
        row = await container_repo.get_by_owner_id(owner_id, consistent=True)
        if not row:
            raise OwnerReleaseConflict("owner container is unavailable")
        pointer = row.get("release_update_id")
        if pointer != release_update_id:
            raise OwnerReleaseConflict("owner mutation is reserved by another release")
        yield row
    finally:
        if entered:
            await asyncio.to_thread(guard.__exit__, None, None, None)
        local_lock.release()
