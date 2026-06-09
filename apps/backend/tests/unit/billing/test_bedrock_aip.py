"""Unit tests for core.billing.bedrock_aip — per-owner AIP + scoped IAM role."""

import json
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from core.billing.bedrock_aip import (
    FAN_OUT_REGIONS,
    FM_IDS,
    SYSTEM_PROFILE_ID,
    TIERS,
    build_owner_bedrock_policy,
)


def _aip_arns(account: str = "877352799272") -> dict[str, str]:
    return {
        "opus": f"arn:aws:bedrock:us-east-1:{account}:application-inference-profile/aip-opus-1",
        "sonnet": f"arn:aws:bedrock:us-east-1:{account}:application-inference-profile/aip-sonnet-1",
        "haiku": f"arn:aws:bedrock:us-east-1:{account}:application-inference-profile/aip-haiku-1",
    }


def test_constants_shape():
    assert TIERS == ("opus", "sonnet", "haiku")
    assert FAN_OUT_REGIONS == ("us-east-1", "us-east-2", "us-west-2")
    assert FM_IDS == (
        "anthropic.claude-opus-4-6-v1",
        "anthropic.claude-sonnet-4-6",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    assert set(SYSTEM_PROFILE_ID) == set(TIERS)
    assert SYSTEM_PROFILE_ID["opus"] == "us.anthropic.claude-opus-4-6-v1"
    assert SYSTEM_PROFILE_ID["haiku"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_policy_has_invoke_allow_with_3_aips_and_9_fm_arns():
    arns = _aip_arns()
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=arns,
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    allow = next(s for s in doc["Statement"] if s["Effect"] == "Allow" and "bedrock:InvokeModel" in s["Action"])
    assert "bedrock:InvokeModelWithResponseStream" in allow["Action"]
    # 3 AIP ARNs are in Resource.
    for arn in arns.values():
        assert arn in allow["Resource"]
    # 9 FM ARNs (3 models x 3 regions) are in Resource.
    expected_fm = {
        f"arn:aws:bedrock:{region}::foundation-model/{model}"
        for region in ("us-east-1", "us-east-2", "us-west-2")
        for model in (
            "anthropic.claude-opus-4-6-v1",
            "anthropic.claude-sonnet-4-6",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
        )
    }
    assert expected_fm.issubset(set(allow["Resource"]))
    assert len(expected_fm) == 9


def test_policy_condition_scopes_to_owner_aips_only():
    arns = _aip_arns()
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=arns,
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    allow = next(s for s in doc["Statement"] if s["Effect"] == "Allow" and "bedrock:InvokeModel" in s["Action"])
    cond = allow["Condition"]["ArnEquals"]["bedrock:InferenceProfileArn"]
    assert sorted(cond) == sorted(arns.values())


def test_policy_explicitly_denies_create_inference_profile():
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=_aip_arns(),
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    deny = next(s for s in doc["Statement"] if s["Effect"] == "Deny")
    assert deny["Action"] == "bedrock:CreateInferenceProfile"
    assert deny["Resource"] == "*"


def test_policy_is_iam_document_version():
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=_aip_arns(),
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    assert doc["Version"] == "2012-10-17"


@pytest.fixture
def aip_tables(monkeypatch):
    """moto-mocked containers + bedrock-aip-owners tables."""
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
        # default "isol8-dev-"). Setting the env var here is too late, so patch
        # the cached prefix directly to "test-" -> "test-containers".
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        # Reset the cached DDB resource so it is rebuilt INSIDE the moto mock
        # (a resource cached by an earlier test points at the real endpoint).
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", None)
        monkeypatch.setenv("BEDROCK_AIP_OWNERS_TABLE", "test-aip-owners")
        yield client


class TestPersistence:
    @pytest.mark.asyncio
    async def test_persist_then_read_back_from_row(self, aip_tables):
        from core.billing.bedrock_aip import (
            persist_owner_aips,
            read_owner_aips,
        )

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-h",
        }
        await persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")
        got = await read_owner_aips("org_abc")
        assert got == arns

    @pytest.mark.asyncio
    async def test_persist_writes_reverse_rows(self, aip_tables):
        from core.billing.bedrock_aip import persist_owner_aips

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-h",
        }
        await persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")
        resp = aip_tables.get_item(
            TableName="test-aip-owners",
            Key={"aip_arn": {"S": arns["opus"]}},
        )
        assert resp["Item"]["owner_id"]["S"] == "org_abc"
        assert resp["Item"]["tier"]["S"] == "opus"

    @pytest.mark.asyncio
    async def test_read_owner_aips_none_when_unset(self, aip_tables):
        from core.billing.bedrock_aip import read_owner_aips

        assert await read_owner_aips("org_abc") is None


class TestResolveOwner:
    @pytest.mark.asyncio
    async def test_round_trips_a_persisted_arn(self, aip_tables):
        from core.billing.bedrock_aip import persist_owner_aips, resolve_owner_from_aip_arn

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-h",
        }
        await persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")
        assert await resolve_owner_from_aip_arn(arns["sonnet"]) == "org_abc"
        assert await resolve_owner_from_aip_arn(arns["haiku"]) == "org_abc"

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_arn(self, aip_tables):
        from core.billing.bedrock_aip import resolve_owner_from_aip_arn

        assert (
            await resolve_owner_from_aip_arn(
                "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/nope"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_resolve_tier_round_trips_a_persisted_arn(self, aip_tables):
        from core.billing.bedrock_aip import persist_owner_aips, resolve_tier_from_aip_arn

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-h",
        }
        await persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")
        assert await resolve_tier_from_aip_arn(arns["opus"]) == "opus"
        assert await resolve_tier_from_aip_arn(arns["sonnet"]) == "sonnet"
        assert await resolve_tier_from_aip_arn(arns["haiku"]) == "haiku"

    @pytest.mark.asyncio
    async def test_resolve_tier_returns_none_for_unknown_arn(self, aip_tables):
        from core.billing.bedrock_aip import resolve_tier_from_aip_arn

        assert (
            await resolve_tier_from_aip_arn("arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/nope")
            is None
        )

    @pytest.mark.asyncio
    async def test_combined_resolver_round_trips_owner_and_tier(self, aip_tables):
        from core.billing.bedrock_aip import (
            persist_owner_aips,
            resolve_owner_and_tier_from_aip_arn,
        )

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-h",
        }
        await persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")
        assert await resolve_owner_and_tier_from_aip_arn(arns["sonnet"]) == ("org_abc", "sonnet")

    @pytest.mark.asyncio
    async def test_combined_resolver_returns_none_pair_for_unknown_arn(self, aip_tables):
        from core.billing.bedrock_aip import resolve_owner_and_tier_from_aip_arn

        assert await resolve_owner_and_tier_from_aip_arn(
            "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/nope"
        ) == (None, None)

    @pytest.mark.asyncio
    async def test_combined_resolver_does_a_single_get_item(self, aip_tables, monkeypatch):
        # Cleanup J: owner + tier come from ONE reverse-table read per record.
        import core.billing.bedrock_aip as mod
        from core.billing.bedrock_aip import (
            persist_owner_aips,
            resolve_owner_and_tier_from_aip_arn,
        )

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-h",
        }
        await persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")

        real_table = mod._aip_owners_table()
        calls = {"get_item": 0}
        real_get_item = real_table.get_item

        def _counting_get_item(*a, **k):
            calls["get_item"] += 1
            return real_get_item(*a, **k)

        real_table.get_item = _counting_get_item
        monkeypatch.setattr(mod, "_aip_owners_table", lambda: real_table)

        owner, tier = await resolve_owner_and_tier_from_aip_arn(arns["opus"])
        assert (owner, tier) == ("org_abc", "opus")
        assert calls["get_item"] == 1


class TestEnsureAips:
    @pytest.mark.asyncio
    async def test_creates_one_aip_per_tier_and_persists(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        created = {}

        def _create(**kwargs):
            # copyFrom is the FULL system inference-profile ARN (not the bare id);
            # parse the tier off the ARN's profile-id suffix for assertion.
            src = kwargs["modelSource"]["copyFrom"]
            profile_id = src.split(":inference-profile/")[1]
            tier = next(t for t, p in mod.SYSTEM_PROFILE_ID.items() if p == profile_id)
            created[tier] = kwargs
            arn = f"arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/{tier}-aip"
            return {"inferenceProfileArn": arn}

        fake = MagicMock()
        fake.create_inference_profile.side_effect = _create
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        # Pin the account so the copyFrom ARN is deterministic (no real STS).
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        arns = await mod.ensure_owner_aips("org_abc")

        assert set(arns) == {"opus", "sonnet", "haiku"}
        assert fake.create_inference_profile.call_count == 3
        # REGRESSION GUARD: copyFrom must be the account-qualified system
        # inference-profile ARN, not the bare id. AWS rejects the bare id with a
        # ValidationException, which silently aborted all AIP provisioning.
        assert created["opus"]["modelSource"]["copyFrom"] == (
            "arn:aws:bedrock:us-east-1:877352799272:inference-profile/us.anthropic.claude-opus-4-6-v1"
        )
        # Env-prefixed name, tag carries owner_id.
        assert created["opus"]["inferenceProfileName"] == "isol8-dev-org_abc-opus"
        tags = {t["key"]: t["value"] for t in created["opus"]["tags"]}
        assert tags["owner_id"] == "org_abc"
        assert tags["tier"] == "opus"
        # Persisted: reverse lookup resolves.
        assert await mod.resolve_owner_from_aip_arn(arns["opus"]) == "org_abc"

    @pytest.mark.asyncio
    async def test_idempotent_no_recreate_when_already_present(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        await mod.persist_owner_aips("org_abc", arns, role_arn="arn:aws:iam::877352799272:role/r")

        fake = MagicMock()
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        got = await mod.ensure_owner_aips("org_abc")
        assert got == arns
        fake.create_inference_profile.assert_not_called()
        # C1 self-heal: short-circuit re-puts reverse rows so the index is
        # consistent even if a prior run's reverse PutItem partially failed.
        assert await mod.resolve_owner_from_aip_arn(arns["haiku"]) == "org_abc"

    @pytest.mark.asyncio
    async def test_conflict_adopts_existing_aip_instead_of_failing(self, aip_tables, monkeypatch):
        """A crashed prior run left the AIP in Bedrock but never persisted the
        row. The re-mint hits ConflictException and must ADOPT the existing AIP
        (via list_inference_profiles) rather than raise (review C2/C3)."""
        import core.billing.bedrock_aip as mod

        def _create(**kwargs):
            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": "exists"}},
                "CreateInferenceProfile",
            )

        def _list(**kwargs):
            return {
                "inferenceProfileSummaries": [
                    {
                        "inferenceProfileName": "isol8-dev-org_abc-opus",
                        "inferenceProfileArn": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/opus-exist",
                    },
                    {
                        "inferenceProfileName": "isol8-dev-org_abc-sonnet",
                        "inferenceProfileArn": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/sonnet-exist",
                    },
                    {
                        "inferenceProfileName": "isol8-dev-org_abc-haiku",
                        "inferenceProfileArn": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/haiku-exist",
                    },
                ]
            }

        fake = MagicMock()
        fake.create_inference_profile.side_effect = _create
        fake.list_inference_profiles.side_effect = _list
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        arns = await mod.ensure_owner_aips("org_abc")

        assert arns["opus"].endswith("opus-exist")
        assert set(arns) == {"opus", "sonnet", "haiku"}
        # Adopted AIPs are persisted -> reverse lookup resolves.
        assert await mod.resolve_owner_from_aip_arn(arns["sonnet"]) == "org_abc"


def _no_such_entity():
    return ClientError({"Error": {"Code": "NoSuchEntity", "Message": "not found"}}, "GetRole")


class TestEnsureRole:
    @pytest.mark.asyncio
    async def test_creates_role_with_boundary_base_and_scoped_policy(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        fake = MagicMock()
        fake.get_role.side_effect = _no_such_entity()
        fake.create_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings,
            "BEDROCK_OWNER_BOUNDARY_ARN",
            "arn:aws:iam::877352799272:policy/isol8-dev-owner-bedrock-boundary",
            raising=False,
        )
        monkeypatch.setattr(
            mod.settings,
            "BEDROCK_OWNER_BASE_POLICY_ARN",
            "arn:aws:iam::877352799272:policy/isol8-dev-owner-bedrock-base",
            raising=False,
        )
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1,us-east-2,us-west-2", raising=False)

        role_arn = await mod.ensure_owner_bedrock_role("org_abc", arns)

        assert role_arn == "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"
        # create_role carries the permissions boundary.
        ck = fake.create_role.call_args.kwargs
        assert ck["RoleName"] == "isol8-dev-owner-bedrock-org_abc"
        assert ck["PermissionsBoundary"] == "arn:aws:iam::877352799272:policy/isol8-dev-owner-bedrock-boundary"
        # base policy attached.
        fake.attach_role_policy.assert_called_once_with(
            RoleName="isol8-dev-owner-bedrock-org_abc",
            PolicyArn="arn:aws:iam::877352799272:policy/isol8-dev-owner-bedrock-base",
        )
        # scoped inline policy put with the 3 AIPs + deny.
        pk = fake.put_role_policy.call_args.kwargs
        assert pk["RoleName"] == "isol8-dev-owner-bedrock-org_abc"
        doc = json.loads(pk["PolicyDocument"])
        allow = next(s for s in doc["Statement"] if s["Effect"] == "Allow")
        assert sorted(allow["Condition"]["ArnEquals"]["bedrock:InferenceProfileArn"]) == sorted(arns.values())
        # role ARN persisted on the row.
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_role_arn"]["S"] == role_arn

    @pytest.mark.asyncio
    async def test_idempotent_existing_role_repts_policy_no_create(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        fake = MagicMock()
        fake.get_role.return_value = {"Role": {"Arn": "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"}}
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1,us-east-2,us-west-2", raising=False)

        role_arn = await mod.ensure_owner_bedrock_role("org_abc", arns)
        assert role_arn.endswith("isol8-dev-owner-bedrock-org_abc")
        fake.create_role.assert_not_called()
        fake.put_role_policy.assert_called_once()  # scope refreshed on existing role

    @pytest.mark.asyncio
    async def test_skip_bedrock_grant_does_not_put_scoped_policy(self, aip_tables, monkeypatch):
        """skip_bedrock_grant=True keeps the role + base policy but does NOT
        (re)put the scoped Bedrock inline policy — so a re-provision of a
        revoked owner does not un-revoke (review C6)."""
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        fake = MagicMock()
        fake.get_role.return_value = {"Role": {"Arn": "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"}}
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )

        role_arn = await mod.ensure_owner_bedrock_role("org_abc", arns, skip_bedrock_grant=True)
        assert role_arn.endswith("isol8-dev-owner-bedrock-org_abc")
        # base policy still attached (role is bootable), but Bedrock NOT granted.
        fake.attach_role_policy.assert_called_once()
        fake.put_role_policy.assert_not_called()


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_revoke_deletes_policy_and_sets_flag(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        await mod.persist_owner_aips(
            "org_abc", arns, role_arn="arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"
        )
        fake = MagicMock()
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        await mod.revoke_owner_bedrock("org_abc")

        fake.delete_role_policy.assert_called_once_with(
            RoleName="isol8-dev-owner-bedrock-org_abc", PolicyName="owner-bedrock"
        )
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_revoked"]["BOOL"] is True

    @pytest.mark.asyncio
    async def test_revoke_idempotent_on_missing_policy(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        await mod.persist_owner_aips(
            "org_abc",
            {"opus": "a", "sonnet": "b", "haiku": "c"},
            role_arn="arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc",
        )
        fake = MagicMock()
        fake.delete_role_policy.side_effect = ClientError(
            {"Error": {"Code": "NoSuchEntity", "Message": "gone"}}, "DeleteRolePolicy"
        )
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        await mod.revoke_owner_bedrock("org_abc")  # must not raise
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_revoked"]["BOOL"] is True

    @pytest.mark.asyncio
    async def test_restore_reputs_policy_and_clears_flag(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        await mod.persist_owner_aips(
            "org_abc", arns, role_arn="arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"
        )
        fake = MagicMock()
        # restore -> ensure_owner_bedrock_role -> get_role must return a REAL
        # ARN string (a bare MagicMock would be written into DynamoDB and blow
        # up boto3's deepcopy of the put-item params).
        fake.get_role.return_value = {"Role": {"Arn": "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_abc"}}
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1,us-east-2,us-west-2", raising=False)

        await mod.restore_owner_bedrock("org_abc")

        pk = fake.put_role_policy.call_args.kwargs
        assert pk["RoleName"] == "isol8-dev-owner-bedrock-org_abc"
        assert pk["PolicyName"] == "owner-bedrock"
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_revoked"]["BOOL"] is False
