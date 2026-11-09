"""Reconcile certified Twilio -> existing LiveKit trunks. Dry-run by default; CI owns --apply.

Creates no phone numbers, calls, subscriptions or charges. Existing LiveKit-managed business line is untouched.
Sources: docs.livekit.io/telephony/start/providers/twilio/ and twilio.com/docs/sip-trunking/api/trunk-resource.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets

import boto3
import httpx
from livekit import api

DOMAIN = "lightbulb-livekit.pstn.twilio.com"
ORIGINATION = "sip:54mqfvh6m9w.sip.livekit.cloud;transport=tcp"
INBOUND = "ST_VbiHBUhL5Z8h"
OUTBOUND = "ST_VNjjGfGcKdUm"
DISPATCH = "SDR_D3AtyzHbPKkF"
MANAGED = "lightbulb-phone-bootstrap-v1"
CONFIG_SECRET = "lightbulb/prod/voice_telephony"


class ProviderMismatch(RuntimeError):
    pass


class TwilioFailure(RuntimeError):
    """Sanitized provider diagnostics; never print request credentials or parameter values."""


class Twilio:
    def __init__(self, sid: str, key: str, secret: str):
        self.sid = sid
        self._sensitive = (sid, key, secret)
        self.http = httpx.AsyncClient(auth=(key, secret), timeout=20)

    async def request(self, method: str, url: str, **fields) -> dict:
        response = await self.http.request(method, url, **({"data": fields} if method != "GET" else {"params": fields}))
        if response.is_error:
            try:
                body = response.json()
            except ValueError:
                body = {}
            message = str(body.get("message") or "provider request failed")
            for value in (*self._sensitive, *(str(x) for x in fields.values())):
                if value:
                    message = message.replace(value, "[redacted]")
            code = body.get("code")
            code = str(code) if isinstance(code, int) or str(code).isdigit() else "unknown"
            raise TwilioFailure(f"Twilio HTTP {response.status_code}, code {code}: {message[:300]}")
        return response.json() if response.content else {}

    @property
    def account(self):
        return f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}"


async def reconcile(twilio, lk, *, apply: bool, config: dict | None = None) -> dict:
    """Require exact existing project objects; never fall back to another account's trunk or route."""
    if apply:
        if not config or config.get("managed_by") != MANAGED or config.get("account_sid") != twilio.sid:
            raise ProviderMismatch("owned_persisted_configuration_required")
        desired_password = config.get("sip_auth_password") or ""
        if not (
            len(desired_password) >= 32
            and any(c.isupper() for c in desired_password)
            and any(c.islower() for c in desired_password)
            and any(c.isdigit() for c in desired_password)
        ):
            raise ProviderMismatch("strong_persisted_sip_password_required")
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
    if not outbound.auth_username:
        raise ProviderMismatch("existing_outbound_auth_missing")
    if apply and config.get("sip_auth_username") != outbound.auth_username:
        raise ProviderMismatch("existing_outbound_username_mismatch")
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
        # ListSIPOutboundTrunk masks its password. Only the persisted desired secret is authoritative.
        await twilio.request(
            "POST",
            f"{twilio.account}/SIP/CredentialLists/{credential_list}/Credentials/{users[0]['sid']}.json",
            Password=config["sip_auth_password"],
        )
        await lk.sip.update_outbound_trunk_fields(
            OUTBOUND, auth_username=config["sip_auth_username"], auth_password=config["sip_auth_password"]
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


def read_configuration(sm, account: str) -> dict | None:
    try:
        raw = sm.get_secret_value(SecretId=CONFIG_SECRET)["SecretString"]
    except sm.exceptions.ResourceNotFoundException:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise ProviderMismatch("telephony_secret_invalid_json") from None
    if not isinstance(value, dict):
        raise ProviderMismatch("telephony_secret_not_owned")
    if (
        value.get("managed_by") != MANAGED
        or value.get("account_sid") != account
        or value.get("inbound_trunk_id") != INBOUND
        or value.get("outbound_trunk_id") != OUTBOUND
    ):
        raise ProviderMismatch("telephony_secret_not_owned")
    return value


def write_configuration(sm, value: dict, *, create: bool = False) -> None:
    if create:
        kms = sm.describe_secret(SecretId="lightbulb/prod/livekit_api_secret").get("KmsKeyId")
        sm.create_secret(Name=CONFIG_SECRET, SecretString=json.dumps(value), **({"KmsKeyId": kms} if kms else {}))
    else:
        sm.put_secret_value(SecretId=CONFIG_SECRET, SecretString=json.dumps(value))


async def provision(sm, twilio, lk, *, credentials: dict, apply: bool) -> dict:
    previous = read_configuration(sm, credentials["account_sid"])  # BEFORE any provider writes
    value = {
        **(previous or {}),
        **credentials,
        "managed_by": MANAGED,
        "inbound_trunk_id": INBOUND,
        "outbound_trunk_id": OUTBOUND,
    }
    if apply:
        # Persist once before either provider changes. Partial failures keep this same desired password for retry.
        if not value.get("sip_auth_password"):
            value["sip_auth_password"] = "Lv1!" + secrets.token_urlsafe(21)
        value.setdefault("sip_auth_username", "lightbulb-livekit")
        value["status"] = "pending"
        write_configuration(sm, value, create=previous is None)
    report = await reconcile(twilio, lk, apply=apply, config=value)
    if apply:
        value["twilio_trunk_sid"] = report["twilioTrunkId"]
        if not all(
            value.get(name)
            for name in (
                "account_sid",
                "api_key_sid",
                "api_key_secret",
                "twilio_trunk_sid",
                "inbound_trunk_id",
                "outbound_trunk_id",
            )
        ):
            raise ProviderMismatch("incomplete_phone_configuration")
        value["status"] = "ready"
        write_configuration(sm, value)
        report["configSecretReady"] = True
    else:
        report["configSecretReady"] = False  # a read-only plan never certifies successful reconciliation
    report["configStatus"] = value.get("status", "not_created")
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
        report = await provision(
            sm,
            twilio,
            lk,
            credentials={"account_sid": account, "api_key_sid": key, "api_key_secret": password},
            apply=args.apply,
        )
        print(json.dumps(report, sort_keys=True))
    finally:
        await twilio.http.aclose()
        await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
