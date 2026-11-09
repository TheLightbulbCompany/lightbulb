"""Provider bootstrap never buys phones and only mutates exact existing owned routing objects."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from livekit import api

from scripts import provision_phone_telephony as p


class Twilio:
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
            return {"credentials": [{"sid": "CRtest", "username": "livekit-user"}]}
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
        auth_username="livekit-user",
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
        )
    )


async def test_dry_run_certifies_existing_route_without_writes():
    twilio = Twilio()
    result = await p.reconcile(twilio, livekit(), apply=False)
    assert result["createTwilioTrunk"] and not twilio.writes
    assert result["numberPurchases"] == result["calls"] == 0


async def test_apply_reuses_resources_on_retry_and_never_purchases_numbers():
    twilio = Twilio()
    first = await p.reconcile(twilio, livekit(), apply=True)
    again = await p.reconcile(twilio, livekit(), apply=True)
    assert first["twilioTrunkId"] == again["twilioTrunkId"] == "TKtest"
    assert len(twilio.trunks) == len(twilio.urls) == len(twilio.mappings) == 1
    assert all("IncomingPhoneNumbers" not in url and "/Calls" not in url for _, url, _ in twilio.writes)


async def test_failed_new_trunk_is_compensated_without_removing_existing_trunk():
    twilio = Twilio()
    twilio.fail_origin = True
    with pytest.raises(RuntimeError, match="origination failed"):
        await p.reconcile(twilio, livekit(), apply=True)
    assert not twilio.trunks and twilio.writes[-1][0] == "DELETE"
    twilio.trunks = [{"sid": "TKexisting", "friendly_name": "lightbulb-livekit", "domain_name": p.DOMAIN}]
    count = len(twilio.writes)
    with pytest.raises(RuntimeError):
        await p.reconcile(twilio, livekit(), apply=True)
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
        await p.reconcile(twilio, lk, apply=True)
    assert not twilio.writes
