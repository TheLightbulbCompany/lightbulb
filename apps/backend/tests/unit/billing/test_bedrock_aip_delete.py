"""Unit tests for core.billing.bedrock_aip's delete paths (Admin Account-Wipe
Task 1): delete_owner_aips + delete_owner_bedrock_role.

Mirrors the mocking style of tests/unit/billing/test_bedrock_aip.py — moto for
DynamoDB (containers + bedrock-aip-owners reverse table), MagicMock for the
boto3 bedrock/iam control-plane clients.
"""

from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError


def _no_such_entity(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "NoSuchEntity", "Message": "not found"}}, op)


def _resource_not_found(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}, op)


@pytest.fixture
def aip_tables(monkeypatch):
    """moto-mocked containers + bedrock-aip-owners tables (copied from
    test_bedrock_aip.py's fixture of the same name — no shared conftest for
    this pair of tables today)."""
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-aip-owners",
            KeySchema=[{"AttributeName": "aip_arn", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "aip_arn", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.put_item(
            TableName="test-containers",
            Item={
                "owner_id": {"S": "org_abc"},
                "id": {"S": "c-1"},
                "created_at": {"S": "2026-06-07T00:00:00Z"},
            },
        )
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", None)
        monkeypatch.setenv("BEDROCK_AIP_OWNERS_TABLE", "test-aip-owners")
        yield client


class TestDeleteOwnerAips:
    @pytest.mark.asyncio
    async def test_deletes_each_aip_and_its_reverse_row_returns_arns(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        await mod.persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")

        fake = MagicMock()
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)

        removed = await mod.delete_owner_aips("org_abc")

        assert sorted(removed) == sorted(arns.values())
        assert fake.delete_inference_profile.call_count == 3
        called_arns = {c.kwargs["inferenceProfileIdentifier"] for c in fake.delete_inference_profile.call_args_list}
        assert called_arns == set(arns.values())

        # Reverse rows are gone.
        for arn in arns.values():
            resp = aip_tables.get_item(TableName="test-aip-owners", Key={"aip_arn": {"S": arn}})
            assert "Item" not in resp

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_aips(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)

        removed = await mod.delete_owner_aips("org_abc")

        assert removed == []
        fake.delete_inference_profile.assert_not_called()

    @pytest.mark.asyncio
    async def test_swallows_resource_not_found_and_still_removes_reverse_row(self, aip_tables, monkeypatch):
        """A prior partial wipe already deleted the AIP in Bedrock (or it was
        never actually minted) — delete_inference_profile 404s. This must not
        raise, and the reverse row + return value still reflect the removal
        (idempotent re-run)."""
        import core.billing.bedrock_aip as mod

        arns = {"sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s"}
        await mod.persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")

        fake = MagicMock()
        fake.delete_inference_profile.side_effect = _resource_not_found("DeleteInferenceProfile")
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)

        removed = await mod.delete_owner_aips("org_abc")

        assert removed == [arns["sonnet"]]
        resp = aip_tables.get_item(TableName="test-aip-owners", Key={"aip_arn": {"S": arns["sonnet"]}})
        assert "Item" not in resp

    @pytest.mark.asyncio
    async def test_reraises_unexpected_client_error(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {"sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s"}
        await mod.persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")

        fake = MagicMock()
        fake.delete_inference_profile.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "rate exceeded"}}, "DeleteInferenceProfile"
        )
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)

        with pytest.raises(ClientError):
            await mod.delete_owner_aips("org_abc")

    @pytest.mark.asyncio
    async def test_aip_arns_kwarg_deletes_without_reading_containers_row(self, aip_tables, monkeypatch):
        """Regression test for the wipe-orchestrator ordering hazard: the
        `ecs_service` step deletes the containers row as a side effect of
        ecs_manager.delete_user_service, and the LATER `bedrock_aips` step
        used to re-read that (now-gone) row via read_owner_aips, silently
        no-op, and report "ok" while orphaning both the Bedrock AIP and the
        bedrock-aip-owners reverse row.

        When `aip_arns` is passed explicitly, delete_owner_aips must delete
        every given ARN (+ its reverse row) WITHOUT ever calling
        read_owner_aips or container_repo — i.e. it must not depend on the
        containers row still existing."""
        import core.billing.bedrock_aip as mod
        from core.repositories import container_repo

        arn1 = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o"
        arn2 = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s"

        # Seed only the reverse-table rows directly -- deliberately do NOT
        # write a containers row, mirroring the post-ecs_service-deletion
        # state the wipe orchestrator's bedrock_aips step actually runs in.
        table = mod._aip_owners_table()
        await mod._put_reverse_row(table, arn=arn1, owner_id="org_abc", tier="opus", plan="paid")
        await mod._put_reverse_row(table, arn=arn2, owner_id="org_abc", tier="sonnet", plan="paid")

        fake = MagicMock()
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)

        read_owner_aips_spy = AsyncMock()
        monkeypatch.setattr(mod, "read_owner_aips", read_owner_aips_spy)
        container_repo_spy = AsyncMock()
        monkeypatch.setattr(container_repo, "get_by_owner_id", container_repo_spy)

        removed = await mod.delete_owner_aips("org_abc", aip_arns=[arn1, arn2])

        assert sorted(removed) == sorted([arn1, arn2])
        assert fake.delete_inference_profile.call_count == 2
        called_arns = {c.kwargs["inferenceProfileIdentifier"] for c in fake.delete_inference_profile.call_args_list}
        assert called_arns == {arn1, arn2}

        read_owner_aips_spy.assert_not_called()
        container_repo_spy.assert_not_called()

        for arn in (arn1, arn2):
            resp = aip_tables.get_item(TableName="test-aip-owners", Key={"aip_arn": {"S": arn}})
            assert "Item" not in resp

    @pytest.mark.asyncio
    async def test_aip_arns_empty_list_returns_empty_without_bedrock_calls(self, aip_tables, monkeypatch):
        """aip_arns=[] (explicitly empty, as opposed to None) is a no-op --
        mirrors the None/absent-map short circuit but for the explicit-ARNs
        path."""
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)

        removed = await mod.delete_owner_aips("org_abc", aip_arns=[])

        assert removed == []
        fake.delete_inference_profile.assert_not_called()


class TestDeleteOwnerBedrockRole:
    @pytest.mark.asyncio
    async def test_deletes_policy_then_detaches_then_deletes_role_in_order(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )

        calls: list[str] = []
        fake.delete_role_policy.side_effect = lambda **k: calls.append("delete_role_policy")
        fake.detach_role_policy.side_effect = lambda **k: calls.append("detach_role_policy")
        fake.delete_role.side_effect = lambda **k: calls.append("delete_role")

        await mod.delete_owner_bedrock_role("org_abc")

        # BOTH inline policies are deleted before the detach: IAM refuses
        # DeleteRole while any inline policy remains, and O6 owners carry
        # owner-litestream-s3 alongside owner-bedrock.
        assert calls == [
            "delete_role_policy",
            "delete_role_policy",
            "detach_role_policy",
            "delete_role",
        ]
        assert [c.kwargs["PolicyName"] for c in fake.delete_role_policy.call_args_list] == [
            "owner-bedrock",
            "owner-litestream-s3",
        ]
        assert all(
            c.kwargs["RoleName"] == "isol8-dev-owner-bedrock-org_abc" for c in fake.delete_role_policy.call_args_list
        )
        fake.detach_role_policy.assert_called_once_with(
            RoleName="isol8-dev-owner-bedrock-org_abc",
            PolicyArn="arn:aws:iam::877352799272:policy/base",
        )
        fake.delete_role.assert_called_once_with(RoleName="isol8-dev-owner-bedrock-org_abc")

    @pytest.mark.asyncio
    async def test_never_deletes_the_base_managed_policy_itself(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )

        await mod.delete_owner_bedrock_role("org_abc")

        assert not fake.delete_policy.called

    @pytest.mark.asyncio
    async def test_swallows_no_such_entity_at_every_step(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.delete_role_policy.side_effect = _no_such_entity("DeleteRolePolicy")
        fake.detach_role_policy.side_effect = _no_such_entity("DetachRolePolicy")
        fake.delete_role.side_effect = _no_such_entity("DeleteRole")
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )

        await mod.delete_owner_bedrock_role("org_abc")  # must not raise

        assert fake.delete_role_policy.call_count == 2  # owner-bedrock + owner-litestream-s3
        fake.detach_role_policy.assert_called_once()
        fake.delete_role.assert_called_once()

    @pytest.mark.asyncio
    async def test_reraises_unexpected_client_error_from_delete_role_policy(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.delete_role_policy.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "DeleteRolePolicy"
        )
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        with pytest.raises(ClientError):
            await mod.delete_owner_bedrock_role("org_abc")

        fake.detach_role_policy.assert_not_called()
        fake.delete_role.assert_not_called()
