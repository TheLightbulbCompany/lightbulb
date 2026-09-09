from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest


def _release():
    return SimpleNamespace(
        release_id="candidate",
        image="repo.example/openclaw:reviewed",
        image_digest=f"sha256:{'a' * 64}",
        digest_image=f"repo.example/openclaw@sha256:{'a' * 64}",
        build_id="build-reviewed",
        upstream_version="2026.9.3",
        fork_tag="v2026.9.3-isol8.2",
        fork_commit="c" * 40,
        schema_id="2026.9.3-isol8.2",
        schema_sha256="d" * 64,
        shared_db_schema_version=20,
        agent_db_schema_version=24,
        config_capabilities=frozenset({"browser.sessionState"}),
        qualification_id="founder-native-v1",
        evidence_id=f"sha256:{'e' * 64}",
        runtime_capabilities=frozenset({"sessions.create", "users.self"}),
        qualification_capabilities=frozenset({"legacy-privacy", "machine-coexistence", "native-human-auth"}),
    )


def _reserved(phase="reserved"):
    container = {
        "owner_id": "owner",
        "id": "container",
        "status": "running",
        "gateway_token": "token",
        "access_point_id": "fsap-0123456789abcdef0",
        "desired_release_id": "candidate",
        "release_update_id": "update",
        "release_revision": 1,
        "provision_claim_id": "claim",
        "provision_attempt_token": "claim",
    }
    update = {
        "owner_id": "owner",
        "update_id": "update",
        "update_type": "release",
        "status": "applying",
        "changes": {"target_release_id": "candidate"},
        "phase": phase,
        "target_release_id": "candidate",
        "release_revision": 1,
        "apply_claim_id": "claim",
        "container_id": "container",
        "prior_task_definition_arn": "arn:old",
        "prior_desired_count": 1,
        "target_desired_count": 1,
        f"{phase}_at": "now",
    }
    return container, update


def _recovery_set():
    return {
        "recovery_set_id": "rs-123",
        "owner_id": "owner",
        "update_id": "update",
        "status": "verified",
        "manifest_bucket": "artifacts",
        "manifest_key": "recovery-sets/owner/rs-123/manifest.json",
        "manifest_version_id": "version-1",
        "manifest_sha256": "3" * 64,
        "release_id": "candidate",
        "release_image_digest": f"sha256:{'a' * 64}",
        "source_service_arn": "arn:service",
        "source_task_arn": "arn:old-task",
        "source_task_definition_arn": "arn:old",
        "source_image_digest": f"sha256:{'b' * 64}",
        "source_build_id": "source-build",
        "access_point_id": "fsap-0123456789abcdef0",
        "file_system_id": "fs-0123456789abcdef0",
        "efs_root": "/users/owner",
        "drain_observed_at": "2026-09-09T12:00:00+00:00",
        "drain_evidence_sha256": "6" * 64,
        "stable_replica_inventory_sha256": "7" * 64,
        "shared_db_generation": "8" * 64,
        "agent_db_generations": {},
        "efs_archive_key": "recovery-sets/owner/rs-123/efs/owner-root.tar",
        "efs_archive_version_id": "version-2",
        "efs_archive_etag": "etag",
        "efs_archive_size": 1,
        "efs_archive_sha256": "4" * 64,
        "efs_tree_manifest_sha256": "5" * 64,
        "provider": {"name": "lightbulb-owner-recovery-set", "version": "1"},
        "tools": {"litestream": "0.5.15", "sqlite": "3"},
        "verified_at": "2026-09-09T12:00:00Z",
    }


def _source():
    return {
        "service_arn": "arn:service",
        "task_arn": "arn:old-task",
        "task_definition_arn": "arn:old",
        "image_digest": f"sha256:{'b' * 64}",
        "build_id": "source-build",
        "access_point_id": "fsap-0123456789abcdef0",
        "file_system_id": "fs-0123456789abcdef0",
        "efs_root": "/users/owner",
        "shared_schema_version": 15,
        "agent_schema_version": 19,
    }


def _drain():
    return {
        "observed_at": "2026-09-09T12:00:00+00:00",
        "service_arn": "arn:service",
        "service_desired_count": 0,
        "service_running_count": 0,
        "service_pending_count": 0,
        "stopped_tasks": [
            {
                "task_arn": "arn:old-task",
                "last_status": "STOPPED",
                "task_definition_arn": "arn:old",
                "image_digest": f"sha256:{'b' * 64}",
                "exit_code": 0,
                "stop_code": "UserInitiated",
                "stopped_reason": "service scale down",
            }
        ],
    }


@pytest.mark.asyncio
async def test_release_executor_runs_the_fenced_phase_sequence(monkeypatch):
    from core.services import openclaw_release_executor as executor

    container, update = _reserved()
    phases = []

    async def read(_owner, _update):
        return dict(container), dict(update)

    async def checkpoint(_container, _update, _claim, *, expected_phase, next_phase, fields=None, remove_fields=()):
        assert expected_phase == update["phase"]
        phases.append(next_phase)
        update.update(fields or {})
        update["phase"] = next_phase
        update[f"{next_phase}_at"] = "now"
        for field in remove_fields:
            update.pop(field, None)
        return True

    runtime = SimpleNamespace(
        snapshot=AsyncMock(
            return_value={
                "service_arn": "arn:service",
                "task_definition_arn": "arn:old",
                "desired_count": 1,
                "running_count": 1,
                "pending_count": 0,
            }
        ),
        prepare=AsyncMock(return_value="arn:target"),
        recover=AsyncMock(return_value=_recovery_set()),
        running_tasks=AsyncMock(
            return_value=[
                {
                    "task_arn": "arn:old-task",
                    "task_definition_arn": "arn:old",
                    "last_status": "RUNNING",
                    "image_digest": f"sha256:{'b' * 64}",
                    "ip": "10.0.0.1",
                }
            ]
        ),
        stop=AsyncMock(),
        drain=AsyncMock(return_value=_drain()),
        stage=AsyncMock(return_value=("1" * 64, "2" * 64)),
        activate=AsyncMock(),
        start=AsyncMock(),
        accept=AsyncMock(return_value={"task_arn": "arn:new-task"}),
    )
    monkeypatch.setattr(executor, "_read_claimed", read)
    monkeypatch.setattr(executor.update_repo, "checkpoint_owner_release", checkpoint)
    monkeypatch.setattr(executor.update_repo, "finish_owner_release", AsyncMock(return_value=True))
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())
    monkeypatch.setattr(
        "core.gateway.operator_rpc.gateway_hello_oneshot",
        AsyncMock(return_value={"server": {"buildId": "source-build"}}),
    )
    monkeypatch.setattr(executor.settings, "EFS_FILE_SYSTEM_ID", "fs-0123456789abcdef0")

    assert await executor.execute_claimed_owner_release("owner", "update", "claim", runtime=runtime)
    assert phases == [
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
        "validating",
    ]
    runtime.prepare.assert_awaited_once()
    runtime.recover.assert_awaited_once()
    runtime.stop.assert_awaited_once()
    runtime.drain.assert_awaited_once_with("owner", ["arn:old-task"], ANY)
    runtime.activate.assert_awaited_once_with(
        "owner",
        "update",
        "1" * 64,
        "2" * 64,
        _release(),
        "claim",
        1,
        ANY,
    )
    runtime.start.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,skipped", [("drained", "stop"), ("validating", "start")])
async def test_release_executor_resumes_from_the_persisted_phase(monkeypatch, phase, skipped):
    from core.services import openclaw_release_executor as executor

    container, update = _reserved(phase)
    update.update(
        target_task_definition_arn="arn:target",
        pre_stop_task_arns=["arn:old-task"],
        pre_stop_captured_at="2026-09-09T11:59:00+00:00",
        source=_source(),
        drain_evidence=_drain(),
    )
    if phase == "validating":
        update.update(
            prior_config_sha256="1" * 64,
            staged_config_sha256="2" * 64,
            recovery_set=_recovery_set(),
        )
    runtime = SimpleNamespace(
        prepare=AsyncMock(return_value="arn:target"),
        recover=AsyncMock(return_value=_recovery_set()),
        running_tasks=AsyncMock(return_value=[{"task_arn": "arn:new-task"}]),
        stop=AsyncMock(),
        drain=AsyncMock(),
        stage=AsyncMock(return_value=("1" * 64, "2" * 64)),
        activate=AsyncMock(),
        start=AsyncMock(),
        accept=AsyncMock(return_value={"task_arn": "arn:new-task"}),
    )

    async def read(_owner, _update):
        return dict(container), dict(update)

    async def checkpoint(_container, _update, _claim, *, expected_phase, next_phase, fields=None, remove_fields=()):
        update.update(fields or {})
        update["phase"] = next_phase
        for field in remove_fields:
            update.pop(field, None)
        return True

    monkeypatch.setattr(executor, "_read_claimed", read)
    monkeypatch.setattr(executor.update_repo, "checkpoint_owner_release", checkpoint)
    monkeypatch.setattr(executor.update_repo, "finish_owner_release", AsyncMock(return_value=True))
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())

    assert await executor.execute_claimed_owner_release("owner", "update", "claim", runtime=runtime)
    getattr(runtime, skipped).assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_worker_and_uncertain_validation_keep_the_release_reserved(monkeypatch):
    from core.services import openclaw_release_executor as executor

    container, update = _reserved("validating")
    update.update(
        target_task_definition_arn="arn:target",
        staged_config_sha256="2" * 64,
        recovery_set=_recovery_set(),
    )
    monkeypatch.setattr(executor, "_read_claimed", AsyncMock(return_value=(container, update)))
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())
    finish = AsyncMock()
    monkeypatch.setattr(executor.update_repo, "finish_owner_release", finish)
    monkeypatch.setattr(executor.update_repo, "begin_owner_release_rollback", AsyncMock(return_value=False))

    stale = SimpleNamespace(accept=AsyncMock(side_effect=executor.OwnerReleaseSuperseded("moved")))
    uncertain = SimpleNamespace(accept=AsyncMock(side_effect=TimeoutError("ECS describe timed out")))

    assert not await executor.execute_claimed_owner_release("owner", "update", "claim", runtime=stale)
    assert not await executor.execute_claimed_owner_release("owner", "update", "claim", runtime=uncertain)
    finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_mismatch_never_finishes_or_clears_the_pointer(monkeypatch):
    from core.services import openclaw_release_executor as executor

    container, update = _reserved("validating")
    update.update(
        target_task_definition_arn="arn:target",
        staged_config_sha256="2" * 64,
        recovery_set=_recovery_set(),
    )
    monkeypatch.setattr(executor, "_read_claimed", AsyncMock(return_value=(container, update)))
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())
    finish = AsyncMock()
    monkeypatch.setattr(executor.update_repo, "finish_owner_release", finish)
    monkeypatch.setattr(executor.update_repo, "begin_owner_release_rollback", AsyncMock(return_value=False))
    runtime = SimpleNamespace(accept=AsyncMock(side_effect=executor.OwnerReleaseValidationError("wrong digest")))

    assert not await executor.execute_claimed_owner_release("owner", "update", "claim", runtime=runtime)
    finish.assert_not_awaited()
    assert container["release_update_id"] == "update"


@pytest.mark.asyncio
async def test_recovery_set_failure_never_prepares_or_mutates_the_owner(monkeypatch):
    from core.services import openclaw_release_executor as executor

    container, update = _reserved("recovery_requested")
    update.update(
        pre_stop_task_arns=["arn:old-task"],
        pre_stop_captured_at="2026-09-09T11:59:00+00:00",
        source=_source(),
        drain_evidence=_drain(),
        effect_kind="create_recovery_set",
        effect_claim_id="recovery-effect",
        effect_executor_id="claim",
    )
    monkeypatch.setattr(executor, "_read_claimed", AsyncMock(return_value=(container, update)))
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())
    checkpoint = AsyncMock()
    monkeypatch.setattr(executor.update_repo, "checkpoint_owner_release", checkpoint)
    runtime = SimpleNamespace(
        recover=AsyncMock(side_effect=executor.OwnerReleaseValidationError("no coherent backup primitive")),
        prepare=AsyncMock(),
        stage=AsyncMock(),
        start=AsyncMock(),
    )

    assert not await executor.execute_claimed_owner_release("owner", "update", "claim", runtime=runtime)
    runtime.prepare.assert_not_awaited()
    runtime.stage.assert_not_awaited()
    runtime.start.assert_not_awaited()
    checkpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_effect_token_cannot_reach_runtime_mutation(monkeypatch):
    from core.services import openclaw_release_executor as executor

    container, update = _reserved("stop_requested")
    update.update(
        pre_stop_task_arns=["arn:old-task"],
        pre_stop_captured_at="2026-09-09T11:59:00+00:00",
        source=_source(),
        effect_kind="stop_service",
        effect_claim_id="successor-effect",
        effect_executor_id="successor",
    )
    monkeypatch.setattr(executor, "_read_claimed", AsyncMock(return_value=(container, update)))

    with pytest.raises(executor.OwnerReleaseSuperseded):
        await executor.assert_current_effect(
            "owner",
            "update",
            "claim",
            1,
            phase="stop_requested",
            kind="stop_service",
            effect_id="stale-effect",
        )


@pytest.mark.asyncio
async def test_current_effect_renews_both_release_leases(monkeypatch):
    from core.services import openclaw_release_executor as executor

    container, update = _reserved("stop_requested")
    update.update(
        pre_stop_task_arns=["arn:old-task"],
        pre_stop_captured_at="2026-09-09T11:59:00+00:00",
        source=_source(),
        effect_kind="stop_service",
        effect_claim_id="effect",
        effect_executor_id="claim",
    )
    heartbeat = AsyncMock(return_value=True)
    monkeypatch.setattr(executor, "_read_claimed", AsyncMock(return_value=(container, update)))
    monkeypatch.setattr(executor.update_repo, "heartbeat_owner_release", heartbeat)

    assert await executor.assert_current_effect(
        "owner",
        "update",
        "claim",
        1,
        phase="stop_requested",
        kind="stop_service",
        effect_id="effect",
    ) == (container, update)
    heartbeat.assert_awaited_once_with(container, update, "claim")


@pytest.mark.asyncio
async def test_running_tasks_paginates_and_observes_each_listed_task(monkeypatch):
    from core.services import openclaw_release_executor as executor

    runtime = executor._ReleaseRuntime.__new__(executor._ReleaseRuntime)
    client = SimpleNamespace()
    client.list_tasks = MagicMock(
        side_effect=[
            {"taskArns": ["arn:one"], "nextToken": "page-2"},
            {"taskArns": ["arn:two"]},
        ]
    )
    client.describe_tasks = MagicMock(
        return_value={
            "tasks": [
                {"taskArn": "arn:one", "lastStatus": "RUNNING", "containers": [], "attachments": []},
                {"taskArn": "arn:two", "lastStatus": "RUNNING", "containers": [], "attachments": []},
            ]
        }
    )
    runtime.ecs = SimpleNamespace(_ecs=client, _cluster="cluster", _service_name=lambda _owner: "service")
    monkeypatch.setattr(executor.asyncio, "to_thread", AsyncMock(side_effect=lambda fn, **kwargs: fn(**kwargs)))

    tasks = await runtime.running_tasks("owner")

    assert [task["task_arn"] for task in tasks] == ["arn:one", "arn:two"]
    assert client.list_tasks.call_args_list[1].kwargs["nextToken"] == "page-2"


@pytest.mark.asyncio
async def test_drain_rejects_missing_describe_task_observation(monkeypatch):
    from core.services import openclaw_release_executor as executor

    runtime = executor._ReleaseRuntime.__new__(executor._ReleaseRuntime)
    runtime.snapshot = AsyncMock(
        return_value={
            "service_arn": "arn:service",
            "desired_count": 0,
            "running_count": 0,
            "pending_count": 0,
        }
    )
    runtime.service_tasks = AsyncMock(return_value=[])
    client = SimpleNamespace(describe_tasks=MagicMock(return_value={"tasks": [], "failures": []}))
    runtime.ecs = SimpleNamespace(_ecs=client, _cluster="cluster")
    monkeypatch.setattr(executor.asyncio, "to_thread", AsyncMock(side_effect=lambda fn, **kwargs: fn(**kwargs)))
    monkeypatch.setattr(executor.asyncio, "sleep", AsyncMock())

    with pytest.raises(executor.OwnerReleaseValidationError, match="omitted a captured task"):
        await runtime.drain("owner", ["arn:captured"], "2026-09-09T12:00:00+00:00")


@pytest.mark.asyncio
async def test_drain_reconciles_a_task_started_in_the_capture_to_stop_gap(monkeypatch):
    from core.services import openclaw_release_executor as executor

    runtime = executor._ReleaseRuntime.__new__(executor._ReleaseRuntime)
    runtime.snapshot = AsyncMock(
        return_value={
            "service_arn": "arn:service",
            "desired_count": 0,
            "running_count": 0,
            "pending_count": 0,
        }
    )
    runtime.service_tasks = AsyncMock(
        side_effect=[
            [{"task_arn": "arn:gap-task"}],
            [],
            [],
            [{"task_arn": "arn:gap-task", "created_at": "2026-09-09T12:00:01+00:00"}],
        ]
    )
    runtime._describe_tasks_exact = AsyncMock(
        return_value=[
            {
                "taskArn": "arn:captured",
                "lastStatus": "STOPPED",
                "taskDefinitionArn": "arn:old",
                "stopCode": "ServiceSchedulerInitiated",
                "stoppedReason": "Scaling activity initiated by service update",
                "containers": [{"name": "openclaw", "imageDigest": f"sha256:{'b' * 64}", "exitCode": 0}],
            },
            {
                "taskArn": "arn:gap-task",
                "lastStatus": "STOPPED",
                "taskDefinitionArn": "arn:old",
                "stopCode": "ServiceSchedulerInitiated",
                "stoppedReason": "Scaling activity initiated by service update",
                "containers": [{"name": "openclaw", "imageDigest": f"sha256:{'b' * 64}", "exitCode": 0}],
            },
        ]
    )
    monkeypatch.setattr(executor.asyncio, "sleep", AsyncMock())

    evidence = await runtime.drain("owner", ["arn:captured"], "2026-09-09T12:00:00+00:00")

    runtime._describe_tasks_exact.assert_awaited_once_with(["arn:captured", "arn:gap-task"])
    assert [task["task_arn"] for task in evidence["stopped_tasks"]] == ["arn:captured", "arn:gap-task"]


@pytest.mark.asyncio
async def test_release_reservation_refuses_applying_ordinary_update(monkeypatch):
    from core.services import openclaw_release_executor as executor

    pending = {
        "owner_id": "owner",
        "update_id": "release",
        "update_type": "release",
        "status": "pending",
        "changes": {"target_release_id": "candidate"},
    }
    monkeypatch.setattr(executor.update_repo, "get_by_id", AsyncMock(return_value=pending))
    monkeypatch.setattr(
        executor.update_repo,
        "get_in_flight",
        AsyncMock(return_value=[pending, {"update_id": "ordinary", "update_type": "resize", "status": "applying"}]),
    )
    reserve = AsyncMock()
    monkeypatch.setattr(executor.update_repo, "reserve_owner_apply", reserve)
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())

    assert not await executor.apply_owner_release("owner", "release")
    reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_snapshot_and_reservation_hold_shared_lifecycle_owner(monkeypatch):
    from core.services import openclaw_release_executor as executor

    pending = {
        "owner_id": "owner",
        "update_id": "release",
        "update_type": "release",
        "status": "pending",
        "changes": {"target_release_id": "candidate"},
    }
    held = False

    @asynccontextmanager
    async def lifecycle_guard(_owner_id):
        nonlocal held
        held = True
        try:
            yield {"owner_id": "owner", "task_definition_arn": "arn:old"}
        finally:
            held = False

    @asynccontextmanager
    async def mutation_guard(_owner_id):
        assert held
        yield {"owner_id": "owner", "task_definition_arn": "arn:old"}

    async def snapshot(_owner_id):
        assert held
        return {"task_definition_arn": "arn:old", "desired_count": 1}

    async def reserve(*_args, **_kwargs):
        assert held
        return False

    runtime = MagicMock()
    runtime.snapshot = AsyncMock(side_effect=snapshot)
    monkeypatch.setattr(executor.update_repo, "get_by_id", AsyncMock(return_value=pending))
    monkeypatch.setattr(executor.update_repo, "get_in_flight", AsyncMock(return_value=[pending]))
    monkeypatch.setattr(executor.update_repo, "reserve_owner_apply", AsyncMock(side_effect=reserve))
    monkeypatch.setattr(executor, "owner_lifecycle_guard", lifecycle_guard)
    monkeypatch.setattr(executor, "owner_mutation_guard", mutation_guard)
    monkeypatch.setattr(executor, "_ReleaseRuntime", lambda: runtime)
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())

    assert not await executor.apply_owner_release("owner", "release")
    assert held is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_definition_image", "running_task_definition", "running_image_digest"),
    [
        ("mutable.example/openclaw:latest", "arn:old", f"sha256:{'b' * 64}"),
        (f"repo.example/openclaw@sha256:{'b' * 64}", "arn:other", f"sha256:{'b' * 64}"),
        (f"repo.example/openclaw@sha256:{'b' * 64}", "arn:old", f"sha256:{'c' * 64}"),
    ],
)
async def test_release_reservation_rejects_drifted_assigned_source_before_effects(
    monkeypatch,
    task_definition_image,
    running_task_definition,
    running_image_digest,
):
    from core.services import openclaw_release_executor as executor

    pending = {
        "owner_id": "owner",
        "update_id": "release",
        "update_type": "release",
        "status": "pending",
        "changes": {"target_release_id": "candidate"},
    }
    container = {
        "owner_id": "owner",
        "id": "container",
        "status": "running",
        "task_definition_arn": "arn:old",
        "desired_release_id": "prior",
        "active_release_id": "prior",
        "release_revision": 4,
    }
    prior = SimpleNamespace(
        release_id="prior",
        digest_image=f"repo.example/openclaw@sha256:{'b' * 64}",
        image_digest=f"sha256:{'b' * 64}",
    )

    @asynccontextmanager
    async def lifecycle_guard(_owner_id):
        yield container

    @asynccontextmanager
    async def mutation_guard(_owner_id):
        yield container

    runtime = SimpleNamespace(
        snapshot=AsyncMock(return_value={"task_definition_arn": "arn:old", "desired_count": 1}),
        _task_definition=AsyncMock(
            return_value={"containerDefinitions": [{"name": "openclaw", "image": task_definition_image}]}
        ),
        running_tasks=AsyncMock(
            return_value=[
                {
                    "task_arn": "arn:task/source",
                    "task_definition_arn": running_task_definition,
                    "last_status": "RUNNING",
                    "image_digest": running_image_digest,
                }
            ]
        ),
    )
    reserve = AsyncMock()
    monkeypatch.setattr(executor.update_repo, "get_by_id", AsyncMock(return_value=pending))
    monkeypatch.setattr(executor.update_repo, "get_in_flight", AsyncMock(return_value=[pending]))
    monkeypatch.setattr(executor.update_repo, "reserve_owner_apply", reserve)
    monkeypatch.setattr(executor, "owner_lifecycle_guard", lifecycle_guard)
    monkeypatch.setattr(executor, "owner_mutation_guard", mutation_guard)
    monkeypatch.setattr(executor, "_ReleaseRuntime", lambda: runtime)
    monkeypatch.setattr(executor, "resolve_active_release", lambda _container: prior)
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())

    assert not await executor.apply_owner_release("owner", "release")
    reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_activation_failure_runs_fenced_compensating_rollback(monkeypatch):
    from core.services import openclaw_release_executor as executor

    container, update = _reserved("validating")
    update.update(
        target_task_definition_arn="arn:target",
        staged_config_sha256="2" * 64,
        pre_stop_task_arns=["arn:old-task"],
        pre_stop_captured_at="2026-09-09T11:59:00+00:00",
        source=_source(),
        drain_evidence=_drain(),
        recovery_set=_recovery_set(),
    )

    async def read(_owner, _update):
        return dict(container), dict(update)

    async def begin(_container, _update, _claim, *, effect_claim_id, failure_reason):
        update.update(
            phase="rollback_requested",
            rollback_requested_at="now",
            effect_kind="rollback",
            effect_claim_id=effect_claim_id,
            effect_executor_id="claim",
            failure_reason=failure_reason,
        )
        return True

    rollback_evidence = {
        "recovery_set_id": "rs-123",
        "source_task_definition_arn": "arn:old",
        "source_image_digest": f"sha256:{'b' * 64}",
        "gateway": "accepted",
    }
    runtime = SimpleNamespace(
        accept=AsyncMock(side_effect=executor.OwnerReleaseValidationError("target unhealthy")),
        rollback=AsyncMock(return_value=rollback_evidence),
    )
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(executor, "_read_claimed", read)
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())
    monkeypatch.setattr(executor.update_repo, "begin_owner_release_rollback", begin)
    monkeypatch.setattr(executor.update_repo, "finish_owner_release_rollback", finish)

    assert await executor.execute_claimed_owner_release("owner", "update", "claim", runtime=runtime)
    runtime.rollback.assert_awaited_once()
    finish.assert_awaited_once()


def test_release_config_files_refuse_live_stage_and_backup_symlinks(monkeypatch, tmp_path):
    from core.services import openclaw_release_executor as executor

    owner = tmp_path / "owner"
    owner.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("do not touch")
    monkeypatch.setattr(executor.settings, "EFS_MOUNT_PATH", str(tmp_path))
    for name in (
        "openclaw.json",
        executor._release_filename("update", "stage"),
        executor._release_filename("update", "backup"),
    ):
        (owner / name).symlink_to(outside)

    with executor._OwnerConfigFiles("owner") as files:
        with pytest.raises(executor.OwnerReleaseValidationError, match="regular file"):
            files.read_regular("openclaw.json")
        with pytest.raises(executor.OwnerReleaseValidationError, match="regular file"):
            files.atomic_write_regular(executor._release_filename("update", "stage"), b"{}")
        with pytest.raises(executor.OwnerReleaseValidationError, match="regular file"):
            files.atomic_write_regular(executor._release_filename("update", "backup"), b"{}")
    assert outside.read_text() == "do not touch"
