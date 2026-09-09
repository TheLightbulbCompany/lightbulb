from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        running_tasks=AsyncMock(side_effect=[[{"task_arn": "arn:old-task"}], [{"task_arn": "arn:new-task"}]]),
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
        "prepared",
        "stop_requested",
        "drained",
        "staged",
        "activating",
        "activated",
        "start_requested",
        "validating",
        "validating",
    ]
    runtime.prepare.assert_awaited_once_with("owner", _release())
    runtime.stop.assert_awaited_once_with("owner")
    runtime.drain.assert_awaited_once_with("owner", ["arn:old-task"])
    runtime.activate.assert_awaited_once_with(
        "owner",
        "update",
        "1" * 64,
        "2" * 64,
        _release(),
        "claim",
        1,
    )
    runtime.start.assert_awaited_once_with("owner", "arn:target", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,skipped", [("drained", "prepare"), ("validating", "start")])
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
        update.update(prior_config_sha256="1" * 64, staged_config_sha256="2" * 64)
    runtime = SimpleNamespace(
        prepare=AsyncMock(),
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
    update.update(target_task_definition_arn="arn:target", staged_config_sha256="2" * 64)
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
    update.update(target_task_definition_arn="arn:target", staged_config_sha256="2" * 64)
    monkeypatch.setattr(executor, "_read_claimed", AsyncMock(return_value=(container, update)))
    monkeypatch.setattr(executor, "release_by_id", lambda _release_id: _release())
    finish = AsyncMock()
    monkeypatch.setattr(executor.update_repo, "finish_owner_release", finish)
    runtime = SimpleNamespace(accept=AsyncMock(side_effect=executor.OwnerReleaseValidationError("wrong digest")))

    assert not await executor.execute_claimed_owner_release("owner", "update", "claim", runtime=runtime)
    finish.assert_not_awaited()
    assert container["release_update_id"] == "update"
