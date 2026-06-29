import re
from urllib.parse import quote

from core.services.credentials import aws_cfn


def test_gen_external_id_is_url_safe_and_long_enough():
    ext = aws_cfn.gen_external_id()
    # secrets.token_urlsafe(32) yields ~43 chars of URL-safe base64 (≥128-bit).
    assert len(ext) >= 43
    assert re.fullmatch(r"[A-Za-z0-9_-]+", ext)
    # Fresh entropy each call.
    assert aws_cfn.gen_external_id() != ext


def test_owner_task_role_arn_reuses_bedrock_aip_name():
    arn = aws_cfn.owner_task_role_arn("org_x")
    assert re.fullmatch(r"arn:aws:iam::877352799272:role/isol8-.*-owner-bedrock-org_x", arn)


def test_quick_create_url_carries_all_params_url_encoded():
    template_url = "https://s3.amazonaws.com/isol8-cfn/readonly.yaml"
    principal_arn = "arn:aws:iam::877352799272:role/isol8-dev-owner-bedrock-org_x"
    policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
    url = aws_cfn.quick_create_url(
        external_id="ext123",
        principal_arn=principal_arn,
        policy_arn=policy_arn,
        region="us-east-1",
        template_url=template_url,
    )
    assert url.startswith(
        "https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review"
    )
    assert "stackName=isol8-readonly-connector" in url
    assert "param_ExternalId=ext123" in url
    # template_url / principal_arn / policy_arn are URL-encoded (safe="").
    assert f"templateURL={quote(template_url, safe='')}" in url
    assert f"param_TrustedPrincipalArn={quote(principal_arn, safe='')}" in url
    assert f"param_ManagedPolicyArn={quote(policy_arn, safe='')}" in url
    # The raw ARN colons/slashes must not leak unencoded into the query.
    assert "param_ManagedPolicyArn=arn:aws:iam" not in url
