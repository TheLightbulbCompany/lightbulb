"""``wake_me``: a bot (a Grok routine, a Muse agent) on the hosted MCP asks to be woken when its
app session gets network mail, instead of polling ``check_inbox``.

The bot gives only its webhook URL (``request``); the person pastes the webhook's key on a
signed-in Lightbulb page (``set_key``), never in a chat. On new mail for that app session (its
pinned computer's link sends a ``mail`` notice) the backend makes ONE content-free POST (``post``: Bearer key, public
addresses only, a bounded retry), at most once per ``WINDOW_S`` per app.

Rows (messaging table): ``APPWAKE#<user>`` / ``<app slug>`` -- the app, the URL's host, its
state (``needs_key`` | ``ready``). URL and key are one sealed
``integration_secret_store`` credential (provider ``agent-webhooks/wake-<app>``), never returned or logged.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo as repo
from core.services import link_preview
from core.services.credentials import integration_secret_store
from core.services.messaging import ApiProblem

logger = logging.getLogger(__name__)

WINDOW_S = 15
PAGE = "/auth/network/apps"
TIMEOUT_S = 5.0
RETRY_DELAYS_S = (0.5, 1.5)  # three attempts in all

# Tests swap this for an httpx.MockTransport.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _provider(agent_id: str) -> str:
    """``integration_secret_store`` provider; the legacy secret was
    ``lightbulb/<env>/integrations/<owner>/agent-webhooks/<agent>``, the same name the store falls back to."""
    return f"agent-webhooks/{agent_id}"


def check_url(url: str) -> str:
    """https to a default port, no credentials. The host is resolved (public only) at every POST."""
    try:
        scheme, _, _ = link_preview._check_url(url)
    except link_preview.Refused as exc:
        raise ApiProblem("INVALID_REQUEST", f"Webhook URL refused: {exc}.") from exc
    if scheme != "https" or len(url) > 2048:
        raise ApiProblem("INVALID_REQUEST", "The webhook URL must be https.")
    return url


async def _credential(owner_id: str, agent_id: str) -> dict:
    cred = await integration_secret_store.get(owner_id, _provider(agent_id))
    if cred is None:
        raise LookupError("no webhook credential")
    return cred


async def post(owner_id: str, agent_id: str, payload: dict) -> tuple[bool, str]:
    """ONE webhook call with a bounded retry: (delivered, why not). The URL's host must resolve to
    public addresses only, and the connection goes to the address checked (link_preview's guard)."""
    try:
        cred = await _credential(owner_id, agent_id)
    except Exception:  # noqa: BLE001 -- a missing secret is a failed delivery, not a crash
        logger.warning("app wake credential unavailable app=%s", agent_id)
        return False, "credential unavailable"
    reason = "unreachable"
    async with httpx.AsyncClient(
        transport=_TRANSPORT, timeout=TIMEOUT_S, follow_redirects=False, trust_env=False
    ) as client:
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            try:
                check_url(cred["url"])
                request = await link_preview.pinned_request(
                    client,
                    "POST",
                    cred["url"],
                    headers={"Authorization": f"Bearer {cred['key']}", "User-Agent": "Lightbulb-Webhook/1.0"},
                    json=payload,
                )
                response = await client.send(request)
            except (ApiProblem, link_preview.Refused):
                return False, "webhook URL refused"
            except httpx.HTTPError as exc:
                reason = type(exc).__name__
            else:
                if response.is_success:
                    return True, ""
                reason = f"HTTP {response.status_code}"
                if response.status_code != 429 and response.status_code < 500:
                    return False, reason  # the webhook said no: retrying will not change that
            if attempt < len(RETRY_DELAYS_S):
                await asyncio.sleep(RETRY_DELAYS_S[attempt])
    logger.info("app wake delivery failed app=%s reason=%s", agent_id, reason)
    return False, reason


def _key(user_id: str, app: str) -> dict:
    return {"pk": f"APPWAKE#{user_id}", "sk": app}


def _secret_id(app: str) -> str:
    return f"wake-{app}"


def _public(row: dict) -> dict:
    return {k: row.get(k) for k in ("app", "client", "host", "state", "createdAt")}


async def request(user_id: str, app: str, client: str, url: str) -> dict:
    """Record (or replace) this app's webhook; it needs its key entered before any wake."""
    from urllib.parse import urlsplit

    check_url(url)
    await integration_secret_store.put(user_id, _provider(_secret_id(app)), {"url": url, "key": ""})
    row = {
        **_key(user_id, app),
        "app": app,
        "client": client,
        "host": urlsplit(url).hostname,
        "state": "needs_key",
        "createdAt": utc_now_iso(),
    }
    await run_in_thread(repo._table().put_item, Item=row)
    logger.info("app wake requested user=%s app=%s", user_id, app)
    page = f"{settings.FRONTEND_URL.rstrip('/')}{PAGE}"
    return {
        "state": "needs_key",
        "page": page,
        "text": (
            f"Ask your person to open {page} and paste this webhook's key there (never in a chat). "
            "Until then, call check_inbox to read your mail."
        ),
    }


async def list_wakes(user_id: str) -> list[dict]:
    return [_public(r) for r in await repo._query(f"APPWAKE#{user_id}")]


async def set_key(user_id: str, app: str, key: str) -> dict:
    if not key.strip() or len(key) > 4096:
        raise ApiProblem("INVALID_REQUEST", "Paste the webhook's key.")
    row = await repo._get(**_key(user_id, app))
    if row is None:
        raise ApiProblem("MEMBER_NOT_FOUND", "No wake-up request from that app.")
    cred = await _credential(user_id, _secret_id(app))
    await integration_secret_store.put(user_id, _provider(_secret_id(app)), {"url": cred["url"], "key": key.strip()})
    await run_in_thread(
        repo._table().update_item,
        Key=_key(user_id, app),
        UpdateExpression="SET #s = :ready",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":ready": "ready"},
    )
    return _public({**row, "state": "ready"})


async def remove(user_id: str, app: str) -> None:
    await integration_secret_store.delete(user_id, _provider(_secret_id(app)))
    await run_in_thread(repo._table().delete_item, Key=_key(user_id, app))


def payload(app: str, session: str) -> dict:
    """Content-free: the bot reads its mail itself (``check_inbox``), so no sender text is in
    the POST and there is nothing to defang."""
    from core.services.hosted_mcp import RESOURCE

    text = (
        f"You have new messages on the Lightbulb network. Call check_inbox on the Lightbulb MCP "
        f"({RESOURCE}) to read them, and send_message with reply_to to answer."
    )
    return {"type": "lightbulb.wake", "app": app, "session": session, "text": text, "prompt": text}


async def notify(user_id: str, session: str) -> bool:
    """New mail for app session ``session`` (``<app>-<person>``) of ``user_id``: one wake, if
    the app asked for it and its key is in. Never raises."""
    from core.services import network_member

    try:
        target = network_member.app_target(session)
        if target is None or target[1] != network_member.app_person(user_id):
            return False
        app = target[0]
        now = int(time.time())
        try:
            await run_in_thread(
                repo._table().update_item,
                Key=_key(user_id, app),
                UpdateExpression="SET lastWakeAt = :now",
                ConditionExpression="#s = :ready AND (attribute_not_exists(lastWakeAt) OR lastWakeAt < :cutoff)",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={":now": now, ":ready": "ready", ":cutoff": now - WINDOW_S},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False  # not asked, no key yet, or woken moments ago
            raise
        delivered, reason = await post(user_id, _secret_id(app), payload(app, session))
        logger.info("app wake user=%s app=%s delivered=%s reason=%s", user_id, app, delivered, reason)
        return delivered
    except Exception:  # noqa: BLE001 -- a wake is a courtesy; mail stays for check_inbox
        logger.warning("app wake failed user=%s", user_id, exc_info=True)
        return False
