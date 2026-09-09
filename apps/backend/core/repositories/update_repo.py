"""Pending-updates repository -- DynamoDB CRUD for the pending-updates table."""

import hashlib
import json
import re
import time
import uuid
from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("pending-updates")


_UNRESERVED_UPDATE = Attr("release_revision").not_exists() & (
    Attr("update_type").not_exists() | Attr("update_type").ne("release")
)
_ORDINARY_OWNER_FIELDS = (
    "ordinary_update_id",
    "ordinary_update_claim_id",
    "ordinary_update_claimed_at",
)

_OWNER_APPLY_CONTAINER_PINS = (
    "id",
    "status",
    "task_definition_arn",
    "desired_release_id",
    "active_release_id",
    "release_revision",
)
_OWNER_APPLY_CONTAINER_EXCLUSIONS = (
    "release_update_id",
    *_ORDINARY_OWNER_FIELDS,
    "provision_claim_id",
    "provision_attempt_token",
    "provision_execution_name",
)
_OWNER_APPLY_UPDATE_PINS = (
    "status",
    "update_type",
    "changes",
    "scheduled_at",
    "last_snoozed_at",
    "force_by",
)
_OWNER_APPLY_UPDATE_EXCLUSIONS = (
    "release_revision",
    "container_id",
    "apply_claim_id",
    "apply_claimed_at",
    "phase",
    "target_release_id",
    "prior_release_id",
    "prior_desired_release_id",
    "prior_task_definition_arn",
    "prior_desired_count",
    "target_desired_count",
    "target_task_definition_arn",
    "pre_stop_task_arns",
    "pre_stop_captured_at",
    "source",
    "drain_evidence",
    "recovery_set",
    "staged_config_sha256",
    "prior_config_sha256",
    "effect_kind",
    "effect_claim_id",
    "effect_executor_id",
    "acceptance",
    "applied_at",
    "failure_reason",
    "reserved_at",
    "prepare_requested_at",
    "prepared_at",
    "recovery_requested_at",
    "recovered_at",
    "stop_requested_at",
    "drained_at",
    "stage_requested_at",
    "staged_at",
    "activating_at",
    "activated_at",
    "start_requested_at",
    "validating_at",
    "rollback_requested_at",
    "rolled_back_at",
    "rollback_evidence",
)

_RELEASE_CHECKPOINT_PINS = (
    "status",
    "update_type",
    "container_id",
    "release_revision",
    "phase",
    "target_release_id",
    "apply_claim_id",
    "target_task_definition_arn",
    "prior_config_sha256",
    "staged_config_sha256",
    "effect_kind",
    "effect_claim_id",
    "effect_executor_id",
    "prior_task_definition_arn",
    "prior_release_id",
    "prior_desired_release_id",
    "prior_desired_count",
    "target_desired_count",
    "pre_stop_task_arns",
    "pre_stop_captured_at",
    "source",
    "recovery_set",
    "drain_evidence",
    "acceptance",
    "reserved_at",
    "prepare_requested_at",
    "prepared_at",
    "recovery_requested_at",
    "recovered_at",
    "stop_requested_at",
    "drained_at",
    "stage_requested_at",
    "staged_at",
    "activating_at",
    "activated_at",
    "start_requested_at",
    "validating_at",
    "rollback_requested_at",
    "rolled_back_at",
    "rollback_evidence",
    "failure_reason",
)
_RELEASE_CHECKPOINT_FIELDS = frozenset(
    {
        "target_task_definition_arn",
        "prior_config_sha256",
        "staged_config_sha256",
        "effect_kind",
        "effect_claim_id",
        "effect_executor_id",
        "pre_stop_task_arns",
        "pre_stop_captured_at",
        "source",
        "recovery_set",
        "drain_evidence",
        "acceptance",
        "failure_reason",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_EFFECT_FIELDS = frozenset({"effect_kind", "effect_claim_id", "effect_executor_id"})
_RELEASE_NEXT_PHASE = {
    "reserved": "stop_requested",
    "stop_requested": "drained",
    "drained": "recovery_requested",
    "recovery_requested": "recovered",
    "recovered": "prepare_requested",
    "prepare_requested": "prepared",
    "prepared": "stage_requested",
    "stage_requested": "staged",
    "staged": "activating",
    "activating": "activated",
    "activated": "start_requested",
    "start_requested": "validating",
}
_RELEASE_PHASE_ORDER = (
    "reserved",
    "stop_requested",
    "drained",
    "recovery_requested",
    "recovered",
    "prepare_requested",
    "prepared",
    "stage_requested",
    "staged",
    "activating",
    "activated",
    "start_requested",
    "validating",
)


def _require_nonempty_string(field: str, value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} is invalid")
    return value


def _release_revision(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, Decimal) and value.is_finite() and value >= 0 and value == value.to_integral_value():
        return int(value)
    raise ValueError("release_revision is invalid")


def _nonempty_string(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _nonnegative_integer(value: object) -> int | None:
    try:
        return _release_revision(value)
    except ValueError:
        return None


def _positive_integer(value: object) -> bool:
    normalized = _nonnegative_integer(value)
    return normalized is not None and normalized > 0


def _valid_task_arns(value: object) -> bool:
    return (
        isinstance(value, list)
        and 0 < len(value) <= 100
        and all(_nonempty_string(item) for item in value)
        and len(value) == len(set(value))
    )


_STOPPED_TASK_FIELDS = {
    "task_arn",
    "last_status",
    "task_definition_arn",
    "image_digest",
    "exit_code",
    "stop_code",
    "stopped_reason",
}


def _valid_stopped_tasks(value: object, *, require_clean_exit: bool) -> bool:
    if not isinstance(value, list) or not value:
        return False
    task_arns = [task.get("task_arn") for task in value if isinstance(task, dict)]
    return (
        all(_nonempty_string(task_arn) for task_arn in task_arns)
        and len(task_arns) == len(value) == len(set(task_arns))
        and all(
            set(task) == _STOPPED_TASK_FIELDS
            and task.get("last_status") == "STOPPED"
            and _nonempty_string(task.get("task_definition_arn"))
            and _nonempty_string(task.get("image_digest"))
            and (task.get("exit_code") == 0 if require_clean_exit else type(task.get("exit_code")) is int)
            and (
                task.get("stop_code") in {"UserInitiated", "ServiceSchedulerInitiated"}
                if require_clean_exit
                else _nonempty_string(task.get("stop_code"))
            )
            and _nonempty_string(task.get("stopped_reason"))
            for task in value
        )
    )


def _valid_drain_evidence(update: dict) -> bool:
    evidence = update.get("drain_evidence")
    pre_stop = update.get("pre_stop_task_arns")
    source = update.get("source")
    stopped = evidence.get("stopped_tasks") if isinstance(evidence, dict) else None
    return (
        _valid_task_arns(pre_stop)
        and isinstance(source, dict)
        and isinstance(evidence, dict)
        and set(evidence)
        == {
            "observed_at",
            "service_arn",
            "service_desired_count",
            "service_running_count",
            "service_pending_count",
            "stopped_tasks",
        }
        and _nonempty_string(evidence.get("observed_at"))
        and evidence.get("service_arn") == source.get("service_arn")
        and _nonnegative_integer(evidence.get("service_desired_count")) == 0
        and _nonnegative_integer(evidence.get("service_running_count")) == 0
        and _nonnegative_integer(evidence.get("service_pending_count")) == 0
        and _valid_stopped_tasks(stopped, require_clean_exit=True)
        and set(pre_stop) <= {task.get("task_arn") for task in stopped}
        and all(
            task.get("task_definition_arn") == source.get("task_definition_arn")
            and task.get("image_digest") == source.get("image_digest")
            for task in stopped
        )
    )


def _valid_source(update: dict) -> bool:
    source = update.get("source")
    return (
        isinstance(source, dict)
        and set(source)
        == {
            "service_arn",
            "task_arn",
            "task_definition_arn",
            "image_digest",
            "build_id",
            "access_point_id",
            "file_system_id",
            "efs_root",
            "shared_schema_version",
            "agent_schema_version",
        }
        and all(
            _nonempty_string(source.get(field))
            for field in (
                "service_arn",
                "task_arn",
                "task_definition_arn",
                "image_digest",
                "build_id",
                "access_point_id",
                "file_system_id",
                "efs_root",
            )
        )
        and _positive_integer(source.get("shared_schema_version"))
        and _positive_integer(source.get("agent_schema_version"))
        and source.get("task_definition_arn") == update.get("prior_task_definition_arn")
        and source.get("task_arn")
        in (update.get("pre_stop_task_arns") if isinstance(update.get("pre_stop_task_arns"), list) else [])
        and _nonempty_string(update.get("pre_stop_captured_at"))
    )


def _valid_rollback_drain(value: object, update: dict) -> bool:
    if not isinstance(value, dict):
        return False
    source = update.get("source")
    if not isinstance(source, dict) or value.get("service_arn") != source.get("service_arn"):
        return False
    if value.get("already_stopped") is True:
        return set(value) == {"observed_at", "service_arn", "already_stopped"} and _nonempty_string(
            value.get("observed_at")
        )
    stopped = value.get("stopped_tasks")
    return (
        set(value)
        == {
            "observed_at",
            "service_arn",
            "service_desired_count",
            "service_running_count",
            "service_pending_count",
            "stopped_tasks",
        }
        and _nonempty_string(value.get("observed_at"))
        and _nonnegative_integer(value.get("service_desired_count")) == 0
        and _nonnegative_integer(value.get("service_running_count")) == 0
        and _nonnegative_integer(value.get("service_pending_count")) == 0
        and _valid_stopped_tasks(stopped, require_clean_exit=False)
    )


def _valid_recovery_set(value: object, update: dict) -> bool:
    if not isinstance(value, dict):
        return False
    agents = value.get("agent_db_generations")
    source = update.get("source")
    if not isinstance(source, dict):
        return False
    expected_fields = {
        "recovery_set_id",
        "owner_id",
        "update_id",
        "status",
        "manifest_bucket",
        "manifest_key",
        "manifest_version_id",
        "manifest_sha256",
        "release_id",
        "release_image_digest",
        "source_service_arn",
        "source_task_arn",
        "source_task_definition_arn",
        "source_image_digest",
        "source_build_id",
        "access_point_id",
        "file_system_id",
        "efs_root",
        "drain_observed_at",
        "drain_evidence_sha256",
        "stable_replica_inventory_sha256",
        "shared_db_generation",
        "agent_db_generations",
        "efs_archive_key",
        "efs_archive_version_id",
        "efs_archive_etag",
        "efs_archive_size",
        "efs_archive_sha256",
        "efs_tree_manifest_sha256",
        "provider",
        "tools",
        "verified_at",
    }
    drain_digest = hashlib.sha256(
        json.dumps(update.get("drain_evidence"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    try:
        from core.services.openclaw_release import release_by_id

        target = release_by_id(update.get("target_release_id"))
    except (TypeError, ValueError):
        return False
    return (
        set(value) == expected_fields
        and _nonempty_string(value.get("recovery_set_id"))
        and value.get("owner_id") == update.get("owner_id")
        and value.get("update_id") == update.get("update_id")
        and value.get("status") == "verified"
        and _nonempty_string(value.get("manifest_bucket"))
        and _nonempty_string(value.get("manifest_key"))
        and _nonempty_string(value.get("manifest_version_id"))
        and _nonempty_string(value.get("manifest_sha256"))
        and _SHA256_RE.fullmatch(value["manifest_sha256"]) is not None
        and value.get("release_id") == update.get("target_release_id")
        and value.get("release_image_digest") == target.image_digest
        and value.get("source_service_arn") == source.get("service_arn")
        and value.get("source_task_arn") == source.get("task_arn")
        and value.get("source_task_definition_arn") == source.get("task_definition_arn")
        and value.get("source_image_digest") == source.get("image_digest")
        and value.get("source_build_id") == source.get("build_id")
        and value.get("access_point_id") == source.get("access_point_id")
        and value.get("file_system_id") == source.get("file_system_id")
        and value.get("efs_root") == source.get("efs_root")
        and value.get("drain_observed_at") == update.get("drain_evidence", {}).get("observed_at")
        and value.get("drain_evidence_sha256") == drain_digest
        and _nonempty_string(value.get("stable_replica_inventory_sha256"))
        and _SHA256_RE.fullmatch(value["stable_replica_inventory_sha256"]) is not None
        and _nonempty_string(value.get("shared_db_generation"))
        and _SHA256_RE.fullmatch(value["shared_db_generation"]) is not None
        and isinstance(agents, dict)
        and all(
            _nonempty_string(agent) and _nonempty_string(generation) and _SHA256_RE.fullmatch(generation) is not None
            for agent, generation in agents.items()
        )
        and _nonempty_string(value.get("efs_archive_key"))
        and _nonempty_string(value.get("efs_archive_version_id"))
        and _nonempty_string(value.get("efs_archive_etag"))
        and _positive_integer(value.get("efs_archive_size"))
        and _nonempty_string(value.get("efs_archive_sha256"))
        and _SHA256_RE.fullmatch(value["efs_archive_sha256"]) is not None
        and _nonempty_string(value.get("efs_tree_manifest_sha256"))
        and _SHA256_RE.fullmatch(value["efs_tree_manifest_sha256"]) is not None
        and value.get("provider") == {"name": "lightbulb-owner-recovery-set", "version": "1"}
        and isinstance(value.get("tools"), dict)
        and _nonempty_string(value.get("verified_at"))
    )


def _valid_effect(update: dict, kind: str) -> bool:
    return (
        update.get("effect_kind") == kind
        and _nonempty_string(update.get("effect_claim_id"))
        and _nonempty_string(update.get("effect_executor_id"))
    )


def _has_no_effect(update: dict) -> bool:
    return not any(field in update for field in _RELEASE_EFFECT_FIELDS)


def _valid_release_phase_evidence(phase: str, update: dict) -> bool:
    try:
        _release_revision(update.get("release_revision"))
    except ValueError:
        return False
    base = (
        update.get("update_type") == "release"
        and update.get("status") == "applying"
        and _nonempty_string(update.get("container_id"))
        and _nonempty_string(update.get("target_release_id"))
        and _nonempty_string(update.get("prior_task_definition_arn"))
        and _nonnegative_integer(update.get("prior_desired_count")) is not None
        and _nonnegative_integer(update.get("target_desired_count")) is not None
    )
    if not base or phase not in _RELEASE_PHASE_ORDER:
        if phase == "rollback_requested":
            return (
                _valid_source(update)
                and _valid_drain_evidence(update)
                and _valid_recovery_set(update.get("recovery_set"), update)
                and _valid_effect(update, "rollback")
                and _nonempty_string(update.get("rollback_requested_at"))
                and _nonempty_string(update.get("failure_reason"))
            )
        return False
    phase_index = _RELEASE_PHASE_ORDER.index(phase)
    if any(
        not _nonempty_string(update.get(f"{milestone}_at")) for milestone in _RELEASE_PHASE_ORDER[: phase_index + 1]
    ):
        return False
    if phase == "reserved":
        return _has_no_effect(update)
    if phase == "stop_requested":
        return _valid_source(update) and _valid_effect(update, "stop_service")
    if not _valid_drain_evidence(update):
        return False
    if phase == "drained":
        return _has_no_effect(update)
    if phase == "recovery_requested":
        return _valid_effect(update, "create_recovery_set")
    if phase == "recovered":
        return _valid_recovery_set(update.get("recovery_set"), update) and _has_no_effect(update)
    if not _valid_recovery_set(update.get("recovery_set"), update):
        return False
    if phase == "prepare_requested":
        return _valid_effect(update, "prepare_task_definition")
    if not _nonempty_string(update.get("target_task_definition_arn")):
        return False
    if phase == "prepared":
        return _has_no_effect(update)
    if phase == "stage_requested":
        return _valid_effect(update, "stage_config")
    if not (
        _nonempty_string(update.get("prior_config_sha256"))
        and _SHA256_RE.fullmatch(update["prior_config_sha256"])
        and _nonempty_string(update.get("staged_config_sha256"))
        and _SHA256_RE.fullmatch(update["staged_config_sha256"])
    ):
        return False
    if phase == "staged":
        return _has_no_effect(update)
    if phase == "activating":
        return _valid_effect(update, "activate_config")
    if phase == "activated":
        return _has_no_effect(update)
    if phase == "start_requested":
        return _positive_integer(update.get("target_desired_count")) and _valid_effect(update, "start_service")
    if phase == "validating":
        return _positive_integer(update.get("target_desired_count")) and _has_no_effect(update)
    return False


def _registered_acceptance(update: dict, acceptance: object) -> dict | None:
    try:
        from core.services.openclaw_release import registered_acceptance

        return registered_acceptance(
            update.get("target_release_id"),
            acceptance,
            task_definition_arn=update.get("target_task_definition_arn"),
            config_sha256=update.get("staged_config_sha256"),
            recovery_set=update.get("recovery_set"),
        )
    except TypeError:
        return None


def _valid_release_transition(
    expected_update: dict,
    expected_phase: str,
    next_phase: str,
    fields: dict,
    remove_fields: tuple[str, ...],
) -> bool:
    removals = set(remove_fields)
    if expected_phase == next_phase:
        if not fields and not removals:
            return _valid_release_phase_evidence(expected_phase, expected_update)
        if expected_phase != "validating" or set(fields) != {"acceptance"} or removals:
            return False
    elif _RELEASE_NEXT_PHASE.get(expected_phase) != next_phase:
        return False
    else:
        required_fields: dict[tuple[str, str], set[str]] = {
            ("reserved", "stop_requested"): set(_RELEASE_EFFECT_FIELDS)
            | {"pre_stop_task_arns", "pre_stop_captured_at", "source"},
            ("stop_requested", "drained"): {"drain_evidence"},
            ("drained", "recovery_requested"): set(_RELEASE_EFFECT_FIELDS),
            ("recovery_requested", "recovered"): {"recovery_set"},
            ("recovered", "prepare_requested"): set(_RELEASE_EFFECT_FIELDS),
            ("prepare_requested", "prepared"): {"target_task_definition_arn"},
            ("prepared", "stage_requested"): set(_RELEASE_EFFECT_FIELDS),
            ("stage_requested", "staged"): {"prior_config_sha256", "staged_config_sha256"},
            ("staged", "activating"): set(_RELEASE_EFFECT_FIELDS),
            ("activating", "activated"): set(),
            ("activated", "start_requested"): set(_RELEASE_EFFECT_FIELDS),
            ("start_requested", "validating"): set(),
        }
        required_removals = (
            set(_RELEASE_EFFECT_FIELDS)
            if (expected_phase, next_phase)
            in {
                ("prepare_requested", "prepared"),
                ("recovery_requested", "recovered"),
                ("stop_requested", "drained"),
                ("stage_requested", "staged"),
                ("activating", "activated"),
                ("start_requested", "validating"),
            }
            else set()
        )
        if set(fields) != required_fields[(expected_phase, next_phase)] or removals != required_removals:
            return False

    candidate = {**expected_update, **fields, "phase": next_phase}
    if next_phase != expected_phase:
        candidate[f"{next_phase}_at"] = "checkpoint"
    for field in removals:
        candidate.pop(field, None)
    if not _valid_release_phase_evidence(next_phase, candidate):
        return False
    if "acceptance" in fields and _registered_acceptance(candidate, fields["acceptance"]) is None:
        return False
    return True


def _snapshot_conditions(
    snapshot: dict,
    pinned_fields: tuple[str, ...],
    exclusion_fields: tuple[str, ...],
    prefix: str,
    names: dict,
    values: dict,
) -> list[str]:
    conditions: list[str] = []
    for index, field in enumerate(pinned_fields):
        name = f"#{prefix}p{index}"
        names[name] = field
        if field in snapshot:
            value = f":{prefix}p{index}"
            values[value] = snapshot[field]
            conditions.append(f"{name} = {value}")
        else:
            conditions.append(f"attribute_not_exists({name})")
    for index, field in enumerate(exclusion_fields):
        name = f"#{prefix}x{index}"
        names[name] = field
        conditions.append(f"attribute_not_exists({name})")
    return conditions


def _is_conditional_transaction_cancel(error: ClientError) -> bool:
    if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return False
    reasons = error.response.get("CancellationReasons")
    if not isinstance(reasons, list) or len(reasons) != 2:
        return False
    codes: list[str | None] = []
    for reason in reasons:
        if not isinstance(reason, dict) or "Code" not in reason:
            return False
        code = reason["Code"]
        if code not in ("ConditionalCheckFailed", "None", None):
            return False
        codes.append(code)
    return "ConditionalCheckFailed" in codes


def _generate_ulid_like() -> str:
    """Generate a ULID-like ID: timestamp prefix + random suffix for sortability.

    Uses millisecond timestamp (base32-ish hex) + uuid4 hex to approximate
    ULID ordering without requiring the python-ulid dependency.
    """
    ts_ms = int(time.time() * 1000)
    ts_hex = f"{ts_ms:012x}"
    rand_hex = uuid.uuid4().hex[:20]
    return f"{ts_hex}{rand_hex}"


async def create(
    owner_id: str,
    update_type: str,
    description: str,
    changes: dict,
    force_by: str | None = None,
    scheduled_at: str | None = None,
) -> dict:
    """Create a pending update, or an atomically scheduled update when set."""
    table = _get_table()
    now = utc_now_iso()
    update_id = _generate_ulid_like()
    item: dict = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": update_type,
        "description": description,
        "changes": changes,
        "status": "scheduled" if scheduled_at is not None else "pending",
        "created_at": now,
        "updated_at": now,
    }
    if force_by is not None:
        item["force_by"] = force_by
    if scheduled_at is not None:
        item["scheduled_at"] = scheduled_at
    await run_in_thread(table.put_item, Item=item)
    return item


async def get_by_id(owner_id: str, update_id: str, *, consistent: bool = False) -> dict | None:
    """Read one update, optionally strongly consistently."""

    _require_nonempty_string("owner_id", owner_id)
    _require_nonempty_string("update_id", update_id)
    table = _get_table()
    response = await run_in_thread(
        table.get_item,
        Key={"owner_id": owner_id, "update_id": update_id},
        ConsistentRead=consistent,
    )
    return response.get("Item")


async def reserve_owner_apply(
    expected_container: dict,
    expected_update: dict,
    claim_id: str,
    *,
    target_release_id: str | None,
    prior_task_definition_arn: str,
    prior_desired_count: int,
    target_desired_count: int,
) -> bool:
    """Atomically reserve an owner row and its queued update for one apply."""

    if not isinstance(expected_container, dict):
        raise ValueError("expected_container is invalid")
    if not isinstance(expected_update, dict):
        raise ValueError("expected_update is invalid")

    owner_id = _require_nonempty_string("owner_id", expected_container.get("owner_id"))
    container_id = _require_nonempty_string("id", expected_container.get("id"))
    _require_nonempty_string("status", expected_container.get("status"))
    update_owner_id = _require_nonempty_string("owner_id", expected_update.get("owner_id"))
    if update_owner_id != owner_id:
        raise ValueError("owner_id is invalid")
    update_id = _require_nonempty_string("update_id", expected_update.get("update_id"))
    update_type = _require_nonempty_string("update_type", expected_update.get("update_type"))
    update_status = _require_nonempty_string("status", expected_update.get("status"))
    _require_nonempty_string("claim_id", claim_id)
    _require_nonempty_string("prior_task_definition_arn", prior_task_definition_arn)

    changes = expected_update.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("changes is invalid")
    for field, count in (
        ("prior_desired_count", prior_desired_count),
        ("target_desired_count", target_desired_count),
    ):
        if type(count) is not int or count < 0:
            raise ValueError(f"{field} is invalid")

    revision = _release_revision(expected_container.get("release_revision", 0))

    desired_present = "desired_release_id" in expected_container
    active_present = "active_release_id" in expected_container
    if desired_present:
        _require_nonempty_string("desired_release_id", expected_container["desired_release_id"])
    if active_present:
        _require_nonempty_string("active_release_id", expected_container["active_release_id"])
    if desired_present != active_present or (
        desired_present and expected_container["desired_release_id"] != expected_container["active_release_id"]
    ):
        raise ValueError("desired_release_id and active_release_id are invalid")

    if target_release_id is not None:
        _require_nonempty_string("target_release_id", target_release_id)
    if update_type == "release":
        if target_release_id is None or changes.get("target_release_id") != target_release_id:
            raise ValueError("target_release_id is invalid")
    elif target_release_id is not None:
        raise ValueError("target_release_id is invalid")

    if any(field in expected_container for field in _OWNER_APPLY_CONTAINER_EXCLUSIONS):
        return False
    if update_status not in {"pending", "scheduled"}:
        return False
    if any(field in expected_update for field in _OWNER_APPLY_UPDATE_EXCLUSIONS):
        return False

    containers = get_table("containers")
    updates = _get_table()
    now_epoch = int(time.time())
    now_iso = utc_now_iso()
    next_revision = revision + 1

    container_names: dict = {}
    container_values: dict = {}
    container_conditions = _snapshot_conditions(
        expected_container,
        _OWNER_APPLY_CONTAINER_PINS,
        _OWNER_APPLY_CONTAINER_EXCLUSIONS,
        "cp",
        container_names,
        container_values,
    )
    container_set = {
        "release_revision": next_revision,
        "release_update_id": update_id,
        "provision_claim_id": claim_id,
        "provision_attempt_token": claim_id,
        "provision_claimed_at": now_epoch,
        "updated_at": now_iso,
    }
    if update_type == "release":
        container_set["desired_release_id"] = target_release_id
    container_updates: list[str] = []
    for index, (field, value) in enumerate(container_set.items()):
        name, binding = f"#cs{index}", f":cs{index}"
        container_names[name] = field
        container_values[binding] = value
        container_updates.append(f"{name} = {binding}")

    update_names: dict = {}
    update_values: dict = {}
    update_conditions = _snapshot_conditions(
        expected_update,
        _OWNER_APPLY_UPDATE_PINS,
        _OWNER_APPLY_UPDATE_EXCLUSIONS,
        "up",
        update_names,
        update_values,
    )
    update_set = {
        "status": "applying",
        "phase": "reserved",
        "reserved_at": now_iso,
        "container_id": container_id,
        "apply_claim_id": claim_id,
        "apply_claimed_at": now_epoch,
        "updated_at": now_iso,
        "release_revision": next_revision,
        "prior_task_definition_arn": prior_task_definition_arn,
        "prior_desired_count": prior_desired_count,
        "target_desired_count": target_desired_count,
    }
    if update_type == "release":
        update_set["target_release_id"] = target_release_id
    if active_present:
        update_set["prior_release_id"] = expected_container["active_release_id"]
    if desired_present:
        update_set["prior_desired_release_id"] = expected_container["desired_release_id"]
    update_parts: list[str] = []
    for index, (field, value) in enumerate(update_set.items()):
        name, binding = f"#us{index}", f":us{index}"
        update_names[name] = field
        update_values[binding] = value
        update_parts.append(f"{name} = {binding}")
    update_names["#ur"] = "ttl"

    try:
        await run_in_thread(
            containers.meta.client.transact_write_items,
            TransactItems=[
                {
                    "Update": {
                        "TableName": containers.name,
                        "Key": {"owner_id": owner_id},
                        "ConditionExpression": " AND ".join(container_conditions),
                        "UpdateExpression": "SET " + ", ".join(container_updates),
                        "ExpressionAttributeNames": container_names,
                        "ExpressionAttributeValues": container_values,
                    }
                },
                {
                    "Update": {
                        "TableName": updates.name,
                        "Key": {"owner_id": owner_id, "update_id": update_id},
                        "ConditionExpression": " AND ".join(update_conditions),
                        "UpdateExpression": "SET " + ", ".join(update_parts) + " REMOVE #ur",
                        "ExpressionAttributeNames": update_names,
                        "ExpressionAttributeValues": update_values,
                    }
                },
            ],
        )
    except ClientError as error:
        if _is_conditional_transaction_cancel(error):
            return False
        raise
    return True


def _release_transaction_conditions(expected_container: dict, expected_update: dict, claim_id: str):
    owner_id = _require_nonempty_string("owner_id", expected_container.get("owner_id"))
    container_id = _require_nonempty_string("id", expected_container.get("id"))
    update_id = _require_nonempty_string("update_id", expected_update.get("update_id"))
    if expected_update.get("owner_id") != owner_id or expected_update.get("container_id") != container_id:
        raise ValueError("release transaction identity is invalid")
    _require_nonempty_string("claim_id", claim_id)
    if expected_update.get("update_type") != "release" or expected_update.get("status") != "applying":
        raise ValueError("release transaction state is invalid")
    revision = expected_update.get("release_revision")
    if expected_container.get("release_revision") != revision:
        raise ValueError("release transaction revision is invalid")

    container_names: dict = {}
    container_values: dict = {}
    container_pins = (
        "id",
        "release_update_id",
        "release_revision",
        "desired_release_id",
        "active_release_id",
        "provision_claim_id",
        "provision_attempt_token",
    )
    container_conditions = _snapshot_conditions(
        expected_container,
        container_pins,
        _ORDINARY_OWNER_FIELDS,
        "rc",
        container_names,
        container_values,
    )
    update_names: dict = {}
    update_values: dict = {}
    update_conditions = _snapshot_conditions(
        expected_update, _RELEASE_CHECKPOINT_PINS, (), "ru", update_names, update_values
    )
    if expected_container.get("release_update_id") != update_id:
        raise ValueError("release transaction pointer is invalid")
    if (
        expected_container.get("provision_claim_id") != claim_id
        or expected_container.get("provision_attempt_token") != claim_id
        or expected_update.get("apply_claim_id") != claim_id
    ):
        return None
    return (
        owner_id,
        update_id,
        container_names,
        container_values,
        container_conditions,
        update_names,
        update_values,
        update_conditions,
    )


async def checkpoint_owner_release(
    expected_container: dict,
    expected_update: dict,
    claim_id: str,
    *,
    expected_phase: str,
    next_phase: str,
    fields: dict | None = None,
    remove_fields: tuple[str, ...] = (),
) -> bool:
    """Advance one release phase while both owner/update claims still match."""

    if (
        type(expected_phase) is not str
        or not expected_phase
        or type(next_phase) is not str
        or not next_phase
        or expected_update.get("phase") != expected_phase
    ):
        return False
    if fields is None:
        fields = {}
    elif not isinstance(fields, dict):
        raise ValueError("release checkpoint fields are invalid")
    if (
        not isinstance(remove_fields, tuple)
        or any(type(field) is not str for field in remove_fields)
        or len(remove_fields) != len(set(remove_fields))
    ):
        raise ValueError("release checkpoint removals are invalid")
    unknown = (set(fields) | set(remove_fields)) - _RELEASE_CHECKPOINT_FIELDS
    if unknown:
        raise ValueError(f"unsupported release checkpoint fields: {sorted(unknown)}")
    if set(fields) & set(remove_fields):
        raise ValueError("release checkpoint cannot set and remove the same field")
    if not _valid_release_transition(expected_update, expected_phase, next_phase, fields, remove_fields):
        return False
    state = _release_transaction_conditions(expected_container, expected_update, claim_id)
    if state is None:
        return False
    owner_id, update_id, cn, cv, cc, un, uv, uc = state
    now_epoch = int(time.time())
    now_iso = utc_now_iso()
    cv.update({":heartbeat": now_epoch, ":updated": now_iso})
    uv.update({":heartbeat": now_epoch, ":updated": now_iso, ":phase": next_phase})
    un["#phase"] = "phase"
    update_sets = ["#phase = :phase", "apply_claimed_at = :heartbeat", "updated_at = :updated"]
    if next_phase != expected_phase:
        un["#milestone"] = f"{next_phase}_at"
        update_sets.append("#milestone = :updated")
    for index, (field, value) in enumerate(fields.items()):
        name, binding = f"#rf{index}", f":rf{index}"
        un[name] = field
        uv[binding] = value
        update_sets.append(f"{name} = {binding}")
    remove_parts: list[str] = []
    for index, field in enumerate(remove_fields):
        name = f"#rr{index}"
        un[name] = field
        remove_parts.append(name)
    update_expression = "SET " + ", ".join(update_sets)
    if remove_parts:
        update_expression += " REMOVE " + ", ".join(remove_parts)
    try:
        await run_in_thread(
            get_table("containers").meta.client.transact_write_items,
            TransactItems=[
                {
                    "Update": {
                        "TableName": get_table("containers").name,
                        "Key": {"owner_id": owner_id},
                        "ConditionExpression": " AND ".join(cc),
                        "UpdateExpression": "SET provision_claimed_at = :heartbeat, updated_at = :updated",
                        "ExpressionAttributeNames": cn,
                        "ExpressionAttributeValues": cv,
                    }
                },
                {
                    "Update": {
                        "TableName": _get_table().name,
                        "Key": {"owner_id": owner_id, "update_id": update_id},
                        "ConditionExpression": " AND ".join(uc),
                        "UpdateExpression": update_expression,
                        "ExpressionAttributeNames": un,
                        "ExpressionAttributeValues": uv,
                    }
                },
            ],
        )
    except ClientError as error:
        if _is_conditional_transaction_cancel(error):
            return False
        raise
    return True


async def heartbeat_owner_release(expected_container: dict, expected_update: dict, claim_id: str) -> bool:
    phase = expected_update.get("phase")
    if type(phase) is not str or not phase:
        return False
    return await checkpoint_owner_release(
        expected_container,
        expected_update,
        claim_id,
        expected_phase=phase,
        next_phase=phase,
    )


async def takeover_owner_release(
    expected_container: dict,
    expected_update: dict,
    claim_id: str,
    *,
    stale_before_epoch: int,
) -> bool:
    """Atomically replace both stale release fencing tokens."""

    phase = expected_update.get("phase")
    if (
        type(phase) is not str
        or not _valid_release_phase_evidence(phase, expected_update)
        or type(stale_before_epoch) is not int
        or stale_before_epoch < 0
    ):
        return False
    old_claim = expected_update.get("apply_claim_id")
    if old_claim == claim_id:
        return False
    state = _release_transaction_conditions(expected_container, expected_update, old_claim)
    if state is None:
        return False
    _require_nonempty_string("claim_id", claim_id)
    owner_id, update_id, cn, cv, cc, un, uv, uc = state
    cc.append("provision_claimed_at <= :stale")
    uc.append("apply_claimed_at <= :stale")
    now_epoch = int(time.time())
    now_iso = utc_now_iso()
    cv.update({":claim": claim_id, ":now": now_epoch, ":updated": now_iso, ":stale": stale_before_epoch})
    uv.update({":claim": claim_id, ":now": now_epoch, ":updated": now_iso, ":stale": stale_before_epoch})
    effect_update = ""
    if _valid_effect(expected_update, expected_update.get("effect_kind")):
        uv[":effect"] = uuid.uuid4().hex
        effect_update = ", effect_claim_id = :effect, effect_executor_id = :claim"
    try:
        await run_in_thread(
            get_table("containers").meta.client.transact_write_items,
            TransactItems=[
                {
                    "Update": {
                        "TableName": get_table("containers").name,
                        "Key": {"owner_id": owner_id},
                        "ConditionExpression": " AND ".join(cc),
                        "UpdateExpression": (
                            "SET provision_claim_id = :claim, provision_attempt_token = :claim, "
                            "provision_claimed_at = :now, updated_at = :updated"
                        ),
                        "ExpressionAttributeNames": cn,
                        "ExpressionAttributeValues": cv,
                    }
                },
                {
                    "Update": {
                        "TableName": _get_table().name,
                        "Key": {"owner_id": owner_id, "update_id": update_id},
                        "ConditionExpression": " AND ".join(uc),
                        "UpdateExpression": (
                            "SET apply_claim_id = :claim, apply_claimed_at = :now, updated_at = :updated"
                            + effect_update
                        ),
                        "ExpressionAttributeNames": un,
                        "ExpressionAttributeValues": uv,
                    }
                },
            ],
        )
    except ClientError as error:
        if _is_conditional_transaction_cancel(error):
            return False
        raise
    return True


async def begin_owner_release_rollback(
    expected_container: dict,
    expected_update: dict,
    claim_id: str,
    *,
    effect_claim_id: str,
    failure_reason: str,
) -> bool:
    """Move a post-drain failure into one recoverable compensating effect."""

    if expected_update.get("phase") not in {
        "recovered",
        "prepare_requested",
        "prepared",
        "stage_requested",
        "staged",
        "activating",
        "activated",
        "start_requested",
        "validating",
    } or not _valid_recovery_set(expected_update.get("recovery_set"), expected_update):
        return False
    _require_nonempty_string("effect_claim_id", effect_claim_id)
    _require_nonempty_string("failure_reason", failure_reason)
    state = _release_transaction_conditions(expected_container, expected_update, claim_id)
    if state is None:
        return False
    owner_id, update_id, cn, cv, cc, un, uv, uc = state
    now_epoch = int(time.time())
    now_iso = utc_now_iso()
    cv.update({":heartbeat": now_epoch, ":updated": now_iso})
    uv.update(
        {
            ":rollback": "rollback_requested",
            ":kind": "rollback",
            ":effect": effect_claim_id,
            ":executor": claim_id,
            ":reason": failure_reason[:1000],
            ":heartbeat": now_epoch,
            ":updated": now_iso,
        }
    )
    try:
        await run_in_thread(
            get_table("containers").meta.client.transact_write_items,
            TransactItems=[
                {
                    "Update": {
                        "TableName": get_table("containers").name,
                        "Key": {"owner_id": owner_id},
                        "ConditionExpression": " AND ".join(cc),
                        "UpdateExpression": "SET provision_claimed_at = :heartbeat, updated_at = :updated",
                        "ExpressionAttributeNames": cn,
                        "ExpressionAttributeValues": cv,
                    }
                },
                {
                    "Update": {
                        "TableName": _get_table().name,
                        "Key": {"owner_id": owner_id, "update_id": update_id},
                        "ConditionExpression": " AND ".join(uc),
                        "UpdateExpression": (
                            "SET phase = :rollback, rollback_requested_at = :updated, "
                            "effect_kind = :kind, effect_claim_id = :effect, effect_executor_id = :executor, "
                            "failure_reason = :reason, apply_claimed_at = :heartbeat, updated_at = :updated "
                            "REMOVE acceptance"
                        ),
                        "ExpressionAttributeNames": un,
                        "ExpressionAttributeValues": uv,
                    }
                },
            ],
        )
    except ClientError as error:
        if _is_conditional_transaction_cancel(error):
            return False
        raise
    return True


async def finish_owner_release_rollback(
    expected_container: dict,
    expected_update: dict,
    claim_id: str,
    *,
    rollback_evidence: dict,
) -> bool:
    """Atomically restore the prior assignment and clear the failed release pointer."""

    recovery_set = expected_update.get("recovery_set")
    expected_fields = {
        "recovery_set_id",
        "manifest_sha256",
        "replica_inventory_sha256",
        "efs_tree_manifest_sha256",
        "replica_object_count",
        "restored_at",
        "rollback_drain_evidence",
        "source_task_definition_arn",
        "source_image_digest",
        "source_task_arn",
        "source_build_id",
        "gateway",
    }
    if isinstance(rollback_evidence, dict) and "displaced_name" in rollback_evidence:
        expected_fields.add("displaced_name")
    if (
        expected_update.get("phase") != "rollback_requested"
        or not _valid_release_phase_evidence("rollback_requested", expected_update)
        or not isinstance(rollback_evidence, dict)
        or not isinstance(recovery_set, dict)
        or set(rollback_evidence) != expected_fields
        or rollback_evidence.get("source_task_definition_arn") != expected_update.get("prior_task_definition_arn")
        or rollback_evidence.get("source_image_digest") != expected_update.get("source", {}).get("image_digest")
        or rollback_evidence.get("source_build_id") != expected_update.get("source", {}).get("build_id")
        or not _nonempty_string(rollback_evidence.get("source_task_arn"))
        or rollback_evidence.get("recovery_set_id") != recovery_set.get("recovery_set_id")
        or rollback_evidence.get("manifest_sha256") != recovery_set.get("manifest_sha256")
        or rollback_evidence.get("replica_inventory_sha256") != recovery_set.get("stable_replica_inventory_sha256")
        or rollback_evidence.get("efs_tree_manifest_sha256") != recovery_set.get("efs_tree_manifest_sha256")
        or not _positive_integer(rollback_evidence.get("replica_object_count"))
        or not _nonempty_string(rollback_evidence.get("restored_at"))
        or not _valid_rollback_drain(rollback_evidence.get("rollback_drain_evidence"), expected_update)
        or ("displaced_name" in rollback_evidence and not _nonempty_string(rollback_evidence.get("displaced_name")))
        or rollback_evidence.get("gateway") != "accepted"
    ):
        return False
    state = _release_transaction_conditions(expected_container, expected_update, claim_id)
    if state is None:
        return False
    owner_id, update_id, cn, cv, cc, un, uv, uc = state
    revision = _release_revision(expected_update.get("release_revision")) + 1
    now_iso = utc_now_iso()
    ttl_epoch = int(time.time()) + (30 * 24 * 60 * 60)
    prior_release = expected_update.get("prior_release_id")
    prior_desired = expected_update.get("prior_desired_release_id")
    if (prior_release is None) != (prior_desired is None) or prior_release != prior_desired:
        return False
    cv.update(
        {
            ":revision": revision,
            ":target_arn": expected_update["prior_task_definition_arn"],
            ":running": "running",
            ":updated": now_iso,
        }
    )
    uv.update(
        {
            ":failed": "failed",
            ":rolled_back": "rolled_back",
            ":evidence": rollback_evidence,
            ":revision": revision,
            ":updated": now_iso,
            ":ttl": ttl_epoch,
        }
    )
    assignment_set = ""
    assignment_remove = "desired_release_id, active_release_id, "
    if prior_release is not None:
        cv[":prior"] = prior_release
        assignment_set = "desired_release_id = :prior, active_release_id = :prior, "
        assignment_remove = ""
    try:
        await run_in_thread(
            get_table("containers").meta.client.transact_write_items,
            TransactItems=[
                {
                    "Update": {
                        "TableName": get_table("containers").name,
                        "Key": {"owner_id": owner_id},
                        "ConditionExpression": " AND ".join(cc),
                        "UpdateExpression": (
                            "SET "
                            + assignment_set
                            + "release_revision = :revision, task_definition_arn = :target_arn, "
                            "#status = :running, updated_at = :updated REMOVE "
                            + assignment_remove
                            + "release_update_id, provision_claim_id, provision_attempt_token, "
                            "provision_claimed_at, provision_execution_name"
                        ),
                        "ExpressionAttributeNames": {**cn, "#status": "status"},
                        "ExpressionAttributeValues": cv,
                    }
                },
                {
                    "Update": {
                        "TableName": _get_table().name,
                        "Key": {"owner_id": owner_id, "update_id": update_id},
                        "ConditionExpression": " AND ".join(uc),
                        "UpdateExpression": (
                            "SET #status = :failed, phase = :rolled_back, rollback_evidence = :evidence, "
                            "rolled_back_at = :updated, release_revision = :revision, updated_at = :updated, #ttl = :ttl "
                            "REMOVE effect_kind, effect_claim_id, effect_executor_id"
                        ),
                        "ExpressionAttributeNames": {**un, "#status": "status", "#ttl": "ttl"},
                        "ExpressionAttributeValues": uv,
                    }
                },
            ],
        )
    except ClientError as error:
        if _is_conditional_transaction_cancel(error):
            return False
        raise
    return True


async def finish_owner_release(
    expected_container: dict,
    expected_update: dict,
    claim_id: str,
    *,
    expected_phase: str,
) -> bool:
    """Accept a release and clear both claims in one DynamoDB transaction."""

    if (
        expected_phase != "validating"
        or expected_update.get("phase") != expected_phase
        or not _valid_release_phase_evidence("validating", expected_update)
    ):
        return False
    acceptance = _registered_acceptance(expected_update, expected_update.get("acceptance"))
    if acceptance is None:
        return False
    state = _release_transaction_conditions(expected_container, expected_update, claim_id)
    if state is None:
        return False
    owner_id, update_id, cn, cv, cc, un, uv, uc = state
    target_release_id = _require_nonempty_string("target_release_id", expected_update.get("target_release_id"))
    revision = _release_revision(expected_update.get("release_revision"))
    target_task_definition_arn = _require_nonempty_string(
        "target_task_definition_arn", expected_update.get("target_task_definition_arn")
    )
    next_revision = revision + 1
    now_iso = utc_now_iso()
    ttl_epoch = int(time.time()) + (30 * 24 * 60 * 60)
    cv.update({":target": target_release_id, ":revision": next_revision, ":updated": now_iso})
    uv.update(
        {
            ":applied": "applied",
            ":healthy": "healthy",
            ":acceptance": acceptance,
            ":revision": next_revision,
            ":updated": now_iso,
            ":ttl": ttl_epoch,
        }
    )
    try:
        await run_in_thread(
            get_table("containers").meta.client.transact_write_items,
            TransactItems=[
                {
                    "Update": {
                        "TableName": get_table("containers").name,
                        "Key": {"owner_id": owner_id},
                        "ConditionExpression": " AND ".join(cc),
                        "UpdateExpression": (
                            "SET desired_release_id = :target, active_release_id = :target, "
                            "release_revision = :revision, task_definition_arn = :target_arn, updated_at = :updated "
                            "REMOVE release_update_id, provision_claim_id, provision_attempt_token, "
                            "provision_claimed_at, provision_execution_name"
                        ),
                        "ExpressionAttributeNames": cn,
                        "ExpressionAttributeValues": {
                            **cv,
                            ":target_arn": target_task_definition_arn,
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": _get_table().name,
                        "Key": {"owner_id": owner_id, "update_id": update_id},
                        "ConditionExpression": " AND ".join(uc),
                        "UpdateExpression": (
                            "SET #status = :applied, phase = :healthy, acceptance = :acceptance, "
                            "release_revision = :revision, applied_at = :updated, updated_at = :updated, #ttl = :ttl "
                            "REMOVE effect_kind, effect_claim_id, effect_executor_id"
                        ),
                        "ExpressionAttributeNames": {**un, "#status": "status", "#ttl": "ttl"},
                        "ExpressionAttributeValues": uv,
                    }
                },
            ],
        )
    except ClientError as error:
        if _is_conditional_transaction_cancel(error):
            return False
        raise
    return True


async def get_pending(owner_id: str) -> list[dict]:
    """Get all pending or scheduled updates for an owner."""
    table = _get_table()
    response = await run_in_thread(
        table.query,
        KeyConditionExpression=Key("owner_id").eq(owner_id),
        FilterExpression=Attr("status").is_in(["pending", "scheduled"]),
    )
    return response.get("Items", [])


async def get_in_flight(owner_id: str) -> list[dict]:
    """Return an owner's non-terminal updates: pending, scheduled, or the transient
    'applying' state.

    Superset of get_pending() (which omits 'applying'). Callers use it to enforce
    per-owner mutual exclusion on container rolls — apply_update's acquire is keyed
    per-update_id, so this is the only way to know an owner already has an update
    queued or mid-apply and must not have a second roll started concurrently.
    Callers decide which rows actually conflict (e.g. those whose ``changes`` rewrite
    the task-def) rather than trusting the update_type label.

    Reads are strongly consistent: the guard must see a row written moments earlier
    (a just-queued update, or a concurrent apply that already flipped to
    'applying') rather than risk an eventually-consistent miss.

    Paginated: the status FilterExpression is applied AFTER DynamoDB's 1 MB page
    read, so an owner with enough historical terminal rows could otherwise push a
    newer in-flight row onto a later page and hide it from the guard.
    """
    table = _get_table()
    items: list[dict] = []
    last_key: dict | None = None
    while True:
        kwargs: dict = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "FilterExpression": Attr("status").is_in(["pending", "scheduled", "applying"]),
            "ConsistentRead": True,
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items


async def set_status_conditional(
    owner_id: str,
    update_id: str,
    new_status: str,
    expected_statuses: list[str],
) -> bool:
    """Conditionally update status. Returns True on success, False if condition fails."""
    table = _get_table()
    now = utc_now_iso()

    # Build condition: status must be one of expected_statuses
    # DynamoDB doesn't support IN in ConditionExpression directly with update_item,
    # so we build an OR chain.
    condition = Attr("status").eq(expected_statuses[0])
    for s in expected_statuses[1:]:
        condition = condition | Attr("status").eq(s)
    condition = _UNRESERVED_UPDATE & condition

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET #s = :new_status, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":new_status": new_status, ":now": now},
            ConditionExpression=condition,
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def claim_apply(
    owner_id: str,
    update_id: str,
    claim_id: str,
) -> dict | None:
    """Atomically claim a pending/scheduled update and return its new row.

    ``apply_claim_id`` is a fencing token. Long-running callers must use the
    claim-aware target and terminal helpers below so a worker superseded after
    backend turnover cannot mutate the successor's row.
    """

    table = _get_table()
    now_epoch = int(time.time())
    now_iso = utc_now_iso()
    try:
        response = await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression=(
                "SET #s = :applying, apply_claim_id = :claim, apply_claimed_at = :claimed, updated_at = :updated"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":applying": "applying",
                ":claim": claim_id,
                ":claimed": now_epoch,
                ":updated": now_iso,
            },
            ConditionExpression=_UNRESERVED_UPDATE & Attr("status").is_in(["pending", "scheduled"]),
            ReturnValues="ALL_NEW",
        )
        return response.get("Attributes")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return None


async def claim_ordinary_owner(owner_id: str, update_id: str, claim_id: str) -> bool:
    """Publish an owner marker that excludes a release before ordinary effects."""

    containers = get_table("containers")
    updates = _get_table()
    try:
        await run_in_thread(
            containers.meta.client.transact_write_items,
            TransactItems=[
                {
                    "Update": {
                        "TableName": containers.name,
                        "Key": {"owner_id": owner_id},
                        "UpdateExpression": (
                            "SET ordinary_update_id = :update, ordinary_update_claim_id = :claim, "
                            "ordinary_update_claimed_at = :claimed"
                        ),
                        "ExpressionAttributeValues": {
                            ":update": update_id,
                            ":claim": claim_id,
                            ":claimed": int(time.time()),
                        },
                        "ConditionExpression": (
                            "attribute_exists(owner_id) AND attribute_not_exists(release_update_id) AND "
                            "attribute_not_exists(provision_claim_id) AND "
                            "(attribute_not_exists(ordinary_update_id) OR ordinary_update_id = :update)"
                        ),
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": updates.name,
                        "Key": {"owner_id": owner_id, "update_id": update_id},
                        "ConditionExpression": (
                            "#status = :applying AND apply_claim_id = :claim AND "
                            "attribute_not_exists(release_revision) AND "
                            "(attribute_not_exists(update_type) OR update_type <> :release)"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":applying": "applying",
                            ":claim": claim_id,
                            ":release": "release",
                        },
                    }
                },
            ],
        )
        return True
    except ClientError as error:
        if _is_conditional_transaction_cancel(error):
            return False
        raise


async def assert_ordinary_owner(owner_id: str, update_id: str, claim_id: str) -> bool:
    """Strongly prove an ordinary update still excludes owner release effects."""

    container_response = await run_in_thread(
        get_table("containers").get_item,
        Key={"owner_id": owner_id},
        ConsistentRead=True,
    )
    update = await get_by_id(owner_id, update_id, consistent=True)
    container = container_response.get("Item")
    return bool(
        container
        and update
        and container.get("ordinary_update_id") == update_id
        and container.get("ordinary_update_claim_id") == claim_id
        and "release_update_id" not in container
        and "provision_claim_id" not in container
        and update.get("status") == "applying"
        and update.get("update_type") != "release"
        and update.get("apply_claim_id") == claim_id
        and "release_revision" not in update
    )


async def release_ordinary_owner(owner_id: str, update_id: str, claim_id: str) -> bool:
    """Clear the exclusion marker iff this exact ordinary claim still owns it."""

    table = get_table("containers")
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="REMOVE ordinary_update_id, ordinary_update_claim_id, ordinary_update_claimed_at",
            ConditionExpression=(
                Attr("ordinary_update_id").eq(update_id) & Attr("ordinary_update_claim_id").eq(claim_id)
            ),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def claim_stale_apply(
    owner_id: str,
    update_id: str,
    claim_id: str,
    *,
    stale_before_epoch: int,
) -> dict | None:
    """Take over an abandoned applying row, returning the newly fenced row."""

    table = _get_table()
    now_epoch = int(time.time())
    now_iso = utc_now_iso()
    try:
        response = await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression=("SET apply_claim_id = :claim, apply_claimed_at = :claimed, updated_at = :updated"),
            ExpressionAttributeValues={
                ":claim": claim_id,
                ":claimed": now_epoch,
                ":updated": now_iso,
            },
            ConditionExpression=(
                _UNRESERVED_UPDATE
                & Attr("status").eq("applying")
                & (Attr("apply_claimed_at").not_exists() | Attr("apply_claimed_at").lte(stale_before_epoch))
            ),
            ReturnValues="ALL_NEW",
        )
        return response.get("Attributes")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return None


async def heartbeat_apply(owner_id: str, update_id: str, claim_id: str) -> bool:
    """Refresh an apply lease iff ``claim_id`` still owns the row."""

    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET apply_claimed_at = :claimed, updated_at = :updated",
            ExpressionAttributeValues={
                ":claimed": int(time.time()),
                ":updated": utc_now_iso(),
            },
            ConditionExpression=(
                _UNRESERVED_UPDATE & Attr("status").eq("applying") & Attr("apply_claim_id").eq(claim_id)
            ),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def set_apply_target(
    owner_id: str,
    update_id: str,
    claim_id: str,
    task_definition_arn: str,
) -> bool:
    """Persist the immutable ECS target while the caller still owns the claim."""

    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET target_task_definition_arn = :target, updated_at = :updated",
            ExpressionAttributeValues={
                ":target": task_definition_arn,
                ":updated": utc_now_iso(),
            },
            ConditionExpression=(
                _UNRESERVED_UPDATE
                & Attr("status").eq("applying")
                & Attr("apply_claim_id").eq(claim_id)
                & (
                    Attr("target_task_definition_arn").not_exists()
                    | Attr("target_task_definition_arn").eq(task_definition_arn)
                )
            ),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def finish_claimed_apply(
    owner_id: str,
    update_id: str,
    claim_id: str,
    *,
    status: str,
    failure_reason: str | None = None,
) -> bool:
    """Write a terminal apply result iff the caller still owns the claim."""

    if status not in {"applied", "failed"}:
        raise ValueError("claimed apply terminal status must be applied or failed")

    table = _get_table()
    now = utc_now_iso()
    ttl_epoch = int(time.time()) + (30 * 24 * 60 * 60)
    names = {"#s": "status", "#t": "ttl"}
    values = {
        ":status": status,
        ":now": now,
        ":ttl": ttl_epoch,
    }
    set_parts = ["#s = :status", "updated_at = :now", "#t = :ttl"]
    if status == "applied":
        set_parts.append("applied_at = :now")
    else:
        set_parts.append("failure_reason = :reason")
        values[":reason"] = (failure_reason or "")[:1000]

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression=(
                _UNRESERVED_UPDATE & Attr("status").eq("applying") & Attr("apply_claim_id").eq(claim_id)
            ),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def set_scheduled(
    owner_id: str,
    update_id: str,
    scheduled_at: str,
) -> bool:
    """Set an update to scheduled status with a scheduled_at timestamp."""
    table = _get_table()
    now = utc_now_iso()
    condition = _UNRESERVED_UPDATE & Attr("status").eq("pending")

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET #s = :status, scheduled_at = :sat, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "scheduled",
                ":sat": scheduled_at,
                ":now": now,
            },
            ConditionExpression=condition,
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def set_snoozed(owner_id: str, update_id: str) -> bool:
    """Record that the update was snoozed (sets last_snoozed_at)."""
    table = _get_table()
    now = utc_now_iso()

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET last_snoozed_at = :now, updated_at = :now2",
            ExpressionAttributeValues={":now": now, ":now2": now},
            ConditionExpression=_UNRESERVED_UPDATE & Attr("owner_id").exists(),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def get_due_scheduled() -> list[dict]:
    """Query GSI for scheduled updates that are due (scheduled_at <= now)."""
    table = _get_table()
    now = utc_now_iso()
    response = await run_in_thread(
        table.query,
        IndexName="status-index",
        KeyConditionExpression=(Key("status").eq("scheduled") & Key("scheduled_at").lte(now)),
    )
    return response.get("Items", [])


async def list_stale_applying(update_type: str, *, stale_before_epoch: int) -> list[dict]:
    """Return every abandoned applying row of ``update_type``.

    This deliberately scans rather than using ``status-index``: older applying
    rows and force-applied rows may have no ``scheduled_at`` and therefore do
    not exist in that GSI. The pending-updates table is TTL-bounded, and this
    recovery pass runs once per worker tick rather than on a request hot path.
    """

    table = _get_table()
    items: list[dict] = []
    last_key: dict | None = None
    while True:
        kwargs: dict = {
            "FilterExpression": (
                Attr("status").eq("applying")
                & Attr("update_type").eq(update_type)
                & (Attr("apply_claimed_at").not_exists() | Attr("apply_claimed_at").lte(stale_before_epoch))
            ),
            "ConsistentRead": True,
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.scan, **kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items


async def delete_all_for_owner(owner_id: str) -> int:
    """Delete all pending-update rows for an owner. Returns count deleted.

    Used by the e2e teardown endpoint. PK=owner_id, SK=update_id.
    Paginates the DDB query so multi-page results don't leak.
    """
    table = _get_table()
    deleted = 0
    last_key: dict | None = None
    while True:
        kwargs = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "ProjectionExpression": "owner_id, update_id",
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        for item in response.get("Items", []):
            await run_in_thread(
                table.delete_item,
                Key={"owner_id": item["owner_id"], "update_id": item["update_id"]},
            )
            deleted += 1
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return deleted


async def count_for_owner(owner_id: str) -> int:
    """Count pending-update rows for an owner. Used by /debug/ddb-rows.

    Paginates so the count matches reality past the 1MB query boundary.
    """
    table = _get_table()
    total = 0
    last_key: dict | None = None
    while True:
        kwargs = {
            "KeyConditionExpression": Key("owner_id").eq(owner_id),
            "Select": "COUNT",
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.query, **kwargs)
        total += int(response.get("Count", 0))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return total


async def mark_applied(owner_id: str, update_id: str) -> bool:
    """Mark an update as applied and set TTL for 30-day expiry."""
    table = _get_table()
    now = utc_now_iso()
    ttl_epoch = int(time.time()) + (30 * 24 * 60 * 60)  # 30 days

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression="SET #s = :status, applied_at = :now, updated_at = :now2, #t = :ttl",
            ExpressionAttributeNames={"#s": "status", "#t": "ttl"},
            ExpressionAttributeValues={
                ":status": "applied",
                ":now": now,
                ":now2": now,
                ":ttl": ttl_epoch,
            },
            # "applying" MUST be accepted: apply_update transitions the row
            # pending/scheduled -> applying BEFORE doing the work and calling
            # this, so at this call site the row is "applying". Omitting it left
            # the condition unsatisfiable there, so the row was never flipped to
            # "applied" and stayed stuck in "applying" forever (the ECS change
            # had already taken effect). "pending"/"scheduled" stay for callers
            # that mark a row applied without an intervening "applying" hop.
            ConditionExpression=_UNRESERVED_UPDATE & Attr("status").is_in(["applying", "pending", "scheduled"]),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def list_pending_by_type(update_type: str) -> list[dict]:
    """Scan the table for pending rows of a given ``update_type``.

    Used by the T13 cleanup cron's provisioning-retry pass — rows
    enqueued by Clerk webhooks (T12) carry ``update_type="paperclip_provision"``
    and need to be picked up regardless of which ``owner_id`` partition
    they live in.

    Implementation: a paginated scan with FilterExpression. The
    ``status-index`` GSI is keyed on ``status`` HASH + ``scheduled_at``
    RANGE; rows without ``scheduled_at`` (which is the case for
    webhook-enqueued retries) are excluded from that index, so we
    cannot use it here. A scan is acceptable because the cron runs
    infrequently and the pending-updates table stays small (TTL purges
    applied rows after 30 days).
    """
    table = _get_table()
    out: list[dict] = []
    last_key: dict | None = None
    while True:
        kwargs: dict = {
            "FilterExpression": Attr("status").eq("pending") & Attr("update_type").eq(update_type),
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.scan, **kwargs)
        out.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return out


async def mark_failed(owner_id: str, update_id: str, reason: str) -> bool:
    """Mark a pending row as failed with a truncated reason string.

    Used by the T13 retry pass when a Paperclip provisioning op
    raises a non-retryable exception (or hits an unknown ``op``).
    Sets a 30-day TTL like ``mark_applied`` so failed rows don't
    accumulate indefinitely.
    """
    table = _get_table()
    now = utc_now_iso()
    ttl_epoch = int(time.time()) + (30 * 24 * 60 * 60)
    truncated = (reason or "")[:1000]

    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id, "update_id": update_id},
            UpdateExpression=("SET #s = :status, failure_reason = :reason, updated_at = :now, #t = :ttl"),
            ExpressionAttributeNames={"#s": "status", "#t": "ttl"},
            ExpressionAttributeValues={
                ":status": "failed",
                ":reason": truncated,
                ":now": now,
                ":ttl": ttl_epoch,
            },
            ConditionExpression=_UNRESERVED_UPDATE & Attr("status").is_in(["pending", "scheduled", "applying"]),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
