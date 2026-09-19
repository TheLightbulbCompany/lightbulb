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
        fake.list_attached_role_policies.return_value = {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::877352799272:policy/base"}]
        }
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

        # EVERY inline policy is deleted before the detach: IAM refuses
        # DeleteRole while any inline policy remains, and O6 owners carry
        # owner-litestream-s3 (and, on AgentCore Browser, owner-browser)
        # alongside owner-bedrock.
        assert calls == [
            "delete_role_policy",
            "delete_role_policy",
            "delete_role_policy",
            "detach_role_policy",
            "delete_role",
        ]
        assert [c.kwargs["PolicyName"] for c in fake.delete_role_policy.call_args_list] == [
            "owner-bedrock",
            "owner-litestream-s3",
            "owner-browser",
        ]
        assert all(
            c.kwargs["RoleName"] == "lightbulb-dev-owner-bedrock-org_abc"
            for c in fake.delete_role_policy.call_args_list
        )
        fake.detach_role_policy.assert_called_once_with(
            RoleName="lightbulb-dev-owner-bedrock-org_abc",
            PolicyArn="arn:aws:iam::877352799272:policy/base",
        )
        fake.delete_role.assert_called_once_with(RoleName="lightbulb-dev-owner-bedrock-org_abc")

    @pytest.mark.asyncio
    async def test_never_deletes_the_base_managed_policy_itself(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.list_attached_role_policies.return_value = {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::877352799272:policy/base"}]
        }
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
        fake.list_attached_role_policies.return_value = {
            "AttachedPolicies": [{"PolicyArn": "arn:aws:iam::877352799272:policy/base"}]
        }
        fake.delete_role_policy.side_effect = _no_such_entity("DeleteRolePolicy")
        fake.detach_role_policy.side_effect = _no_such_entity("DetachRolePolicy")
        fake.delete_role.side_effect = _no_such_entity("DeleteRole")
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )

        await mod.delete_owner_bedrock_role("org_abc")  # must not raise

        assert fake.delete_role_policy.call_count == 3  # owner-bedrock + owner-litestream-s3 + owner-browser
        fake.detach_role_policy.assert_called_once()
        fake.delete_role.assert_called_once()

    @pytest.mark.asyncio
    async def test_reraises_unexpected_client_error_from_delete_role_policy(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        fake = MagicMock()
        fake.list_attached_role_policies.return_value = {"AttachedPolicies": []}
        fake.delete_role_policy.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "DeleteRolePolicy"
        )
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        with pytest.raises(ClientError):
            await mod.delete_owner_bedrock_role("org_abc")

        fake.detach_role_policy.assert_not_called()
        fake.delete_role.assert_not_called()


ROW = {
    "owner_id": "org_abc",
    "bedrock_aip_arns": {"sonnet": "arn:aws:bedrock:us-east-1:1:application-inference-profile/abc"},
    "bedrock_role_arn": "arn:aws:iam::1:role/lightbulb-dev-owner-bedrock-org_abc",
    "bedrock_gate_token_hash": "hash_current",
    "bedrock_gate_superseded_hashes": ["hash_old"],
}


async def _reap(row):
    import core.billing.bedrock_aip as mod

    return await mod.reap_owner_bedrock_resources("org_abc", row)


@pytest.fixture
def reaper(monkeypatch):
    """Stub the three primitives + the fence read, so these assert on intent."""
    import core.billing.bedrock_aip as mod
    import core.billing.bedrock_gate_tokens as tokens

    calls = MagicMock(
        aips=AsyncMock(return_value=["arn:aws:bedrock:us-east-1:1:application-inference-profile/abc"]),
        role=AsyncMock(return_value=None),
        gate=AsyncMock(return_value=2),
        container_get=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(mod, "delete_owner_aips", calls.aips)
    monkeypatch.setattr(mod, "delete_owner_bedrock_role", calls.role)
    monkeypatch.setattr(tokens, "delete_gate_token_rows", calls.gate)
    monkeypatch.setattr(mod.container_repo, "get_by_owner_id", calls.container_get)
    return calls


class TestReapOwnerBedrockResources:
    """The shared reaper, invoked from ``ecs_manager.delete_user_service`` — the
    seam every teardown path crosses. Its whole job is that no path leaks and no
    path carries its own copy."""

    @pytest.mark.asyncio
    async def test_reaps_gate_tokens_aips_and_role(self, reaper):
        detail = await _reap(ROW)

        # Gate-token rows are the security-critical residue: that table has no
        # owner index, so once the containers row naming them is gone they are
        # unreachable, un-denyable and un-expirable forever.
        reaper.gate.assert_awaited_once_with(["hash_current", "hash_old"])
        reaper.aips.assert_awaited_once()
        reaper.role.assert_awaited_once_with("org_abc")
        assert "gate-token" in detail and "AIP" in detail and "IAM role" in detail

    @pytest.mark.asyncio
    async def test_reaps_the_role_even_when_the_row_never_named_it(self, reaper):
        """The role name derives from owner_id alone. Gating on
        ``bedrock_role_arn`` is exactly the bug the account-wipe carried: a
        legacy row without the field silently skipped the delete and stranded
        the role forever."""
        await _reap({k: v for k, v in ROW.items() if k != "bedrock_role_arn"})

        reaper.role.assert_awaited_once_with("org_abc")

    @pytest.mark.asyncio
    async def test_a_reprovision_mid_teardown_fences_the_whole_reap(self, reaper):
        """Every identifier here is deterministic or re-minted under the same
        owner, so a resubscribe's provision recreates exactly what this would
        delete — and the AIPs have NO self-heal, because that provision already
        returned success. A containers row existing again means a provision has
        run since, so these are its resources now."""
        reaper.container_get.return_value = {"owner_id": "org_abc", "provision_generation": "NEW"}

        detail = await _reap(ROW)

        assert "fenced" in detail
        reaper.gate.assert_not_awaited()
        reaper.aips.assert_not_awaited()
        reaper.role.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_fence_read_is_strongly_consistent(self, reaper):
        """A stale empty read here is precisely the wrong answer — it reports
        "no live container" for one that was just reprovisioned."""
        await _reap(ROW)

        assert reaper.container_get.await_args.kwargs.get("consistent") is True

    @pytest.mark.asyncio
    async def test_an_owner_who_never_had_bedrock_costs_no_aws_calls(self, reaper):
        """ChatGPT-OAuth and BYOK owners never had any of this. IAM write TPS is
        low enough that four no-op calls per cancelling owner could throttle IAM
        account-wide during bulk churn and stall legitimate provisioning."""
        detail = await _reap({"owner_id": "org_abc"})

        assert "no Bedrock footprint" in detail
        reaper.container_get.assert_not_awaited()  # not even the fence read
        reaper.gate.assert_not_awaited()
        reaper.aips.assert_not_awaited()
        reaper.role.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_failing_resource_still_lets_the_others_be_reaped(self, reaper):
        """The role is the half that frees the account-wide quota, so an AIP
        throttle must not skip it. The failure is still re-raised, so an audited
        caller reports a partial reap rather than claiming success."""
        reaper.aips.side_effect = RuntimeError("bedrock throttled")

        with pytest.raises(RuntimeError):
            await _reap(ROW)

        reaper.gate.assert_awaited_once()
        reaper.role.assert_awaited_once_with("org_abc")

    @pytest.mark.asyncio
    async def test_a_non_dict_row_is_treated_as_no_footprint(self, reaper):
        """Teardown reaches this from webhook handlers, where raising means a
        5xx and a provider retry. An unexpected row shape must degrade."""
        assert "no Bedrock footprint" in await _reap(object())


class TestDeleteOwnerAipsAttemptsEveryArn:
    @pytest.mark.asyncio
    async def test_a_throttle_on_one_arn_does_not_strand_the_next(self, aip_tables, monkeypatch):
        """By the time teardown calls this, the containers row naming these arns
        is already gone — so aborting the loop at arn #2 strands arn #3 with
        nothing left in the system able to name it."""
        import core.billing.bedrock_aip as mod

        arns = ["arn:one", "arn:two", "arn:three"]
        fake = MagicMock()
        fake.delete_inference_profile.side_effect = [
            None,
            ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "DeleteInferenceProfile"),
            None,
        ]
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)

        with pytest.raises(ClientError):
            await mod.delete_owner_aips("org_abc", aip_arns=arns)

        # All three attempted, not just up to the failure.
        assert [c.kwargs["inferenceProfileIdentifier"] for c in fake.delete_inference_profile.call_args_list] == arns


class TestDetachByEnumeration:
    """Teardown must detach what is ACTUALLY attached, not the ARN the current
    setting names. A role minted before a policy-ARN change carries the OLD
    attachment; detaching the new ARN returns NoSuchEntity, which is swallowed,
    so the old attachment survives and DeleteRole then fails DeleteConflict --
    the role leaks and the old policy can never be deleted."""

    @pytest.mark.asyncio
    async def test_detaches_a_policy_the_current_setting_does_not_name(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        stale = "arn:aws:iam::877352799272:policy/lightbulb-dev-owner-bedrock-base"
        fake = MagicMock()
        fake.list_attached_role_policies.return_value = {"AttachedPolicies": [{"PolicyArn": stale}]}
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        # the CURRENT setting names a DIFFERENT policy than the one attached
        monkeypatch.setattr(
            mod.settings,
            "BEDROCK_OWNER_BASE_POLICY_ARN",
            "arn:aws:iam::877352799272:policy/lightbulb-dev-owner-bedrock-base",
            raising=False,
        )

        await mod.delete_owner_bedrock_role("org_abc")

        fake.detach_role_policy.assert_called_once()
        assert fake.detach_role_policy.call_args.kwargs["PolicyArn"] == stale
        fake.delete_role.assert_called_once()

    @pytest.mark.asyncio
    async def test_detaches_every_attached_policy_not_just_one(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = [
            "arn:aws:iam::877352799272:policy/lightbulb-dev-owner-bedrock-base",
            "arn:aws:iam::877352799272:policy/lightbulb-dev-owner-bedrock-base",
        ]
        fake = MagicMock()
        fake.list_attached_role_policies.return_value = {"AttachedPolicies": [{"PolicyArn": a} for a in arns]}
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        await mod.delete_owner_bedrock_role("org_abc")

        assert [c.kwargs["PolicyArn"] for c in fake.detach_role_policy.call_args_list] == arns
