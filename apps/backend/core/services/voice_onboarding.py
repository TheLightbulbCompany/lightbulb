"""Owner onboarding for the voice assistant (spec 2026-11-07 §5): draft the Agent page from the business's own
website, and give the workspace a phone number.

`draft(website, business, city)`: the homepage plus up to three pages it links to that look like services, prices,
hours, team or FAQ, fetched through `link_preview.safe_get` (public http(s) only, pinned, capped), turned into the
Agent page's fields by one Bedrock call. Nothing is invented: what the site does not say comes back empty, and the
questions callers usually ask that the site does not answer come back as `toAdd`. Any failure is an empty draft, so
the owner fills the cards by hand.

`assign_number(ws, user_id)`: the line row `PHONE#<e164> / LINE -> {ownerId, ownerUserId}` the phone worker
resolves a call with (routers/voice_phone.py). The number is `settings.VOICE_LINE_NUMBER` (per environment; empty =
`not_available`).

Numbers at onboarding: `reserve` stores the owner's choice (keep their number and forward it, or a new one); nothing
is bought until Go live, when `provision_at_go_live` gives the number (bought from Twilio once VOICE_BUY_NUMBERS is
on). `release_number` gives it back.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import re
from typing import Literal
from urllib.parse import urljoin, urlsplit

import boto3
import httpx
from bs4 import BeautifulSoup
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from core.config import settings
from core.billing import voice_billing
from core.dynamodb import run_in_thread
from core.repositories import billing_repo, container_repo, messaging_repo
from core.services import link_preview, livekit_huddles
from livekit import api

logger = logging.getLogger(__name__)

PAGE_CHARS = 12_000
MAX_PAGES = 4
_USEFUL = re.compile(r"service|menu|price|pricing|hour|about|team|staff|stylist|faq|contact|book", re.I)
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

PROMPT = """You set up a phone receptionist for a small business from its own website text.
Return ONLY one JSON object, no prose:
{"hours": {"Mon": {"open": "09:00", "close": "18:00", "closed": false}, ...all 7 days Mon..Sun, or {} if the site does not say},
 "services": [{"name": "...", "minutes": 45, "minutesGuessed": true, "price": "$55", "staff": ""}],
 "staff": ["first names"],
 "knowledge": [{"q": "a question a caller would ask", "a": "the answer as the site states it"}],
 "toAdd": ["common caller questions the site does NOT answer, e.g. Parking? Walk-ins? Gift cards?"],
 "phone": "the business phone number the site lists, or empty"}
Rules: only facts the text states. Durations the site does not give: your best guess with minutesGuessed true.
Prices exactly as written. At most 30 services, 12 knowledge items, 5 toAdd. 24-hour HH:MM times."""


@functools.cache
def _bedrock():
    cfg = Config(connect_timeout=3, read_timeout=45, retries={"max_attempts": 1})
    return boto3.client("bedrock-runtime", region_name=settings.AWS_REGION, config=cfg)


def _site_url(raw: str) -> str:
    raw = raw.strip()
    return raw if re.match(r"^https?://", raw, re.I) else f"https://{raw}"


def _page(html: str) -> tuple[str, list[tuple[str, str]]]:
    soup = BeautifulSoup(html, "html.parser")
    links = [(a.get("href") or "", a.get_text(" ", strip=True)) for a in soup.find_all("a")]
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())[:PAGE_CHARS], links


async def _fetch(url: str) -> tuple[str, str, list[tuple[str, str]]]:
    final, body, ctype = await link_preview.safe_get(url, accept="text/html")
    if "html" not in ctype and "text" not in ctype:
        raise link_preview.Refused("not a page")
    text, links = _page(link_preview._decode(body, ctype))
    return final, text, links


async def read_site(website: str) -> str:
    """The homepage and up to MAX_PAGES - 1 same-site pages that look useful, as plain text."""
    home, text, links = await _fetch(_site_url(website))
    host = urlsplit(home).hostname
    more: list[str] = []
    for href, label in links:
        url = urljoin(home, href).split("#")[0]
        if urlsplit(url).hostname == host and url != home and url not in more and _USEFUL.search(f"{href} {label}"):
            more.append(url)
    pages = [f"[{home}]\n{text}"]
    for got in await asyncio.gather(*(_fetch(u) for u in more[: MAX_PAGES - 1]), return_exceptions=True):
        if not isinstance(got, BaseException):
            pages.append(f"[{got[0]}]\n{got[1]}")
    return "\n\n".join(pages)


def _list(v, n: int) -> list:
    """A model field as a list: a bare string is one item, anything else not a list is none."""
    return [v] if isinstance(v, str) else (v[:n] if isinstance(v, list) else [])


def _service(s) -> dict | None:
    if not isinstance(s, dict) or not s.get("name"):
        return None
    try:
        minutes = max(5, min(int(s.get("minutes") or 45), 600))
    except (TypeError, ValueError):
        minutes, s = 45, {**s, "minutesGuessed": True}
    return {"name": str(s["name"])[:80], "minutes": minutes, "price": str(s.get("price") or "")[:20],
            "staff": str(s.get("staff") or "")[:80], "minutesGuessed": bool(s.get("minutesGuessed"))}  # fmt: skip


def _clean(raw) -> dict:
    """Only the fields and shapes the Agent page takes. A bad field or item is skipped, never the whole draft."""
    raw = raw if isinstance(raw, dict) else {}
    got = raw.get("hours") if isinstance(raw.get("hours"), dict) else {}
    hours = {
        d: {
            "open": str(h.get("open") or "09:00")[:5],
            "close": str(h.get("close") or "18:00")[:5],
            "closed": bool(h.get("closed")),
        }  # fmt: skip
        for d in _DAYS
        if isinstance(h := got.get(d), dict)
    }
    knowledge = [
        {"q": str(k["q"])[:200], "a": str(k.get("a") or "")[:600], "source": "from site"}
        for k in _list(raw.get("knowledge"), 12)
        if isinstance(k, dict) and k.get("q")
    ]
    return {
        "hours": hours if len(hours) == 7 else {},
        "services": [x for s in _list(raw.get("services"), 30) if (x := _service(s))],
        "staff": [str(s)[:40] for s in _list(raw.get("staff"), 20) if s and isinstance(s, (str, int))],
        "knowledge": knowledge,
        "toAdd": [str(q)[:200] for q in _list(raw.get("toAdd"), 5) if q and isinstance(q, str)],
        "phone": str(raw.get("phone") or "")[:30] if isinstance(raw.get("phone"), (str, int)) else "",
    }


EMPTY = {"hours": {}, "services": [], "staff": [], "knowledge": [], "toAdd": [], "phone": ""}


async def draft(website: str, business: str, city: str) -> dict:
    """The Agent page fields the site states, plus `read`: whether the site could be read at all."""
    try:
        text = await read_site(website)
    except (link_preview.Refused, httpx.HTTPError, OSError, TimeoutError, ValueError) as exc:
        logger.info("onboarding site read failed: %s", type(exc).__name__)
        return {**EMPTY, "read": False}
    try:
        resp = await run_in_thread(
            _bedrock().converse,
            modelId=settings.MEMORY_EXPAND_MODEL,
            system=[{"text": PROMPT}],
            messages=[{"role": "user", "content": [{"text": f"Business: {business}, {city}\n\n{text}"}]}],
            inferenceConfig={"maxTokens": 3000, "temperature": 0},
        )
        out = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        return {**_clean(json.loads(out[out.index("{") : out.rindex("}") + 1])), "read": True}
    except (ClientError, BotoCoreError, KeyError, ValueError, TypeError) as exc:
        logger.warning("onboarding draft failed: %s", type(exc).__name__)
        return {**EMPTY, "read": True}


TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts"


async def _twilio(method: str, path: str, **params: str) -> dict:
    """One Twilio REST call under the account (GET params as the query, POST as the form); {} for a 204."""
    url = f"{TWILIO_API}/{settings.TWILIO_ACCOUNT_SID}/{path}"
    credentials = (
        (settings.TWILIO_API_KEY_SID, settings.TWILIO_API_KEY_SECRET)
        if settings.TWILIO_API_KEY_SID and settings.TWILIO_API_KEY_SECRET
        else (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    )
    async with httpx.AsyncClient(auth=credentials, timeout=10) as client:
        kw = {"params": params} if method == "GET" else {"data": params}
        resp = await client.request(method, url, **kw)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


async def release_number(ws: str) -> str:
    """Delete the workspace's `PHONE#` line row, so its number answers nobody, and give a bought number back to
    Twilio. The number released, or "" when the workspace had none. Wipes call it before the owner partition goes
    (the Agent page's `number` is how a bought number is found).
    ponytail: the 30-day paused/unused release has no caller yet: nothing is bought while VOICE_BUY_NUMBERS is off.
    Call it from the watchdog's daily tick (core/temporal/watchdog_workflow.py) once buying is on."""
    cfg = await messaging_repo.get_voice_agent(ws) or {}
    provision_key = {"pk": messaging_repo.owner_pk(ws), "sk": "VOICENUMBER"}
    pending = await messaging_repo._get(**provision_key) or {}
    number = str(cfg.get("number") or pending.get("number") or "") or settings.VOICE_LINE_NUMBER
    line = await messaging_repo._get(f"PHONE#{number}", "LINE") if number else None
    if not line and pending.get("number") == number and pending.get("sid"):
        line = {"ownerId": ws, "sid": pending["sid"]}  # purchase succeeded before config/line save failed
    if not line or line.get("ownerId") != ws:
        return ""  # no line, or another workspace's
    if line.get("sid"):  # bought by provision_at_go_live: back to Twilio first, so a retried wipe still finds it
        try:
            await _twilio("DELETE", f"IncomingPhoneNumbers/{line['sid']}.json")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:  # 404: already released
                raise
        if settings.VOICE_INBOUND_TRUNK_ID:
            await _route_number(number, remove=True)
    await run_in_thread(messaging_repo._table().delete_item, Key={"pk": f"PHONE#{number}", "sk": "LINE"})
    await run_in_thread(messaging_repo._table().delete_item, Key=provision_key)
    return number


class NoNumber(Exception):
    """No number can be given to this workspace now (the code is the reason)."""


async def assign_number(ws: str, user_id: str) -> str:
    """The workspace's assistant number, assigning one if it has none. Idempotent per workspace."""
    number = settings.VOICE_LINE_NUMBER
    if not number:
        raise NoNumber("not_available")  # this environment has no line
    line = await messaging_repo._get(f"PHONE#{number}", "LINE")
    if line and line.get("ownerId") != ws:
        raise NoNumber("taken")  # the line already answers another workspace
    item = {
        "pk": f"PHONE#{number}",
        "sk": "LINE",
        "ownerId": ws,
        "ownerUserId": (line or {}).get("ownerUserId") or user_id,
    }
    try:
        await run_in_thread(
            messaging_repo._table().put_item,
            Item=item,
            ConditionExpression="attribute_not_exists(pk) OR ownerId = :ws",
            ExpressionAttributeValues={":ws": ws},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise NoNumber("taken") from None
        raise
    return number


async def reserve(ws: str, path: Literal["keep", "new"], area_code: str | None, carrier: str | None,
                  mode: Literal["missed", "all"] | None, *, user_id: str) -> dict:  # fmt: skip
    """Onboarding step 4: keep the business's number and forward it, or get a new one. Stored on the Agent page as
    `numberChoice` (`number` stays the E.164 string the page shows). Nothing is bought here."""
    choice = {"path": path, "areaCode": area_code, "carrier": carrier, "mode": mode}
    cfg = await messaging_repo.get_voice_agent(ws) or {}
    await messaging_repo.put_voice_agent(ws, {**cfg, "numberChoice": choice}, user_id)
    return choice


def _area_code(raw) -> str | None:
    """The area code of a US number as the page stores it ("(484) 555-0100", "+14845550100")."""
    digits = re.sub(r"\D", "", raw) if isinstance(raw, str) else ""
    digits = digits[1:] if len(digits) == 11 and digits[0] == "1" else digits
    return digits[:3] if len(digits) == 10 else None


async def _route_number(number: str, *, remove: bool = False) -> None:
    """Atomically add/remove without replacing the shared trunk list or its IP restrictions."""
    async with livekit_huddles._api() as lk:
        await lk.sip.update_sip_inbound_trunk(
            api.UpdateSIPInboundTrunkRequest(
                sip_trunk_id=settings.VOICE_INBOUND_TRUNK_ID,
                update=api.SIPInboundTrunkUpdate(numbers=api.ListUpdate(**{"remove" if remove else "add": [number]})),
            )
        )


async def provision_at_go_live(ws: str, cfg: dict, user_id: str) -> str:
    """Serialize paid provisioning; recover provider outcomes before buying another number on retry."""
    if not settings.VOICE_BUY_NUMBERS:
        have = cfg.get("number")
        if have and ((await messaging_repo._get(f"PHONE#{have}", "LINE")) or {}).get("ownerId") == ws:
            return have
        return await assign_number(ws, user_id)
    lease = f"voice-number#{ws}"
    token = await container_repo.try_claim_owner_lifecycle(lease, stale_s=180)
    if not token:
        raise NoNumber("provisioning_in_progress")
    try:
        return await _provision_number(ws, cfg, user_id)
    finally:
        await container_repo.release_owner_lifecycle_claim(lease, token=token)


async def _provision_number(ws: str, cfg: dict, user_id: str) -> str:
    have = cfg.get("number")
    if (
        isinstance(have, str)
        and have
        and ((await messaging_repo._get(f"PHONE#{have}", "LINE")) or {}).get("ownerId") == ws
    ):
        return have
    key = {"pk": messaging_repo.owner_pk(ws), "sk": "VOICENUMBER"}
    pending = await messaging_repo._get(**key) or {}
    if (
        pending.get("number")
        and ((await messaging_repo._get(f"PHONE#{pending['number']}", "LINE")) or {}).get("ownerId") == ws
    ):
        return pending["number"]  # an earlier config-save failure must not purchase or gate a second number
    paid = voice_billing.has_voice_plan(await billing_repo.get_by_owner_id(ws, consistent_read=True))
    if not paid and not pending:
        raise NoNumber("voice_payment_required")
    if not (
        settings.TWILIO_ACCOUNT_SID
        and (settings.TWILIO_AUTH_TOKEN or (settings.TWILIO_API_KEY_SID and settings.TWILIO_API_KEY_SECRET))
        and settings.TWILIO_TRUNK_SID
        and settings.VOICE_INBOUND_TRUNK_ID
    ):
        raise NoNumber("not_available")
    tag = f"lightbulb-{settings.ENVIRONMENT}-{hashlib.sha256(ws.encode()).hexdigest()[:24]}"
    query = {"FriendlyName": tag, **({"PhoneNumber": pending["number"]} if pending.get("number") else {})}
    owned = await _twilio("GET", "IncomingPhoneNumbers.json", **query)
    matches = owned.get("incoming_phone_numbers") or []
    if len(matches) > 1 or any(x.get("friendly_name") != tag for x in matches):
        raise NoNumber("provider_ownership_mismatch")
    bought_now = False
    if matches:
        bought = matches[0]
        number = bought["phone_number"]
        if pending.get("number") and pending["number"] != number:
            raise NoNumber("provider_ownership_mismatch")
        if bought.get("trunk_sid") != settings.TWILIO_TRUNK_SID:
            raise NoNumber("provider_route_mismatch")
    else:
        if not paid:
            raise NoNumber("voice_payment_required")  # recovery of an existing purchase is allowed; a new charge is not
        number = pending.get("number")
        if not number:
            choice = cfg.get("numberChoice") if isinstance(cfg.get("numberChoice"), dict) else {}
            area = choice.get("areaCode") if choice.get("path") == "new" else _area_code(cfg.get("forwardFrom"))
            found = await _twilio(
                "GET",
                "AvailablePhoneNumbers/US/Local.json",
                **({"AreaCode": area} if area else {}),
                VoiceEnabled="true",
                SmsEnabled="true",
            )
            if not found.get("available_phone_numbers"):
                raise NoNumber("none_in_area_code")
            number = found["available_phone_numbers"][0]["phone_number"]
            await run_in_thread(
                messaging_repo._table().put_item,
                Item={**key, "number": number, "friendlyName": tag, "ownerUserId": user_id},
            )
        try:
            bought = await _twilio(
                "POST",
                "IncomingPhoneNumbers.json",
                PhoneNumber=number,
                TrunkSid=settings.TWILIO_TRUNK_SID,
                FriendlyName=tag,
            )
            bought_now = True
        except httpx.HTTPError as exc:
            # Twilio documents 21422 as a confirmed unavailable/reserved number. Reconcile once more
            # before discarding only an unpurchased choice; ambiguous timeouts/5xx keep their receipt.
            code = None
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400:
                try:
                    code = exc.response.json().get("code")
                except ValueError:
                    pass
            if code == 21422:
                recovered = await _twilio("GET", "IncomingPhoneNumbers.json", FriendlyName=tag, PhoneNumber=number)
                rows = recovered.get("incoming_phone_numbers") or []
                if (
                    len(rows) == 1
                    and rows[0].get("friendly_name") == tag
                    and rows[0].get("trunk_sid") == settings.TWILIO_TRUNK_SID
                ):
                    bought = rows[0]
                else:
                    await run_in_thread(
                        messaging_repo._table().delete_item,
                        Key=key,
                        ConditionExpression="attribute_not_exists(sid) AND #n = :number",
                        ExpressionAttributeNames={"#n": "number"},
                        ExpressionAttributeValues={":number": number},
                    )
                    raise NoNumber("none_in_area_code") from None
            else:
                # Keep the chosen number on an uncertain response: retry reconciles this exact resource.
                logger.warning("number buy failed: %s", type(exc).__name__)
                raise NoNumber("buy_failed") from None
    if bought["phone_number"] != number:
        raise NoNumber("provider_ownership_mismatch")
    await run_in_thread(
        messaging_repo._table().put_item,
        Item={
            **key,
            "number": number,
            "sid": bought["sid"],
            "friendlyName": tag,
            "ownerUserId": pending.get("ownerUserId") or user_id,
        },
    )
    try:
        await _route_number(number)
    except Exception as exc:
        if bought_now:
            try:  # Compensate unroutable purchases; retain receipt if release fails for later recovery.
                await _twilio("DELETE", f"IncomingPhoneNumbers/{bought['sid']}.json")
                await run_in_thread(messaging_repo._table().delete_item, Key=key)
            except Exception:
                logger.exception("unroutable number release failed")
        raise NoNumber("route_failed") from exc
    item = {
        "pk": f"PHONE#{number}",
        "sk": "LINE",
        "ownerId": ws,
        "ownerUserId": pending.get("ownerUserId") or user_id,
        "sid": bought["sid"],
    }
    await run_in_thread(
        messaging_repo._table().put_item,
        Item=item,
        ConditionExpression="attribute_not_exists(pk) OR ownerId = :ws",
        ExpressionAttributeValues={":ws": ws},
    )
    return number
