import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from core.billing import bedrock_aip


def test_aws_sts_policy_doc_scopes_exact_arn_and_denies_our_account():
    doc = bedrock_aip._build_owner_aws_sts_doc(["arn:aws:iam::111122223333:role/isol8-readonly-x"])
    stmts = doc["Statement"]
    allow = next(s for s in stmts if s["Effect"] == "Allow")
    deny = next(s for s in stmts if s["Effect"] == "Deny")
    assert allow["Action"] == "sts:AssumeRole"
    assert allow["Resource"] == ["arn:aws:iam::111122223333:role/isol8-readonly-x"]
    assert "*" not in str(allow["Resource"])
    assert deny["Action"] == "sts:AssumeRole"
    assert deny["Resource"] == "arn:aws:iam::877352799272:role/*"


def test_aws_sts_policy_doc_allows_multiple_exact_arns_never_wildcard():
    arns = [
        "arn:aws:iam::111122223333:role/isol8-readonly-a",
        "arn:aws:iam::444455556666:role/isol8-readonly-b",
    ]
    doc = bedrock_aip._build_owner_aws_sts_doc(arns)
    allow = next(s for s in doc["Statement"] if s["Effect"] == "Allow")
    assert allow["Resource"] == arns
    assert "*" not in str(allow["Resource"])
    assert doc["Version"] == "2012-10-17"


@pytest.mark.asyncio
async def test_put_uses_owner_aws_sts_policy_name_on_owner_role(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(bedrock_aip, "_iam_client", lambda: fake)
    monkeypatch.setattr(bedrock_aip.settings, "ENVIRONMENT", "dev", raising=False)

    await bedrock_aip.put_owner_aws_sts_policy("org_abc", ["arn:aws:iam::111122223333:role/ro"])

    pk = fake.put_role_policy.call_args.kwargs
    # Same per-owner role the Bedrock grant uses (_owner_role_name).
    assert pk["RoleName"] == "isol8-dev-owner-bedrock-org_abc"
    # SEPARATE policy name — never folded into "owner-bedrock".
    assert pk["PolicyName"] == "owner-aws-sts"
    doc = json.loads(pk["PolicyDocument"])
    allow = next(s for s in doc["Statement"] if s["Effect"] == "Allow")
    assert allow["Resource"] == ["arn:aws:iam::111122223333:role/ro"]


@pytest.mark.asyncio
async def test_delete_uses_owner_aws_sts_policy_name_on_owner_role(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(bedrock_aip, "_iam_client", lambda: fake)
    monkeypatch.setattr(bedrock_aip.settings, "ENVIRONMENT", "dev", raising=False)

    await bedrock_aip.delete_owner_aws_sts_policy("org_abc")

    fake.delete_role_policy.assert_called_once_with(
        RoleName="isol8-dev-owner-bedrock-org_abc", PolicyName="owner-aws-sts"
    )


@pytest.mark.asyncio
async def test_delete_idempotent_on_missing_policy(monkeypatch):
    fake = MagicMock()
    fake.delete_role_policy.side_effect = ClientError(
        {"Error": {"Code": "NoSuchEntity", "Message": "gone"}}, "DeleteRolePolicy"
    )
    monkeypatch.setattr(bedrock_aip, "_iam_client", lambda: fake)
    monkeypatch.setattr(bedrock_aip.settings, "ENVIRONMENT", "dev", raising=False)

    await bedrock_aip.delete_owner_aws_sts_policy("org_abc")  # must not raise


@pytest.mark.asyncio
async def test_delete_reraises_non_nosuchentity_clienterror(monkeypatch):
    fake = MagicMock()
    fake.delete_role_policy.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "DeleteRolePolicy"
    )
    monkeypatch.setattr(bedrock_aip, "_iam_client", lambda: fake)
    monkeypatch.setattr(bedrock_aip.settings, "ENVIRONMENT", "dev", raising=False)

    with pytest.raises(ClientError):
        await bedrock_aip.delete_owner_aws_sts_policy("org_abc")
