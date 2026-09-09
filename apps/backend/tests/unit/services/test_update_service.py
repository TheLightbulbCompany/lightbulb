"""Tests for ordinary update execution and release-only image admission."""

import asyncio

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from core.services import update_service


def _claimed_repo(update_item: dict) -> MagicMock:
    repo = MagicMock()
    repo.claim_apply = AsyncMock(return_value={**update_item, "status": "applying"})
    repo.claim_ordinary_owner = AsyncMock(return_value=True)
    repo.assert_ordinary_owner = AsyncMock(return_value=True)
    repo.release_ordinary_owner = AsyncMock(return_value=True)
    repo.heartbeat_apply = AsyncMock(return_value=True)
    repo.set_apply_target = AsyncMock(return_value=True)
    repo.finish_claimed_apply = AsyncMock(return_value=True)
    return repo


@pytest.mark.asyncio
async def test_queue_fleet_task_definition_refresh_is_atomic_and_skips_conflicts():
    rows = [
        {
            "owner_id": "owner-a",
            "service_name": "service-a",
            "access_point_id": "fsap-a",
            "task_definition_arn": "arn:a",
            "status": "running",
        },
        {
            "owner_id": "owner-b",
            "service_name": "service-b",
            "access_point_id": "fsap-b",
            "task_definition_arn": "arn:b",
            "status": "stopped",
        },
        {
            "owner_id": "owner-provisioning",
            "service_name": "service-provisioning",
            "access_point_id": "fsap-provisioning",
            "task_definition_arn": "arn:provisioning",
            "status": "provisioning",
        },
    ]
    rows.extend(
        [
            {
                "owner_id": "owner-error",
                "service_name": "service-error",
                "access_point_id": "fsap-error",
                "task_definition_arn": "arn:error",
                "status": "error",
            },
            {
                "owner_id": "owner-claimed-error",
                "service_name": "service-claimed-error",
                "access_point_id": "fsap-claimed-error",
                "task_definition_arn": "arn:claimed-error",
                "status": "error",
                "provision_claim_id": "live-provision",
            },
            {
                "owner_id": "owner-incomplete-error",
                "service_name": "service-incomplete-error",
                "status": "error",
            },
        ]
    )
    repo = MagicMock()
    repo.get_in_flight = AsyncMock(
        side_effect=[
            [],
            [{"changes": {"new_image": "image:already-rolling"}}],
            [],
        ]
    )
    repo.create = AsyncMock()

    with (
        patch.object(update_service, "container_repo") as container_repo,
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "utc_now_iso", return_value="2026-08-15T12:00:00+00:00"),
    ):
        container_repo.scan_all = AsyncMock(return_value=rows)
        queued, skipped = await update_service.queue_fleet_task_definition_refresh("Protocol rollout")

    assert (queued, skipped) == (2, 4)
    assert [call.kwargs["owner_id"] for call in repo.create.await_args_list] == ["owner-a", "owner-error"]
    for call in repo.create.await_args_list:
        assert call.kwargs == {
            "owner_id": call.kwargs["owner_id"],
            "update_type": update_service.TASK_DEFINITION_REFRESH_KIND,
            "description": "Protocol rollout",
            "changes": {update_service.TASK_DEFINITION_REFRESH_CHANGE: True},
            "scheduled_at": "2026-08-15T12:00:00+00:00",
        }


@pytest.mark.asyncio
async def test_apply_task_definition_refresh_waits_for_protocol_rollout():
    owner_id, update_id = "owner-refresh", "upd-refresh"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": update_service.TASK_DEFINITION_REFRESH_KIND,
        "changes": {update_service.TASK_DEFINITION_REFRESH_CHANGE: True},
    }
    repo = _claimed_repo(update_item)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock(return_value="arn:new")
    ecs.rollout_config_lock_protocol = AsyncMock(return_value="arn:new")
    ecs.resize_user_container = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    ecs.prepare_config_lock_protocol_task_definition.assert_awaited_once_with(owner_id, _effect_fence=ANY)
    repo.set_apply_target.assert_awaited_once()
    ecs.rollout_config_lock_protocol.assert_awaited_once_with(
        owner_id,
        target_task_definition_arn="arn:new",
        _effect_fence=ANY,
    )
    ecs.resize_user_container.assert_not_awaited()
    assert repo.finish_claimed_apply.await_args.kwargs["status"] == "applied"


def _refresh_item(*, target: str | None = None) -> dict:
    item = {
        "owner_id": "owner-refresh",
        "update_id": "upd-refresh",
        "update_type": update_service.TASK_DEFINITION_REFRESH_KIND,
        "changes": {update_service.TASK_DEFINITION_REFRESH_CHANGE: True},
        "status": "applying",
    }
    if target is not None:
        item["target_task_definition_arn"] = target
    return item


@pytest.mark.asyncio
async def test_ordinary_update_that_loses_owner_claim_runs_no_effect():
    item = _refresh_item()
    repo = _claimed_repo(item)
    repo.claim_ordinary_owner = AsyncMock(return_value=False)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock()
    ecs.rollout_config_lock_protocol = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        assert not await update_service._apply_claimed_update(item, "claim-old")

    ecs.prepare_config_lock_protocol_task_definition.assert_not_awaited()
    ecs.rollout_config_lock_protocol.assert_not_awaited()
    assert repo.finish_claimed_apply.await_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_refresh_crash_before_register_leaves_claim_for_stale_recovery():
    item = _refresh_item()
    repo = _claimed_repo(item)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock(side_effect=asyncio.CancelledError)
    ecs.rollout_config_lock_protocol = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        pytest.raises(asyncio.CancelledError),
    ):
        await update_service._apply_claimed_update(item, "claim-old")

    repo.set_apply_target.assert_not_awaited()
    repo.finish_claimed_apply.assert_not_awaited()
    ecs.rollout_config_lock_protocol.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_crash_after_register_before_target_persist_never_touches_service():
    item = _refresh_item()
    repo = _claimed_repo(item)
    repo.set_apply_target = AsyncMock(side_effect=asyncio.CancelledError)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock(return_value="arn:orphan")
    ecs.rollout_config_lock_protocol = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        pytest.raises(asyncio.CancelledError),
    ):
        await update_service._apply_claimed_update(item, "claim-old")

    ecs.rollout_config_lock_protocol.assert_not_awaited()
    repo.finish_claimed_apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_recovery_reuses_stored_target_after_service_update_interruption():
    item = _refresh_item()
    repo = _claimed_repo(item)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock(return_value="arn:target")
    ecs.rollout_config_lock_protocol = AsyncMock(side_effect=asyncio.CancelledError)

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        pytest.raises(asyncio.CancelledError),
    ):
        await update_service._apply_claimed_update(item, "claim-old")

    repo.set_apply_target.assert_awaited_once()

    reclaimed = _refresh_item(target="arn:target")
    repo.finish_claimed_apply = AsyncMock(return_value=True)
    ecs.prepare_config_lock_protocol_task_definition.reset_mock()
    ecs.rollout_config_lock_protocol = AsyncMock(return_value="arn:target")
    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        assert await update_service._apply_claimed_update(reclaimed, "claim-new") is True

    ecs.prepare_config_lock_protocol_task_definition.assert_not_awaited()
    ecs.rollout_config_lock_protocol.assert_awaited_once_with(
        "owner-refresh",
        target_task_definition_arn="arn:target",
        _effect_fence=ANY,
    )


@pytest.mark.asyncio
async def test_refresh_recovery_after_stability_before_terminal_write_reuses_target():
    item = _refresh_item(target="arn:target")
    repo = _claimed_repo(item)
    repo.finish_claimed_apply = AsyncMock(side_effect=asyncio.CancelledError)
    ecs = MagicMock()
    ecs.prepare_config_lock_protocol_task_definition = AsyncMock()
    ecs.rollout_config_lock_protocol = AsyncMock(return_value="arn:target")

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        pytest.raises(asyncio.CancelledError),
    ):
        await update_service._apply_claimed_update(item, "claim-old")

    repo.finish_claimed_apply = AsyncMock(return_value=True)
    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        assert await update_service._apply_claimed_update(item, "claim-new") is True
    assert ecs.rollout_config_lock_protocol.await_count == 2
    assert all(
        call.kwargs["target_task_definition_arn"] == "arn:target"
        for call in ecs.rollout_config_lock_protocol.await_args_list
    )


@pytest.mark.asyncio
async def test_stale_refresh_worker_runs_only_the_winning_reclaim():
    stale = _refresh_item(target="arn:target")
    lost = {**stale, "owner_id": "owner-lost", "update_id": "upd-lost"}
    repo = MagicMock()
    repo.list_stale_applying = AsyncMock(return_value=[stale, lost])
    repo.claim_stale_apply = AsyncMock(side_effect=[stale, None])

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "_apply_claimed_update", new=AsyncMock()) as apply_claimed,
    ):
        await update_service._recover_stale_task_definition_refreshes()

    assert repo.claim_stale_apply.await_count == 2
    apply_claimed.assert_awaited_once()
    assert apply_claimed.await_args.args[0] == stale


@pytest.mark.asyncio
async def test_migrate_removed_config_keys_scrubs_defaults_and_entries():
    with patch.object(update_service, "strip_removed_agent_keys", new=AsyncMock()) as strip_mock:
        await update_service.migrate_removed_config_keys("owner_1")

    strip_mock.assert_awaited_once()
    _args, kwargs = strip_mock.await_args
    assert kwargs["defaults_keys"] == ("embeddedHarness",)
    # the AgentEntrySchema removals are scrubbed from agents.list[] entries too
    assert {
        "embeddedHarness",
        "embeddedPi",
        "agentRuntime",
        "systemPromptOverride",
    } <= set(kwargs["entry_keys"])


@pytest.mark.asyncio
async def test_ordinary_image_row_fails_before_any_runtime_mutation():
    owner_id, update_id = "owner_1", "legacy-image-row"
    repo = _claimed_repo(
        {
            "owner_id": owner_id,
            "update_id": update_id,
            "update_type": "image_update",
            "changes": {"new_image": "unregistered.example/openclaw:mutable"},
        }
    )
    ecs = MagicMock()
    ecs.resize_user_container = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is False
    ecs.resize_user_container.assert_not_awaited()
    failed = repo.finish_claimed_apply.await_args_list[-1]
    assert failed.kwargs["status"] == "failed"
    assert "registered OpenClaw release" in failed.kwargs["failure_reason"]


@pytest.mark.asyncio
async def test_queue_image_update_translates_exact_registry_image_to_release(monkeypatch):
    release = MagicMock(release_id="qualified-9.3")
    monkeypatch.setattr("core.services.openclaw_release.release_by_image", lambda _image: release)
    create = AsyncMock(return_value={"update_id": "release-row"})
    monkeypatch.setattr(update_service.update_repo, "create", create)

    item = await update_service.queue_image_update("owner", "registered:tag", force_by="admin")

    assert item == {"update_id": "release-row"}
    create.assert_awaited_once_with(
        owner_id="owner",
        update_type="release",
        description="Image update to registered:tag",
        changes={"target_release_id": "qualified-9.3"},
        force_by="admin",
    )


@pytest.mark.asyncio
async def test_apply_update_skips_migration_for_resize_only_update():
    """A cpu/memory-only resize (no new_image) keeps the same image, so
    there's no schema change to migrate for — the migration must not run."""
    owner_id, update_id = "owner_2", "upd_2"
    update_item = {
        "owner_id": owner_id,
        "update_id": update_id,
        "update_type": "resize",
        "changes": {"new_cpu": 1024, "new_memory": 2048},
    }

    repo = _claimed_repo(update_item)

    ecs = MagicMock()
    ecs.resize_user_container = AsyncMock()
    migrate = AsyncMock()
    heal = AsyncMock()

    with (
        patch.object(update_service, "update_repo", repo),
        patch.object(update_service, "migrate_removed_config_keys", migrate),
        patch.object(update_service, "heal_seeded_files", heal),
        patch("core.containers.get_ecs_manager", return_value=ecs),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is True
    migrate.assert_not_awaited()
    heal.assert_not_awaited()
    ecs.resize_user_container.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_update_returns_false_when_claim_is_not_acquired():
    owner_id, update_id = "owner_x", "upd_x"

    repo = MagicMock()
    repo.claim_apply = AsyncMock(return_value=None)
    repo.get_by_id = AsyncMock(return_value=None)

    with (
        patch.object(update_service, "update_repo", repo),
    ):
        result = await update_service.apply_update(owner_id, update_id)

    assert result is False
    repo.claim_apply.assert_awaited_once()
