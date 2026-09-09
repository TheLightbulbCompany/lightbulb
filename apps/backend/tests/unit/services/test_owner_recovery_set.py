import io
import sqlite3
import tarfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import owner_recovery_set as recovery


OWNER = "org_test"
UPDATE = "update_test"
SOURCE = {
    "service_arn": "arn:aws:ecs:us-east-1:123456789012:service/cluster/founder",
    "task_arn": "arn:aws:ecs:us-east-1:123456789012:task/cluster/task-1",
    "task_definition_arn": "arn:aws:ecs:us-east-1:123456789012:task-definition/openclaw:1",
    "image_digest": f"sha256:{'a' * 64}",
    "build_id": "source-build-123",
    "access_point_id": "fsap-0123456789abcdef0",
    "file_system_id": "fs-0123456789abcdef0",
    "efs_root": f"/users/{OWNER}",
    "shared_schema_version": 15,
    "agent_schema_version": 19,
}
RELEASE = {
    "release_id": "openclaw-2026.9.3-isol8.1",
    "image_digest": f"sha256:{'b' * 64}",
    "build_id": "build-123",
    "schema_id": "2026.9.3-isol8.1",
}
DRAIN = {
    "observed_at": "2026-09-09T15:00:00+00:00",
    "service_arn": SOURCE["service_arn"],
    "service_desired_count": 0,
    "service_running_count": 0,
    "service_pending_count": 0,
    "stopped_tasks": [
        {
            "task_arn": SOURCE["task_arn"],
            "last_status": "STOPPED",
            "task_definition_arn": SOURCE["task_definition_arn"],
            "image_digest": SOURCE["image_digest"],
            "exit_code": 0,
            "stop_code": "UserInitiated",
            "stopped_reason": "Scaling activity initiated by service update",
        }
    ],
}


def _object(key: str, *, etag: str = "etag", size: int = 10) -> recovery.ReplicaObject:
    return recovery.ReplicaObject(
        key=key,
        etag=etag,
        size=size,
        last_modified="2026-09-09T15:00:00+00:00",
    )


def _sqlite(path, *, role: str, schema_version: int, agent_id: str | None) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE schema_meta (
          meta_key TEXT PRIMARY KEY,
          role TEXT NOT NULL,
          schema_version INTEGER NOT NULL,
          agent_id TEXT
        );
        """
    )
    db.execute(
        "INSERT INTO schema_meta(meta_key, role, schema_version, agent_id) VALUES ('primary', ?, ?, ?)",
        (role, schema_version, agent_id),
    )
    db.execute(f"PRAGMA user_version = {schema_version}")
    db.commit()
    db.close()


def test_rejects_evidence_until_the_exact_source_task_is_stopped():
    running = {**DRAIN, "stopped_tasks": [{**DRAIN["stopped_tasks"][0], "last_status": "RUNNING"}]}
    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="STOPPED"):
        recovery.validate_recovery_inputs(OWNER, UPDATE, RELEASE, SOURCE, running)

    wrong_task = {
        **DRAIN,
        "stopped_tasks": [{**DRAIN["stopped_tasks"][0], "task_arn": "arn:aws:ecs:other"}],
    }
    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="source task"):
        recovery.validate_recovery_inputs(OWNER, UPDATE, RELEASE, SOURCE, wrong_task)


@pytest.mark.parametrize(
    "change",
    [
        {"exit_code": 137},
        {"stop_code": "SpotInterruption"},
        {"stopped_reason": ""},
    ],
)
def test_recovery_requires_a_clean_supervised_container_exit(change):
    dirty = {
        **DRAIN,
        "stopped_tasks": [{**DRAIN["stopped_tasks"][0], **change}],
    }

    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="cleanly"):
        recovery.validate_recovery_inputs(OWNER, UPDATE, RELEASE, SOURCE, dirty)


def test_recovery_identity_binds_release_source_and_drain_evidence():
    original = recovery.recovery_set_identity(OWNER, UPDATE, RELEASE, SOURCE, DRAIN)

    assert original != recovery.recovery_set_identity(
        OWNER,
        UPDATE,
        {**RELEASE, "build_id": "different-target"},
        SOURCE,
        DRAIN,
    )
    assert original != recovery.recovery_set_identity(
        OWNER,
        UPDATE,
        RELEASE,
        {**SOURCE, "build_id": "different-source"},
        DRAIN,
    )
    assert original != recovery.recovery_set_identity(
        OWNER,
        UPDATE,
        RELEASE,
        SOURCE,
        {**DRAIN, "observed_at": "2026-09-09T15:00:01+00:00"},
    )


def test_groups_shared_and_non_tombstoned_agent_replicas_and_allows_zero_agents():
    shared = _object(f"{OWNER}/state/openclaw.sqlite/0000/00000001-00000002.ltx")
    deleted = _object(f"{OWNER}/agents/deleted/agent/openclaw-agent.sqlite/0000/00000001-00000002.ltx")
    tombstone = _object(f"{OWNER}/agents/deleted/.replica-deleted", size=0)

    grouped = recovery.group_replica_objects(OWNER, (shared, deleted, tombstone))

    assert grouped.shared == (shared,)
    assert grouped.agents == {}
    assert grouped.tombstoned_agents == ("deleted",)


def test_rejects_unknown_replica_shapes_instead_of_silently_omitting_state():
    unknown = _object(f"{OWNER}/agents/main/agent/another.sqlite/0000/00000001-00000002.ltx")
    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="unrecognized replica object"):
        recovery.group_replica_objects(OWNER, (unknown,))


@pytest.mark.asyncio
async def test_stable_inventory_waits_longer_than_sync_interval_and_rejects_a_change():
    first = _object(f"{OWNER}/state/openclaw.sqlite/0000/00000001-00000002.ltx")
    second = _object(first.key, etag="changed")
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.side_effect = [
        iter([{"Contents": [first.as_aws_dict()]}]),
        iter([{"Contents": [second.as_aws_dict()]}]),
    ]
    wait = AsyncMock()
    provider = recovery.OwnerRecoverySetProvider(s3=s3, wait=wait)

    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="changed after drain"):
        await provider.stable_replica_inventory(OWNER)

    wait.assert_awaited_once()
    assert wait.await_args.args[0] > 10
    assert s3.get_paginator.return_value.paginate.call_count == 2


def test_copy_requires_an_immutable_destination_version():
    source = _object(f"{OWNER}/state/openclaw.sqlite/0000/00000001-00000002.ltx")
    s3 = MagicMock()
    s3.head_object.side_effect = recovery.ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadObject")
    s3.copy_object.return_value = {"CopyObjectResult": {"ETag": '"etag"'}}
    provider = recovery.OwnerRecoverySetProvider(s3=s3)

    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="VersionId"):
        provider.copy_replica_object(
            source,
            "live",
            "artifacts",
            "recovery-sets/o/r/replicas/db/0000/x.ltx",
            lambda: None,
        )


def test_database_copy_restores_exact_versions_and_removes_temporary_bytes(tmp_path):
    source = _object(f"{OWNER}/state/openclaw.sqlite/0000/00000001-00000002.ltx", size=4)
    destination = "recovery-sets/o/r/replicas/state/openclaw.sqlite/0000/00000001-00000002.ltx"
    provider = recovery.OwnerRecoverySetProvider(s3=MagicMock())
    provider.copy_replica_object = MagicMock(
        return_value={
            "key": destination,
            "version_id": "version-1",
            "etag": "etag",
            "size": 4,
            "source_key": source.key,
            "source_etag": source.etag,
            "source_size": source.size,
            "source_last_modified": source.last_modified,
        }
    )
    provider._read_version_bytes = MagicMock(return_value=b"data")

    def restored(*, output, **_kwargs):
        output.write_bytes(b"restored")
        return {"quick_check": "ok", "schema_version": 15}

    provider._restore_database = MagicMock(side_effect=restored)

    provider._copy_database(
        objects=[source],
        owner_id=OWNER,
        recovery_prefix="recovery-sets/o/r",
        relative_db_prefix="state/openclaw.sqlite",
        expected_role="global",
        expected_schema=15,
        temporary_directory=tmp_path,
        effect_fence_sync=lambda: None,
    )

    provider._read_version_bytes.assert_called_once_with(
        recovery.settings.MARKETPLACE_ARTIFACTS_BUCKET,
        destination,
        "version-1",
    )
    assert list(tmp_path.iterdir()) == []


def test_restored_replica_heads_must_match_the_pinned_key_size_and_etag():
    artifact = {"source_key": f"{OWNER}/state/db/one.ltx", "source_size": 4, "etag": "pinned"}

    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="digest"):
        recovery.OwnerRecoverySetProvider._validate_restored_replica_heads(
            [{"Key": artifact["source_key"], "Size": 4, "ETag": '"different"'}],
            [artifact],
        )


def test_restored_databases_require_quick_check_schema_role_and_agent_identity(tmp_path):
    shared = tmp_path / "shared.sqlite"
    agent = tmp_path / "agent.sqlite"
    _sqlite(shared, role="global", schema_version=15, agent_id=None)
    _sqlite(agent, role="agent", schema_version=19, agent_id="worker")

    shared_evidence = recovery.inspect_restored_database(shared, expected_role="global", expected_schema=15)
    agent_evidence = recovery.inspect_restored_database(
        agent,
        expected_role="agent",
        expected_schema=19,
        expected_agent_id="worker",
    )

    assert shared_evidence["quick_check"] == "ok"
    assert shared_evidence["schema_version"] == 15
    assert shared_evidence["role"] == "global"
    assert len(shared_evidence["sha256"]) == 64
    assert agent_evidence["agent_id"] == "worker"

    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="schema version"):
        recovery.inspect_restored_database(
            agent,
            expected_role="agent",
            expected_schema=20,
            expected_agent_id="worker",
        )
    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="agent identity"):
        recovery.inspect_restored_database(
            agent,
            expected_role="agent",
            expected_schema=19,
            expected_agent_id="other",
        )


def test_efs_manifest_and_tar_preserve_but_never_follow_symlinks(tmp_path):
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    (owner_root / "workspace-main").mkdir()
    (owner_root / "workspace-main" / "SOUL.md").write_text("inside")
    (owner_root / "workspace-main" / "link").symlink_to("SOUL.md")

    entries = recovery.scan_efs_tree(owner_root)
    sink = io.BytesIO()
    recovery.write_efs_tar(owner_root, entries, sink)

    by_path = {entry["path"]: entry for entry in entries}
    assert by_path["workspace-main/link"]["type"] == "symlink"
    assert by_path["workspace-main/link"]["link_target"] == "SOUL.md"
    with tarfile.open(fileobj=io.BytesIO(sink.getvalue()), mode="r:") as archive:
        link = archive.getmember("workspace-main/link")
        assert link.issym()
        assert link.linkname == "SOUL.md"


@pytest.mark.parametrize("target", ["/etc/passwd", "../../outside-owner"])
def test_efs_manifest_rejects_symlinks_python_data_filter_cannot_restore(tmp_path, target):
    owner_root = tmp_path / "owner"
    (owner_root / "workspace-main").mkdir(parents=True)
    (owner_root / "workspace-main" / "link").symlink_to(target)

    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="unsafe symlink"):
        recovery.scan_efs_tree(owner_root)


def test_efs_swap_resumes_from_deterministic_displaced_and_stage_state(tmp_path, monkeypatch):
    recovery_set_id = "recovery-set-crash"
    state_token = recovery.sha256_bytes(recovery_set_id.encode())[:24]
    displaced_name = f".release-target-{state_token}"
    stage_name = f".release-restore-{state_token}"
    displaced = tmp_path / displaced_name
    stage = tmp_path / stage_name
    displaced.mkdir()
    (displaced / "target.txt").write_text("post-release")
    stage.mkdir()
    (stage / "source.txt").write_text("pre-release")
    expected = recovery.scan_efs_tree(stage)
    manifest = {
        "recovery_set_id": recovery_set_id,
        "efs": {
            "archive": {
                "tree_entries": list(expected),
                "key": "recovery-sets/archive.tar",
                "version_id": "version-1",
                "sha256": "a" * 64,
            }
        },
    }
    provider = recovery.OwnerRecoverySetProvider(s3=MagicMock())
    provider._read_version_bytes = MagicMock(side_effect=AssertionError("verified stage must be reused"))
    monkeypatch.setattr(recovery.settings, "EFS_MOUNT_PATH", str(tmp_path))

    restored = provider._restore_efs_archive(OWNER, manifest, lambda: None)

    assert (tmp_path / OWNER / "source.txt").read_text() == "pre-release"
    assert (displaced / "target.txt").read_text() == "post-release"
    assert restored["displaced_name"] == displaced_name
    resumed = provider._restore_efs_archive(OWNER, manifest, lambda: None)
    assert resumed["already_restored"] is True
    assert resumed["displaced_name"] == displaced_name


def test_tar_fails_closed_when_a_file_changes_after_the_manifest_scan(tmp_path):
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    file_path = owner_root / "openclaw.json"
    file_path.write_text("before")
    entries = recovery.scan_efs_tree(owner_root)
    file_path.write_text("after")

    with pytest.raises(recovery.OwnerRecoverySetUnavailable, match="changed while archiving"):
        recovery.write_efs_tar(owner_root, entries, io.BytesIO())


def test_canonical_manifest_is_owner_scoped_and_hash_stable():
    manifest = {
        "owner_id": OWNER,
        "update_id": UPDATE,
        "verified_at": datetime(2026, 9, 9, tzinfo=timezone.utc).isoformat(),
        "replicas": {"agents": {}, "shared": {"inventory_sha256": "a" * 64}},
    }
    first = recovery.canonical_json(manifest)
    second = recovery.canonical_json(dict(reversed(list(manifest.items()))))
    assert first == second
    assert recovery.sha256_bytes(first) == recovery.sha256_bytes(second)
