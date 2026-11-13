"""Workspace website logins: the store behind every agent's Hermes browser vault.

One store per workspace, shared by all of its agents (like the shared browser profile's
cookies). Hermes's own vault (``agent/vault_store.py``) keeps a Fernet key BESIDE its
ciphertext under ``$HERMES_HOME/vault/``, and our bridge syncs that directory to S3 with the
profile -- so the bridge swaps the vault's store for one that reads this service instead
(``apps/infra/hermes/bridge/website_logins.py``) and nothing credential-shaped is ever
written to a profile.

Where things live:

* **Password** -- Secrets Manager, ``lightbulb/<env>/integrations/<owner>/website-login/<id>``
  via ``integration_secret_store`` (the owner-prefix sweep in ``owner_secret_sweep`` takes it
  on an account wipe, and ``integrations_repo.delete_all_for_owner`` the metadata row). Never
  DynamoDB, never a log line, never a message.
* **Metadata** -- one reserved row ``(owner, "website_logins")`` in the integrations table, a
  map keyed by login id (origin, username, who added it, last used). Per-key update
  expressions, so a concurrent add and a fill's last-used touch never drop each other.
* **Requests** -- an agent that meets a login (or a 2FA code) with nothing saved asks through
  ``create_request``: a ``LOGINREQ#`` row in the messaging table, a Needs-you Activity row for
  each human who can see it, and a notice in the room. The human answers on Activity's secure
  form (``answer``); the bridge polls ``poll_request``. A code is Fernet-encrypted on the row
  and erased on its first read.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import tldextract
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso
from core.repositories import messaging_repo
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)

LOGINS_KEY = "website_logins"  # reserved integrations sort key, like integrations_repo.MANUAL_MCP_KEY
LOGIN_ID_RE = re.compile(r"^vault_[0-9a-f]{12}$")  # Hermes's local-backend handle shape ("vault_" prefix)
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
REQUEST_TTL_S = 24 * 3600
# The bridge waits ~6.5 min (under Hermes's 420 s tool deadline); after this a pending
# request reads as expired even if the bridge never said so.
REQUEST_OPEN_S = 7 * 60
ACTIVITY_TTL_S = 30 * 24 * 3600
MAX_FIELD = 512
MAX_SECRET = 4096
_DEFAULT_PORTS = {"http": 80, "https": 443}


class LoginError(ValueError):
    """Safe to show: never carries a secret."""


# ---------------------------------------------------------------------------
# origins
# ---------------------------------------------------------------------------


def normalize_origin(value: str) -> str:
    """``scheme://host[:port]`` exactly as Hermes's ``normalize_origin`` compares it. A bare
    host ("app.example.com") means https."""
    raw = (value or "").strip()
    if not raw:
        raise LoginError("Enter the site's address.")
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    scheme, host = (parts.scheme or "").lower(), (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host or "." not in host:
        raise LoginError("Enter a web address like https://app.example.com.")
    try:
        port = parts.port
    except ValueError as exc:
        raise LoginError("That address has an invalid port.") from exc
    return f"{scheme}://{host}" if port in (None, _DEFAULT_PORTS[scheme]) else f"{scheme}://{host}:{port}"


def host_of(origin: str) -> str:
    return origin.split("://", 1)[-1]


# The Public Suffix List (ICANN + private sections, so someone.github.io is its own site), from
# tldextract's BUNDLED snapshot only: no suffix_list_urls and no cache, so it never fetches.
# ponytail: the snapshot is as old as the pinned tldextract. A suffix newer than it, or a host
# the list does not know at all (an intranet name, a made-up TLD), is its own site: an unknown
# case can only split one site into two rows, never merge two sites into one. Bump tldextract
# to refresh the list.
_PSL = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)


def _host(value: str | None) -> str:
    host = (value or "").strip().lower()
    if "://" in host:
        host = urlsplit(host).hostname or ""
    else:
        host = host.lstrip(".").split("/", 1)[0]
        if host.count(":") == 1:  # host:port (an IPv6 literal has more than one colon)
            host = host.split(":", 1)[0]
    return host.strip(".")


def site_of(host: str | None) -> str:
    """The registrable domain a host, origin or cookie domain belongs to ("accounts.google.com",
    ".google.com", "https://mail.google.com:8443" -> "google.com"): one sign-ins row per site.
    Anything without one (an IP address, a bare public suffix, an unknown name) is the whole
    host, so it never shares a row with another host."""
    host = _host(host)
    return _PSL(host).top_domain_under_public_suffix or host


async def connector_hint(owner_id: str, origin: str) -> str | None:
    """For a login ask: the site belongs to a service this workspace already has CONNECTED (its
    API connector), so say the password is a separate thing. None when not connected."""
    from core.repositories import integrations_repo, slack_installs_repo
    from core.services.credentials.registry import all_providers

    site = site_of(origin)
    try:
        for key, provider in all_providers().items():
            if site in provider.browser_domains:
                row = await integrations_repo.get(owner_id, key)
                if row and row.get("status") == "connected":
                    return f"{provider.browser_name or provider.display_name} is connected. This password is only for the browser."
        # Slack is not in the connector catalog (it is a channel install), so it is checked here.
        if site == "slack.com" and any(
            i.get("status") == "active" for i in await slack_installs_repo.list_for_owner(owner_id)
        ):
            return "Slack is connected. This password is only for the browser."
    except Exception:  # noqa: BLE001 -- a hint is decoration; the ask still goes out
        logger.warning("website_login connector hint failed owner=%s", owner_id)
    return None


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def _table():
    return get_table("integrations")


def _secret_key(login_id: str) -> str:
    return f"website-login/{login_id}"


def _public(login_id: str, row: dict) -> dict:
    return {
        "id": login_id,
        "origin": row.get("origin", ""),
        "username": row.get("username", ""),
        "addedBy": row.get("addedBy"),
        "addedByName": row.get("addedByName"),
        "createdAt": row.get("createdAt"),
        "lastUsedAt": row.get("lastUsedAt"),
    }


async def _rows(owner_id: str) -> dict[str, dict]:
    response = await run_in_thread(
        _table().get_item, Key={"owner_id": owner_id, "provider": LOGINS_KEY}, ConsistentRead=True
    )
    return dict((response.get("Item") or {}).get("logins") or {})


async def list_logins(owner_id: str) -> list[dict]:
    rows = await _rows(owner_id)
    return sorted((_public(k, v) for k, v in rows.items()), key=lambda r: (r["origin"], r["username"]))


async def add_login(
    owner_id: str, origin: str, username: str, password: str, *, added_by: str, added_by_name: str | None = None
) -> dict:
    """Save (or replace the password of) the login for ``origin`` + ``username``. The secret is
    written first so a listed login always has one."""
    origin = normalize_origin(origin)
    username = (username or "").strip()
    if not username or len(username) > MAX_FIELD:
        raise LoginError("Enter the username or email.")
    if not password or len(password) > MAX_SECRET:
        raise LoginError("Enter the password.")
    rows = await _rows(owner_id)
    login_id = next(
        (k for k, v in rows.items() if v.get("origin") == origin and v.get("username") == username),
        f"vault_{secrets.token_hex(6)}",
    )
    replaced = login_id in rows
    await integration_secret_store.put(owner_id, _secret_key(login_id), {"password": password})
    now = utc_now_iso()
    meta = {
        **rows.get(login_id, {}),
        "origin": origin,
        "username": username,
        "addedBy": added_by,
        "addedByName": added_by_name,
        "createdAt": rows.get(login_id, {}).get("createdAt") or now,
        "updatedAt": now,
    }
    # Two updates: a nested SET needs the map to exist first.
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": LOGINS_KEY},
        UpdateExpression="SET logins = if_not_exists(logins, :empty), updated_at = :t",
        ExpressionAttributeValues={":empty": {}, ":t": int(time.time())},
    )
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": LOGINS_KEY},
        UpdateExpression="SET logins.#id = :m",
        ExpressionAttributeNames={"#id": login_id},
        ExpressionAttributeValues={":m": meta},
    )
    logger.info(
        "website_login.%s owner=%s login=%s origin=%s actor=%s",
        "replaced" if replaced else "added",
        owner_id,
        login_id,
        origin,
        added_by,
    )
    return _public(login_id, meta)


async def resolve_login(owner_id: str, login_id: str, *, agent_id: str) -> dict | None:
    """The runtime's fill path: username + password for one login, and a last-used stamp."""
    if not LOGIN_ID_RE.match(login_id or ""):
        return None
    row = (await _rows(owner_id)).get(login_id)
    if row is None:
        return None
    secret = await integration_secret_store.get(owner_id, _secret_key(login_id))
    if not secret or not secret.get("password"):
        return None
    try:
        await run_in_thread(
            _table().update_item,
            Key={"owner_id": owner_id, "provider": LOGINS_KEY},
            UpdateExpression="SET logins.#id.lastUsedAt = :t",
            ConditionExpression="attribute_exists(logins.#id)",
            ExpressionAttributeNames={"#id": login_id},
            ExpressionAttributeValues={":t": utc_now_iso()},
        )
    except ClientError:
        pass  # deleted between the read and the stamp: the fill still has what it read
    logger.info("website_login.used owner=%s login=%s agent=%s", owner_id, login_id, agent_id)
    return {"username": row.get("username", ""), "password": secret["password"]}


# ---------------------------------------------------------------------------
# asks: an agent meets a login (or a 2FA code) with nothing saved
# ---------------------------------------------------------------------------


def request_sk(request_id: str) -> str:
    return f"LOGINREQ#{request_id}"


def activity_item_id(request_id: str) -> str:
    return f"login:{request_id}"


async def get_request(owner_id: str, request_id: str) -> dict | None:
    if not REQUEST_ID_RE.match(request_id or ""):
        return None
    return await messaging_repo._get(messaging_repo.owner_pk(owner_id), request_sk(request_id))


def request_status(row: dict) -> str:
    if row.get("status") == "pending" and time.time() - float(row.get("createdEpoch") or 0) > REQUEST_OPEN_S:
        return "expired"
    return str(row.get("status") or "expired")


async def _humans_to_ask(owner_id: str, room_id: str | None) -> list[str]:
    """The room's own-workspace humans plus every workspace member. Only admins can answer and
    roles live in Clerk, not here, so the whole workspace is asked: a DM between a member and an
    agent must still reach an admin. Guests from another workspace (a shared room) are not."""
    from core.repositories.workspace_repo import WorkspaceRepo

    humans: set[str] = set()
    if room_id:
        humans |= {
            m["id"]
            for m in await messaging_repo.list_members(room_id)
            if m.get("kind") == "human" and messaging_repo.member_home(m, owner_id) == owner_id
        }
    workspace = await WorkspaceRepo().get_workspace(owner_id)
    humans |= set((workspace.member_user_ids if workspace else None) or [])
    return sorted(humans)


async def _agent_name(owner_id: str, agent_id: str) -> str:
    from core.services.messaging import load_agent_roster

    try:
        roster = await load_agent_roster(owner_id)
    except Exception:  # noqa: BLE001 -- a name is decoration; the ask still goes out
        roster = []
    return next((a.get("name") for a in roster if a.get("id") == agent_id), None) or "Your agent"


def request_title(row: dict) -> str:
    site = host_of(row.get("origin") or row.get("site") or "") or "a website"
    return f"{row.get('agentName')} needs the login for {site}"


async def create_request(
    owner_id: str,
    *,
    kind: str,
    origin: str,
    agent_id: str,
    room_id: str | None,
    thread_root_message_id: str | None,
    hint: str | None = None,
    surface: str = "cloud",
) -> dict:
    """A ``login`` ask: the row, a Needs-you Activity row per human, and a notice in the room.

    A ``code`` ask is only the notice (owner ruling 2026-10-07): a one-time code is typed by a person
    into the shared browser, never into a form for the agent, so there is no row, no Activity item
    and nothing to answer. The bridge watches the page instead. ``hint`` is the sign-in page's
    heading, quoted so the person knows which step the site is on; ``surface`` says which browser
    the run is in -- the cloud Browser panel, or a selected Mac's own "Lightbulb agent" Chrome."""
    from core.config import settings
    from core.services.live_events import publish_owner_users
    from core.services.messaging import make_live_event

    if kind not in ("login", "code"):
        raise LoginError("kind must be login or code")
    origin = normalize_origin(origin)
    if room_id:
        room_id = str(uuid.UUID(room_id))
        meta = await messaging_repo.get_room(room_id)
        if not meta or meta.get("ownerId") != owner_id or meta.get("deletedAt"):
            room_id = None  # not this workspace's room: ask the workspace instead
    request_id = uuid.uuid4().hex
    if kind == "code":
        posted = False
        if room_id:
            site = host_of(origin)
            heading = " ".join(str(hint or "").split())[:120]
            asking = f"{site} is asking for a verification code{f' ({heading})' if heading else ''}."
            if surface == "mac":
                text = (
                    f"{asking} Type it into the Lightbulb agent Chrome window on your Mac — the one I'm "
                    f"browsing in — and I'll carry on as soon as the page moves."
                )
            else:
                text = (
                    f"{asking} Type it into the shared browser — open the Browser panel on a computer (the "
                    f"phone has no live view): {settings.FRONTEND_URL.rstrip('/')}/chat?panel=browser — and "
                    f"I'll carry on as soon as the page moves."
                )
            posted = await _post_notice(
                owner_id,
                {
                    "requestId": request_id,
                    "roomId": room_id,
                    "threadRootMessageId": thread_root_message_id,
                    "agentId": agent_id,
                    "agentName": await _agent_name(owner_id, agent_id),
                },
                text,
            )
        logger.info(
            "website_login.code_notice owner=%s origin=%s agent=%s room=%s posted=%s",
            owner_id,
            origin,
            agent_id,
            room_id,
            posted,
        )
        # "unposted" tells the bridge nobody was told, so it does not wait for anyone.
        return {"requestId": None, "status": "posted" if posted else "unposted"}
    created = utc_now_iso()
    row = {
        "pk": messaging_repo.owner_pk(owner_id),
        "sk": request_sk(request_id),
        "requestId": request_id,
        "kind": kind,
        "origin": origin,
        "agentId": agent_id,
        "agentName": await _agent_name(owner_id, agent_id),
        "roomId": room_id,
        "threadRootMessageId": thread_root_message_id if room_id else None,
        # Decided once, at the ask (it lives ~7 minutes); Activity renders it from the row.
        "hint": await connector_hint(owner_id, origin),
        "status": "pending",
        "createdAt": created,
        "createdEpoch": int(time.time()),
        "expiresAt": int(time.time()) + REQUEST_TTL_S,
    }
    await run_in_thread(messaging_repo._table().put_item, Item={k: v for k, v in row.items() if v is not None})
    humans = await _humans_to_ask(owner_id, room_id)
    await asyncio.gather(
        *(
            messaging_repo.put_activity_row(
                {
                    "pk": messaging_repo.activity_pk(owner_id, user_id),
                    "sk": messaging_repo.activity_sk(created, activity_item_id(request_id)),
                    "kind": "login",
                    "requestId": request_id,
                    "createdAt": created,
                    "expiresAt": int(time.time()) + ACTIVITY_TTL_S,
                }
            )
            for user_id in humans
        ),
        return_exceptions=True,
    )
    link = f"{settings.FRONTEND_URL.rstrip('/')}/chat?panel=activity&focus={activity_item_id(request_id)}"
    if room_id:
        await _post_notice(
            owner_id,
            row,
            f"I need a login for {host_of(origin)} to continue. Add it here — the password goes straight to "
            f"the workspace's website sign-ins, never into this chat: {link}",
        )
    try:
        await publish_owner_users(owner_id, make_live_event("runtime.resource.invalidated", {"resource": "activity"}))
    except Exception:  # noqa: BLE001 -- the badge catches up on the next focus
        logger.warning("website_login request live event failed owner=%s", owner_id)
    logger.info(
        "website_login.request owner=%s request=%s kind=%s origin=%s agent=%s humans=%d",
        owner_id,
        request_id,
        kind,
        origin,
        agent_id,
        len(humans),
    )
    return {"requestId": request_id, "status": "pending"}


async def _post_notice(owner_id: str, row: dict, text: str) -> bool:
    """The agent's line in the room. Written straight through the canonical writer with no
    dispatch targets, so it wakes nobody (an ``active`` teammate would otherwise take it as a
    room event). No run id: that would retire the asking run's live card. False = it did not land."""
    try:
        await messaging_repo.create_message(
            room_id=row["roomId"],
            owner_id=owner_id,
            idemp_pk=messaging_repo.owner_pk(owner_id),
            idemp_sk=messaging_repo.agent_idemp_sk(f"login-request:{row['requestId']}"),
            req_hash=messaging_repo.request_hash(row["requestId"]),
            author={"kind": "agent", "id": row["agentId"], "displayName": row["agentName"]},
            text=text,
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=row.get("threadRootMessageId"),
            run_id=None,
            dispatch_targets=[],
            source_agent_id=row["agentId"],
            client_message_id=None,
        )
    except Exception:  # noqa: BLE001 -- a login ask is still carried by Activity; a code ask is not
        logger.warning(
            "website_login request notice failed owner=%s request=%s", owner_id, row["requestId"], exc_info=True
        )
        return False
    return True


async def _settle(owner_id: str, request_id: str, status: str, **fields: Any) -> bool:
    """pending -> ``status`` exactly once."""
    names = {"#s": "status"}
    values: dict[str, Any] = {":p": "pending", ":s": status, ":at": utc_now_iso()}
    sets = ["#s = :s", "resolvedAt = :at"]
    for i, (key, value) in enumerate(fields.items()):
        names[f"#f{i}"] = key
        values[f":f{i}"] = value
        sets.append(f"#f{i} = :f{i}")
    try:
        await run_in_thread(
            messaging_repo._table().update_item,
            Key={"pk": messaging_repo.owner_pk(owner_id), "sk": request_sk(request_id)},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="#s = :p",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def answer(
    owner_id: str, request_id: str, *, user_id: str, value: dict[str, str] | None, user_name: str | None = None
) -> dict:
    """A human's answer from the secure form (``value`` None = "Not now"). Admin-gating is the
    caller's. Returns the settled row."""
    row = await get_request(owner_id, request_id)
    if row is None:
        raise LoginError("This request is gone.")
    if request_status(row) != "pending":
        return row
    if value is None:
        await _settle(owner_id, request_id, "declined", resolvedBy=user_id)
    elif row["kind"] == "login":
        saved = await add_login(
            owner_id,
            row["origin"],
            value.get("username", ""),
            value.get("password", ""),
            added_by=user_id,
            added_by_name=user_name,
        )
        await _settle(owner_id, request_id, "answered", resolvedBy=user_id, loginId=saved["id"])
    else:
        raise LoginError("A sign-in code is typed into the shared browser, not here.")
    logger.info(
        "website_login.request_%s owner=%s request=%s actor=%s",
        "declined" if value is None else "answered",
        owner_id,
        request_id,
        user_id,
    )
    return await get_request(owner_id, request_id) or row


async def poll_request(owner_id: str, request_id: str) -> dict | None:
    """The bridge's wait on a login ask."""
    row = await get_request(owner_id, request_id)
    if row is None:
        return None
    status = request_status(row)
    out: dict[str, Any] = {"status": status, "kind": row["kind"]}
    if status == "answered" and row["kind"] == "login":
        out["loginId"] = row.get("loginId")
    return out


async def cancel_request(owner_id: str, request_id: str) -> None:
    """The asking turn stopped waiting (timeout, abort): close the ask."""
    await _settle(owner_id, request_id, "expired")
