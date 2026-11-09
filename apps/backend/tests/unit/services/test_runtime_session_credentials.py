"""Per-session scoped credentials: the backend vends each invoke credentials for its owner only."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import patch

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
    assert [st["Condition"] for st in memory] == [
        {"StringLike": {"bedrock-agentcore:actorId": ["org_a", "org_a:user_*"]}},
        {"StringLike": {"bedrock-agentcore:namespace": ["/owners/org_a/", "/owners/org_a:user_*/"]}},
    ]


#: Real-length names: prod's memory ARN (CDK names it lightbulb_<env>_workspace_memory-<10>), the
#: real bucket, and the longest owner id there is (a personal workspace, user_ + 27 = 32 characters).
REAL_MEMORY = "arn:aws:bedrock-agentcore:us-east-1:877352799272:memory/lightbulb_prod_workspace_memory-AbCdEf1234"
REAL_BUCKET = "lightbulb-prod-openclaw-replicas"
LONGEST_OWNER = "user_" + "X" * 27


def test_the_session_policy_fits_stss_inline_limit_with_margin():
    assert len(LONGEST_OWNER) == 32
    policy = build_owner_session_policy(LONGEST_OWNER, REAL_BUCKET, REAL_MEMORY)
    assert len(json.dumps(policy, separators=(",", ":"))) <= 2048 - 300  # room for one more statement


def test_the_session_policy_carries_no_browser_grant():
    assert "Browser" not in json.dumps(build_owner_session_policy("org_a", REAL_BUCKET, REAL_MEMORY))


def test_owner_as_credentials_never_reach_owner_bs_prefix_or_memory():
    """Tenant isolation, as the shared runtime relies on it: A's set names A everywhere, B nowhere."""
    a = json.dumps(build_owner_session_policy("org_a", BUCKET, MEMORY))
    assert "org_b" not in a and f"{BUCKET}/*" not in a
    b = build_owner_session_policy("org_b", BUCKET, MEMORY)
    assert {st["Resource"] for st in b["Statement"] if "/org_" in st["Resource"]} == {f"arn:aws:s3:::{BUCKET}/org_b/*"}


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
    monkeypatch.setattr(rt, "_SESSION_FAILED", {})
    client = _Sts()
    client.kw = {}
    monkeypatch.setattr("boto3.client", lambda name, **kw: (client.kw.update(kw), client)[1])
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


def test_sts_gets_short_budgets(sts):
    rt._session_credentials_header("org_a")
    config = sts.kw["config"]
    assert (config.connect_timeout, config.read_timeout) == (2, 5)
    assert config.retries == {"total_max_attempts": 2, "mode": "standard"}


def test_no_role_or_a_failed_mint_sends_no_credentials(sts, monkeypatch):
    from core.config import settings

    sts.fail = True
    assert rt._session_credentials_header("org_a") == {}
    assert rt._session_credentials_header("org_a") == {}
    assert len(sts.calls) == 1  # a failure is not retried on every invoke
    monkeypatch.setitem(rt._SESSION_FAILED, "org_a", 0.0)  # the 60 s are up
    sts.fail = False
    assert rt._session_credentials_header("org_a")
    assert len(sts.calls) == 2
    monkeypatch.setattr(settings, "HERMES_TENANT_ROLE_ARN", "")
    assert rt._session_credentials_header("org_b") == {}


def test_a_malformed_role_arn_sends_no_credentials_and_never_fails_the_invoke(sts, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "HERMES_TENANT_ROLE_ARN", "not-an-arn")
    assert rt._session_credentials_header("org_a") == {}
    assert sts.calls == []


@pytest.mark.asyncio
async def test_every_invoke_and_the_wake_carry_the_owners_credentials(sts, monkeypatch):
    captured = []
    monkeypatch.setattr("core.repositories.container_repo.get_session_sync", lambda *a, **k: {})

    class Client(_Sts):
        def invoke_agent_runtime(self, **kwargs):
            captured.append(json.loads(kwargs["payload"].decode()))
            return {"statusCode": 200, "response": b'{"status":200,"body":{}}'}

    client = Client()
    monkeypatch.setattr("boto3.client", lambda name, **kw: client)
    # patch.object, not monkeypatch: conftest stubs this with patch.object, and a monkeypatch undo
    # would put that stub back after conftest restored the real one (leaking it into later files).
    with patch.object(rt, "_ensure_session", lambda owner_id, arn, deadline, wake: wake()):  # a cold session
        await rt.request(
            {"owner_id": "org_a", "agentcore_runtime_arn": "arn:runtime/abc"},
            "/plugins/lightbulb-channel/rpc",
            json_body={},
        )
    assert len(captured) == 2  # the wake, then the call
    assert all(json.loads(e["headers"][rt.SESSION_CREDENTIALS_HEADER])["sessionToken"] == "token" for e in captured)


def test_the_network_seed_is_the_boot_secrets_else_derived_per_row(monkeypatch):
    """Owners provisioned before the shared runtime keep their member key (the boot secret's); one
    with no boot secret gets a stable derived one, new with each row (a teardown rotates it)."""
    from core.config import settings
    from core.services import agentcore_runtime

    monkeypatch.setattr(settings, "GATE_BEARER_HMAC_KEY", "k" * 32)
    monkeypatch.setattr("boto3.client", lambda *a, **kw: object())
    secrets = {"lightbulb/dev/hermes/org_old/boot": {"networkKey": "kept"}}
    monkeypatch.setattr(agentcore_runtime, "_read_secret", lambda sm, name: secrets.get(name))
    monkeypatch.setattr(agentcore_runtime, "boot_secret_name", lambda owner: f"lightbulb/dev/hermes/{owner}/boot")
    assert agentcore_runtime.network_seed_sync("org_old", "row-1") == "kept"
    new = agentcore_runtime.network_seed_sync("org_new", "row-1")
    assert (
        new
        == agentcore_runtime.network_seed_sync("org_new", "row-1")
        != agentcore_runtime.network_seed_sync("org_new", "row-2")
    )
    import base64

    assert len(base64.b64decode(new)) == 32  # an Ed25519 seed
