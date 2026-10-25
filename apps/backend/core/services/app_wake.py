"""``wake_me``: a bot (a Grok routine, a Muse agent) on the hosted MCP asks to be woken when its
app session gets network mail, instead of polling ``check_inbox``.

The bot gives only its webhook URL (``request``); the person pastes the webhook's key on a
signed-in Lightbulb page (``set_key``), never in a chat. On new mail for that app session (the
route's engine tells us: a computer's link sends a ``mail`` notice, a workspace's bridge posts
one) the backend makes ONE content-free POST (``webhook_agents.post``: Bearer key, public
addresses only, a bounded retry), at most once per ``WINDOW_S`` per app.

Rows (messaging table): ``APPWAKE#<user>`` / ``<app slug>`` -- the app, the URL's host, its
state (``needs_key`` | ``ready``). URL and key are one Secrets Manager secret
(``webhook_agents.secret_name(<user>, wake-<app>)``), never returned or logged.
"""

from __future__ import annotations

import json
import logging
import time

from botocore.exceptions import ClientError

from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo as repo
from core.services import webhook_agents
from core.services.messaging import ApiProblem

logger = logging.getLogger(__name__)

WINDOW_S = 15
PAGE = "/auth/network/apps"


def _key(user_id: str, app: str) -> dict:
    return {"pk": f"APPWAKE#{user_id}", "sk": app}


def _secret_id(app: str) -> str:
    return f"wake-{app}"


def _public(row: dict) -> dict:
    return {k: row.get(k) for k in ("app", "client", "host", "state", "createdAt")}


async def request(user_id: str, app: str, client: str, url: str) -> dict:
    """Record (or replace) this app's webhook; it needs its key entered before any wake."""
    from urllib.parse import urlsplit

    from core.config import settings
    from core.secrets_manager import create_or_update_secret

    webhook_agents.check_url(url)
    secret = json.dumps({"url": url, "key": ""})
    name = webhook_agents.secret_name(user_id, _secret_id(app))
    await run_in_thread(create_or_update_secret, webhook_agents._sm(), name, secret)
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
    from core.secrets_manager import create_or_update_secret

    if not key.strip() or len(key) > 4096:
        raise ApiProblem("INVALID_REQUEST", "Paste the webhook's key.")
    row = await repo._get(**_key(user_id, app))
    if row is None:
        raise ApiProblem("MEMBER_NOT_FOUND", "No wake-up request from that app.")
    name = webhook_agents.secret_name(user_id, _secret_id(app))
    cred = await webhook_agents._credential(user_id, _secret_id(app))
    await run_in_thread(
        create_or_update_secret, webhook_agents._sm(), name, json.dumps({"url": cred["url"], "key": key.strip()})
    )
    await run_in_thread(
        repo._table().update_item,
        Key=_key(user_id, app),
        UpdateExpression="SET #s = :ready",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":ready": "ready"},
    )
    return _public({**row, "state": "ready"})


async def remove(user_id: str, app: str) -> None:
    sm = webhook_agents._sm()
    try:
        await run_in_thread(
            sm.delete_secret,
            SecretId=webhook_agents.secret_name(user_id, _secret_id(app)),
            ForceDeleteWithoutRecovery=True,
        )
    except sm.exceptions.ResourceNotFoundException:
        pass
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
        delivered, reason = await webhook_agents.post(user_id, _secret_id(app), payload(app, session))
        logger.info("app wake user=%s app=%s delivered=%s reason=%s", user_id, app, delivered, reason)
        return delivered
    except Exception:  # noqa: BLE001 -- a wake is a courtesy; mail stays for check_inbox
        logger.warning("app wake failed user=%s", user_id, exc_info=True)
        return False


async def notify_workspace(owner_id: str, session: str) -> bool:
    """A workspace's bridge saw mail for app session ``session``: wake its person's app."""
    from core.services import network_member
    from core.services.website_logins import _humans_to_ask

    target = network_member.app_target(session)
    if target is None:
        return False
    for user_id in await _humans_to_ask(owner_id, None):
        if network_member.app_person(user_id) == target[1]:
            return await notify(user_id, session)
    return False
