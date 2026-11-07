"""Owner onboarding for the voice assistant (spec 2026-11-07 §5): draft the Agent page from the business's own
website, and give the workspace a phone number.

`draft(website, business, city)`: the homepage plus up to three pages it links to that look like services, prices,
hours, team or FAQ, fetched through `link_preview.safe_get` (public http(s) only, pinned, capped), turned into the
Agent page's fields by one Bedrock call. Nothing is invented: what the site does not say comes back empty, and the
questions callers usually ask that the site does not answer come back as `toAdd`. Any failure is an empty draft, so
the owner fills the cards by hand.

`assign_number(ws, user_id)`: the line row `PHONE#<e164> / LINE -> {ownerId, ownerUserId}` the phone worker
resolves a call with (routers/voice_phone.py on feat/voice-phone-dev). Dev has exactly one number, the LiveKit one.
Prod answers `not_available` until buying a number per business (LiveKit's phone-number API) is built.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from urllib.parse import urljoin, urlsplit

import boto3
from bs4 import BeautifulSoup
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import messaging_repo
from core.services import link_preview

logger = logging.getLogger(__name__)

DEV_NUMBER = "+14842079294"  # the dev LiveKit project's one number
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


def _clean(raw: dict) -> dict:
    """Only the fields and shapes the Agent page takes; anything else is dropped."""
    hours = {}
    for d in _DAYS:
        h = (raw.get("hours") or {}).get(d)
        if isinstance(h, dict):
            hours[d] = {"open": str(h.get("open") or "09:00")[:5], "close": str(h.get("close") or "18:00")[:5],
                        "closed": bool(h.get("closed"))}  # fmt: skip
    services = [
        {
            "name": str(s.get("name"))[:80],
            "minutes": int(s.get("minutes") or 45),
            "price": str(s.get("price") or "")[:20],
            "staff": str(s.get("staff") or "")[:80],
            "minutesGuessed": bool(s.get("minutesGuessed")),
        }  # fmt: skip
        for s in (raw.get("services") or [])[:30]
        if isinstance(s, dict) and s.get("name")
    ]
    knowledge = [
        {"q": str(k["q"])[:200], "a": str(k.get("a") or "")[:600], "source": "from site"}
        for k in (raw.get("knowledge") or [])[:12]
        if isinstance(k, dict) and k.get("q")
    ]
    return {
        "hours": hours if len(hours) == 7 else {},
        "services": services,
        "staff": [str(s)[:40] for s in (raw.get("staff") or [])[:20] if s],
        "knowledge": knowledge,
        "toAdd": [str(q)[:200] for q in (raw.get("toAdd") or [])[:5] if q],
        "phone": str(raw.get("phone") or "")[:30],
    }


EMPTY = {"hours": {}, "services": [], "staff": [], "knowledge": [], "toAdd": [], "phone": ""}


async def draft(website: str, business: str, city: str) -> dict:
    """The Agent page fields the site states, plus `read`: whether the site could be read at all."""
    try:
        text = await read_site(website)
    except (link_preview.Refused, OSError, TimeoutError, ValueError) as exc:
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


class NoNumber(Exception):
    """No number can be given to this workspace now (the code is the reason)."""


async def assign_number(ws: str, user_id: str) -> str:
    """The workspace's assistant number, assigning one if it has none. Idempotent per workspace."""
    if settings.ENVIRONMENT == "prod":
        # ponytail: prod numbers are bought per business (LiveKit phone-number API, Twilio later) behind a flag
        # that is off; until it is built, prod onboarding skips the number step.
        raise NoNumber("not_available")
    number = DEV_NUMBER
    line = await messaging_repo._get(f"PHONE#{number}", "LINE")
    if line and line.get("ownerId") != ws:
        raise NoNumber("taken")  # dev's one number already answers another workspace
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
