"""Provider bootstrap never buys phones and only mutates exact existing owned routing objects."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from livekit import api

from scripts import provision_phone_telephony as p


CONFIG = {
    "managed_by": p.MANAGED,
    "account_sid": "ACtest",
    "sip_auth_username": "lightbulb-livekit",
    "sip_auth_password": "Aa1!" + "test-only-password-" * 3,
}


class Twilio:
    sid = "ACtest"
    account = "https://api.twilio.com/2010-04-01/Accounts/ACtest"

    def __init__(self):
        self.trunks, self.urls, self.mappings, self.writes = [], [], [], []
        self.fail_origin = False
        self.certified = True

    async def request(self, method, url, **fields):
        if method != "GET":
            self.writes.append((method, url, fields))
        if "CustomerProfiles" in url:
            return {"results": [{"status": "twilio-approved" if self.certified else "pending-review"}]}
        if url.endswith("/Trunks"):
            if method == "POST":
                self.trunks.append(
                    {"sid": "TKtest", "friendly_name": fields["FriendlyName"], "domain_name": fields["DomainName"]}
                )
                return self.trunks[-1]
            return {"trunks": self.trunks}
        if url.endswith("/SIP/CredentialLists.json"):
            return {"credential_lists": [{"sid": "CLtest", "friendly_name": "lightbulb-livekit-termination"}]}
        if url.endswith("/Credentials.json"):
            return {"credentials": [{"sid": "CRtest", "username": "lightbulb-livekit"}]}
        if url.endswith("/CredentialLists"):
            if method == "POST":
                self.mappings.append({"sid": fields["CredentialListSid"]})
            return {"credential_lists": self.mappings}
        if url.endswith("/OriginationUrls"):
            if method == "POST":
                if self.fail_origin:
                    raise RuntimeError("origination failed")
                self.urls.append({"sip_url": fields["SipUrl"], "enabled": True})
            return {"origination_urls": self.urls}
        if method == "DELETE":
            self.trunks.clear()
        return {}


def livekit():
    inbound = api.SIPInboundTrunkInfo(
        sip_trunk_id=p.INBOUND, name="twilio-inbound", allowed_addresses=["54.172.60.0/30"]
    )
    outbound = api.SIPOutboundTrunkInfo(
        sip_trunk_id=p.OUTBOUND,
        name="twilio-outbound",
        address=p.DOMAIN,
        auth_username="lightbulb-livekit",
        auth_password="test-only-password",
    )
    dispatch = api.SIPDispatchRuleInfo(
        sip_dispatch_rule_id=p.DISPATCH,
        name="lightbulb-phone-twilio",
        trunk_ids=[p.INBOUND],
        rule=api.SIPDispatchRule(
            dispatch_rule_callee=api.SIPDispatchRuleCallee(room_prefix="phone-prod-", randomize=True)
        ),
        room_config=api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name="lightbulb-huddle")]),
    )
    return SimpleNamespace(
        sip=SimpleNamespace(
            list_sip_inbound_trunk=AsyncMock(return_value=SimpleNamespace(items=[inbound])),
            list_sip_outbound_trunk=AsyncMock(return_value=SimpleNamespace(items=[outbound])),
            list_sip_dispatch_rule=AsyncMock(return_value=SimpleNamespace(items=[dispatch])),
            update_outbound_trunk_fields=AsyncMock(),
            update_inbound_trunk=AsyncMock(),
        )
    )


async def test_inbound_trunk_maps_the_stir_shaken_header_once_keeping_its_route():
    """Security review 9 Nov: the worker reads Twilio's X-Twilio-VerStat as the `sip.verstat` attribute."""
    lk = livekit()
    report = await p.reconcile(Twilio(), lk, apply=False)
    assert report["mapVerstat"] is True and not lk.sip.update_inbound_trunk.await_count  # a plan writes nothing
    await p.reconcile(Twilio(), lk, apply=True, config=CONFIG)
    trunk_id, info = lk.sip.update_inbound_trunk.await_args.args
    assert trunk_id == p.INBOUND and dict(info.headers_to_attributes) == {"X-Twilio-VerStat": "sip.verstat"}
    assert info.name == "twilio-inbound" and list(info.allowed_addresses) == ["54.172.60.0/30"]
    lk.sip.list_sip_inbound_trunk.return_value.items[0] = info  # already mapped: nothing to do
    lk.sip.update_inbound_trunk.reset_mock()
    assert (await p.reconcile(Twilio(), lk, apply=True, config=CONFIG))["mapVerstat"] is False
    assert not lk.sip.update_inbound_trunk.await_count


async def test_inbound_trunk_with_auth_is_never_replaced():
    """Replace sends the whole trunk, and a listed trunk's password is masked: never overwrite it with the mask."""
    lk = livekit()
    lk.sip.list_sip_inbound_trunk.return_value.items[0].auth_username = "someone"
    with pytest.raises(p.ProviderMismatch, match="inbound_trunk_auth"):
        await p.reconcile(Twilio(), lk, apply=True, config=CONFIG)
    assert not lk.sip.update_inbound_trunk.await_count


async def test_dry_run_certifies_existing_route_without_writes():
    twilio = Twilio()
    result = await p.reconcile(twilio, livekit(), apply=False)
    assert result["createTwilioTrunk"] and not twilio.writes
    assert result["numberPurchases"] == result["calls"] == 0


async def test_reconcile_uses_real_sdk_partial_update_without_replacing_route_or_numbers():
    from livekit.api.sip_service import SipService

    lk = livekit()
    fake_reads = lk.sip
    real = SipService.__new__(SipService)
    real._client = SimpleNamespace(request=AsyncMock())
    real._admin_headers = lambda: {}
    for name in ("list_sip_inbound_trunk", "list_sip_outbound_trunk", "list_sip_dispatch_rule"):
        setattr(real, name, getattr(fake_reads, name))
    lk.sip = real
    await p.reconcile(Twilio(), lk, apply=True, config=CONFIG)
    args = real._client.request.await_args.args
    assert args[1] == "UpdateSIPOutboundTrunk"
    request = args[2]
    assert request.sip_trunk_id == p.OUTBOUND
    assert request.update.auth_username == CONFIG["sip_auth_username"]
    assert request.update.auth_password == CONFIG["sip_auth_password"]
    assert not request.HasField("replace") and not request.update.numbers.ListFields()
    assert not request.update.HasField("address")


async def test_apply_reuses_resources_on_retry_and_never_purchases_numbers():
    twilio = Twilio()
    first = await p.reconcile(twilio, livekit(), apply=True, config=CONFIG)
    again = await p.reconcile(twilio, livekit(), apply=True, config=CONFIG)
    assert first["twilioTrunkId"] == again["twilioTrunkId"] == "TKtest"
    assert len(twilio.trunks) == len(twilio.urls) == len(twilio.mappings) == 1
    assert all("IncomingPhoneNumbers" not in url and "/Calls" not in url for _, url, _ in twilio.writes)


async def test_failed_new_trunk_is_compensated_without_removing_existing_trunk():
    twilio = Twilio()
    twilio.fail_origin = True
    with pytest.raises(RuntimeError, match="origination failed"):
        await p.reconcile(twilio, livekit(), apply=True, config=CONFIG)
    assert not twilio.trunks and twilio.writes[-1][0] == "DELETE"
    twilio.trunks = [{"sid": "TKexisting", "friendly_name": "lightbulb-livekit", "domain_name": p.DOMAIN}]
    count = len(twilio.writes)
    with pytest.raises(RuntimeError):
        await p.reconcile(twilio, livekit(), apply=True, config=CONFIG)
    assert twilio.trunks and all(method != "DELETE" for method, _, _ in twilio.writes[count:])


@pytest.mark.parametrize("cause", ["certification", "foreign_trunk", "wrong_dispatch"])
async def test_mismatched_ownership_or_unapproved_profile_never_mutates(cause):
    twilio, lk = Twilio(), livekit()
    if cause == "certification":
        twilio.certified = False
    elif cause == "foreign_trunk":
        twilio.trunks = [{"sid": "TKforeign", "friendly_name": "not-lightbulb", "domain_name": p.DOMAIN}]
    else:
        lk.sip.list_sip_dispatch_rule.return_value.items[0].room_config.agents[0].agent_name = "other-worker"
    with pytest.raises(p.ProviderMismatch):
        await p.reconcile(twilio, lk, apply=True, config=CONFIG)
    assert not twilio.writes


class SecretStore:
    exceptions = SimpleNamespace(ResourceNotFoundException=KeyError)

    def __init__(self, value=None):
        self.value, self.versions = value, []

    def get_secret_value(self, **_kwargs):
        if self.value is None:
            raise KeyError("missing")
        return {"SecretString": json.dumps(self.value)}

    def describe_secret(self, **_kwargs):
        return {"KmsKeyId": "test-kms-key"}

    def create_secret(self, **kwargs):
        assert self.value is None
        self.put_secret_value(**kwargs)

    def put_secret_value(self, **kwargs):
        self.value = json.loads(kwargs["SecretString"])
        self.versions.append(dict(self.value))


CREDENTIALS = {"account_sid": "ACtest", "api_key_sid": "SKtest", "api_key_secret": "test-only-api-secret"}


async def test_masked_livekit_password_is_never_used_and_saved_secret_is_reused():
    sm, twilio, lk = SecretStore(), Twilio(), livekit()
    lk.sip.list_sip_outbound_trunk.return_value.items[0].auth_password = "********"
    first = await p.provision(sm, twilio, lk, credentials=CREDENTIALS, apply=True)
    desired = sm.value["sip_auth_password"]
    assert len(desired) == 32 and desired != "********"
    assert sm.versions[0]["status"] == "pending" and sm.value["status"] == "ready"
    assert first["configSecretReady"] is True
    await p.provision(sm, twilio, lk, credentials=CREDENTIALS, apply=True)
    assert sm.value["sip_auth_password"] == desired
    password_writes = [fields["Password"] for method, url, fields in twilio.writes if "Password" in fields]
    assert password_writes == [desired, desired]
    for call in lk.sip.update_outbound_trunk_fields.await_args_list:
        assert call.args[0] == p.OUTBOUND and call.kwargs["auth_password"] == desired


async def test_partial_failure_retains_desired_password_pending_then_retry_completes():
    sm, twilio, lk = SecretStore(), Twilio(), livekit()
    lk.sip.update_outbound_trunk_fields.side_effect = [RuntimeError("LiveKit update failed"), None]
    with pytest.raises(RuntimeError, match="LiveKit update failed"):
        await p.provision(sm, twilio, lk, credentials=CREDENTIALS, apply=True)
    saved = sm.value["sip_auth_password"]
    assert sm.value["status"] == "pending" and not twilio.trunks
    dry = await p.provision(sm, twilio, lk, credentials=CREDENTIALS, apply=False)
    assert dry["configSecretReady"] is False and dry["configStatus"] == "pending"
    await p.provision(sm, twilio, lk, credentials=CREDENTIALS, apply=True)
    assert sm.value["sip_auth_password"] == saved and sm.value["status"] == "ready"


async def test_foreign_config_secret_blocks_before_any_provider_or_secret_writes():
    sm = SecretStore({"managed_by": "another-system"})
    twilio, lk = Twilio(), livekit()
    with pytest.raises(p.ProviderMismatch, match="telephony_secret_not_owned"):
        await p.provision(sm, twilio, lk, credentials=CREDENTIALS, apply=True)
    assert not twilio.writes and not sm.versions
    lk.sip.update_outbound_trunk_fields.assert_not_awaited()


@pytest.mark.parametrize("raw", ["not valid JSON", "[]"])
async def test_malformed_preexisting_secret_fails_before_writes(monkeypatch, raw):
    sm, twilio, lk = SecretStore(), Twilio(), livekit()
    monkeypatch.setattr(sm, "get_secret_value", lambda **kwargs: {"SecretString": raw})
    with pytest.raises(p.ProviderMismatch):
        await p.provision(sm, twilio, lk, credentials=CREDENTIALS, apply=True)
    assert not twilio.writes and not sm.versions


async def test_final_secret_write_failure_stays_pending_and_retry_reuses_password(monkeypatch):
    sm, twilio, lk = SecretStore(), Twilio(), livekit()
    original = sm.put_secret_value

    def interrupted(**kwargs):
        if json.loads(kwargs["SecretString"]).get("status") == "ready":
            raise RuntimeError("final secret save failed")
        return original(**kwargs)

    monkeypatch.setattr(sm, "put_secret_value", interrupted)
    with pytest.raises(RuntimeError, match="final secret save failed"):
        await p.provision(sm, twilio, lk, credentials=CREDENTIALS, apply=True)
    desired = sm.value["sip_auth_password"]
    assert sm.value["status"] == "pending" and len(twilio.trunks) == 1
    monkeypatch.setattr(sm, "put_secret_value", original)
    result = await p.provision(sm, twilio, lk, credentials=CREDENTIALS, apply=True)
    assert result["configSecretReady"] is True and sm.value["status"] == "ready"
    assert sm.value["sip_auth_password"] == desired and len(twilio.trunks) == 1


async def test_provider_failure_reports_code_but_redacts_credentials_and_request_values():
    import httpx

    twilio = p.Twilio("ACtest", "SK-private-test", "api-private-test")
    await twilio.http.aclose()

    def fail(request):
        return httpx.Response(
            400, json={"code": 21220, "message": "Invalid password password-private-test or api-private-test"}
        )

    twilio.http = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(p.TwilioFailure) as failure:
            await twilio.request("POST", "https://api.twilio.test/credentials", Password="password-private-test")
        text = str(failure.value)
        assert "21220" in text and "password-private-test" not in text and "api-private-test" not in text
    finally:
        await twilio.http.aclose()
