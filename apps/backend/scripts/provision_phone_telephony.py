"""Reconcile certified Twilio -> existing LiveKit trunks. Dry-run by default; CI owns --apply.

Creates no phone numbers, calls, subscriptions or charges. Existing LiveKit-managed business line is untouched.
Sources: docs.livekit.io/telephony/start/providers/twilio/ and twilio.com/docs/sip-trunking/api/trunk-resource.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import boto3
import httpx
from livekit import api

DOMAIN = "lightbulb-livekit.pstn.twilio.com"
ORIGINATION = "sip:54mqfvh6m9w.sip.livekit.cloud;transport=tcp"
INBOUND = "ST_VbiHBUhL5Z8h"
OUTBOUND = "ST_VNjjGfGcKdUm"
DISPATCH = "SDR_D3AtyzHbPKkF"
MANAGED = "lightbulb-phone-bootstrap-v1"


class ProviderMismatch(RuntimeError):
    pass


class Twilio:
    def __init__(self, sid: str, key: str, secret: str):
        self.sid = sid
        self.http = httpx.AsyncClient(auth=(key, secret), timeout=20)

    async def request(self, method: str, url: str, **fields) -> dict:
        response = await self.http.request(method, url, **({"data": fields} if method != "GET" else {"params": fields}))
        response.raise_for_status()
        return response.json() if response.content else {}

    @property
    def account(self):
        return f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}"


async def reconcile(twilio, lk, *, apply: bool) -> dict:
    """Require exact existing project objects; never fall back to another account's trunk or route."""
    profiles = await twilio.request("GET", "https://trusthub.twilio.com/v1/CustomerProfiles", PageSize=100)
    if not any(row.get("status") == "twilio-approved" for row in profiles.get("results", [])):
        raise ProviderMismatch("approved_twilio_profile_required")
    inbound = next(
        (
            x
            for x in (await lk.sip.list_sip_inbound_trunk(api.ListSIPInboundTrunkRequest())).items
            if x.sip_trunk_id == INBOUND
        ),
        None,
    )
    outbound = next(
        (
            x
            for x in (await lk.sip.list_sip_outbound_trunk(api.ListSIPOutboundTrunkRequest())).items
            if x.sip_trunk_id == OUTBOUND
        ),
        None,
    )
    dispatch = next(
        (
            x
            for x in (await lk.sip.list_sip_dispatch_rule(api.ListSIPDispatchRuleRequest())).items
            if x.sip_dispatch_rule_id == DISPATCH
        ),
        None,
    )
    if not inbound or inbound.name != "twilio-inbound" or not inbound.allowed_addresses:
        raise ProviderMismatch("existing_inbound_trunk_mismatch")
    if not outbound or outbound.name != "twilio-outbound" or outbound.address != DOMAIN:
        raise ProviderMismatch("existing_outbound_trunk_mismatch")
    if not (outbound.auth_username and outbound.auth_password):
        raise ProviderMismatch("existing_outbound_auth_missing")
    if (
        not dispatch
        or set(dispatch.trunk_ids) != {INBOUND}
        or dispatch.name != "lightbulb-phone-twilio"
        or dispatch.rule.WhichOneof("rule") != "dispatch_rule_callee"
        or not dispatch.rule.dispatch_rule_callee.randomize
        or dispatch.rule.dispatch_rule_callee.room_prefix != "phone-prod-"
        or [x.agent_name for x in dispatch.room_config.agents] != ["lightbulb-huddle"]
    ):
        raise ProviderMismatch("existing_dispatch_rule_mismatch")

    trunks = (await twilio.request("GET", "https://trunking.twilio.com/v1/Trunks", PageSize=100)).get("trunks", [])
    matches = [x for x in trunks if x.get("domain_name") == DOMAIN]
    if len(matches) > 1 or any(x.get("friendly_name") != "lightbulb-livekit" for x in matches):
        raise ProviderMismatch("twilio_trunk_ownership_mismatch")
    lists = (await twilio.request("GET", f"{twilio.account}/SIP/CredentialLists.json", PageSize=100)).get(
        "credential_lists", []
    )
    credentials = [x for x in lists if x.get("friendly_name") == "lightbulb-livekit-termination"]
    if len(credentials) != 1:
        raise ProviderMismatch("existing_credential_list_required")
    credential_list = credentials[0]["sid"]
    users = (
        await twilio.request(
            "GET", f"{twilio.account}/SIP/CredentialLists/{credential_list}/Credentials.json", PageSize=100
        )
    ).get("credentials", [])
    users = [x for x in users if x.get("username") == outbound.auth_username]
    if len(users) != 1:
        raise ProviderMismatch("existing_sip_credential_required")

    report = {
        "apply": apply,
        "certification": "twilio-approved",
        "existingInboundId": INBOUND,
        "existingOutboundId": OUTBOUND,
        "existingDispatchId": DISPATCH,
        "createTwilioTrunk": not matches,
        "numberPurchases": 0,
        "calls": 0,
    }
    if not apply:
        return report
    created = False
    trunk = (
        matches[0]
        if matches
        else await twilio.request(
            "POST", "https://trunking.twilio.com/v1/Trunks", FriendlyName="lightbulb-livekit", DomainName=DOMAIN
        )
    )
    created = not matches
    trunk_url = f"https://trunking.twilio.com/v1/Trunks/{trunk['sid']}"
    try:
        # Reconcile to the existing LiveKit password in memory; neither credential is ever printed or committed.
        await twilio.request(
            "POST",
            f"{twilio.account}/SIP/CredentialLists/{credential_list}/Credentials/{users[0]['sid']}.json",
            Password=outbound.auth_password,
        )
        mapped = (await twilio.request("GET", f"{trunk_url}/CredentialLists")).get("credential_lists", [])
        if not any(x.get("sid") == credential_list for x in mapped):
            await twilio.request("POST", f"{trunk_url}/CredentialLists", CredentialListSid=credential_list)
        urls = (await twilio.request("GET", f"{trunk_url}/OriginationUrls")).get("origination_urls", [])
        correct = [x for x in urls if x.get("sip_url") == ORIGINATION]
        if any(x.get("sip_url") != ORIGINATION for x in urls):
            raise ProviderMismatch("unexpected_existing_origination_route")
        if not correct:
            await twilio.request(
                "POST",
                f"{trunk_url}/OriginationUrls",
                FriendlyName="livekit",
                SipUrl=ORIGINATION,
                Weight="1",
                Priority="1",
                Enabled="true",
            )
        elif not all(x.get("enabled") for x in correct):
            raise ProviderMismatch("existing_origination_disabled")
    except Exception:
        if created:
            # No number is ever attached here; remove only the empty trunk this execution just created.
            await twilio.request("DELETE", trunk_url)
        raise
    report["twilioTrunkId"] = trunk["sid"]
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sm = boto3.client("secretsmanager", region_name="us-east-1")

    def secret(name):
        return sm.get_secret_value(SecretId=name)["SecretString"].strip()

    # These are the already-certified single platform Twilio account's existing credential names.
    account = secret("lightbulb/dev/twilio_account_sid")
    key = secret("lightbulb/dev/twilio_api_key_sid")
    password = secret("lightbulb/dev/twilio_api_key_secret")
    twilio = Twilio(account, key, password)
    lk = api.LiveKitAPI(
        "https://lightbulb-svx3a1rx.livekit.cloud",
        secret("lightbulb/prod/livekit_api_key"),
        secret("lightbulb/prod/livekit_api_secret"),
    )
    try:
        report = await reconcile(twilio, lk, apply=args.apply)
        if args.apply:
            value = {
                "managed_by": MANAGED,
                "account_sid": account,
                "api_key_sid": key,
                "api_key_secret": password,
                "twilio_trunk_sid": report["twilioTrunkId"],
                "inbound_trunk_id": INBOUND,
                "outbound_trunk_id": OUTBOUND,
            }
            name = "lightbulb/prod/voice_telephony"
            try:
                previous = json.loads(secret(name))
            except sm.exceptions.ResourceNotFoundException:
                kms = sm.describe_secret(SecretId="lightbulb/prod/livekit_api_secret").get("KmsKeyId")
                sm.create_secret(Name=name, SecretString=json.dumps(value), **({"KmsKeyId": kms} if kms else {}))
            else:
                if previous.get("managed_by") != MANAGED:
                    raise ProviderMismatch("telephony_secret_not_owned")
                if previous != value:
                    sm.put_secret_value(SecretId=name, SecretString=json.dumps(value))
            report["configSecretReady"] = True
        print(json.dumps(report, sort_keys=True))
    finally:
        await twilio.http.aclose()
        await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
