"""Unit tests for core.billing.bedrock_aip — per-owner AIP + scoped IAM role."""

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
