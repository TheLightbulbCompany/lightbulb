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
    build_owner_bedrock_direct_policy,
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


def test_fence_policy_carries_no_invoke_only_the_create_profile_deny():
    """THE FENCE: the standard owner policy grants NO bedrock:Invoke — the
    bedrock-gate is the only inference path (its own role invokes the owner's
    AIP). Only the CreateInferenceProfile Deny remains, as belt-and-suspenders
    against base-policy drift."""
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=_aip_arns(),
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    allows = [s for s in doc["Statement"] if s["Effect"] == "Allow"]
    assert allows == []
    assert [s["Sid"] for s in doc["Statement"]] == ["DenyTenantProfileEscalation"]


def test_direct_policy_has_invoke_allow_with_3_aips_and_9_fm_arns():
    # BREAK-GLASS shape only (scripts/bedrock_gate_break_glass.py).
    arns = _aip_arns()
    doc = build_owner_bedrock_direct_policy(
        owner_id="org_abc",
        aip_arns=arns,
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    # The 3 AIP ARNs + 9 FM ARNs are split across two Allow statements (AIPs
    # unconditional, FMs gated) — assert the UNION covers them all.
    allows = [s for s in doc["Statement"] if s["Effect"] == "Allow" and "bedrock:InvokeModel" in s["Action"]]
    for s in allows:
        assert "bedrock:InvokeModelWithResponseStream" in s["Action"]
    all_resources = {r for s in allows for r in s["Resource"]}
    for arn in arns.values():
        assert arn in all_resources
    # 9 FM ARNs (3 models x 3 regions).
    expected_fm = {
        f"arn:aws:bedrock:{region}::foundation-model/{model}"
        for region in ("us-east-1", "us-east-2", "us-west-2")
        for model in (
            "anthropic.claude-opus-4-6-v1",
            "anthropic.claude-sonnet-4-6",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
        )
    }
    assert expected_fm.issubset(all_resources)
    assert len(expected_fm) == 9


def test_direct_aip_invoke_unconditional_fm_invoke_gated_to_owner_aips():
    # REGRESSION GUARD on the BREAK-GLASS builder: conditioning the
    # AIP-resource Allow on bedrock:InferenceProfileArn denies the invoke —
    # AWS does NOT set that key when authorizing the profile RESOURCE itself,
    # so the invoke fails closed with AccessDeniedException (verified live +
    # simulate-principal-policy). The AIP statement MUST be unconditional; only
    # the FM statement carries the condition.
    arns = _aip_arns()
    doc = build_owner_bedrock_direct_policy(
        owner_id="org_abc",
        aip_arns=arns,
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    aip_stmt = next(s for s in doc["Statement"] if s.get("Sid") == "OwnerScopedProfileInvoke")
    fm_stmt = next(s for s in doc["Statement"] if s.get("Sid") == "OwnerScopedFmViaProfile")
    # AIP statement: exactly the owner's AIP ARNs, and NO condition.
    assert sorted(aip_stmt["Resource"]) == sorted(arns.values())
    assert "Condition" not in aip_stmt
    # FM statement: condition scopes to the owner's AIPs, and carries no AIP ARNs.
    assert fm_stmt["Condition"]["ArnEquals"]["bedrock:InferenceProfileArn"] == sorted(arns.values())
    for arn in arns.values():
        assert arn not in fm_stmt["Resource"]


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
        # default "lightbulb-dev-"). Setting the env var here is too late, so patch
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

    @pytest.mark.asyncio
    async def test_persist_defaults_reverse_row_plan_to_paid(self, aip_tables):
        from core.billing.bedrock_aip import persist_owner_aips

        arns = {"sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s"}
        await persist_owner_aips("org_abc", arns)
        resp = aip_tables.get_item(
            TableName="test-aip-owners",
            Key={"aip_arn": {"S": arns["sonnet"]}},
        )
        assert resp["Item"]["plan"]["S"] == "paid"

    @pytest.mark.asyncio
    async def test_persist_writes_trial_plan_when_specified(self, aip_tables):
        from core.billing.bedrock_aip import persist_owner_aips

        arns = {"sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s"}
        await persist_owner_aips("org_abc", arns, plan="trial")
        resp = aip_tables.get_item(
            TableName="test-aip-owners",
            Key={"aip_arn": {"S": arns["sonnet"]}},
        )
        assert resp["Item"]["plan"]["S"] == "trial"


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
        assert created["opus"]["inferenceProfileName"] == "lightbulb-dev-org_abc-opus"
        tags = {t["key"]: t["value"] for t in created["opus"]["tags"]}
        assert tags["owner_id"] == "org_abc"
        assert tags["tier"] == "opus"
        # Persisted: reverse row resolves.
        resp = aip_tables.get_item(TableName="test-aip-owners", Key={"aip_arn": {"S": arns["opus"]}})
        assert resp["Item"]["owner_id"]["S"] == "org_abc"

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
        resp = aip_tables.get_item(TableName="test-aip-owners", Key={"aip_arn": {"S": arns["haiku"]}})
        assert resp["Item"]["owner_id"]["S"] == "org_abc"

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
                        "inferenceProfileName": "lightbulb-dev-org_abc-opus",
                        "inferenceProfileArn": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/opus-exist",
                    },
                    {
                        "inferenceProfileName": "lightbulb-dev-org_abc-sonnet",
                        "inferenceProfileArn": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/sonnet-exist",
                    },
                    {
                        "inferenceProfileName": "lightbulb-dev-org_abc-haiku",
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
        # Adopted AIPs are persisted -> reverse row resolves.
        resp = aip_tables.get_item(TableName="test-aip-owners", Key={"aip_arn": {"S": arns["sonnet"]}})
        assert resp["Item"]["owner_id"]["S"] == "org_abc"

    @pytest.mark.asyncio
    async def test_default_tiers_none_mints_three_with_paid_plan(self, aip_tables, monkeypatch):
        """tiers=None (the call shape every existing caller uses today) must
        remain byte-identical: all 3 tiers minted, reverse rows carry
        plan="paid"."""
        import core.billing.bedrock_aip as mod

        created = {}

        def _create(**kwargs):
            src = kwargs["modelSource"]["copyFrom"]
            profile_id = src.split(":inference-profile/")[1]
            tier = next(t for t, p in mod.SYSTEM_PROFILE_ID.items() if p == profile_id)
            created[tier] = kwargs
            arn = f"arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/{tier}-aip"
            return {"inferenceProfileArn": arn}

        fake = MagicMock()
        fake.create_inference_profile.side_effect = _create
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        # No `tiers` kwarg at all — the exact call shape ecs_manager.py uses.
        arns = await mod.ensure_owner_aips("org_paid")

        assert set(arns) == {"opus", "sonnet", "haiku"}
        assert fake.create_inference_profile.call_count == 3
        for tier in ("opus", "sonnet", "haiku"):
            resp = aip_tables.get_item(
                TableName="test-aip-owners",
                Key={"aip_arn": {"S": arns[tier]}},
            )
            assert resp["Item"]["plan"]["S"] == "paid"

    @pytest.mark.asyncio
    async def test_single_tier_mints_exactly_one_aip_and_persists_trial_plan(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        created = {}

        def _create(**kwargs):
            src = kwargs["modelSource"]["copyFrom"]
            profile_id = src.split(":inference-profile/")[1]
            tier = next(t for t, p in mod.SYSTEM_PROFILE_ID.items() if p == profile_id)
            created[tier] = kwargs
            arn = f"arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/{tier}-aip"
            return {"inferenceProfileArn": arn}

        fake = MagicMock()
        fake.create_inference_profile.side_effect = _create
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        arns = await mod.ensure_owner_aips("org_trial", tiers=["sonnet"])

        assert set(arns) == {"sonnet"}
        assert fake.create_inference_profile.call_count == 1
        assert "opus" not in created
        assert "haiku" not in created

        resp = aip_tables.get_item(
            TableName="test-aip-owners",
            Key={"aip_arn": {"S": arns["sonnet"]}},
        )
        assert resp["Item"]["owner_id"]["S"] == "org_trial"
        assert resp["Item"]["tier"]["S"] == "sonnet"
        assert resp["Item"]["plan"]["S"] == "trial"

    @pytest.mark.asyncio
    async def test_single_tier_idempotent_short_circuit_repersists_trial_plan(self, aip_tables, monkeypatch):
        """The C1 self-heal (short-circuit still re-puts reverse rows) must keep
        working for a single-tier trial mint, and the re-put must now carry
        plan="trial". Uses "org_abc" (the aip_tables fixture's pre-seeded
        containers row) — update_fields no-ops on an owner with no row, so a
        fresh owner id here would never persist to the containers row and the
        short-circuit this test targets would never trigger."""
        import core.billing.bedrock_aip as mod

        arns = {"sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s"}
        await mod.persist_owner_aips("org_abc", arns, plan="trial")

        fake = MagicMock()
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        got = await mod.ensure_owner_aips("org_abc", tiers=["sonnet"])
        assert got == arns
        fake.create_inference_profile.assert_not_called()

        resp = aip_tables.get_item(
            TableName="test-aip-owners",
            Key={"aip_arn": {"S": arns["sonnet"]}},
        )
        assert resp["Item"]["plan"]["S"] == "trial"

    @pytest.mark.asyncio
    async def test_failed_upgrade_mint_never_leaves_a_paying_owner_stamped_trial(self, aip_tables, monkeypatch):
        """THE PAYING-CUSTOMER KILL. A trial owner converts to paid: ensure_owner_aips
        is called with tiers=None to mint opus + haiku on top of the existing sonnet.

        persist_owner_aips runs only at the END of that function, so if ANY
        create_inference_profile raises, it never runs — and the pre-existing sonnet
        reverse row keeps plan="trial". The meter Lambda reads exactly that row to
        decide whether to apply the $3/day cap and the IAM-revoke + ECS desiredCount=0
        kill. So one transient mint failure during an upgrade would cut a PAYING
        customer off from Bedrock and stop their container.

        The plan must be re-stamped for the tiers already held BEFORE any mint is
        attempted: it is a property of the owner, not of the mint's outcome.
        """
        import core.billing.bedrock_aip as mod

        sonnet = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s"
        await mod.persist_owner_aips("org_abc", {"sonnet": sonnet}, plan="trial")

        # The upgrade mint blows up partway through (transient AWS error).
        fake = MagicMock()
        fake.create_inference_profile.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "rate exceeded"}},
            "CreateInferenceProfile",
        )
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        with pytest.raises(ClientError):
            await mod.ensure_owner_aips("org_abc")  # tiers=None -> the PAID tier set

        # The mint failed and persist_owner_aips never ran — but the owner is paid,
        # so the row the meter reads must NOT still say "trial".
        resp = aip_tables.get_item(
            TableName="test-aip-owners",
            Key={"aip_arn": {"S": sonnet}},
        )
        assert resp["Item"]["plan"]["S"] == "paid", (
            "a converted owner is still stamped trial after a failed upgrade mint — "
            "the meter would cap and KILL a paying customer"
        )

    @pytest.mark.asyncio
    async def test_single_tier_mint_feeds_policy_pinned_to_one_model(self, aip_tables, monkeypatch):
        """SECURITY PROPERTY: minting only the sonnet tier and feeding the
        result into build_owner_bedrock_policy produces a per-owner IAM policy
        whose unconditional AIP-invoke Allow lists ONLY that one AIP ARN, and
        whose FM-invoke condition scopes to only that one AIP — so even in
        BREAK-GLASS direct mode a trial owner's role has no path to opus/haiku,
        even via a raw foundation-model ARN. (The STANDARD policy is the fence:
        no invoke at all — see test_fence_policy_carries_no_invoke...)"""
        import core.billing.bedrock_aip as mod

        def _create(**kwargs):
            return {
                "inferenceProfileArn": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/sonnet-aip"
            }

        fake = MagicMock()
        fake.create_inference_profile.side_effect = _create
        monkeypatch.setattr(mod, "_bedrock_client", lambda: fake)
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        arns = await mod.ensure_owner_aips("org_trial", tiers=["sonnet"])
        doc = mod.build_owner_bedrock_direct_policy(
            owner_id="org_trial",
            aip_arns=arns,
            regions=mod.FAN_OUT_REGIONS,
        )

        aip_stmt = next(s for s in doc["Statement"] if s.get("Sid") == "OwnerScopedProfileInvoke")
        assert aip_stmt["Resource"] == [arns["sonnet"]]
        fm_stmt = next(s for s in doc["Statement"] if s.get("Sid") == "OwnerScopedFmViaProfile")
        assert fm_stmt["Condition"]["ArnEquals"]["bedrock:InferenceProfileArn"] == [arns["sonnet"]]


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
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1,us-east-2,us-west-2", raising=False)

        role_arn = await mod.ensure_owner_bedrock_role("org_abc", arns)

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
        # scoped inline policy put with the FENCE shape: NO invoke Allow at all
        # (the bedrock-gate is the only inference path), only the
        # CreateInferenceProfile Deny.
        pk = fake.put_role_policy.call_args.kwargs
        assert pk["RoleName"] == "lightbulb-dev-owner-bedrock-org_abc"
        doc = json.loads(pk["PolicyDocument"])
        assert [st["Effect"] for st in doc["Statement"]] == ["Deny"]
        assert doc["Statement"][0]["Sid"] == "DenyTenantProfileEscalation"
        # role ARN persisted on the row.
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_role_arn"]["S"] == role_arn

    @pytest.mark.asyncio
    async def test_new_role_trust_policy_carries_sourcearn_sourceaccount(self, aip_tables, monkeypatch):
        """A freshly minted per-owner role's trust policy must condition the
        ecs-tasks assumption on aws:SourceAccount + aws:SourceArn so a confused
        deputy in another account/service cannot assume it (the per-owner role
        previously trusted ecs-tasks.amazonaws.com unconditionally)."""
        import core.billing.bedrock_aip as mod

        arns = _aip_arns()
        fake = MagicMock()
        fake.get_role.side_effect = _no_such_entity()
        fake.create_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1", raising=False)

        await mod.ensure_owner_bedrock_role("org_abc", arns)

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

        arns = _aip_arns()
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
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1", raising=False)

        await mod.ensure_owner_bedrock_role("org_abc", arns)

        fake.update_assume_role_policy.assert_called_once()
        trust = json.loads(fake.update_assume_role_policy.call_args.kwargs["PolicyDocument"])
        stmt = trust["Statement"][0]
        assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == "877352799272"
        assert stmt["Condition"]["ArnLike"]["aws:SourceArn"] == "arn:aws:ecs:us-east-1:877352799272:task/*"
        # Convergence is marked so later calls skip the backfill.
        fake.tag_role.assert_called_once()
        assert fake.tag_role.call_args.kwargs["Tags"] == [{"Key": "trust_hardened", "Value": "1"}]

    @pytest.mark.asyncio
    async def test_already_hardened_role_skips_trust_backfill(self, aip_tables, monkeypatch):
        """A role already tagged trust_hardened=1 must NOT re-run the trust
        backfill on every ensure call — avoids a dead IAM round-trip on each
        re-provision / container-status poll once the fleet is converged."""
        import core.billing.bedrock_aip as mod

        arns = _aip_arns()
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
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1", raising=False)

        await mod.ensure_owner_bedrock_role("org_abc", arns)

        fake.update_assume_role_policy.assert_not_called()
        fake.tag_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_trust_backfill_clienterror_is_swallowed(self, aip_tables, monkeypatch):
        """A ClientError from update_assume_role_policy (e.g. the new
        iam:UpdateAssumeRolePolicy grant has not propagated yet) must NOT break
        provisioning — the role is still usable on its old trust, and the
        scoped policy + DDB write still happen."""
        import core.billing.bedrock_aip as mod

        arns = _aip_arns()
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
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1", raising=False)

        role_arn = await mod.ensure_owner_bedrock_role("org_abc", arns)

        assert role_arn.endswith("lightbulb-dev-owner-bedrock-org_abc")  # provisioning completed
        fake.put_role_policy.assert_called_once()  # scoped policy still applied
        # not tagged, so the next provision retries the converge
        fake.tag_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_existing_role_repts_policy_no_create(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        arns = {
            "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/o",
            "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/s",
            "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/h",
        }
        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1,us-east-2,us-west-2", raising=False)

        role_arn = await mod.ensure_owner_bedrock_role("org_abc", arns)
        assert role_arn.endswith("lightbulb-dev-owner-bedrock-org_abc")
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
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            mod.settings, "BEDROCK_OWNER_BASE_POLICY_ARN", "arn:aws:iam::877352799272:policy/base", raising=False
        )

        role_arn = await mod.ensure_owner_bedrock_role("org_abc", arns, skip_bedrock_grant=True)
        assert role_arn.endswith("lightbulb-dev-owner-bedrock-org_abc")
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
            "org_abc", arns, role_arn="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"
        )
        fake = MagicMock()
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)

        await mod.revoke_owner_bedrock("org_abc")

        fake.delete_role_policy.assert_called_once_with(
            RoleName="lightbulb-dev-owner-bedrock-org_abc", PolicyName="owner-bedrock"
        )
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_revoked"]["BOOL"] is True

    @pytest.mark.asyncio
    async def test_revoke_idempotent_on_missing_policy(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        await mod.persist_owner_aips(
            "org_abc",
            {"opus": "a", "sonnet": "b", "haiku": "c"},
            role_arn="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc",
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
            "org_abc", arns, role_arn="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"
        )
        fake = MagicMock()
        # restore -> ensure_owner_bedrock_role -> get_role must return a REAL
        # ARN string (a bare MagicMock would be written into DynamoDB and blow
        # up boto3's deepcopy of the put-item params).
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1,us-east-2,us-west-2", raising=False)

        await mod.restore_owner_bedrock("org_abc")

        pk = fake.put_role_policy.call_args.kwargs
        assert pk["RoleName"] == "lightbulb-dev-owner-bedrock-org_abc"
        assert pk["PolicyName"] == "owner-bedrock"
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_revoked"]["BOOL"] is False

    @pytest.mark.asyncio
    async def test_revoke_also_flips_gate_deny_flag(self, aip_tables, monkeypatch):
        """bedrock-gate transition: revoke ALSO denies the gate token (the gateway
        invokes under its own role, so the IAM revoke does not reach it). Both
        fences held during transition."""
        import core.billing.bedrock_aip as mod

        await mod.persist_owner_aips(
            "org_abc", {"sonnet": "s"}, role_arn="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"
        )
        monkeypatch.setattr(mod, "_iam_client", lambda: MagicMock())
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        deny = AsyncMock()
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.deny_gate_token", deny)

        await mod.revoke_owner_bedrock("org_abc")

        deny.assert_awaited_once_with("org_abc", True)

    @pytest.mark.asyncio
    async def test_restore_also_clears_gate_deny_flag(self, aip_tables, monkeypatch):
        import core.billing.bedrock_aip as mod

        await mod.persist_owner_aips(
            "org_abc",
            {"opus": "o", "sonnet": "s", "haiku": "h"},
            role_arn="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc",
        )
        fake = MagicMock()
        fake.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"}
        }
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1,us-east-2,us-west-2", raising=False)
        deny = AsyncMock()
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.deny_gate_token", deny)

        await mod.restore_owner_bedrock("org_abc")

        deny.assert_awaited_once_with("org_abc", False)

    @pytest.mark.asyncio
    async def test_revoke_survives_deny_token_failure(self, aip_tables, monkeypatch):
        """A deny-token hiccup MUST NOT break the IAM revoke — the IAM half is the
        primary fence; the deny is a best-effort backstop."""
        import core.billing.bedrock_aip as mod

        await mod.persist_owner_aips(
            "org_abc", {"sonnet": "s"}, role_arn="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_abc"
        )
        fake = MagicMock()
        monkeypatch.setattr(mod, "_iam_client", lambda: fake)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(
            "core.billing.bedrock_gate_tokens.deny_gate_token",
            AsyncMock(side_effect=RuntimeError("ddb down")),
        )

        await mod.revoke_owner_bedrock("org_abc")  # must not raise

        # The IAM revoke + flag still happened.
        fake.delete_role_policy.assert_called_once()
        row = aip_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert row["bedrock_revoked"]["BOOL"] is True


class TestLitestreamS3Policy:
    """local-sqlite: the per-owner Litestream S3 grant must reach EVERY owner (it is
    storage durability, not inference) and must live under its own PolicyName
    so the break-glass owner-bedrock swap can never strip it."""

    def test_policy_shape_prefix_scoped(self):
        import core.billing.bedrock_aip as mod

        doc = mod.build_owner_litestream_s3_policy("org_abc", "isol8-dev-openclaw-replicas")
        sids = {st["Sid"]: st for st in doc["Statement"]}
        assert sids["ReplicaObjects"]["Resource"] == "arn:aws:s3:::isol8-dev-openclaw-replicas/org_abc/*"
        assert set(sids["ReplicaObjects"]["Action"]) == {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}
        assert sids["ReplicaBucketList"]["Condition"] == {"StringLike": {"s3:prefix": ["org_abc/*"]}}
        assert sids["ReplicaBucketLocation"]["Resource"] == "arn:aws:s3:::isol8-dev-openclaw-replicas"

    @pytest.mark.asyncio
    async def test_put_even_under_skip_bedrock_grant(self, aip_tables, monkeypatch):
        """chatgpt_oauth/byo_key/revoked owners get the S3 grant: their
        containers replicate too. The Bedrock inline policy stays withheld."""
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

        await mod.ensure_owner_bedrock_role("org_abc", {}, skip_bedrock_grant=True)

        fake.put_role_policy.assert_called_once()
        kwargs = fake.put_role_policy.call_args.kwargs
        assert kwargs["PolicyName"] == "owner-litestream-s3"
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

        await mod.ensure_owner_bedrock_role("org_abc", {}, skip_bedrock_grant=True)

        fake.put_role_policy.assert_not_called()


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
        monkeypatch.setattr(mod, "_caller_account_id", AsyncMock(return_value="877352799272"))
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
        monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1", raising=False)
        return fake

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
        # NOT `{account}:browser/*` -- that is the CUSTOM-browser ARN shape, which we never
        # create and the CDK permissions boundary does not cover, so it would authorize
        # nothing while reading as though it did.
        assert "877352799272:browser/*" not in raw
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
