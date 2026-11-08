"""Per-session scoped credentials: the backend vends each invoke credentials for its owner only."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.billing.bedrock_aip import build_owner_session_policy
from core.services import runtime_transport as rt

BUCKET = "lightbulb-dev-owner-state"
MEMORY = "arn:aws:bedrock-agentcore:us-east-1:111122223333:memory/ws-mem-AbCdEf1234"
ROLE = "arn:aws:iam::111122223333:role/lightbulb-dev-hermes-tenant"


def _allows(policy):
    return [st for st in policy["Statement"] if st["Effect"] == "Allow"]


def test_the_session_policy_reaches_only_the_owners_prefix():
    policy = build_owner_session_policy("org_a", BUCKET)
    objects = [st for st in _allows(policy) if "s3:GetObject" in st["Action"]]
    assert [st["Resource"] for st in objects] == [f"arn:aws:s3:::{BUCKET}/org_a/*"]
    listing = next(st for st in _allows(policy) if st["Action"] == ["s3:ListBucket"])
    assert listing["Condition"] == {"StringLike": {"s3:prefix": ["org_a/*"]}}
    resources = {st["Resource"] for st in policy["Statement"]}
    assert resources == {
        f"arn:aws:s3:::{BUCKET}",
        f"arn:aws:s3:::{BUCKET}/org_a/*",
    }  # nothing bucket-wide but list/location
    assert "*" not in json.dumps(policy).replace("org_a/*", "")


def test_the_session_policy_scopes_memory_to_the_owners_actors_and_namespaces():
    policy = build_owner_session_policy("org_a", BUCKET, MEMORY)
    memory = [st for st in policy["Statement"] if st["Resource"] == MEMORY]
    assert memory and all("Condition" in st for st in memory)
    keys = [v for st in memory for cond in st["Condition"].values() for v in cond.values()]
    assert keys == ["org_a", "org_a:user_*", "/owners/org_a/", "/owners/org_a:user_*/"]


def test_the_session_policy_fits_stss_inline_limit():
    long_owner = "org_" + "x" * 28  # a real Clerk org id is 31 characters
    policy = build_owner_session_policy(long_owner, BUCKET + "-111122223333", MEMORY)
    assert len(json.dumps(policy, separators=(",", ":"))) <= 2048


class _Sts:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def assume_role(self, **kw):
        from botocore.exceptions import ClientError

        self.calls.append(kw)
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "AssumeRole")
        return {
            "Credentials": {
                "AccessKeyId": "ASIA1",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(timezone.utc) + timedelta(hours=1),
            }
        }


@pytest.fixture
def sts(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "HERMES_TENANT_ROLE_ARN", ROLE)
    monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", BUCKET)
    monkeypatch.setattr(settings, "AGENTCORE_MEMORY_ID", "ws-mem-AbCdEf1234")
    monkeypatch.setattr(rt, "_SESSION_CREDENTIALS", {})
    client = _Sts()
    monkeypatch.setattr("boto3.client", lambda name, **kw: client)
    return client


def test_credentials_are_minted_with_the_owners_session_policy_and_cached(sts):
    header = rt._session_credentials_header("org_a")
    assert json.loads(header[rt.SESSION_CREDENTIALS_HEADER])["accessKeyId"] == "ASIA1"
    (call,) = sts.calls
    assert call["RoleArn"] == ROLE and call["DurationSeconds"] == 3600
    assert json.loads(call["Policy"]) == build_owner_session_policy("org_a", BUCKET, MEMORY)
    assert rt._session_credentials_header("org_a") == header
    assert len(sts.calls) == 1  # cached until 15 min before expiry
    rt._session_credentials_header("org_b")
    assert len(sts.calls) == 2 and "org_b" in sts.calls[1]["Policy"]


def test_no_role_or_a_failed_mint_sends_no_credentials(sts, monkeypatch):
    from core.config import settings

    sts.fail = True
    assert rt._session_credentials_header("org_a") == {}
    monkeypatch.setattr(settings, "HERMES_TENANT_ROLE_ARN", "")
    assert rt._session_credentials_header("org_a") == {}


@pytest.mark.asyncio
async def test_every_invoke_and_the_wake_carry_the_owners_credentials(sts, monkeypatch):
    captured = []

    class Client(_Sts):
        def invoke_agent_runtime(self, **kwargs):
            captured.append(json.loads(kwargs["payload"].decode()))
            return {"statusCode": 200, "response": b'{"status":200,"body":{}}'}

    client = Client()
    monkeypatch.setattr("boto3.client", lambda name, **kw: client)
    monkeypatch.setattr(rt, "_ensure_session", lambda owner_id, arn, deadline, wake: wake())  # a cold session
    await rt.request(
        {"owner_id": "org_a", "agentcore_runtime_arn": "arn:runtime/abc"},
        "/plugins/lightbulb-channel/rpc",
        json_body={},
    )
    assert len(captured) == 2  # the wake, then the call
    assert all(json.loads(e["headers"][rt.SESSION_CREDENTIALS_HEADER])["sessionToken"] == "token" for e in captured)
