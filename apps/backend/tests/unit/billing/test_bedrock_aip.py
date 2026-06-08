"""Unit tests for core.billing.bedrock_aip — per-owner AIP + scoped IAM role."""

import boto3
import pytest
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
        account="877352799272",
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
        account="877352799272",
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    allow = next(s for s in doc["Statement"] if s["Effect"] == "Allow" and "bedrock:InvokeModel" in s["Action"])
    cond = allow["Condition"]["ArnEquals"]["bedrock:InferenceProfileArn"]
    assert sorted(cond) == sorted(arns.values())


def test_policy_explicitly_denies_create_inference_profile():
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=_aip_arns(),
        account="877352799272",
        regions=("us-east-1", "us-east-2", "us-west-2"),
    )
    deny = next(s for s in doc["Statement"] if s["Effect"] == "Deny")
    assert deny["Action"] == "bedrock:CreateInferenceProfile"
    assert deny["Resource"] == "*"


def test_policy_is_iam_document_version():
    doc = build_owner_bedrock_policy(
        owner_id="org_abc",
        aip_arns=_aip_arns(),
        account="877352799272",
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
