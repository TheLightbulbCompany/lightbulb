"""The platform Slack app: ONE Request URL for every workspace, plus the OAuth
install rail (Task 9). Spec: docs/superpowers/specs/2026-09-04-slack-marketplace-app-design.md §3.1.

Ingress contract (Slack disables an app only at >95 % failed deliveries over
60 min, but one tenant's wedged box must never count against everyone):

| condition                              | response                         |
|-----------------------------------------|----------------------------------|
| url_verification (signed)              | 200 {"challenge"}                |
| bad/missing/stale signature            | 401                              |
| platform app unseeded                  | 503 (config error, ours)         |
| duplicate event_id                     | 200                              |
| unknown team_id                        | 200 (log)                        |
| app_uninstalled / tokens_revoked       | revoke install, 200              |
| own-bot echo (except lifecycle events) | 200 (never enqueue our own posts)|
| owner not always-on entitled           | 200 + x-slack-no-retry: 1        |
| no container IP                        | 503 + Retry-After (Slack retries)|
| AgentCore runtime not answering yet    | 503 + Retry-After, dedup released|
| forward ok (2xx)                       | upstream status                  |
| forward timeout / non-2xx              | 200 + metric slack.forward_failed|

Forward = raw body byte-identical, Content-Type preserved, X-Slack-Request-
Timestamp preserved, X-Slack-Signature RE-SIGNED with the owner's hook_token
(the container's Bolt receiver verifies against OPENCLAW_HOOK_TOKEN — the
app-wide signing secret never leaves this process). No Authorization header:
the plugin route is auth:"plugin".

The no-runtime 503 is the single deliberate exception to "per-owner failures never
5xx": Slack's retry IS the recovery for a workspace mid-roll, and one owner's
503s cannot approach the app-wide 95 %/60 min threshold.

Dedup is recorded only once we are about to forward; a 503 leaves no row so
Slack's retry is delivered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from urllib.parse import parse_qs, quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo, oauth_state_repo, slack_installs_repo
from core.services import owner_lifecycle, runtime_transport, slack_install_service, slack_signing, webhook_dedup
from core.services.credentials import platform_oauth_app_store
from core.services.provision_gate import has_always_on_entitlement

logger = logging.getLogger(__name__)
router = APIRouter()

PLATFORM_APP = "slack"
FORWARD_TIMEOUT_S = 2.0
DEDUP_TTL_S = 3600
_REVOKE_EVENTS = {"app_uninstalled", "tokens_revoked"}
# Events whose `user` is the bot as SUBJECT, not as actor — exempt from the
# own-post echo filter below.
_BOT_SUBJECT_EVENTS = frozenset({"member_joined_channel", "member_left_channel"})


def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(FORWARD_TIMEOUT_S))


def _json_200(payload: dict) -> Response:
    return Response(status_code=200, content=json.dumps(payload).encode(), media_type="application/json")


def _parse(content_type: str, body: bytes) -> dict:
    """Slack sends events as JSON and slash/interactive payloads as form data
    (interactive carries a JSON string under ``payload``). Returns the dict we
    route on; ``{}`` when unparseable (the signature already proved it's Slack's)."""
    try:
        if content_type.startswith("application/x-www-form-urlencoded"):
            form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            if "payload" in form:
                inner = json.loads(form["payload"])
                return inner if isinstance(inner, dict) else {}
            return form
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _dedup_key(team_id: str, event_id: str) -> str:
    return f"slack:{team_id}:{event_id}"


def _team_id(data: dict) -> str | None:
    if isinstance(data.get("team_id"), str):
        return data["team_id"]
    team = data.get("team")
    if isinstance(team, dict) and isinstance(team.get("id"), str):
        return team["id"]
    return None


async def _defer_to_slack_retry(team_id: str, event_id: str | None, *, reason: str) -> Response:
    """503 + Retry-After so Slack re-delivers, with the dedup row released so the retry is
    not swallowed as a duplicate. Only for a runtime that has not answered yet."""
    if event_id:
        await webhook_dedup.delete_event(_dedup_key(team_id, event_id))
    put_metric("slack.forward_deferred", 1, dimensions={"reason": reason})
    return Response(
        status_code=503,
        headers={"Retry-After": "60"},
        content=b'{"detail":"runtime_waking"}',
        media_type="application/json",
    )


@router.post("/events")
async def receive_slack_event(request: Request) -> Response:
    body = await request.body()
    app = await platform_oauth_app_store.get(PLATFORM_APP)
    if not app or not app.get("signing_secret"):
        return Response(
            status_code=503, content=b'{"detail":"slack_app_not_configured"}', media_type="application/json"
        )
    if not slack_signing.verify(
        app["signing_secret"],
        request.headers.get("X-Slack-Request-Timestamp"),
        body,
        request.headers.get("X-Slack-Signature"),
    ):
        logger.warning("slack events: rejected unsigned delivery")
        return Response(status_code=401, content=b'{"detail":"invalid_signature"}', media_type="application/json")

    content_type = request.headers.get("Content-Type", "")
    data = _parse(content_type, body)
    if data.get("type") == "url_verification":
        return _json_200({"challenge": data.get("challenge", "")})

    team_id = _team_id(data)
    if not team_id:
        logger.warning("slack events: signed payload without a team id type=%s", data.get("type"))
        return Response(status_code=200)

    # Only event_callbacks carry event_id and get retried; interactive/slash
    # payloads are one-shot, so they skip the dedup table.
    event_id = data.get("event_id") if data.get("type") == "event_callback" else None

    install = await slack_installs_repo.get(team_id)
    if not install or install.get("status") != slack_installs_repo.STATUS_ACTIVE:
        logger.info("slack events: no active install for team=%s", team_id)
        return Response(status_code=200)

    event = data.get("event") if isinstance(data.get("event"), dict) else {}
    if event.get("type") in _REVOKE_EVENTS:
        # These never 503, so there's no reason to defer dedup like the
        # forward path does — recording it up front stops a Slack retry from
        # re-running the (idempotent but not free) container roll every time.
        if event_id:
            seen = await webhook_dedup.record_event_or_skip(
                _dedup_key(team_id, event_id), source="slack", ttl_seconds=DEDUP_TTL_S
            )
            if seen == webhook_dedup.WebhookDedupResult.ALREADY_SEEN:
                return Response(status_code=200)
        try:
            await slack_install_service.revoke_install(team_id)
        except Exception as exc:  # noqa: BLE001 — per-owner cleanup must never 5xx the shared app
            logger.exception("slack events: revoke failed team=%s err=%s", team_id, exc)
            put_metric("slack.revoke_failed", 1, dimensions={"reason": type(exc).__name__})
        return Response(status_code=200)
    # On these, `user` is the bot as the SUBJECT of a lifecycle fact, not the
    # actor of something we did — and the bot's own member_joined_channel is
    # what starts the plugin's room admission (members.ts
    # reportChannelRoomJoin), so dropping it leaves the container never knowing
    # it was added to a channel. Everything else keeps the identity filter:
    # for a message or a reaction, `user == our bot` genuinely IS our own
    # action echoing back, and forwarding those invites a self-trigger loop
    # (the plugin's reaction handler defaults to reactionNotifications="own",
    # which admits exactly a reaction on the bot's own message).
    if event.get("type") not in _BOT_SUBJECT_EVENTS and (
        event.get("bot_id") or (event.get("user") and event.get("user") == install.get("bot_user_id"))
    ):
        return Response(status_code=200)  # our own post echoing back

    owner_id = install["owner_id"]
    container = await container_repo.get_by_owner_id(owner_id)
    if not container or not container.get("hook_token"):
        logger.warning("slack events: owner=%s has no container/hook_token", owner_id)
        return Response(status_code=200)
    billing = await billing_repo.get_by_owner_id(owner_id)
    if not has_always_on_entitlement(billing):
        # Phase 1 is paid-only. Phase 2 replaces this with wake + templated reply.
        return Response(status_code=200, headers={"x-slack-no-retry": "1"})

    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    headers = {
        "Content-Type": content_type or "application/json",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": slack_signing.sign(container["hook_token"], ts, body),
    }
    # Dedup is recorded only once we are about to forward; a 503 leaves no
    # row so Slack's retry is delivered. Everything above that can handle
    # revoke/echo/unentitled runs before dedup.
    if event_id:
        seen = await webhook_dedup.record_event_or_skip(
            _dedup_key(team_id, event_id), source="slack", ttl_seconds=DEDUP_TTL_S
        )
        if seen == webhook_dedup.WebhookDedupResult.ALREADY_SEEN:
            return Response(status_code=200)

    path = f"/slack/events/{quote(team_id, safe='')}"
    try:
        # The seam's own ceiling (timeout_s + margin + 2s) can run well past
        # FORWARD_TIMEOUT_S -- this module's invariant is that we never eat
        # into Slack's 3s retry budget, so the wait is bounded here too.
        upstream = await asyncio.wait_for(
            runtime_transport.request(container, path, content=body, headers=headers, timeout_s=FORWARD_TIMEOUT_S),
            timeout=FORWARD_TIMEOUT_S,
        )
    except (httpx.RequestError, asyncio.TimeoutError) as exc:
        # A parked microVM answers nothing inside Slack's budget, and a booting one
        # answers 503 — on AgentCore that is the NORMAL first contact, not a fault. The
        # invoke we just abandoned is what wakes it, so hand the event back to Slack's
        # retry (1 min / 5 min) and release the dedup row so that retry is delivered.
        logger.info("slack events: runtime not answering yet owner=%s err=%s", owner_id, exc)
        return await _defer_to_slack_retry(team_id, event_id, reason="cold_runtime")
    if upstream.status_code == 503:
        logger.info("slack events: runtime still booting owner=%s", owner_id)
        return await _defer_to_slack_retry(team_id, event_id, reason="cold_runtime")
    if upstream.status_code >= 300:
        logger.warning("slack events: container answered %s owner=%s", upstream.status_code, owner_id)
        put_metric("slack.forward_failed", 1, dimensions={"reason": f"upstream_{upstream.status_code // 100}xx"})
        return Response(status_code=200)
    return Response(
        status_code=upstream.status_code,
        content=upstream.text.encode("utf-8"),
        media_type=upstream.headers.get("content-type", "application/json"),
    )


SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"


def _redirect_uri() -> str:
    return f"{settings.PUBLIC_API_BASE}/api/v1/slack/oauth/callback"


def _frontend_return(agent_id: str, outcome: str) -> str:
    """Back to the agent's Channels tab (the same deep link ChatLayout uses)."""
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/chat?panel=agent-inspector&agent={quote(agent_id, safe='')}&inspectorTab=channels&slack={outcome}"


@router.get("/oauth/start")
async def slack_oauth_start(agent_id: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    """Mint the Slack authorize URL for the signed-in org admin. The browser
    navigates to it (a bearer can't ride a navigation, so this is JSON)."""
    require_org_admin(auth)
    app = await platform_oauth_app_store.get(PLATFORM_APP)
    if not app:
        raise HTTPException(status_code=503, detail="slack_app_not_configured")
    owner_id = resolve_owner_id(auth)
    # Phase 1 forwards Slack traffic for paid owners only (the events route
    # drops everything else). Refusing here keeps a trial owner from
    # connecting a workspace the bot would never answer.
    if not has_always_on_entitlement(await billing_repo.get_by_owner_id(owner_id)):
        raise HTTPException(status_code=402, detail="slack_requires_paid_plan")
    state = secrets.token_urlsafe(32)
    await oauth_state_repo.put(
        state,
        owner_id=owner_id,
        provider=PLATFORM_APP,
        verifier="",
        extra={"agent_id": agent_id, "member_id": auth.user_id},
    )
    query = urlencode(
        {
            "client_id": app["client_id"],
            "scope": ",".join(slack_install_service.BOT_SCOPES),
            "redirect_uri": _redirect_uri(),
            "state": state,
        }
    )
    return {"url": f"{SLACK_AUTHORIZE_URL}?{query}"}


@router.get("/oauth/callback")
async def slack_oauth_callback(code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    """Slack redirects here after the admin authorizes. Unauthenticated by
    nature — the single-use ``state`` row is the identity. Every path out is a
    302 — a human browser lands here with no way to retry a 5xx, so an outer
    catch-all backstops the specific branches below."""
    agent_id = ""
    try:
        rec = await oauth_state_repo.consume(state) if state else None
        if not rec or rec.get("provider") != PLATFORM_APP:
            return RedirectResponse(_frontend_return("", "error:invalid_state"), status_code=302)
        agent_id = rec.get("agent_id") or ""
        if error or not code:
            return RedirectResponse(
                _frontend_return(agent_id, f"error:{quote(error or 'missing_code', safe='')}"), status_code=302
            )

        app = await platform_oauth_app_store.get(PLATFORM_APP)
        if not app:
            return RedirectResponse(_frontend_return(agent_id, "error:slack_app_not_configured"), status_code=302)
        try:
            async with _client_factory() as client:
                resp = await client.post(
                    SLACK_TOKEN_URL,
                    data={
                        "client_id": app["client_id"],
                        "client_secret": app["client_secret"],
                        "code": code,
                        "redirect_uri": _redirect_uri(),
                    },
                )
            token = resp.json()
        except (httpx.RequestError, ValueError) as exc:
            logger.warning("slack oauth: token exchange failed: %s", exc)
            return RedirectResponse(_frontend_return(agent_id, "error:exchange_failed"), status_code=302)
        if not token.get("ok"):
            return RedirectResponse(
                _frontend_return(agent_id, f"error:{quote(token.get('error', 'exchange_failed'), safe='')}"),
                status_code=302,
            )

        owner_id = rec["owner_id"]
        try:
            # Same lease revoke-all reads its Slack snapshot under: an install either lands
            # before that snapshot (and is uninstalled) or finds the mark here.
            async with owner_lifecycle.owner_lifecycle_guard(owner_id):
                if await owner_lifecycle.revoked_since(owner_id, rec):
                    return RedirectResponse(_frontend_return(agent_id, "error:revoked"), status_code=302)
                await slack_install_service.apply_install(
                    owner_id=owner_id,
                    agent_id=agent_id,
                    team_id=token["team"]["id"],
                    team_name=token["team"].get("name") or "",
                    bot_token=token["access_token"],
                    bot_user_id=token.get("bot_user_id") or "",
                    app_id=token.get("app_id") or "",
                    scopes=[s for s in (token.get("scope") or "").split(",") if s],
                    installer_slack_user_id=(token.get("authed_user") or {}).get("id") or "",
                    installer_member_id=rec.get("member_id") or "",
                )
        except owner_lifecycle.OwnerLifecycleBusyError:
            return RedirectResponse(_frontend_return(agent_id, "error:busy"), status_code=302)
        except slack_install_service.SlackInstallConflict:
            return RedirectResponse(_frontend_return(agent_id, "error:workspace_already_connected"), status_code=302)
        return RedirectResponse(_frontend_return(agent_id, "connected"), status_code=302)
    except Exception:  # noqa: BLE001 — a human browser lands here; never let it 5xx
        logger.exception("slack oauth: callback failed unexpectedly")
        return RedirectResponse(_frontend_return(agent_id, "error:internal"), status_code=302)


@router.get("/install")
async def list_slack_installs(auth: AuthContext = Depends(get_current_user)) -> dict:
    rows = await slack_installs_repo.list_for_owner(resolve_owner_id(auth))
    keys = ("team_id", "team_name", "agent_id", "status", "installed_at")
    return {"installs": [{k: r.get(k) for k in keys} for r in rows]}


@router.delete("/install/{team_id}")
async def delete_slack_install(team_id: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    require_org_admin(auth)
    row = await slack_installs_repo.get(team_id)
    if not row or row.get("owner_id") != resolve_owner_id(auth):
        raise HTTPException(status_code=404, detail="install_not_found")
    await slack_install_service.revoke_install(team_id)
    return {"status": "revoked"}
