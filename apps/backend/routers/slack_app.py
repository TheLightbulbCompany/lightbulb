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
| own-bot echo                           | 200 (never enqueue our own posts)|
| owner not always-on entitled           | 200 + x-slack-no-retry: 1        |
| no container IP                        | 503 + Retry-After (Slack retries)|
| forward ok (2xx)                       | upstream status                  |
| forward timeout / non-2xx              | 200 + metric slack.forward_failed|

Forward = raw body byte-identical, Content-Type preserved, X-Slack-Request-
Timestamp preserved, X-Slack-Signature RE-SIGNED with the owner's hook_token
(the container's Bolt receiver verifies against OPENCLAW_HOOK_TOKEN — the
app-wide signing secret never leaves this process). No Authorization header:
the plugin route is auth:"plugin". Never resolve_running_container here — its
5 s TCP probe eats Slack's whole 3 s budget.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Request, Response

from core.containers import get_ecs_manager
from core.containers.ecs_manager import GATEWAY_PORT
from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo, slack_installs_repo
from core.services import slack_install_service, slack_signing, webhook_dedup
from core.services.credentials import platform_oauth_app_store
from core.services.provision_gate import has_always_on_entitlement

logger = logging.getLogger(__name__)
router = APIRouter()

PLATFORM_APP = "slack"
FORWARD_TIMEOUT_S = 2.0
DEDUP_TTL_S = 3600
_REVOKE_EVENTS = {"app_uninstalled", "tokens_revoked"}

# Module-level so tests can patch (same idiom as hooks_proxy).
ecs_manager = get_ecs_manager()


def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(FORWARD_TIMEOUT_S))


def _json_200(payload: dict, **headers: str) -> Response:
    return Response(
        status_code=200, content=json.dumps(payload).encode(), media_type="application/json", headers=headers
    )


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


def _team_id(data: dict) -> str | None:
    if isinstance(data.get("team_id"), str):
        return data["team_id"]
    team = data.get("team")
    if isinstance(team, dict) and isinstance(team.get("id"), str):
        return team["id"]
    return None


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
    if event_id:
        seen = await webhook_dedup.record_event_or_skip(
            f"slack:{team_id}:{event_id}", source="slack", ttl_seconds=DEDUP_TTL_S
        )
        if seen == webhook_dedup.WebhookDedupResult.ALREADY_SEEN:
            return Response(status_code=200)

    install = await slack_installs_repo.get(team_id)
    if not install or install.get("status") != slack_installs_repo.STATUS_ACTIVE:
        logger.info("slack events: no active install for team=%s", team_id)
        return Response(status_code=200)

    event = data.get("event") if isinstance(data.get("event"), dict) else {}
    if event.get("type") in _REVOKE_EVENTS:
        await slack_install_service.revoke_install(team_id)
        return Response(status_code=200)
    if event.get("bot_id") or (event.get("user") and event.get("user") == install.get("bot_user_id")):
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

    ip = ecs_manager.discover_ip(container["service_name"])
    if not ip:
        return Response(
            status_code=503,
            headers={"Retry-After": "30"},
            content=b'{"detail":"container_not_running"}',
            media_type="application/json",
        )

    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    headers = {
        "Content-Type": content_type or "application/json",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": slack_signing.sign(container["hook_token"], ts, body),
    }
    target = f"http://{ip}:{GATEWAY_PORT}/slack/events/{team_id}"
    try:
        async with _client_factory() as client:
            upstream = await client.post(target, headers=headers, content=body)
    except httpx.RequestError as exc:
        logger.warning("slack events: forward failed owner=%s err=%s", owner_id, exc)
        put_metric("slack.forward_failed", 1, dimensions={"reason": "request_error"})
        return Response(status_code=200)
    if upstream.status_code >= 300:
        logger.warning("slack events: container answered %s owner=%s", upstream.status_code, owner_id)
        put_metric("slack.forward_failed", 1, dimensions={"reason": f"upstream_{upstream.status_code // 100}xx"})
        return Response(status_code=200)
    return Response(
        status_code=upstream.status_code,
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "application/json"),
    )
