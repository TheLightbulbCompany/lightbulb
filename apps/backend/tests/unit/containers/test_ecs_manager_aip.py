"""Tests for per-owner task-role threading + bedrock AIP provision wiring."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import EcsManager


@pytest.fixture
def mock_settings():
    with patch("core.containers.ecs_manager.settings") as s:
        s.AWS_REGION = "us-east-1"
        s.ENVIRONMENT = "dev"
        s.ECS_CLUSTER_ARN = "arn:aws:ecs:us-east-1:123456789:cluster/test-cluster"
        s.ECS_SUBNETS = "subnet-aaa,subnet-bbb"
        s.ECS_SECURITY_GROUP_ID = "sg-12345"
        s.CLOUD_MAP_SERVICE_ARN = "arn:aws:servicediscovery:us-east-1:123456789:service/srv-test"
        s.CLOUD_MAP_NAMESPACE_NAME = "isol8-dev.local"
        s.CLOUD_MAP_SERVICE_NAME = "openclaw-containers"
        s.EFS_FILE_SYSTEM_ID = "fs-test123"
        yield s


@pytest.fixture
def mock_ecs_client():
    client = MagicMock()
    client.describe_task_definition.return_value = {
        "taskDefinition": {
            "family": "isol8-dev-openclaw",
            "taskRoleArn": "arn:aws:iam::123456789:role/shared-task-role",
            "executionRoleArn": "arn:aws:iam::123456789:role/exec-role",
            "networkMode": "awsvpc",
            "containerDefinitions": [{"name": "openclaw", "image": "ghcr.io/openclaw:1"}],
            "volumes": [
                {
                    "name": "openclaw-workspace",
                    "efsVolumeConfiguration": {
                        "fileSystemId": "fs-test123",
                        "transitEncryption": "ENABLED",
                        "authorizationConfig": {"accessPointId": "fsap-base", "iam": "ENABLED"},
                    },
                }
            ],
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "512",
            "memory": "2048",
            "runtimePlatform": None,
        }
    }
    client.register_task_definition.return_value = {
        "taskDefinition": {
            "taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789:task-definition/isol8-dev-openclaw-user:9"
        }
    }
    return client


@pytest.fixture
def manager(mock_settings, mock_ecs_client):
    with patch("core.containers.ecs_manager.boto3") as mock_boto3:
        mock_boto3.client.side_effect = lambda service, **kw: mock_ecs_client if service == "ecs" else MagicMock()
        mgr = EcsManager()
    return mgr


def test_build_kwargs_overrides_task_role(manager, mock_ecs_client):
    kwargs = manager._build_register_kwargs_from_base(
        "fsap-user", task_role_arn="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"
    )
    assert kwargs["taskRoleArn"] == "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"


def test_build_kwargs_defaults_to_base_role_when_none(manager, mock_ecs_client):
    kwargs = manager._build_register_kwargs_from_base("fsap-user")
    assert kwargs["taskRoleArn"] == "arn:aws:iam::123456789:role/shared-task-role"


def test_register_task_definition_passes_owner_role(manager, mock_ecs_client):
    manager._register_task_definition(
        "fsap-user", task_role_arn="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"
    )
    reg_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
    assert reg_kwargs["taskRoleArn"] == "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"


class TestBedrockProvisionMint:
    @pytest.mark.asyncio
    async def test_bedrock_branch_mints_aips_role_and_threads_them(self, manager, monkeypatch):
        arns = {
            "opus": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/h",
        }
        role_arn = "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_abc"

        ensure_aips = AsyncMock(return_value=arns)
        ensure_role = AsyncMock(return_value=role_arn)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", ensure_aips)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", ensure_role)
        # EMF metric emit -> no-op in tests (see Task 9 mock-surface note).
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())

        # No existing service -> fresh-provision path.
        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
        manager._service_exists = MagicMock(return_value=None)
        create_svc = AsyncMock(return_value="openclaw-org_abc-hash")
        write_cfg = AsyncMock()
        start_svc = AsyncMock()
        manager.create_user_service = create_svc
        manager.write_user_configs = write_cfg
        manager.start_user_service = start_svc

        await manager.provision_user_container("org_abc", provider_choice="bedrock_claude", owner_type="org")

        ensure_aips.assert_awaited_once_with("org_abc")
        # Fresh (non-revoked) owner -> role minted WITH the Bedrock grant.
        ensure_role.assert_awaited_once_with("org_abc", arns, skip_bedrock_grant=False)
        # create_user_service got the per-owner role.
        assert create_svc.call_args.kwargs["task_role_arn"] == role_arn
        # write_user_configs got the AIP map.
        assert write_cfg.call_args.kwargs["aip_arns"] == arns
        # mint ran before start.
        assert start_svc.await_count == 1

    @pytest.mark.asyncio
    async def test_first_provision_persists_bedrock_fields_onto_the_row(self, manager, monkeypatch):
        """C-1 regression: on a BRAND-NEW bedrock owner the AIP/role mint runs
        BEFORE the containers row exists, so its container_repo.update_fields
        calls no-op (update_fields writes nothing when the row is absent). The
        row is first CREATED by create_user_service's upsert. If that upsert
        does not fold in bedrock_aip_arns/bedrock_role_arn, the row carries
        neither — and the next resize/restart re-register reads
        bedrock_role_arn off the row, gets None, and reverts the container to
        the shared (now Bedrock-less) task role -> every chat AccessDenies; and
        restore_owner_bedrock can't recover the owner (it reads the AIP map off
        the row). This test reproduces the real ordering with an in-memory
        container_repo (update_fields no-ops on an absent row, exactly like DDB)
        and asserts the row ends up carrying both fields.
        """
        arns = {
            "opus": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/h",
        }
        role_arn = "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_new"

        # In-memory container_repo that mirrors the real contract: update_fields
        # NO-OPS (returns None, writes nothing) when the row is absent; upsert
        # CREATES the row. Patched on the module both ecs_manager AND bedrock_aip
        # import so the mint's persistence and the upsert hit the same store.
        store: dict[str, dict] = {}

        async def fake_get_by_owner_id(owner_id):
            return store.get(owner_id)

        async def fake_upsert(owner_id, fields):
            row = store.get(owner_id) or {"owner_id": owner_id, "id": "c-1"}
            row.update(fields)
            row["owner_id"] = owner_id
            store[owner_id] = row
            return row

        async def fake_update_fields(owner_id, fields):
            row = store.get(owner_id)
            if row is None:
                return None  # exactly the DDB no-op that causes C-1
            row.update(fields)
            store[owner_id] = row
            return row

        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", fake_get_by_owner_id)
        monkeypatch.setattr("core.repositories.container_repo.upsert", fake_upsert)
        monkeypatch.setattr("core.repositories.container_repo.update_fields", fake_update_fields)

        # The mint returns the arns/role; its own update_fields calls no-op
        # because the row does not exist yet (the bug surface).
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", AsyncMock(return_value=arns))
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", AsyncMock(return_value=role_arn))
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())

        # Let create_user_service run FOR REAL (it owns the row-creating upsert);
        # stub only its AWS-side side effects + the post-create steps.
        manager._service_exists = MagicMock(return_value=None)
        manager._create_access_point = MagicMock(return_value="fsap-user")
        manager._register_task_definition = MagicMock(
            return_value="arn:aws:ecs:us-east-1:123456789:task-definition/isol8-dev-openclaw-user:9"
        )
        manager._ecs.create_service = MagicMock()
        manager.write_user_configs = AsyncMock()
        manager.start_user_service = AsyncMock()

        await manager.provision_user_container("org_new", provider_choice="bedrock_claude", owner_type="org")

        row = store["org_new"]
        assert row.get("bedrock_role_arn") == role_arn
        assert row.get("bedrock_aip_arns") == arns
        # And the per-owner role was passed to the task-def register (first boot
        # already correct in-memory; this guards the row-persist regression too).
        reg_kwargs = manager._register_task_definition.call_args.kwargs
        assert reg_kwargs["task_role_arn"] == role_arn

    @pytest.mark.asyncio
    async def test_revoked_owner_provision_does_not_regrant_bedrock(self, manager, monkeypatch):
        """A re-provision of a kill-switched (bedrock_revoked=True) owner mints
        the AIPs + role SHELL but passes skip_bedrock_grant=True so the scoped
        Bedrock policy is NOT re-put — the kill-switch holds (review C6)."""
        arns = {
            "opus": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/h",
        }
        role_arn = "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-org_rev"
        ensure_aips = AsyncMock(return_value=arns)
        ensure_role = AsyncMock(return_value=role_arn)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", ensure_aips)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", ensure_role)
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())

        # Revoked owner row. The C6 guard reads bedrock_revoked via its own
        # get_by_owner_id inside the mint block (runs before the main fetch);
        # _service_exists -> None routes to the fresh-provision branch, which is
        # fine — the load-bearing assertion is skip_bedrock_grant=True.
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "org_rev",
                    "bedrock_revoked": True,
                    "service_name": "openclaw-org_rev-hash",
                    "hook_token": "ht",
                }
            ),
        )
        manager._service_exists = MagicMock(return_value=None)
        manager.create_user_service = AsyncMock(return_value="openclaw-org_rev-hash")
        manager.write_user_configs = AsyncMock()
        manager.start_user_service = AsyncMock()

        await manager.provision_user_container("org_rev", provider_choice="bedrock_claude", owner_type="org")

        ensure_role.assert_awaited_once_with("org_rev", arns, skip_bedrock_grant=True)


class TestNonBedrockProvisionMint:
    """Every owner — not just Bedrock owners — runs under the per-owner ECS task
    role isol8-<env>-owner-bedrock-<owner_id>. A non-Bedrock owner mints the role
    SHELL (base policy + permissions boundary) with skip_bedrock_grant=True so it
    carries NO Bedrock inline grant and NO Bedrock AIPs, and the registered task
    def's taskRoleArn is the per-owner role — never the shared base role."""

    @pytest.mark.asyncio
    async def test_chatgpt_oauth_mints_owner_role_shell_no_aips(self, manager, monkeypatch):
        role_arn = "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-user_oauth"

        ensure_aips = AsyncMock()
        ensure_role = AsyncMock(return_value=role_arn)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", ensure_aips)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", ensure_role)
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())
        # chatgpt_oauth pre-task setup — stub both the token-owner resolve and
        # the EFS oauth.json pre-stage so no real OAuth row is needed.
        monkeypatch.setattr("core.services.oauth_service.resolve_token_owner", AsyncMock(return_value="user_oauth"))
        manager._pre_stage_oauth_for_user = AsyncMock()

        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
        manager._service_exists = MagicMock(return_value=None)
        create_svc = AsyncMock(return_value="openclaw-user_oauth-hash")
        manager.create_user_service = create_svc
        manager.write_user_configs = AsyncMock()
        manager.start_user_service = AsyncMock()

        await manager.provision_user_container("user_oauth", provider_choice="chatgpt_oauth", owner_type="personal")

        # Role minted as a SHELL: empty AIP map + skip_bedrock_grant=True.
        ensure_role.assert_awaited_once_with("user_oauth", {}, skip_bedrock_grant=True)
        # No Bedrock AIPs for a non-Bedrock owner.
        ensure_aips.assert_not_awaited()
        # The per-owner role is threaded to the task-def register — NOT the shared base.
        assert create_svc.call_args.kwargs["task_role_arn"] == role_arn
        # No AIP map written for a non-Bedrock owner.
        assert manager.write_user_configs.call_args.kwargs["aip_arns"] is None

    @pytest.mark.asyncio
    async def test_byo_key_mints_owner_role_shell_no_aips(self, manager, monkeypatch):
        role_arn = "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-user_byo"

        ensure_aips = AsyncMock()
        ensure_role = AsyncMock(return_value=role_arn)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", ensure_aips)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", ensure_role)
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())
        # byo_key pre-task setup — stub the per-user Secrets Manager wiring.
        manager._build_byo_secrets_for_user = AsyncMock(return_value=[])

        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
        manager._service_exists = MagicMock(return_value=None)
        create_svc = AsyncMock(return_value="openclaw-user_byo-hash")
        manager.create_user_service = create_svc
        manager.write_user_configs = AsyncMock()
        manager.start_user_service = AsyncMock()

        await manager.provision_user_container(
            "user_byo", provider_choice="byo_key", byo_provider="anthropic", owner_type="personal"
        )

        ensure_role.assert_awaited_once_with("user_byo", {}, skip_bedrock_grant=True)
        ensure_aips.assert_not_awaited()
        assert create_svc.call_args.kwargs["task_role_arn"] == role_arn

    @pytest.mark.asyncio
    async def test_non_bedrock_restart_uses_owner_role_not_base(self, manager, monkeypatch):
        """A non-Bedrock owner whose service exists but is scaled to zero
        restarts on the freshly minted per-owner role (the in-memory mint), never
        falling back to the shared base task role."""
        role_arn = "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-user_oauth"

        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", AsyncMock())
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", AsyncMock(return_value=role_arn))
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())
        monkeypatch.setattr("core.services.oauth_service.resolve_token_owner", AsyncMock(return_value="user_oauth"))
        manager._pre_stage_oauth_for_user = AsyncMock()

        # Service exists, scaled to zero -> restart/scale-up branch.
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "user_oauth",
                    "service_name": "openclaw-user_oauth-hash",
                    "access_point_id": "fsap-user",
                    "hook_token": "ht",
                    "gateway_token": "gt",
                    # NOTE: row carries NO bedrock_role_arn (legacy non-Bedrock row).
                }
            ),
        )
        monkeypatch.setattr("core.repositories.container_repo.update_fields", AsyncMock())
        manager._service_exists = MagicMock(return_value={"status": "ACTIVE", "desiredCount": 0, "runningCount": 0})
        manager.write_user_configs = AsyncMock()
        register = MagicMock(return_value="arn:aws:ecs:us-east-1:123456789:task-definition/isol8-dev-openclaw-user:9")
        manager._register_task_definition = register
        manager._ecs.update_service = MagicMock()
        manager._deregister_task_definition = MagicMock()
        manager._await_running_transition = AsyncMock()

        await manager.provision_user_container("user_oauth", provider_choice="chatgpt_oauth", owner_type="personal")

        # The restart re-register used the freshly-minted per-owner role, even
        # though the (legacy) row carried no bedrock_role_arn.
        assert register.call_args.kwargs["task_role_arn"] == role_arn
