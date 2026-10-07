"""agent_mail_tenant: deterministic names, idempotent creation, fail-soft."""

import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from core.services import agent_mail_tenant as mod


def test_tenant_name_sanitizes():
    assert mod.tenant_name_for("user_2abc") == "owner-user_2abc"
    assert mod.tenant_name_for("org_2abc") == "owner-org_2abc"
    assert mod.tenant_name_for("weird id!") == "owner-weird-id-"


@pytest.fixture
def ses(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(mod, "_client", lambda: fake)
    monkeypatch.setattr(mod, "_account_id", lambda: "123456789012")  # sync in tests; see impl
    monkeypatch.setattr(mod.settings, "AGENT_MAIL_CONFIGURATION_SET", "isol8-dev-agent-mail")
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "dev.isol8.co")
    monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1")
    mod._ensured.clear()
    return fake


@pytest.mark.asyncio
async def test_ensure_tenant_creates_associates_and_sets_policy(ses):
    # The real tenant ARN carries a trailing tenant-id segment
    # (tenant/{name}/{tn-id}) that a caller cannot predict — it must come
    # from create_tenant's own response, not be hand-built.
    tenant_arn = "arn:aws:ses:us-east-1:123456789012:tenant/owner-user_1/tn-abc"
    ses.create_tenant.return_value = {"TenantArn": tenant_arn}
    name = await mod.ensure_tenant("user_1")
    assert name == "owner-user_1"
    ses.create_tenant.assert_called_once_with(TenantName="owner-user_1")
    ses.get_tenant.assert_not_called()
    arns = {c.kwargs["ResourceArn"] for c in ses.create_tenant_resource_association.call_args_list}
    assert arns == {
        "arn:aws:ses:us-east-1:123456789012:identity/isol8.co",
        "arn:aws:ses:us-east-1:123456789012:configuration-set/isol8-dev-agent-mail",
    }
    pol = ses.update_reputation_entity_policy.call_args.kwargs
    assert pol["ReputationEntityPolicy"] == "arn:aws:ses:us-east-1:aws:reputation-policy/standard"
    assert pol["ReputationEntityReference"] == tenant_arn


@pytest.mark.asyncio
async def test_ensure_tenant_is_idempotent_and_memoized(ses):
    # AlreadyExistsException means create_tenant's response never arrives, so
    # the real ARN has to be fetched back via get_tenant instead.
    tenant_arn = "arn:aws:ses:us-east-1:123456789012:tenant/owner-user_1/tn-abc"
    ses.create_tenant.side_effect = ClientError({"Error": {"Code": "AlreadyExistsException"}}, "CreateTenant")
    ses.get_tenant.return_value = {"Tenant": {"TenantArn": tenant_arn}}
    assert await mod.ensure_tenant("user_1") == "owner-user_1"
    assert await mod.ensure_tenant("user_1") == "owner-user_1"
    assert ses.create_tenant.call_count == 1
    ses.get_tenant.assert_called_once_with(TenantName="owner-user_1")
    pol = ses.update_reputation_entity_policy.call_args.kwargs
    assert pol["ReputationEntityReference"] == tenant_arn


@pytest.mark.asyncio
async def test_ensure_tenant_off_when_no_config_set(ses, monkeypatch):
    monkeypatch.setattr(mod.settings, "AGENT_MAIL_CONFIGURATION_SET", "")
    assert await mod.ensure_tenant("user_1") is None
    ses.create_tenant.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_tenant_never_raises(ses):
    ses.create_tenant.side_effect = RuntimeError("boom")
    assert await mod.ensure_tenant("user_1") is None


@pytest.mark.asyncio
async def test_ensure_tenant_bounded_by_timeout(ses, monkeypatch):
    """A hung _ensure_sync (dead network, throttled retries) must not stall
    the send path indefinitely — anyio.fail_after bounds it and the resulting
    TimeoutError is caught by the existing fail-soft except Exception."""
    monkeypatch.setattr(mod, "_ENSURE_TIMEOUT_S", 0.05)

    def _sleeper(owner_id):
        time.sleep(0.5)
        return mod.tenant_name_for(owner_id)

    monkeypatch.setattr(mod, "_ensure_sync", _sleeper)
    assert await mod.ensure_tenant("user_1") is None
    assert "owner-user_1" not in mod._ensured
