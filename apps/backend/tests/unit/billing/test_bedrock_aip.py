"""Unit tests for core.billing.bedrock_aip — the per-owner IAM role and its policies."""

import json
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws


@pytest.fixture
def aip_tables(monkeypatch):
    """moto-mocked containers table."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # The containers row must exist before update_fields can patch it.
        client.put_item(
            TableName="test-containers",
            Item={
                "owner_id": {"S": "org_abc"},
                "id": {"S": "c-1"},
                "created_at": {"S": "2026-06-07T00:00:00Z"},
            },
        )
        # container_repo resolves the containers table via
        # core.dynamodb.get_table("containers") -> table_name() which prepends
        # the module-level _table_prefix (cached at import from settings,
        # default "lightbulb-dev-"). Setting the env var here is too late, so patch
        # the cached prefix directly to "test-" -> "test-containers".
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        # Reset the cached DDB resource so it is rebuilt INSIDE the moto mock
        # (a resource cached by an earlier test points at the real endpoint).
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", None)
        yield client


def _no_such_entity():
    return ClientError({"Error": {"Code": "NoSuchEntity", "Message": "not found"}}, "GetRole")


class TestEnsureRole:
    @pytest.mark.asyncio
    async def test_creates_role_with_boundary_and_base_but_no_bedrock_policy(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.get_role.side_effect = _no_such_entity()
        fake.create_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings,
            "BEDROCK_OWNER_BOUNDARY_ARN",
            "arn:aws:iam::877352799272:policy/lightbulb-dev-owner-bedrock-boundary",
            raising=False,
        )
        monkeypatch.setattr(
            mod.settings,
            "BEDROCK_OWNER_BASE_POLICY_ARN",
            "arn:aws:iam::877352799272:policy/lightbulb-dev-owner-bedrock-base",
            raising=False,
        )

        role_arn = await mod.ensure_owner_bedrock_role("org_abc")

        assert role_arn == "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"
        # create_role carries the permissions boundary.
        ck = fake.create_role.call_args.kwargs
        assert ck["RoleName"] == "lightbulb-dev-owner-bedrock-org_abc"
        assert ck["PermissionsBoundary"] == "arn:aws:iam::877352799272:policy/lightbulb-dev-owner-bedrock-boundary"
        # base policy attached.
        fake.attach_role_policy.assert_called_once_with(
            RoleName="lightbulb-dev-owner-bedrock-org_abc",
            PolicyArn="arn:aws:iam::877352799272:policy/lightbulb-dev-owner-bedrock-base",
        )
        # No Bedrock inline policy: the role is the AgentCore execution role, nothing more.
        assert all(c.kwargs["PolicyName"] != "owner-bedrock" for c in fake.put_role_policy.call_args_list)
        # role ARN persisted on the row.
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_role_arn"]["S"] == role_arn

    @pytest.mark.asyncio
    async def test_create_role_conflict_adopts_existing_role(self, aip_tables, monkeypatch):
        """Two concurrent provision runs both see NoSuchEntity on get_role and
        both call create_role. The loser must ADOPT the winner's role (a second
        get_role) instead of raising EntityAlreadyExists, so both runs converge
        on the same role_arn (the owner ruling: no more leases, both must succeed)."""
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.get_role.side_effect = [
            _no_such_entity(),
            {"Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}},
        ]
        fake.create_role.side_effect = ClientError(
            {"Error": {"Code": "EntityAlreadyExists", "Message": "already exists"}}, "CreateRole"
        )
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        role_arn = await mod.ensure_owner_bedrock_role("org_abc")

        assert role_arn == "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"
        assert fake.get_role.call_count == 2

    @pytest.mark.asyncio
    async def test_new_role_trust_policy_carries_sourcearn_sourceaccount(self, aip_tables, monkeypatch):
        """A freshly minted per-owner role's trust policy must condition the
        ecs-tasks assumption on aws:SourceAccount + aws:SourceArn so a confused
        deputy in another account/service cannot assume it (the per-owner role
        previously trusted ecs-tasks.amazonaws.com unconditionally)."""
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.get_role.side_effect = _no_such_entity()
        fake.create_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        await mod.ensure_owner_bedrock_role("org_abc")

        trust = json.loads(fake.create_role.call_args.kwargs["AssumeRolePolicyDocument"])
        stmt = trust["Statement"][0]
        assert stmt["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
        assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == "877352799272"
        assert stmt["Condition"]["ArnLike"]["aws:SourceArn"] == "arn:aws:ecs:us-east-1:877352799272:task/*"

    @pytest.mark.asyncio
    async def test_existing_role_backfills_conditioned_trust_policy(self, aip_tables, monkeypatch):
        """An already-minted role (created before the trust hardening) converges
        on the conditioned trust policy via update_assume_role_policy. Account is
        parsed from the existing role ARN, so the backfill needs no STS call."""
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )

        await mod.ensure_owner_bedrock_role("org_abc")

        fake.update_assume_role_policy.assert_called_once()
        trust = json.loads(fake.update_assume_role_policy.call_args.kwargs["PolicyDocument"])
        stmt = trust["Statement"][0]
        assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == "877352799272"
        assert stmt["Condition"]["ArnLike"]["aws:SourceArn"] == "arn:aws:ecs:us-east-1:877352799272:task/*"
        # Convergence is marked so later calls skip the backfill.
        fake.tag_role.assert_called_once()
        assert fake.tag_role.call_args.kwargs["Tags"] == [{"Key": "trust_hardened", "Value": "1"}]

    @pytest.mark.asyncio
    async def test_trust_backfill_grants_agentcore_trust_too(self, aip_tables, monkeypatch):
        """The legacy trust backfill must build its document via the AgentCore
        superset helper (trust_policy_with_agentcore), not the ECS-only
        _assume_role_doc — otherwise a backfill racing after
        ensure_agentcore_trust already added the AgentCore statement would
        clobber it back to ECS-only, and AgentCore could never assume the role."""
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )

        await mod.ensure_owner_bedrock_role("org_abc")

        trust = json.loads(fake.update_assume_role_policy.call_args.kwargs["PolicyDocument"])
        principals = [s["Principal"].get("Service") for s in trust["Statement"]]
        assert "ecs-tasks.amazonaws.com" in principals
        assert "bedrock-agentcore.amazonaws.com" in principals

    @pytest.mark.asyncio
    async def test_already_hardened_role_skips_trust_backfill(self, aip_tables, monkeypatch):
        """A role already tagged trust_hardened=1 must NOT re-run the trust
        backfill on every ensure call — avoids a dead IAM round-trip on each
        re-provision / container-status poll once the fleet is converged."""
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc",
                "Tags": [{"Key": "trust_hardened", "Value": "1"}],
            }
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        await mod.ensure_owner_bedrock_role("org_abc")

        fake.update_assume_role_policy.assert_not_called()
        fake.tag_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_trust_backfill_clienterror_is_swallowed(self, aip_tables, monkeypatch):
        """A ClientError from update_assume_role_policy (e.g. the new
        iam:UpdateAssumeRolePolicy grant has not propagated yet) must NOT break
        provisioning — the role is still usable on its old trust, and the
        DDB write still happens."""
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        fake.update_assume_role_policy.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "not propagated"}}, "UpdateAssumeRolePolicy"
        )
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )

        role_arn = await mod.ensure_owner_bedrock_role("org_abc")

        assert role_arn.endswith("lightbulb-dev-owner-bedrock-org_abc")  # provisioning completed
        # not tagged, so the next provision retries the converge
        fake.tag_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_existing_role_no_create(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )

        role_arn = await mod.ensure_owner_bedrock_role("org_abc")
        assert role_arn.endswith("lightbulb-dev-owner-bedrock-org_abc")
        fake.create_role.assert_not_called()


class TestLitestreamS3Policy:
    """local-sqlite: the per-owner Litestream S3 grant must reach EVERY owner (it is
    storage durability, not inference) under its own PolicyName."""

    def test_policy_shape_prefix_scoped(self):
        import core.billing.bedrock_aip as mod

        doc = mod.build_owner_litestream_s3_policy("org_abc", "isol8-dev-openclaw-replicas")
        sids = {st["Sid"]: st for st in doc["Statement"]}
        assert sids["ReplicaObjects"]["Resource"] == "arn:aws:s3:::isol8-dev-openclaw-replicas/org_abc/*"
        assert set(sids["ReplicaObjects"]["Action"]) == {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}
        assert sids["ReplicaBucketList"]["Condition"] == {"StringLike": {"s3:prefix": ["org_abc/*"]}}
        assert sids["ReplicaBucketLocation"]["Resource"] == "arn:aws:s3:::isol8-dev-openclaw-replicas"

    @pytest.mark.asyncio
    async def test_put_for_every_owner(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )
        monkeypatch.setattr(mod.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas", raising=False)

        await mod.ensure_owner_bedrock_role("org_abc")

        puts = {c.kwargs["PolicyName"]: c.kwargs for c in fake.put_role_policy.call_args_list}
        assert set(puts) == {"owner-logs", "owner-litestream-s3"}
        kwargs = puts["owner-litestream-s3"]
        assert "org_abc/*" in kwargs["PolicyDocument"]
        # The grant is minted off OWNER_STATE_BUCKET, the one name for the owner-state bucket.
        # It used to read LITESTREAM_BUCKET; that env var is gone from the task definition, so
        # a reader still on it would silently mint NO grant and every profile read would 403.
        assert "lightbulb-dev-openclaw-replicas" in kwargs["PolicyDocument"]

    @pytest.mark.asyncio
    async def test_not_put_when_owner_state_bucket_unset(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )
        monkeypatch.setattr(mod.settings, "OWNER_STATE_BUCKET", "", raising=False)

        await mod.ensure_owner_bedrock_role("org_abc")

        assert [c.kwargs["PolicyName"] for c in fake.put_role_policy.call_args_list] == ["owner-logs"]


def test_owner_logs_policy_is_the_owners_own_runtime_log_group_only(monkeypatch):
    """The shell reaches this role over the metadata service: logs must not be account-wide."""
    import core.billing.bedrock_aip as mod
    from core.services.agentcore_runtime import runtime_name_for

    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
    monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
    [st] = mod.build_owner_logs_policy("org_3JhR0rr1ZGMFnZc2D6dQ8BHhh9G", "877352799272")["Statement"]
    name = runtime_name_for("org_3JhR0rr1ZGMFnZc2D6dQ8BHhh9G")
    assert name == "lb_dev_org_3JhR0rr1ZGMFnZc2D6dQ8BHh_33b7b61b11c8"  # the live dev runtime's name
    assert st["Resource"] == (f"arn:aws:logs:us-east-1:877352799272:log-group:/aws/bedrock-agentcore/runtimes/{name}-*")
    assert set(st["Action"]) == {"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"}


@pytest.mark.asyncio
async def test_delete_owner_role_removes_both_inline_policies(aip_tables, monkeypatch):
    """IAM refuses DeleteRole while ANY inline policy remains, so leaving
    owner-litestream-s3 behind fails the wipe's bedrock_role step for every
    local-sqlite owner and leaks the role."""
    import core.billing.bedrock_aip as mod

    fake = MagicMock()
    # ENUMERATED, not named: whatever IAM reports is what gets deleted, so a future inline
    # policy can never leak the role by being missing from a hardcoded tuple.
    attached = ["owner-bedrock", "owner-litestream-s3", "owner-browser", "owner-something-new"]
    fake.list_role_policies.return_value = {"PolicyNames": attached}
    monkeypatch.setattr(mod, "_iam_client", lambda: fake)
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
    monkeypatch.setattr(mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "", raising=False)

    await mod.delete_owner_bedrock_role("org_abc")

    deleted = {c.kwargs["PolicyName"] for c in fake.delete_role_policy.call_args_list}
    assert deleted == set(attached)
    fake.delete_role.assert_called_once()


class TestOwnerBrowserPolicy:
    """The per-owner AgentCore Browser grant: its own inline PolicyName, scoped to the ONE
    profile this workspace owns."""

    def _put(self, monkeypatch) -> MagicMock:
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        # This retained helper manages legacy owner roles; current pool sessions use STS scope.
        monkeypatch.setattr(mod.settings, "HERMES_SHARED_RUNTIME_ARN", "")
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
        return fake

    @pytest.mark.asyncio
    async def test_owner_browser_policy_names_the_envs_vpc_browser_and_keeps_the_built_in_one(self, monkeypatch):
        """The env's VPC browser (fixed egress address) is where sessions start; the built-in
        one stays so a session started there before the move can still be reached."""
        import core.billing.bedrock_aip as mod

        monkeypatch.setattr(mod, "BROWSER_ID", "lightbulb_dev_workspace_browser-Ab12Cd34Ef")
        fake = self._put(monkeypatch)
        await mod.ensure_owner_browser_policy("org_x", "org_x-ab12cd34ef")
        doc = json.loads(fake.put_role_policy.call_args.kwargs["PolicyDocument"])
        assert doc["Statement"][0]["Resource"] == [
            "arn:aws:bedrock-agentcore:us-east-1:877352799272:browser-custom/lightbulb_dev_workspace_browser-Ab12Cd34Ef",
            "arn:aws:bedrock-agentcore:us-east-1:aws:browser/aws.browser.v1",
        ]

    @pytest.mark.asyncio
    async def test_owner_browser_policy_names_only_this_workspaces_profile(self, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = self._put(monkeypatch)

        await mod.ensure_owner_browser_policy("org_x", "org_x-ab12cd34ef")

        kw = fake.put_role_policy.call_args.kwargs
        assert kw["RoleName"] == "lightbulb-dev-owner-bedrock-org_x"
        # A SEPARATE PolicyName, for the same reason owner-litestream-s3 is separate: the
        # break-glass script swaps "owner-bedrock" wholesale.
        assert kw["PolicyName"] == "owner-browser"
        raw = kw["PolicyDocument"]
        doc = json.loads(raw)
        assert "browser-profile/org_x-ab12cd34ef" in raw and "browser-profile/*" not in raw
        # The built-in browser lives under the literal `aws` pseudo-account.
        assert ":aws:browser/aws.browser.v1" in raw
        assert "bedrock-agentcore:InvokeBrowser" in raw  # computer_use
        # No wildcard: the CDK permissions boundary names the env's browser and nothing wider.
        assert "877352799272:browser/*" not in raw and "browser-custom/*" not in raw
        # Live 403 (Task 1): a start carrying profileConfiguration authorizes against the
        # PROFILE arn as well as the browser arn.
        profile_stmt = next(st for st in doc["Statement"] if "browser-profile/" in json.dumps(st["Resource"]))
        assert "bedrock-agentcore:StartBrowserSession" in profile_stmt["Action"]
        assert "bedrock-agentcore:SaveBrowserSessionProfile" in profile_stmt["Action"]
        # No ListBrowserSessions: it is the fence that stops one runtime learning another
        # tenant's session ids. No control-plane action: profiles are a 100/account quota.
        assert "ListBrowserSessions" not in raw and "CreateBrowserProfile" not in raw

    @pytest.mark.asyncio
    async def test_owner_browser_policy_writes_nothing_without_a_profile(self, monkeypatch):
        """ensure_browser_profile returns "" when AWS refused; an unscoped grant must not
        be written in its place."""
        import core.billing.bedrock_aip as mod

        fake = self._put(monkeypatch)
        await mod.ensure_owner_browser_policy("org_x", "")
        fake.put_role_policy.assert_not_called()


class TestOwnerMemoryPolicy:
    """The per-owner workspace-memory grant. ONE shared memory resource per env, so isolation IS
    this document -- the exact shape the 2026-09-22 dev spike proved (owner B AccessDenied on every
    cross-owner call, including CreateEvent with owner A's actorId)."""

    MEMORY_ARN = "arn:aws:bedrock-agentcore:us-east-1:877352799272:memory/lightbulb_dev_workspace_memory-abc"

    def _put(self, monkeypatch, memory_id: str = "lightbulb_dev_workspace_memory-abc") -> MagicMock:
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
        monkeypatch.setattr(mod.settings, "AGENTCORE_MEMORY_ID", memory_id, raising=False)
        return fake

    @pytest.mark.asyncio
    async def test_owner_memory_policy_is_the_spike_proven_document(self, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = self._put(monkeypatch)
        await mod.ensure_owner_memory_policy("org_x")

        kw = fake.put_role_policy.call_args.kwargs
        assert kw["RoleName"] == "lightbulb-dev-owner-bedrock-org_x"
        assert kw["PolicyName"] == "owner-memory"
        doc = json.loads(kw["PolicyDocument"])
        by_sid = {st["Sid"]: st for st in doc["Statement"]}
        events = by_sid["WorkspaceMemoryOwnEvents"]
        assert events["Effect"] == "Allow" and events["Resource"] == self.MEMORY_ARN
        assert sorted(events["Action"]) == [
            "bedrock-agentcore:CreateEvent",
            "bedrock-agentcore:GetEvent",
            "bedrock-agentcore:ListEvents",
            "bedrock-agentcore:ListSessions",
        ]
        assert events["Condition"] == {"StringEquals": {"bedrock-agentcore:actorId": "org_x"}}
        records = by_sid["WorkspaceMemoryOwnRecords"]
        assert records["Resource"] == self.MEMORY_ARN
        assert sorted(records["Action"]) == [
            "bedrock-agentcore:ListMemoryRecords",
            "bedrock-agentcore:RetrieveMemoryRecords",
        ]
        # EXACT namespace, never a prefix: there is no namespacePath condition key, and a
        # namespacePath call fails closed under this Allow (spike).
        assert records["Condition"] == {"StringEquals": {"bedrock-agentcore:namespace": "/owners/org_x/"}}
        # A member's private scope (1:1 DMs): separate Allows, so the proven team pair above
        # keeps working even if StringLike were not honoured. "org_x:" never reaches "org_xy".
        member_events = by_sid["WorkspaceMemoryMemberEvents"]
        assert member_events["Effect"] == "Allow" and member_events["Resource"] == self.MEMORY_ARN
        assert sorted(member_events["Action"]) == sorted(events["Action"])
        assert member_events["Condition"] == {"StringLike": {"bedrock-agentcore:actorId": "org_x:user_*"}}
        member_records = by_sid["WorkspaceMemoryMemberRecords"]
        assert member_records["Effect"] == "Allow" and member_records["Resource"] == self.MEMORY_ARN
        assert sorted(member_records["Action"]) == sorted(records["Action"])
        assert member_records["Condition"] == {"StringLike": {"bedrock-agentcore:namespace": "/owners/org_x:user_*/"}}
        assert len(doc["Statement"]) == 5
        deny = by_sid["WorkspaceMemoryNoUnconditionable"]
        assert deny["Effect"] == "Deny" and deny["Resource"] == "*"
        # These carry NO condition keys: allowed at all, they reach any owner's record by id.
        assert set(deny["Action"]) == {
            "bedrock-agentcore:GetMemoryRecord",
            "bedrock-agentcore:DeleteMemoryRecord",
            "bedrock-agentcore:BatchCreateMemoryRecords",
            "bedrock-agentcore:BatchUpdateMemoryRecords",
            "bedrock-agentcore:BatchDeleteMemoryRecords",
            "bedrock-agentcore:DeleteEvent",
            "bedrock-agentcore:ListActors",
        }

    @pytest.mark.asyncio
    async def test_owner_memory_policy_writes_nothing_when_memory_is_not_configured(self, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = self._put(monkeypatch, memory_id="")
        await mod.ensure_owner_memory_policy("org_x")
        fake.put_role_policy.assert_not_called()
