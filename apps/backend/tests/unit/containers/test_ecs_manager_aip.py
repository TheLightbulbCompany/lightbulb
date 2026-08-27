"""Tests for per-owner task-role threading + bedrock AIP provision wiring."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.billing.bedrock_gate_tokens import persist_gate_token_hash as _REAL_PERSIST
from core.containers.ecs_manager import EcsManager


@pytest.fixture(autouse=True)
def _provision_lease_stub(monkeypatch):
    """provision_user_container is now lease-fenced: a direct call (the
    deliberate-door path these tests exercise) takes the lease over and
    checkpoints it, which are real DynamoDB conditional writes. Stub the lease
    primitives so the tests keep exercising the provisioning logic itself;
    the lease semantics have their own moto-backed suite
    (test_container_repo_provision_claim.py)."""
    monkeypatch.setattr(
        "core.repositories.container_repo.takeover_provision",
        AsyncMock(return_value="test-claim"),
    )
    monkeypatch.setattr(
        "core.repositories.container_repo.update_container_leased",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "core.repositories.container_repo.release_provision_to_error",
        AsyncMock(),
    )


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


def test_build_kwargs_marks_backend_config_lock_protocol(manager, mock_ecs_client):
    kwargs = manager._build_register_kwargs_from_base(
        "fsap-user",
        environment_for_task=[
            {"name": "CODEX_HOME", "value": "/state/codex"},
            {"name": "ISOL8_CONFIG_LOCK_PROTOCOL", "value": "stale-value"},
        ],
    )
    environment = kwargs["containerDefinitions"][0]["environment"]
    assert {"name": "CODEX_HOME", "value": "/state/codex"} in environment
    assert [entry for entry in environment if entry["name"] == "ISOL8_CONFIG_LOCK_PROTOCOL"] == [
        {"name": "ISOL8_CONFIG_LOCK_PROTOCOL", "value": "guard-v1"}
    ]


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

        # claim_id/attempt_token are accepted and IGNORED: this fake stands in
        # for the whole repo, and its store never carries a lease (takeover is
        # stubbed too). The swap's lease clause has its own moto-backed suite in
        # tests/unit/billing/test_bedrock_gate_tokens.py.
        async def fake_swap(owner_id, *, new_hash, superseded, expected_prior_hash, claim_id=None, attempt_token=None):
            row = store.get(owner_id)
            if row is None:
                return "absent", None
            if row.get("bedrock_gate_token_hash") != expected_prior_hash:
                return "conflict", dict(row)
            row["bedrock_gate_token_hash"] = new_hash
            row["bedrock_gate_superseded_hashes"] = superseded
            return "written", None

        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", fake_get_by_owner_id)
        monkeypatch.setattr("core.repositories.container_repo.upsert", fake_upsert)
        monkeypatch.setattr("core.repositories.container_repo.update_fields", fake_update_fields)
        monkeypatch.setattr("core.repositories.container_repo.swap_gate_token_hash", fake_swap)
        # Put the REAL persist back over conftest's blanket stub: this test
        # asserts the hash actually lands on the row, so the swap has to run.
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.persist_gate_token_hash", _REAL_PERSIST)

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
        # The gate-token hash lands the same way, but NOT through the upsert:
        # mint's own swap no-opped on the absent row, so create_user_service
        # re-asserts it through container_repo.swap_gate_token_hash once the
        # upsert has created the row (else deny_gate_token / rotation silently
        # no-op until the next reprovision).
        from core.billing.bedrock_gate_tokens import hash_token
        from tests.unit.containers.conftest import STUB_GATE_TOKEN

        assert row.get("bedrock_gate_token_hash") == hash_token(STUB_GATE_TOKEN)
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

        # Revoked owner row. The C6 guard reads the row ONCE up front (the
        # single get_by_owner_id at ecs_manager.py:1554, before the mint block)
        # and checks existing.get("bedrock_revoked") (ecs_manager.py:1586) — a
        # single-read approach, no second round-trip inside the mint block.
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


class TestTrialProvisionMint:
    """`bedrock_trial` — the no-card free tier. A trial owner is a REAL Bedrock
    owner (the grant is NOT skipped); what makes the tier safe is that the mint
    is scoped to ONE tier, and build_owner_bedrock_policy pins the owner's IAM
    role to exactly the AIPs it is handed. See TestTrialProvisionPolicyPin for
    the assertion on the generated policy document."""

    @staticmethod
    def _trial_arns() -> dict[str, str]:
        return {"sonnet": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/s"}

    @pytest.mark.asyncio
    async def test_trial_mints_only_sonnet_and_keeps_the_bedrock_grant(self, manager, monkeypatch):
        arns = self._trial_arns()
        role_arn = "arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-user_trial"

        ensure_aips = AsyncMock(return_value=arns)
        ensure_role = AsyncMock(return_value=role_arn)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", ensure_aips)
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", ensure_role)
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())

        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
        manager._service_exists = MagicMock(return_value=None)
        create_svc = AsyncMock(return_value="openclaw-user_trial-hash")
        write_cfg = AsyncMock()
        manager.create_user_service = create_svc
        manager.write_user_configs = write_cfg
        manager.start_user_service = AsyncMock()

        await manager.provision_user_container("user_trial", provider_choice="bedrock_trial", owner_type="personal")

        # ONE tier. `tiers=["sonnet"]` is what makes ensure_owner_aips stamp
        # plan="trial" on the reverse rows (the meter Lambda's kill-switch arm).
        ensure_aips.assert_awaited_once_with("user_trial", tiers=["sonnet"])
        # The grant is NOT skipped — a trial owner needs a real, scoped role.
        ensure_role.assert_awaited_once_with("user_trial", arns, skip_bedrock_grant=False)
        assert create_svc.call_args.kwargs["task_role_arn"] == role_arn
        # The trial CONFIG MODE + the one-tier AIP map reach the config writer.
        assert write_cfg.call_args.kwargs["provider_choice"] == "bedrock_trial"
        assert write_cfg.call_args.kwargs["aip_arns"] == arns

    @pytest.mark.asyncio
    async def test_trial_gets_no_acp_harness_env(self, manager, monkeypatch):
        """derive_enabled_harnesses returns [] for bedrock_trial — no ACP on the
        free tier — so the task must carry no Claude-Code Bedrock env either.
        (It could not be built anyway: the harness env needs a haiku AIP the
        trial owner does not have.)"""
        arns = self._trial_arns()
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", AsyncMock(return_value=arns))
        monkeypatch.setattr(
            "core.billing.bedrock_aip.ensure_owner_bedrock_role",
            AsyncMock(return_value="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-user_trial"),
        )
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())
        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
        manager._service_exists = MagicMock(return_value=None)
        create_svc = AsyncMock(return_value="openclaw-user_trial-hash")
        manager.create_user_service = create_svc
        manager.write_user_configs = AsyncMock()
        manager.start_user_service = AsyncMock()

        await manager.provision_user_container("user_trial", provider_choice="bedrock_trial", owner_type="personal")

        env_names = {e["name"] for e in create_svc.call_args.kwargs["environment_for_task"]}
        assert "CLAUDE_CODE_USE_BEDROCK" not in env_names
        assert "ANTHROPIC_MODEL" not in env_names

    @pytest.mark.asyncio
    async def test_revoked_trial_owner_provision_does_not_regrant_bedrock(self, manager, monkeypatch):
        """The $3/day cap kill-switch revokes a trial owner's Bedrock grant. A
        re-provision must not silently restore it — same C6 guard the paid path
        has, and on the free tier it is the ONLY thing stopping spend."""
        arns = self._trial_arns()
        ensure_role = AsyncMock(return_value="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-user_capped")
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", AsyncMock(return_value=arns))
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", ensure_role)
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "user_capped",
                    "bedrock_revoked": True,
                    "hook_token": "ht",
                }
            ),
        )
        manager._service_exists = MagicMock(return_value=None)
        manager.create_user_service = AsyncMock(return_value="openclaw-user_capped-hash")
        manager.write_user_configs = AsyncMock()
        manager.start_user_service = AsyncMock()

        await manager.provision_user_container("user_capped", provider_choice="bedrock_trial", owner_type="personal")

        ensure_role.assert_awaited_once_with("user_capped", arns, skip_bedrock_grant=True)


class TestBedrockGateTokenProvision:
    """bedrock-gate wiring: mint a per-owner gate token on EVERY Bedrock
    provision; inject the bearer/http1 task env whenever the gate URL resolves
    (the standard path). In break-glass 'direct' mode the bearer env is
    withheld — injecting it while baseUrl targets real Bedrock would 403 direct
    invocation (the amazon-bedrock plugin switches to httpBearerAuth on the env
    var regardless of baseUrl)."""

    _ARNS = {
        "opus": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/o",
        "sonnet": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/s",
        "haiku": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/h",
    }

    def _wire_mint(self, manager, monkeypatch):
        monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_aips", AsyncMock(return_value=self._ARNS))
        monkeypatch.setattr(
            "core.billing.bedrock_aip.ensure_owner_bedrock_role",
            AsyncMock(return_value="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-x"),
        )
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())
        mint = AsyncMock(return_value="GATE_TOKEN_ABC")
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.mint_gate_token", mint)
        manager._service_exists = MagicMock(return_value=None)
        manager.create_user_service = AsyncMock(return_value="openclaw-x-hash")
        manager.write_user_configs = AsyncMock()
        manager.start_user_service = AsyncMock()
        return mint

    @pytest.mark.asyncio
    async def test_token_minted_even_in_break_glass_direct_mode(self, manager, mock_settings, monkeypatch):
        """Minted even when the resolver is in break-glass 'direct' mode — the
        deny fence stays armed fleet-wide and returning to the gate never
        requires a secret rollout. (The conftest autouse default patches the
        resolver to "" = direct.)"""
        mint = self._wire_mint(manager, monkeypatch)
        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))

        await manager.provision_user_container("user_x", provider_choice="bedrock_claude", owner_type="personal")

        mint.assert_awaited_once_with("user_x", self._ARNS)
        # Direct mode -> no bearer/http1 env (injecting the bearer while
        # baseUrl targets real Bedrock would 403 direct invocation).
        env_names = {e["name"] for e in manager.create_user_service.call_args.kwargs["environment_for_task"]}
        assert "AWS_BEARER_TOKEN_BEDROCK" not in env_names
        assert "AWS_BEDROCK_FORCE_HTTP1" not in env_names
        # The mint hash still rides onto the row-creating upsert.
        assert manager.create_user_service.call_args.kwargs["bedrock_gate_token_hash"]

    @pytest.mark.asyncio
    async def test_bearer_env_injected_when_gate_url_resolves(self, manager, mock_settings, monkeypatch):
        """The gateway is the standard path: a resolved gate URL means every
        Bedrock owner gets the bearer + HTTP/1.1 env — no per-owner flag."""
        monkeypatch.setattr("core.billing.bedrock_gate_url.gate_url", lambda: "https://gate.dev.example/")
        self._wire_mint(manager, monkeypatch)
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "user_x"}),
        )

        await manager.provision_user_container("user_x", provider_choice="bedrock_claude", owner_type="personal")

        env = {e["name"]: e["value"] for e in manager.create_user_service.call_args.kwargs["environment_for_task"]}
        assert env["AWS_BEARER_TOKEN_BEDROCK"] == "GATE_TOKEN_ABC"
        assert env["AWS_BEDROCK_FORCE_HTTP1"] == "1"

    @pytest.mark.asyncio
    async def test_isol8_api_url_env_injected(self, manager, mock_settings, monkeypatch):
        """Every provisioned container gets ISOL8_API_URL so the isol8 skill can
        live-read the connectable catalog from GET /api/v1/platform-info. It is
        unconditional (added before any provider branch) and derived from the
        single URL builder — a refactor of provision_user_container must not
        silently drop it.

        The builder reads ``settings.PUBLIC_API_BASE`` (wired by CDK per env),
        NOT the env name, so this pins that setting the way CDK does. Note the
        ``mock_settings`` fixture patches ``ecs_manager.settings`` only; the
        builder lives in ``atlas_seed`` and holds its own settings reference,
        which is why it needs its own patch here."""
        self._wire_mint(manager, monkeypatch)
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "user_x"}),
        )
        monkeypatch.setattr(
            "core.services.atlas_seed.settings.PUBLIC_API_BASE",
            "https://api-dev.flickmeon.ai",
            raising=False,
        )

        await manager.provision_user_container("user_x", provider_choice="bedrock_claude", owner_type="personal")

        env = {e["name"]: e["value"] for e in manager.create_user_service.call_args.kwargs["environment_for_task"]}
        assert env["ISOL8_API_URL"] == "https://api-dev.flickmeon.ai"

    @pytest.mark.asyncio
    async def test_bearer_env_absent_in_direct_mode(self, manager, mock_settings, monkeypatch):
        """Break-glass 'direct' mode (resolver returns "") -> baseUrl stays
        direct (config.py), so the bearer token MUST NOT be injected (it would
        break direct auth). Conftest default already patches the resolver to ""."""
        self._wire_mint(manager, monkeypatch)
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "user_x"}),
        )

        await manager.provision_user_container("user_x", provider_choice="bedrock_claude", owner_type="personal")

        env_names = {e["name"] for e in manager.create_user_service.call_args.kwargs["environment_for_task"]}
        assert "AWS_BEARER_TOKEN_BEDROCK" not in env_names

    @pytest.mark.asyncio
    async def test_revoked_owner_gate_token_denied(self, manager, mock_settings, monkeypatch):
        """A reprovision of a revoked owner re-asserts deny on the fresh token
        row (mint wrote denied=False) — the kill switch holds across reprovision."""
        mock_settings.BEDROCK_GATE_URL = "https://gate.dev.example/"
        self._wire_mint(manager, monkeypatch)
        deny = AsyncMock()
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.deny_gate_token", deny)
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "user_x", "bedrock_revoked": True}),
        )

        await manager.provision_user_container("user_x", provider_choice="bedrock_claude", owner_type="personal")

        deny.assert_awaited_once_with("user_x", True)


@pytest.fixture
def wired_bedrock_aip(monkeypatch):
    """Run the REAL mint (ensure_owner_aips + ensure_owner_bedrock_role) against
    fake AWS clients, so the provision path's GENERATED IAM policy document and
    reverse-index rows can be asserted on directly — not just the call args."""
    import core.billing.bedrock_aip as mod

    account = "877352799272"
    role_arn = f"arn:aws:iam::{account}:role/isol8-dev-owner-bedrock-x"

    def _create(**kwargs):
        profile_id = kwargs["modelSource"]["copyFrom"].split(":inference-profile/")[1]
        tier = next(t for t, p in mod.SYSTEM_PROFILE_ID.items() if p == profile_id)
        return {
            "inferenceProfileArn": (f"arn:aws:bedrock:us-east-1:{account}:application-inference-profile/{tier}-aip")
        }

    bedrock = MagicMock()
    bedrock.create_inference_profile.side_effect = _create

    iam = MagicMock()
    iam.get_role.return_value = {"Role": {"Arn": role_arn, "Tags": [{"Key": "trust_hardened", "Value": "1"}]}}

    table = MagicMock()

    monkeypatch.setattr(mod, "_bedrock_client", lambda: bedrock)
    monkeypatch.setattr(mod, "_iam_client", lambda: iam)
    monkeypatch.setattr(mod, "_aip_owners_table", lambda: table)
    monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value=account))
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
    monkeypatch.setattr(mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "", raising=False)
    # Fresh owner: no containers row yet (update_fields no-ops, exactly like DDB).
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr("core.repositories.container_repo.update_fields", AsyncMock(return_value=None))

    return MagicMock(bedrock=bedrock, iam=iam, table=table, role_arn=role_arn, account=account)


def _reverse_rows(table) -> list[dict]:
    return [call.kwargs["Item"] for call in table.put_item.call_args_list]


def _inline_policy_doc(iam) -> dict:
    """The scoped Bedrock inline policy the provision actually PUT on the role."""
    assert iam.put_role_policy.call_count == 1, "the Bedrock grant was skipped or double-put"
    kwargs = iam.put_role_policy.call_args.kwargs
    assert kwargs["PolicyName"] == "owner-bedrock"
    return json.loads(kwargs["PolicyDocument"])


def _invoke_allows(doc: dict) -> list[dict]:
    return [s for s in doc["Statement"] if s["Effect"] == "Allow" and "bedrock:InvokeModel" in s["Action"]]


def _denies_create_profile(doc: dict) -> bool:
    return any(s["Effect"] == "Deny" and s["Action"] == "bedrock:CreateInferenceProfile" for s in doc["Statement"])


class TestTrialProvisionPolicyPin:
    """THE security boundary. A trial owner has `exec: full` and can rewrite
    their own openclaw.json — the config is not a fence. What they cannot do is
    invoke a model their task role has no credential path to. These assert on
    the IAM policy document the provision actually generates."""

    async def _provision(self, manager, monkeypatch, owner_id: str, provider_choice: str) -> None:
        monkeypatch.setattr("core.containers.ecs_manager.put_metric", MagicMock())
        manager._service_exists = MagicMock(return_value=None)
        manager.create_user_service = AsyncMock(return_value=f"openclaw-{owner_id}-hash")
        manager.write_user_configs = AsyncMock()
        manager.start_user_service = AsyncMock()
        await manager.provision_user_container(owner_id, provider_choice=provider_choice, owner_type="personal")

    @pytest.mark.asyncio
    async def test_trial_role_carries_the_fence_no_invoke_at_all(self, manager, wired_bedrock_aip, monkeypatch):
        """THE FENCE: the owner role gets NO bedrock:Invoke — inference flows
        only through the bedrock-gate (the container authenticates with its
        bearer token; the GATE's role invokes the owner's AIP). The exec-full
        shell path — which no proxy can see — therefore AccessDenies. The AIP
        is still minted (it remains the attribution + pricing unit the gate
        invokes and the meter keys on)."""
        aws = wired_bedrock_aip
        await self._provision(manager, monkeypatch, "user_trial", "bedrock_trial")

        # Exactly one AIP minted, and it is sonnet — no opus, no haiku. The
        # single-tier mint is still the model-pinning boundary: the gate's
        # cross-tenant check only authorizes AIPs in the owner's own token row.
        assert aws.bedrock.create_inference_profile.call_count == 1
        rows = _reverse_rows(aws.table)
        assert len(rows) == 1
        assert rows[0]["owner_id"] == "user_trial"
        assert rows[0]["tier"] == "sonnet"
        # plan="trial" is what tells the meter Lambda to count-and-kill instead
        # of ingesting to Metronome (a trial owner has no Stripe customer).
        assert rows[0]["plan"] == "trial"

        doc = _inline_policy_doc(aws.iam)
        assert _invoke_allows(doc) == [], "owner roles must carry NO direct bedrock:Invoke (the gate is the only path)"
        # ...and they cannot mint themselves a profile either.
        assert _denies_create_profile(doc)

    @pytest.mark.asyncio
    async def test_paid_role_carries_the_same_fence(self, manager, wired_bedrock_aip, monkeypatch):
        """Paid mints three tiers (plan="paid") but the role is fenced exactly
        like trial: NO direct invoke — the gate is the only inference path for
        every Bedrock owner."""
        aws = wired_bedrock_aip
        await self._provision(manager, monkeypatch, "user_paid", "bedrock_claude")

        assert aws.bedrock.create_inference_profile.call_count == 3
        rows = _reverse_rows(aws.table)
        assert {r["tier"] for r in rows} == {"opus", "sonnet", "haiku"}
        assert {r["plan"] for r in rows} == {"paid"}

        doc = _inline_policy_doc(aws.iam)
        assert _invoke_allows(doc) == []
        assert _denies_create_profile(doc)
