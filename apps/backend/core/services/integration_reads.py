"""Bounded reads over a workspace's connected integrations, for a voice seat (the meeting agent's
`read_integration`, spec §4.1b): one search/list/get, at most READ_S seconds, at most ANSWER_CHARS of plain
text. Anything slower or heavier is a Worker's job (start_work).

Tokens come from where the backend already holds them: `access_token.get` for backend-refreshed OAuth
(the brokered MCP providers in dev/prod), else the retained copy in `integration_secret_store` (GitHub,
PostHog), the business's own Stripe key (`company_stripe`). A provider whose only grant lives on the
workspace computer (Google today: the gog CLI refreshes it) is "not readable from here", never a 500.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime

import httpx

from core.repositories import integrations_repo
from core.services.company_stripe import API_KEY_SECRET_PROVIDER
from core.services.credentials import access_token, integration_secret_store, registry

logger = logging.getLogger(__name__)

READ_S = 2.0
ANSWER_CHARS = 2000
HEAVY = "That needs a proper look; ask me to start work on it."


class NotReadable(Exception):
    """Said back as it is: why this provider cannot be read from a voice seat."""


async def _token(ws: str, key: str) -> str:
    try:
        return await access_token.get(ws, key)
    except access_token.ReconnectRequired as exc:
        raise NotReadable(f"{key} needs reconnecting in Settings.") from exc
    except ValueError:  # not backend-refreshed: the retained copy, if the backend keeps one
        cred = await integration_secret_store.get(ws, key) or {}
        if token := cred.get("access_token"):
            return token
        raise NotReadable(f"{key} is connected on the workspace computer only. {HEAVY}") from None


async def _get(c: httpx.AsyncClient, url: str, token: str, **params) -> dict:
    r = await c.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
    r.raise_for_status()
    return r.json()


# --- Google (Gmail, Calendar, Drive) ------------------------------------------------------------------


async def _gmail(c, ws, query):
    token = await _token(ws, "google")
    base = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    ids = [m["id"] for m in (await _get(c, base, token, q=query, maxResults=3)).get("messages", [])]
    msgs = await asyncio.gather(
        *(_get(c, f"{base}/{i}", token, format="metadata", metadataHeaders=["From", "Subject"]) for i in ids)
    )
    out = []
    for m in msgs:
        h = {x["name"]: x["value"] for x in m.get("payload", {}).get("headers", [])}
        out.append(f"{h.get('Subject', '(no subject)')} — from {h.get('From', '?')}: {m.get('snippet', '')}")
    return out


async def _calendar(c, ws, query):
    token = await _token(ws, "google")
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    params = {"timeMin": datetime.now(UTC).isoformat(), "maxResults": 5, "singleEvents": "true", "orderBy": "startTime"}
    if query:
        params["q"] = query
    items = (await _get(c, url, token, **params)).get("items", [])
    return [
        f"{e.get('summary', '(untitled)')} at {(e.get('start') or {}).get('dateTime') or (e.get('start') or {}).get('date')}"
        for e in items
    ]


async def _drive(c, ws, query):
    token = await _token(ws, "google")
    q = "fullText contains '" + query.replace("\\", "\\\\").replace("'", "\\'") + "' and trashed = false"
    files = (
        await _get(c, "https://www.googleapis.com/drive/v3/files", token, q=q, pageSize=5,
                   fields="files(name,modifiedTime)")
    ).get("files", [])  # fmt: skip
    return [f"{f['name']} (modified {f.get('modifiedTime', '')[:10]})" for f in files]


# --- GitHub, PostHog (retained tokens) -----------------------------------------------------------------


async def _github(c, ws, query):
    token = await _token(ws, "github")
    items = (await _get(c, "https://api.github.com/search/issues", token, q=query, per_page=5)).get("items", [])
    repo = lambda i: "/".join(i.get("repository_url", "").split("/")[-2:])  # noqa: E731
    return [f"{i['title']} ({i.get('state')}, {repo(i)} #{i.get('number')})" for i in items]


_EVENT = re.compile(r"^[\w$ .:-]{1,60}$")


async def _posthog(c, ws, query):
    token = await _token(ws, "posthog")
    event = query.strip() if _EVENT.fullmatch(query.strip() or "-") and query.strip() else "$pageview"
    hogql = (  # event is interpolated only after the _EVENT fence, quotes excluded by it
        f"SELECT count(), count(DISTINCT person_id) FROM events WHERE event = '{event}' "
        "AND timestamp > now() - INTERVAL 7 DAY"
    )
    r = await c.post(
        "https://us.posthog.com/api/projects/@current/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": {"kind": "HogQLQuery", "query": hogql}},
    )
    r.raise_for_status()
    total, people = (r.json().get("results") or [[0, 0]])[0]
    return [f"{event}: {total} events from {people} people in the last 7 days"]


# --- Stripe, Notion, Linear (hosted MCP, brokered tokens) ------------------------------------------------


def _rpc(r: httpx.Response) -> dict:
    """A JSON-RPC answer, as JSON or as the last `data:` line of an SSE body."""
    r.raise_for_status()
    if "event-stream" in r.headers.get("content-type", ""):
        data = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        return json.loads(data[-1]) if data else {}
    return r.json() if r.content else {}


async def _mcp(c, ws, key: str, prefer: tuple[str, ...], args: dict) -> list[str]:
    """initialize -> tools/list -> tools/call on the provider's hosted MCP. The tool is the first of `prefer`
    the server lists, else the first whose name has "search" in it."""
    provider = registry.get_provider(key)
    url = provider.mcp_remote.url
    h = {
        "Authorization": f"Bearer {await _token(ws, key)}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    init = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "lightbulb-voice", "version": "1"},
    }
    r = await c.post(url, headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
    _rpc(r)
    if sid := r.headers.get("mcp-session-id"):
        h["Mcp-Session-Id"] = sid
    await c.post(url, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = [t["name"] for t in _rpc(await c.post(url, headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).get("result", {}).get("tools", [])]  # fmt: skip
    name = next((p for p in prefer if p in tools), None) or next((t for t in tools if "search" in t), None)
    if name is None:
        raise NotReadable(f"{key} has no search I can use. {HEAVY}")
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": args}}
    result = _rpc(await c.post(url, headers=h, json=body)).get("result", {})
    return [x.get("text", "") for x in result.get("content", []) if x.get("type") == "text"]


async def _stripe(c, ws, query):
    key = (await integration_secret_store.get(ws, API_KEY_SECRET_PROVIDER) or {}).get("api_key")
    if not key:  # the brokered Stripe MCP grant
        return await _mcp(c, ws, "stripe", ("retrieve_balance",), {})
    bal = (await _get(c, "https://api.stripe.com/v1/balance", key)).get("available", [])
    charges = (await _get(c, "https://api.stripe.com/v1/charges", key, limit=5)).get("data", [])
    out = [f"Available balance: {', '.join(f'{b["amount"] / 100:.2f} {b["currency"].upper()}' for b in bal) or 'none'}"]
    out += [
        f"{ch['amount'] / 100:.2f} {ch['currency'].upper()} {ch.get('status')} — {ch.get('description') or ch.get('billing_details', {}).get('name') or ''}"
        for ch in charges
    ]
    return out


async def _notion(c, ws, query):
    return await _mcp(c, ws, "notion", ("notion-search", "search"), {"query": query})


async def _linear(c, ws, query):
    return await _mcp(c, ws, "linear", ("list_issues", "search_issues"), {"query": query, "limit": 5})


async def _slack(c, ws, query):
    # The workspace holds Slack's BOT token (slack_install_service); search.messages needs a user token.
    raise NotReadable(f"Slack search isn't available from a meeting. {HEAVY}")


# name -> (integrations row it needs, reader)
READERS = {
    "gmail": ("google", _gmail),
    "calendar": ("google", _calendar),
    "drive": ("google", _drive),
    "github": ("github", _github),
    "posthog": ("posthog", _posthog),
    "stripe": ("stripe", _stripe),
    "notion": ("notion", _notion),
    "linear": ("linear", _linear),
    "slack": ("slack", _slack),
}


async def connected(ws: str) -> list[str]:
    """The readable names this workspace has connected."""
    rows = {
        r["provider"] for r in await integrations_repo.list_for_owner(ws) if r.get("status", "connected") == "connected"
    }
    names = [n for n, (key, _) in READERS.items() if key in rows]
    if "stripe" not in names and await integration_secret_store.get(ws, API_KEY_SECRET_PROVIDER):
        names.append("stripe")
    return names


async def read(ws: str, provider: str, query: str) -> str:
    """Plain text for the voice to say or summarise; never raises."""
    name = provider.strip().lower()
    if name not in READERS or name not in await connected(ws):
        return f"{provider} is not connected."
    _key, reader = READERS[name]
    try:
        async with httpx.AsyncClient(timeout=READ_S) as c:
            lines = await asyncio.wait_for(reader(c, ws, query.strip()[:300]), READ_S)
    except NotReadable as e:
        return str(e)
    except (TimeoutError, httpx.TimeoutException):
        return f"{name} didn't answer in time. {HEAVY}"
    except Exception:  # noqa: BLE001 -- a provider error is a sentence, not a failed meeting
        logger.warning("integration read failed ws=%s provider=%s", ws, name, exc_info=True)
        return f"I couldn't read {name} just now."
    text = "\n".join(x for x in lines if x).strip()
    return text[:ANSWER_CHARS] if text else "Nothing found."
