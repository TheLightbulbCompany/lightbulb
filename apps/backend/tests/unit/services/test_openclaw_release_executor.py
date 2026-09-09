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
        "recovery_set_id": "owner/update/recovery-v1",
        "manifest_sha256": "3" * 64,
        "source_task_arns": ["arn:old-task"],
        "shared_db_generation": "shared-generation-1",
        "agent_db_generations": {"main": "agent-generation-1"},
        "efs_archive_sha256": "4" * 64,
        "workshop_archive_sha256": "5" * 64,
        "verified_at": "2026-09-09T12:00:00Z",
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
        prepare=AsyncMock(return_value="arn:target"),
        recover=AsyncMock(return_value=_recovery_set()),
        running_tasks=AsyncMock(return_value=[{"task_arn": "arn:old-task"}]),
        stop=AsyncMock(),
        drain=AsyncMock(
            return_value={
                "observed_at": "now",
                "service_desired_count": 0,
                "service_running_count": 0,
                "service_pending_count": 0,
                "stopped_task_arns": ["arn:old-task"],
            }
        ),
        stage=AsyncMock(return_value=("1" * 64, "2" * 64)),
        activate=AsyncMock(),
        start=AsyncMock(),
        accept=AsyncMock(return_value={"task_arn": "arn:new-task"}),
    )
    monkeypatch.setattr(executor, "_read_claimed", read)
    monkeypatch.setattr(executor.update_repo, "checkpoint_owner_release", checkpoint)
    monkeypatch.setattr(executor.update_repo, "finish_owner_release", AsyncMock(return_value=True))
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())

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
    runtime.drain.assert_awaited_once_with("owner", ["arn:old-task"])
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
        drain_evidence={
            "observed_at": "now",
            "service_desired_count": 0,
            "service_running_count": 0,
            "service_pending_count": 0,
            "stopped_task_arns": ["arn:old-task"],
        },
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

    stale = SimpleNamespace(accept=AsyncMock(side_effect=executor.OwnerReleaseSuperseded("moved")))
    uncertain = SimpleNamespace(accept=AsyncMock(side_effect=TimeoutError("ECS describe timed out")))

    assert not await executor.execute_claimed_owner_release("owner", "update", "claim", runtime=stale)
    with pytest.raises(TimeoutError):
        await executor.execute_claimed_owner_release("owner", "update", "claim", runtime=uncertain)
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
        drain_evidence={
            "observed_at": "now",
            "service_desired_count": 0,
            "service_running_count": 0,
            "service_pending_count": 0,
            "stopped_task_arns": ["arn:old-task"],
        },
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
    runtime.snapshot = AsyncMock(return_value={"desired_count": 0, "running_count": 0, "pending_count": 0})
    runtime.running_tasks = AsyncMock(return_value=[])
    client = SimpleNamespace(describe_tasks=MagicMock(return_value={"tasks": [], "failures": []}))
    runtime.ecs = SimpleNamespace(_ecs=client, _cluster="cluster")
    monkeypatch.setattr(executor.asyncio, "to_thread", AsyncMock(side_effect=lambda fn, **kwargs: fn(**kwargs)))
    monkeypatch.setattr(executor.asyncio, "sleep", AsyncMock())

    with pytest.raises(executor.OwnerReleaseValidationError, match="omitted a captured task"):
        await runtime.drain("owner", ["arn:captured"])


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
