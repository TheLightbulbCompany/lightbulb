"""One account cannot read or change another's data through any door that resolves a tenant.

For every (method, path) in CASES, tenant A's rows are seeded through the product's own writers (A's own HTTP
calls where a door writes them), carrying MARKER; then B, signed in, makes the call. Every case asserts:
  * A's rows are unchanged: every DynamoDB item and S3 object naming A, before and after;
  * nothing B gets back, and no outbound request B's call makes (HTTP, Stripe, a faked service), names A;
  * path id (`a_id=True`: B names A's room, task, key...): B gets 403 or 404, never 200;
  * token only: B's call succeeds, and no AWS call it makes names A either (B reads and writes only B's keys).
`ok` overrides the expected statuses for a door whose answer to B is something else (the reason is beside it);
`says` names the text that makes a refusal the tenant check rather than a generic one.
Each case runs twice (`tenants`): with plain session tokens, and with tokens that carry a leftover `o` claim naming the other person.

`test_every_tenant_door_has_a_case` walks the app: a new door with no case fails (no sampling);
`test_every_router_seeds_a`: a router whose doors find nothing of A's fails unless NO_SEED says why."""

import json
import uuid
from urllib.parse import parse_qs, urlsplit
from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
import stripe
from fastapi.routing import _IncludedRouter
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient

from core import dynamodb, tasks_schema as ts
from core.auth import (
    AuthContext,
    get_current_user,
    get_human_session,
    get_optional_human_session,
    require_platform_admin,
    resolve_owner_id,
)
from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import (
    agent_email_repo,
    agent_mail_inbox_repo,
    agent_registry_repo,
    automations_repo,
    billing_repo,
    company_slug_repo,
    container_repo,
    integrations_repo,
    messaging_repo,
    questions_repo,
    tasks_repo,
    user_repo,
)
from core.services import hermes_profiles, messaging, workspace_provisioning
from core.services.credentials import integration_secret_store
from core.temporal import client as temporal_client
from core.voice import store
from core.voice.phone import phone_pin
from main import app
from tests.unit.auth.conftest import MARKER, Tenant, mentions, snapshot


@dataclass(frozen=True)
class Case:
    router: str
    method: str
    path: str  # {name}: an id from the router's seed
    body: dict | None = None  # str values are templates too
    query: dict | None = None
    a_id: bool = False  # path id: B names A's resource
    ok: tuple[int, ...] = ()  # what this door answers B, when not the default
    says: str = ""  # text B's answer must carry: the refusal is the tenant check, not a generic one
    files: bool = False  # a multipart image upload

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


async def call(who: Tenant, method: str, path: str, body=None, query=None, files=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, path, headers=who.headers, json=body, params=query, files=files)


async def ok(who: Tenant, method: str, path: str, body=None, files=None) -> dict:
    """A's own write through the door that makes it."""
    r = await call(who, method, path, body, files=files)
    assert r.status_code < 300, f"seed {method} {path}: {r.status_code} {r.text}"
    return r.json() if r.content else {}


def fill(value, ids: dict):
    if isinstance(value, str):
        return value.format(**ids)
    if isinstance(value, dict):
        return {k: fill(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, ids) for v in value]
    return value


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def fake(mp, calls, target: str, result=None):
    """Replace an external service call with one that records its arguments in `calls`."""

    async def recorded(*args, **kwargs):
        calls.append(("fake", target, {"args": args, "kwargs": kwargs}))
        return result

    mp.setattr(target, recorded)


async def put_row(item: dict) -> None:
    """A messaging-table row as the product's internal doors write it (voice_phone's waitlist and callers)."""

    await run_in_thread(messaging_repo._table().put_item, Item=item)


async def workspaces(*people: Tenant) -> None:
    """Each tenant provisioned as `provision_org` leaves it: the META row and the starter roster."""

    for t in people:
        await workspace_provisioning.provision_org(t.tenant, provisioner=t.user_id)


def ceo(t: Tenant) -> str:
    return workspace_provisioning.starter_agent_id(t.tenant, "ceo")


async def billing_rows(a: Tenant, b: Tenant, *, b_paid: bool = True) -> None:
    """A is a paying Stripe customer; B has a customer, and a live subscription when `b_paid`."""

    for t, customer in ((a, f"cus_{MARKER}"), (b, "cus_bbb")):
        await billing_repo.create_if_not_exists(t.tenant, customer)
        if t is a or b_paid:
            await billing_repo.set_subscription(owner_id=t.tenant, subscription_id=f"sub_{customer}", status="active")


async def a_runtime(a: Tenant) -> None:
    """A running workspace: B's calls must never reach it (no AWS call or request names A)."""
    await container_repo.mark_provision_error(a.tenant, last_error="", retryable=True)
    await container_repo.update_fields(a.tenant, {"status": "running", "agentcore_runtime_arn": f"arn:{MARKER}"})


# --- A's rows, per router -------------------------------------------------------------------------------


async def seed_messaging(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    room = await ok(
        a,
        "POST",
        "/api/v1/messaging/rooms",
        {"clientRoomId": str(uuid.uuid4()), "name": f"{MARKER} room", "kind": "channel", "members": []},
    )
    rid = room["roomId"]
    text = {"clientMessageId": str(uuid.uuid4()), "text": f"{MARKER} hi", "replyToMessageId": None}
    message = await ok(a, "POST", f"/api/v1/messaging/rooms/{rid}/messages", {**text, "threadRootMessageId": None})
    image = await ok(
        a, "POST", f"/api/v1/messaging/rooms/{rid}/attachments", files={"image": ("a.png", PNG, "image/png")}
    )
    run = str(uuid.uuid4())
    await messaging_repo.create_run(
        a.tenant, run, room_id=rid, agent_id=ceo(a), workspace_id=room["workspaceId"],
        trigger_message_id=message["messageId"], event_kind="user_request",
    )  # fmt: skip
    return {"room": rid, "run": run, "attachment": image["id"], "member": a.user_id}


async def seed_huddles(a, b, mp, calls) -> dict:
    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        mp.setattr(settings, name, "wss://livekit.test" if name == "LIVEKIT_URL" else "test-livekit-secret-0123456789")
    return await seed_messaging(a, b, mp, calls)


async def seed_voice(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    await store.put_voice_agent(a.tenant, {"greeting": f"{MARKER} greeting"}, a.user_id)
    await phone_pin.save(a.tenant, a.user_id, "+15550100001", "4321")
    pk = messaging_repo.owner_pk(a.tenant)
    await put_row({"pk": pk, "sk": "VOICEWAIT#2026-11-20#call_a", "id": "call_a", "name": f"{MARKER} waiter"})
    await put_row({"pk": pk, "sk": "VOICECALLER#+15550100002", "name": f"{MARKER} caller", "facts": [MARKER]})
    room = str(uuid.uuid4())  # a call, as voice_phone's start writes it
    await messaging_repo.create_room(
        owner_id=a.tenant, room_id=room, client_room_id=room, creator_user_id=a.user_id, name=f"{MARKER} call",
        kind="call", members=[], req_hash="h", extra={"call": {"caller": "+15550100002", "callerName": MARKER}},
    )  # fmt: skip
    fake(mp, calls, "core.voice.phone.voice_outbound.forward_check", {"ok": True})
    fake(mp, calls, "core.voice.phone.voice_onboarding.draft", {"business": "b"})
    fake(mp, calls, "core.voice.phone.voice_onboarding.reserve", {"path": "new"})
    return {"date": "2026-11-20", "entry": "call_a", "caller": "+15550100002", "_needles": ["+15550100001"]}


async def seed_billing(a, b, mp, calls) -> dict:
    for name in ("STRIPE_WORKSPACE_PRICE_ID", "STRIPE_FRONT_DESK_PRICE_ID", "STRIPE_VOICE_PLAN_PRICE_ID"):
        mp.setattr(settings, name, f"price_{name}")
    for name in ("METRONOME_VOICE_ENROLLMENT_ENABLED", "METRONOME_API_KEY", "METRONOME_VOICE_RATE_CARD_ID"):
        mp.setattr(settings, name, True if name.endswith("ENABLED") else "test")
    mp.setattr(settings, "METRONOME_VOICE_METRIC_ID", "test")
    mp.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_cross_account")
    await workspaces(a, b)
    await billing_rows(a, b, b_paid=False)  # B checks out: Stripe sees B's customer only
    return {}


async def seed_apple_iap(a, b, mp, calls) -> dict:
    await billing_rows(a, b, b_paid=False)
    mp.setattr(settings, "APPLE_IAP_SELLING", True)
    return {}


async def seed_link_wallet(a, b, mp, calls) -> dict:
    for name in ("LINK_CLIENT_ID", "LINK_CLIENT_SECRET", "LINK_PUBLISHABLE_KEY"):
        mp.setattr(settings, name, "test")
    await billing_rows(a, b)
    limits = {"perPurchaseCents": 100, "projectMonthlyCents": 1000, "merchants": [f"https://{MARKER}.example"]}
    await billing_repo.set_purchase_limits(a.tenant, limits)
    request = uuid.uuid4().hex
    await put_row({"pk": messaging_repo.owner_pk(a.tenant), "sk": f"PURCHASE#{request}", "status": "needs_owner",
                   "merchant": MARKER})  # fmt: skip
    return {"request": request}


async def _a_task(a: Tenant) -> str:
    row, _ = await tasks_repo.create_task(
        a.tenant,
        {
            "taskId": ts.new_task_id(), "identifier": "LB-1", "title": f"{MARKER} task", "status": "queued",
            "audience": {"kind": "person", "userIds": [a.user_id]}, "lane": "private", "ownerUserId": a.user_id,
            "triggerId": ts.trigger_id("chat", ts.new_task_id()),
        },
    )  # fmt: skip
    return row["taskId"]


def _temporal(mp, calls) -> None:
    for name in ("update_task", "start_task_workflow"):
        fake(mp, calls, f"core.temporal.client.{name}")
    mp.setattr(temporal_client, "task_handle", lambda ws, tid: AsyncMock())


async def seed_tasks(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    _temporal(mp, calls)
    return {"task": await _a_task(a)}


async def seed_questions(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    _temporal(mp, calls)
    task = await _a_task(a)
    q = {
        "taskId": task, "qid": "q-a", "kind": "approval", "subkind": "purchase", "title": f"{MARKER} buy",
        "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
        "askedBy": {"kind": "worker"}, "answerers": [a.user_id], "answererRule": "admins", "baseAnswerers": [],
    }  # fmt: skip
    await questions_repo.open_question(a.tenant, q)
    return {"task": task, "qid": "q-a"}


async def seed_automations(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    await billing_rows(a, b)  # both paid: B's webhook automation reaches the seed
    fake(mp, calls, "core.services.automation_schedule.upsert")
    fake(mp, calls, "core.services.automation_schedule.delete")
    made, _ = await automations_repo.create_schedule(  # what POST /automations writes
        a.tenant, {"scheduleId": "sched-a", "name": f"{MARKER} auto", "ownerUserId": a.user_id,
                   "policyText": f"{MARKER} do it", "cron": [], "tz": "UTC"},
    )  # fmt: skip
    hook = {
        "publicId": "hookaaaa", "scheduleId": made["scheduleId"], "provider": "stripe",
        "signingMode": "stripe", "secretRef": f"hooks/{a.tenant}/hookaaaa",
    }  # fmt: skip
    await automations_repo.put_hook(a.tenant, hook)
    return {"sid": made["scheduleId"], "hook": "hookaaaa"}


async def seed_teams_agents(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    await agent_registry_repo.upsert_fields(a.tenant, ceo(a), {"name": f"{MARKER} ceo"})
    return {"agent": ceo(a)}


async def seed_workspace_files(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    await hermes_profiles.write_profile_file(a.tenant, ceo(a), "USER.md", f"{MARKER} user")
    return {"agent": ceo(a)}


async def seed_memory(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    draft = {"id": "d-a", "user_id": a.user_id, "kind": "lesson", "text": f"{MARKER} lesson", "evidence": []}
    draft |= {"status": "proposed", "created": "2026-11-20T00:00:00+00:00", "mirrored": False}
    await put_row({"pk": f"DRAFTS#{a.user_id}", "sk": "D#d-a", **draft})
    return {"draft": "d-a"}


async def _mail(t: Tenant, slug: str) -> str:
    assert await company_slug_repo.hold(slug=slug, user_id=t.user_id)
    assert await company_slug_repo.claim(slug=slug, user_id=t.user_id, owner_id=t.tenant)
    address = f"agents@{slug}.mail.test"
    await agent_email_repo.put(address=address, owner_id=t.tenant, verified_senders=[])
    return address


async def seed_agent_mail(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    mp.setattr("routers.agent_mail._owner_email", AsyncMock(return_value=None))
    address = await _mail(a, "acme-a")
    await _mail(b, "acme-b")
    mail = await agent_mail_inbox_repo.put_message(
        address=address, owner_id=a.tenant, agent_id=ceo(a), message_id="m-a", from_addr="c@example.com",
        subject=f"{MARKER} subject", text=f"{MARKER} body", headers={}, attachments=[],
    )  # fmt: skip
    room = str(uuid.uuid4())
    transcript = messaging.store_attachment(a.tenant, room, "t.txt", "text/plain", f"{MARKER} said".encode())
    meeting = {"title": f"{MARKER} sync", "startAt": 1, "held": [{"startAt": 1, "transcriptId": str(transcript.id)}]}
    await messaging_repo.create_room(
        owner_id=a.tenant, room_id=room, client_room_id=room, creator_user_id=a.user_id, name=f"{MARKER} meet",
        kind="channel", members=[], req_hash="h", extra={"meeting": meeting},
    )  # fmt: skip
    await agent_email_repo.add_meeting(address, room)
    return {"key": agent_mail_inbox_repo.reply_key(address, mail["received_key"]), "meeting": room}


async def seed_agent_text(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    mp.setattr("core.services.photon.enabled", lambda: True)  # else a reply is "disabled" before the key is read
    team = await _mail(a, "acme-a")
    for t, number in ((a, "+15551230000"), (b, "+15551239999")):
        assert await agent_email_repo.put_pending_line(owner_id=t.tenant, agent_id=ceo(t), operation_id="op")
        await agent_email_repo.record_text_line(
            number=number, owner_id=t.tenant, agent_id=ceo(t), photon_resource_id="r", photon_user_id="u",
            operation_id="op",
        )  # fmt: skip
    text = await agent_mail_inbox_repo.put_message(
        address=team, owner_id=a.tenant, agent_id=ceo(a), message_id="t-a", from_addr="+15559870000",
        subject="", text=f"{MARKER} text", headers={}, attachments=[], channel="text",
    )  # fmt: skip
    return {
        "key": agent_mail_inbox_repo.reply_key(team, text["received_key"]),
        "_needles": ["+15551230000", "+15559870000"],
    }


async def seed_company_slug(a, b, mp, calls) -> dict:
    await _mail(a, "acme-a")  # A's claimed slug, as a claim leaves it
    await ok(a, "POST", "/api/v1/company/slug/hold", {"slug": "held-a"})
    return {}


async def seed_connections(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    await integrations_repo.put_status(a.tenant, "github", identity=f"{MARKER}-gh")
    await integration_secret_store.put(a.tenant, "aws", {"external_id": MARKER})  # what A's aws-setup mints
    fake(mp, calls, "routers.connections._validate_token", True)
    fake(mp, calls, "core.services.credentials.apply_connection.apply_connection")
    return {}


async def seed_container(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    await billing_rows(a, b)
    await a_runtime(a)
    fake(mp, calls, "core.services.provision_dispatch.dispatch_provision")
    return {}


class _NoWorkflows:
    """A Temporal client with nothing running: the self-delete's engine step terminates and deletes nothing."""

    async def list_workflows(self, query):
        return
        yield

    def get_workflow_handle(self, wid):
        return AsyncMock()


async def seed_users(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    mp.setattr(temporal_client, "_client", _NoWorkflows())
    await user_repo.put(a.user_id, f"{MARKER}@example.com")
    await user_repo.put(b.user_id, f"{b.user_id}@example.com")  # the self-delete's typed confirmation
    return {}


async def seed_os(a, b, mp, calls) -> dict:
    from core.voice import os_voice

    await ok(a, "GET", "/api/v1/os/account")  # A's Spark account
    mp.setattr(settings, "OPENAI_API_KEY", "sk-test-not-real")
    fake(mp, calls, "core.voice.os_voice._create_live", ("live_fake", "v=0\r\nanswer"))  # OpenAI
    mp.setattr(os_voice, "start_sideband", lambda sid: None)
    fake(mp, calls, "core.services.clerk_admin.delete_user")
    voice = await ok(a, "POST", "/api/v1/os/voice-session", {"sdp": f"v=0\r\n{MARKER}", "kind": "hold", "dcid": 4})
    return {"a_voice": voice["sessionId"], "_needles": [voice["sessionId"]]}


async def seed_runtime_rpc(a, b, mp, calls) -> dict:
    await a_runtime(a)
    op = str(uuid.uuid4())
    await messaging_repo.create_operation(
        owner_id=a.tenant, operation_id=op, requester_user_id=a.user_id, method="health", params={"q": MARKER}
    )
    return {"op": op}


async def seed_oauth(a, b, mp, calls) -> dict:
    """A's pending ChatGPT sign-in. The doors take no id: each reads only the caller's own row."""
    from core.services import oauth_service

    await billing_rows(a, b)  # both paid: B's start runs to OpenAI (the network fake), with B's ids only
    row = {"user_id": a.user_id, "state": "pending", "device_auth_id": MARKER, "user_code": "AAAA-AAAA"}
    await run_in_thread(oauth_service._table().put_item, Item={**row, "interval": 5, "bound_owner_id": a.tenant})
    return {}


async def seed_debug(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    await billing_rows(a, b)
    await a_runtime(a)
    fake(mp, calls, "core.services.provision_dispatch.dispatch_provision")
    return {}


async def seed_config(a, b, mp, calls) -> dict:
    """Each agent's config.yaml, as provisioning leaves it (A's carry MARKER)."""
    await workspaces(a, b)
    for t, body in ((a, f"marker: {MARKER}\n"), (b, "x: 0\n")):
        for agent in await messaging.load_agent_roster(t.tenant):
            hermes_profiles.put_owner_object(
                f"{hermes_profiles.profile_prefix(t.tenant, agent['id'])}config.yaml", body.encode()
            )
    return {}


SEEDS = {
    "messaging": seed_messaging,
    "huddles": seed_huddles,
    "voice": seed_voice,
    "billing": seed_billing,
    "apple_iap": seed_apple_iap,
    "company_link_wallet": seed_link_wallet,
    "tasks": seed_tasks,
    "questions": seed_questions,
    "automations": seed_automations,
    "teams.agents": seed_teams_agents,
    "workspace_files": seed_workspace_files,
    "memory": seed_memory,
    "agent_mail": seed_agent_mail,
    "agent_text": seed_agent_text,
    "company_slug": seed_company_slug,
    "connections": seed_connections,
    "container": seed_container,
    "users": seed_users,
    "os": seed_os,
    "runtime_rpc": seed_runtime_rpc,
    "oauth": seed_oauth,
    "debug": seed_debug,
    "config": seed_config,
}

VOICE_PLAN = {"expectedOwnerId": "{b_tenant}", "planVersion": "voice-agents-v1", "acceptMonthlyUsd": 250}
VOICE_PLAN |= {"acceptIncludedMinutes": 1500, "acceptOverageUsdPerMinute": 0.35}
MSG = {"clientMessageId": "{new}", "text": "hi", "replyToMessageId": None, "threadRootMessageId": None}

CASES = [
    # messaging: tenant = caller; a room door also requires room membership
    Case("messaging", "GET", "/api/v1/messaging/live-config"),
    Case("messaging", "GET", "/api/v1/messaging/rooms"),
    Case(
        "messaging",
        "POST",
        "/api/v1/messaging/rooms",
        {"clientRoomId": "{new}", "name": "b", "kind": "channel", "members": []},
    ),
    Case("messaging", "GET", "/api/v1/messaging/rooms/{room}", a_id=True),
    Case("messaging", "PATCH", "/api/v1/messaging/rooms/{room}", {"name": "taken"}, a_id=True),
    Case("messaging", "DELETE", "/api/v1/messaging/rooms/{room}", a_id=True),
    Case("messaging", "PATCH", "/api/v1/messaging/rooms/{room}/me", {"starred": True}, a_id=True),
    Case(
        "messaging",
        "POST",
        "/api/v1/messaging/rooms/{room}/members",
        {"kind": "human", "id": "{b}", "role": "member", "listenMode": None},
        a_id=True,
    ),
    Case("messaging", "DELETE", "/api/v1/messaging/rooms/{room}/members/human/{member}", a_id=True),
    Case("messaging", "POST", "/api/v1/messaging/rooms/{room}/leave", a_id=True),
    Case("messaging", "GET", "/api/v1/messaging/rooms/{room}/messages", a_id=True),
    Case("messaging", "POST", "/api/v1/messaging/rooms/{room}/messages", MSG, a_id=True),
    Case("messaging", "POST", "/api/v1/messaging/rooms/{room}/attachments", a_id=True, files=True),
    Case("messaging", "GET", "/api/v1/messaging/rooms/{room}/attachments/{attachment}", a_id=True),
    Case("messaging", "GET", "/api/v1/messaging/rooms/{room}/attachments/{attachment}/url", a_id=True),
    Case("messaging", "PUT", "/api/v1/messaging/rooms/{room}/read", {"lastReadSeq": 1}, a_id=True),
    Case("messaging", "POST", "/api/v1/messaging/rooms/{room}/unread", {"fromSeq": 1}, a_id=True),
    Case("messaging", "POST", "/api/v1/messaging/typing", {"roomId": "{room}", "isTyping": True}, a_id=True),
    Case("messaging", "GET", "/api/v1/messaging/runs/{run}", a_id=True, ok=(200,)),  # "terminal": a run not B's
    Case("messaging", "POST", "/api/v1/messaging/runs/{run}/abort", a_id=True),
    Case("messaging", "POST", "/api/v1/messaging/runs/{run}/steer", {"text": "go"}, a_id=True),
    # huddles: a room member of a room the caller's tenant owns
    Case("huddles", "GET", "/api/v1/huddles/{room}", a_id=True),
    Case("huddles", "POST", "/api/v1/huddles/{room}/join", a_id=True),
    # voice: tenant = caller; the owner sets the agent, number, forward check and draft
    Case("voice", "GET", "/api/v1/voice/timeline"),
    Case("voice", "GET", "/api/v1/voice/usage"),
    Case("voice", "GET", "/api/v1/voice/waitlist"),
    Case("voice", "DELETE", "/api/v1/voice/waitlist/{date}/{entry}", a_id=True),
    Case("voice", "DELETE", "/api/v1/voice/callers/{caller}/memory", a_id=True),
    Case("voice", "GET", "/api/v1/voice/phone-pin"),
    Case("voice", "PUT", "/api/v1/voice/phone-pin", {"mobile": "+15550100003", "pin": "8765"}),
    Case("voice", "GET", "/api/v1/voice/agent"),
    Case("voice", "PUT", "/api/v1/voice/agent", {"config": {"greeting": "b greeting"}}),
    Case("voice", "POST", "/api/v1/voice/forward-check", {"forwardFrom": "+15550100004"}),
    Case("voice", "POST", "/api/v1/voice/onboarding/draft", {"website": "example.com"}),
    Case("voice", "POST", "/api/v1/voice/number", {"path": "new", "areaCode": "415"}),
    # billing: tenant = caller; the owner checks out, opens the portal, cancels
    Case("billing", "GET", "/api/v1/billing/account"),
    Case("billing", "GET", "/api/v1/billing/trial-recap"),
    Case("billing", "GET", "/api/v1/billing/credits/balance"),
    Case("billing", "POST", "/api/v1/billing/portal"),
    Case("billing", "POST", "/api/v1/billing/checkout", {"plan": "workspace"}),
    Case("billing", "POST", "/api/v1/billing/front-desk", {}),
    Case("billing", "GET", "/api/v1/billing/voice-plan"),
    Case("billing", "POST", "/api/v1/billing/voice-plan/checkout", VOICE_PLAN),
    Case("billing", "DELETE", "/api/v1/billing/voice-plan", query={"expectedOwnerId": "{b_tenant}"}),
    # apple_iap: tenant = caller (selling is off in prod)
    Case("apple_iap", "POST", "/api/v1/billing/apple/purchase-intent"),
    Case(
        "apple_iap",
        "POST",
        "/api/v1/billing/apple/link",
        {"signed_transaction": "not-a-jws"},
        ok=(503,),
    ),  # no Apple keys in tests: the org case proves only that nothing of A's moves
    # company_link_wallet: tenant = caller
    Case("company_link_wallet", "GET", "/api/v1/company/link-wallet/status"),
    Case("company_link_wallet", "POST", "/api/v1/company/link-wallet/connect"),
    Case("company_link_wallet", "POST", "/api/v1/company/link-wallet/disconnect"),
    Case("company_link_wallet", "GET", "/api/v1/company/link-wallet/limits"),
    Case(
        "company_link_wallet",
        "PUT",
        "/api/v1/company/link-wallet/limits",
        {"perPurchaseCents": 5, "projectMonthlyCents": 9},
    ),
    Case("company_link_wallet", "GET", "/api/v1/company/link-wallet/purchases"),
    Case(
        "company_link_wallet",
        "POST",
        "/api/v1/company/link-wallet/purchases/{request}/approval-link",
        a_id=True,
    ),
    # tasks: tenant = caller; the owner exports
    Case("tasks", "GET", "/api/v1/tasks"),
    Case("tasks", "GET", "/api/v1/tasks/outputs"),
    Case("tasks", "GET", "/api/v1/tasks/export"),
    Case("tasks", "GET", "/api/v1/tasks/{task}", a_id=True),
    Case("tasks", "POST", "/api/v1/tasks/{task}/read", a_id=True),
    Case("tasks", "POST", "/api/v1/tasks/{task}/stop", {"version": 1}, a_id=True),
    Case("tasks", "GET", "/api/v1/tasks/{task}/trace", a_id=True),
    # questions: tenant = caller
    Case("questions", "GET", "/api/v1/needs-you"),
    Case("questions", "GET", "/api/v1/tasks/{task}/questions", a_id=True),
    Case(
        "questions",
        "POST",
        "/api/v1/tasks/{task}/questions/{qid}/answer",
        {"optionId": "allow_once", "clientAnswerId": "c-1"},
        a_id=True,
    ),
    # automations: tenant = caller; the owner wires triggers
    Case("automations", "GET", "/api/v1/automations"),
    Case("automations", "POST", "/api/v1/automations", {"name": "b", "instructions": "b"}),
    Case(
        "automations",
        "POST",
        "/api/v1/automations/from-integration",
        {"provider": "stripe", "event_ids": ["invoice.payment_failed"]},
    ),
    Case(
        "automations",
        "PUT",
        "/api/v1/automations/{hook}/webhook-secret",
        {"webhook_secret": "whsec_b"},
        a_id=True,
    ),
    Case("automations", "GET", "/api/v1/automations/{sid}", a_id=True),
    Case("automations", "GET", "/api/v1/automations/{sid}/runs", a_id=True),
    Case("automations", "PATCH", "/api/v1/automations/{sid}", {"version": 1, "name": "taken"}, a_id=True),
    Case("automations", "POST", "/api/v1/automations/{sid}/activate", a_id=True),
    Case("automations", "POST", "/api/v1/automations/{sid}/resume", a_id=True),
    Case("automations", "POST", "/api/v1/automations/{sid}/pause", a_id=True),
    Case("automations", "POST", "/api/v1/automations/{sid}/run", a_id=True),
    Case("automations", "DELETE", "/api/v1/automations/{sid}", a_id=True),
    # teams.agents: tenant = caller
    Case("teams.agents", "GET", "/api/v1/teams/agents"),
    Case("teams.agents", "GET", "/api/v1/teams/members"),
    Case("teams.agents", "GET", "/api/v1/teams/agents/{agent}", a_id=True),
    Case("teams.agents", "PATCH", "/api/v1/teams/agents/{agent}", {"name": "taken"}, a_id=True),
    Case("teams.agents", "GET", "/api/v1/teams/agents/{agent}/voice-notes", a_id=True),
    Case("teams.agents", "PUT", "/api/v1/teams/agents/{agent}/voice-notes", {"notes": "taken"}, a_id=True),
    # workspace_files: tenant = caller. The profile is read and written under the CALLER's tenant, so A's agent id
    # names an empty profile under B (200, nothing of A's): the agent is not checked against B's roster (reported).
    Case("workspace_files", "GET", "/api/v1/container/workspace/{agent}/tree", a_id=True, ok=(200,)),
    Case("workspace_files", "GET", "/api/v1/container/workspace/{agent}/file", query={"path": "USER.md"}, a_id=True),
    Case("workspace_files", "GET", "/api/v1/container/workspace/{agent}/config-files", a_id=True, ok=(200,)),
    Case(
        "workspace_files",
        "GET",
        "/api/v1/container/workspace/{agent}/config-file",
        query={"path": "USER.md"},
        a_id=True,
    ),
    Case(
        "workspace_files",
        "PUT",
        "/api/v1/container/workspace/{agent}/file",
        {"path": "USER.md", "content": "taken", "tab": "config"},
        a_id=True,
        ok=(200,),
    ),
    # memory: tenant = caller
    Case("memory", "POST", "/api/v1/memory/recall", {"question": "what"}),
    # agent_text: tenant = caller
    Case("agent_text", "GET", "/api/v1/agent-text/line"),
    Case("agent_text", "POST", "/api/v1/agent-text/me/phone/code"),
    Case("agent_text", "DELETE", "/api/v1/agent-text/me/phone"),
    Case(
        "agent_text",
        "POST",
        "/api/v1/agent-text/reply",
        {"key": "{key}", "text": "hi"},
        a_id=True,
        ok=(200,),
        says="bad_reply_key",
    ),
    Case("agent_text", "POST", "/api/v1/agent-text/thread", {"key": "{key}", "mode": "human"}, a_id=True),
    # agent_mail: tenant = caller; mail keys and transcripts are read at the caller's own team address
    Case("agent_mail", "GET", "/api/v1/agent-mail/address"),  # an org's read creates its mailbox; a person's reads
    Case("agent_mail", "PATCH", "/api/v1/agent-mail/address", {"send_to_anyone": False}),
    Case("agent_mail", "GET", "/api/v1/agent-mail/inbox"),
    Case("agent_mail", "GET", "/api/v1/agent-mail/inbox/new", query={"since": "2026-01-01T00:00:00+00:00"}),
    Case("agent_mail", "GET", "/api/v1/agent-mail/message", query={"key": "{key}"}, a_id=True),
    Case("agent_mail", "GET", "/api/v1/agent-mail/meetings/{meeting}/transcript", query={"start_at": 1}, a_id=True),
    # company_slug: a claim moves only the caller's own hold
    Case("company_slug", "GET", "/api/v1/company/slug-availability", query={"slug": "acme-b"}),
    Case("company_slug", "POST", "/api/v1/company/slug/hold", {"slug": "acme-a"}, a_id=True, ok=(409,)),  # taken
    Case("company_slug", "POST", "/api/v1/company/slug/claim", {"slug": "held-a", "name": "B"}, a_id=True, ok=(409,)),
    Case("company_slug", "POST", "/api/v1/company/source", {"source": "web"}),
    # container: tenant = caller, an org or a person alike; the owner reads status, provisions and retries. B's own
    # answers: B's gate wants credits (402), B's own provision starts (200), B has no container to retry (404).
    Case("container", "GET", "/api/v1/container/status", ok=(402,)),
    Case("container", "POST", "/api/v1/container/provision", ok=(200,)),
    Case("container", "POST", "/api/v1/container/retry", ok=(404,)),
    Case("container", "POST", "/api/v1/browser/viewing", {}, ok=(402,)),
    Case("container", "POST", "/api/v1/browser/input", {"type": "key", "key": "a"}, ok=(409,)),  # no live page
    # connections: tenant = caller; the owner manages connections
    Case("connections", "GET", "/api/v1/connections"),
    Case("connections", "POST", "/api/v1/connections/parallel", {"token": "b"}),
    Case("connections", "DELETE", "/api/v1/connections/github"),
    Case(
        "connections", "GET", "/api/v1/connections/github/authorize", ok=(503,)
    ),  # no GitHub OAuth app in tests: the org case proves only that nothing of A's moves
    Case("connections", "GET", "/api/v1/connections/aws/aws-setup"),
    Case(
        "connections",
        "POST",
        "/api/v1/connections/aws/aws-complete",
        {"role_arn": "arn:aws:iam::123456789012:role/lightbulb-connector"},
        ok=(409,),
        says="aws-setup",
    ),  # A ran aws-setup, B did not: A's external id is not B's
    Case("connections", "PUT", "/api/v1/connections/x/byo-app", {"client_id": "b", "client_secret": "b"}),
    Case("connections", "DELETE", "/api/v1/connections/x/byo-app"),
    # oauth: tenant = caller; the ChatGPT binding belongs to the tenant that started it
    Case("oauth", "GET", "/api/v1/oauth/chatgpt/status"),
    Case("oauth", "POST", "/api/v1/oauth/chatgpt/start", ok=(502,), says="usercode"),  # OpenAI is the network fake
    Case("oauth", "POST", "/api/v1/oauth/chatgpt/poll", ok=(502,), says="user_b"),  # B's own row only: none
    Case("oauth", "POST", "/api/v1/oauth/chatgpt/disconnect"),
    # debug: 403 in prod; tenant = caller in dev
    Case("debug", "GET", "/api/v1/debug/ddb-rows", query={"owner_id": "{a_tenant}"}, a_id=True),
    Case("debug", "POST", "/api/v1/debug/provision"),
    Case("debug", "PATCH", "/api/v1/debug/provision", ok=(404,)),  # B has no container; A's is untouched
    Case("debug", "DELETE", "/api/v1/debug/provision"),
    Case("debug", "DELETE", "/api/v1/debug/user-data"),
    # config: tenant = caller
    Case("config", "GET", "/api/v1/config"),
    Case("config", "PATCH", "/api/v1/config", {"patch": {"x": 1}}),
    # users: the caller's own row; self-delete wipes only the caller
    Case("users", "POST", "/api/v1/users/sync"),
    Case("users", "GET", "/api/v1/users/me"),
    Case("users", "GET", "/api/v1/users/me/prefs"),
    Case("users", "PUT", "/api/v1/users/me/prefs", {"timezone": "Europe/London"}),
    Case("users", "POST", "/api/v1/users/me/delete", {"confirm_email": "{b}@example.com"}),
    # os: the caller's own Spark account; a referral marks only the caller (A's own code would credit A,
    # which is the point of a code, so test_os covers that one)
    Case("os", "GET", "/api/v1/os/account"),
    Case("os", "DELETE", "/api/v1/os/account"),  # B's own Spark data and Clerk user only
    Case("os", "POST", "/api/v1/os/referral", {"code": "nobody"}),
    Case("os", "POST", "/api/v1/os/voice-session", {"sdp": "v=0\r\n", "kind": "hold", "dcid": 4}),  # B's own
    Case("os", "GET", "/api/v1/os/voice-session/{a_voice}", a_id=True),
    Case("os", "DELETE", "/api/v1/os/voice-session/{a_voice}", a_id=True),
    Case("os", "POST", "/api/v1/os/event", {"event": "app_opened", "installId": "{new}"}),  # B's own install
    # support: the caller's own
    Case("support_tickets", "POST", "/api/v1/support/tickets", {"message": "help"}),
    # runtime_rpc: tenant = caller
    Case(
        "runtime_rpc",
        "POST",
        "/api/v1/runtime/rpc",
        {"requestId": "{new}", "method": "health", "params": {}},
        ok=(503,),
    ),  # B has no runtime
    Case("runtime_rpc", "GET", "/api/v1/runtime/operations/{op}", a_id=True),
    # main: the signed-in person
    Case("main", "GET", "/protected"),
]


# Routers with tenant doors and no seed for A, each with why there is nothing of A's to seed.
NO_SEED = {
    "main": "/protected echoes the caller's own id",
    "support_tickets": "one POST: it reads the caller's own billing and container rows, emails support and stores "
    "nothing; it takes no id",
}


@pytest.mark.parametrize("case", [pytest.param(c, id=f"{c.router} {c}") for c in CASES])
async def test_cross_account(case, tenants, calls, ddb, monkeypatch):
    a, b = tenants
    seeded = await SEEDS[case.router](a, b, monkeypatch, calls) if case.router in SEEDS else {}
    ids = {"b": b.user_id, "b_tenant": b.tenant, "a_tenant": a.tenant, "new": str(uuid.uuid4()), "agent": ceo(a)}
    ids |= seeded
    needles = {MARKER, a.user_id, a.tenant, *seeded.get("_needles", [])}
    before = snapshot(ddb, *needles)
    calls.clear()
    files = {"image": ("b.png", PNG, "image/png")} if case.files else None
    path, query = fill(case.path, ids), fill(case.query, ids)
    r = await call(b, case.method, path, fill(case.body, ids), query, files)
    out = [c for c in calls if case.a_id is False or c[0] != "aws"]  # a path id is looked up; nothing else may name A
    seen = json.dumps(out, default=str)
    assert snapshot(ddb, *needles) == before, "B's call changed A's rows"
    expected = case.ok or ((403, 404) if case.a_id else tuple(range(200, 400)))
    assert r.status_code in expected, f"{r.status_code} {r.text[:400]}"
    assert case.says in r.text, r.text[:400]
    reply = r.text
    for sent in {*path.split("/"), *map(str, (query or {}).values())} & needles:
        reply = reply.replace(sent, "")  # a refusal may echo the id B sent, and only that
    named = mentions(reply + seen, needles)
    assert not named, f"B's call named A ({named}): {r.text[:200]} {seen[:400]}"


def test_every_router_seeds_a():
    """A router whose doors find nothing of A's proves nothing: each has a seed, or a named reason."""
    routers = {c.router for c in CASES}
    assert routers - SEEDS.keys() == NO_SEED.keys(), sorted(routers - SEEDS.keys() ^ NO_SEED.keys())


# --- the meta-test: every door has a case -------------------------------------------------------------


def _tenant_doors() -> set[tuple[str, str]]:
    """(method, path) of every route whose dependencies reach a Clerk sign-in, platform-admin doors excepted
    (they read across tenants by design and are guarded by PLATFORM_ADMIN_USER_IDS)."""
    from routers import questions

    # person_only and get_optional_human_session call one of them in their body
    signed_in = {get_current_user, get_human_session, get_optional_human_session, questions.person_only}

    def deps(d):
        for s in d.dependencies:
            yield s.call
            yield from deps(s)

    def walk(routes):
        for r in routes:
            yield from walk(r.effective_candidates()) if isinstance(r, _IncludedRouter) else [r]

    doors = set()
    for r in walk(app.routes):
        calls = set(deps(r.dependant)) if getattr(r, "dependant", None) else set()
        if calls & signed_in and require_platform_admin not in calls:
            doors |= {(m, r.path) for m in r.methods}
    return doors


def _door_of(case: Case, doors: set[tuple[str, str]]) -> tuple[str, str] | None:
    """The one door a case calls: a case path carries values where the route has parameters
    (`/tasks/{task}/stop` is `/tasks/{task_id}/{action}`); the route with the most literal segments wins
    (`/tasks/{task}/read` is `/tasks/{task_id}/read`, not `{action}`)."""
    c = case.path.strip("/").split("/")

    def literal(path: str) -> int | None:
        r = path.strip("/").split("/")
        if len(r) != len(c) or not all(x == y or x.startswith("{") for x, y in zip(r, c)):
            return None
        return sum(not x.startswith("{") for x in r)

    scored = [(literal(p), (m, p)) for m, p in doors if m == case.method and literal(p) is not None]
    return max(scored)[1] if scored else None


def test_every_tenant_door_has_a_case():
    doors = _tenant_doors()
    called = {str(c): _door_of(c, doors) for c in CASES}
    missing = doors - set(called.values())
    assert not missing, f"tenant doors with no cross-account case: {sorted(missing)}"
    stale = [c for c, door in called.items() if door is None]
    assert not stale, f"cases for doors that are gone: {stale}"


# --- the rule itself ------------------------------------------------------------------------------------


async def test_live_config_cross_account(tenants, calls, ddb, monkeypatch):
    """B's live config is B's workspace and stream, never A's, and enrolls B in no channel of A's."""
    a, b = tenants
    await workspaces(a, b)
    mine = await ok(a, "GET", "/api/v1/messaging/live-config")
    theirs = await ok(b, "GET", "/api/v1/messaging/live-config")
    assert theirs["workspaceId"] != mine["workspaceId"]
    assert theirs["userChannelKey"] != mine["userChannelKey"]
    assert not mentions(" ".join(snapshot(ddb, f"WORKSPACE#{mine['workspaceId']}")), [b.user_id])


def test_resolve_owner_id_is_sub():
    """The tenant is a session token's person; a context that is not a first-party session has no tenant."""
    assert resolve_owner_id(AuthContext(user_id="user_x", human_session=True)) == "user_x"
    assert resolve_owner_id(AuthContext("user_x", verified_issuer="i", human_session=True)) == "user_x"
    with pytest.raises(HTTPException) as refused:
        resolve_owner_id(AuthContext(user_id="user_x"))
    assert refused.value.status_code == 403 and refused.value.detail == {"code": "session_required"}


@pytest.mark.parametrize("role", ["admin", None])
async def test_org_claim_is_ignored(role, ddb, jwks, calls):
    """A session token whose `o.id` names another person's tenant (with a role in the claim, or without) acts on its
    own `sub` and cannot see A's rows (Review Focus 3)."""
    a, b = Tenant("user_a", None), Tenant("user_b", "user_a", role=role)
    await workspaces(a, b)
    await ok(
        a,
        "POST",
        "/api/v1/messaging/rooms",
        {"clientRoomId": str(uuid.uuid4()), "name": f"{MARKER} room", "kind": "channel", "members": []},
    )
    ctx = await get_current_user(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=b.headers["Authorization"][7:])
    )
    assert resolve_owner_id(ctx) == b.user_id
    mine = await ok(a, "GET", "/api/v1/messaging/live-config")
    theirs = await ok(b, "GET", "/api/v1/messaging/live-config")
    assert theirs["workspaceId"] != mine["workspaceId"]
    rooms = await ok(b, "GET", "/api/v1/messaging/rooms")
    assert MARKER not in json.dumps(rooms)


@pytest.mark.parametrize("token", ["oauth", "template"])
async def test_non_session_token_is_refused_at_a_tenant_door(token, ddb, jwks, calls):
    """An OAuth access token or a JWT-template token, even carrying an org claim, has no tenant: 403, nothing written."""
    a, b = Tenant("user_a", None), Tenant("user_b", "user_a", token=token)
    await workspaces(a, b)
    before = snapshot(ddb, a.user_id, b.user_id)
    r = await call(
        b,
        "POST",
        "/api/v1/messaging/rooms",
        {"clientRoomId": str(uuid.uuid4()), "name": "x", "kind": "channel", "members": []},
    )
    assert r.status_code == 403 and r.json()["detail"] == {"code": "session_required"}
    assert snapshot(ddb, a.user_id, b.user_id) == before


# The doors that still refuse a person on their own tenant, and what they answer: the ChatGPT sign-in needs the
# paid workspace plan (402, exactly as for an unpaid org); the bodyless number door would hand any sign-up the
# environment's one shared line (402: a person's number comes with the voice plan, at Go live).
REFUSED = [
    ("POST", "/api/v1/oauth/chatgpt/start", None, 402),
    ("POST", "/api/v1/voice/number", {}, 402),  # no body: the shared line; a person's number comes with the voice plan
]


@pytest.mark.parametrize(("method", "path", "body", "status"), REFUSED, ids=[f"{m} {p}" for m, p, _, _ in REFUSED])
async def test_no_org_caller_reads_own_empty_data_and_is_refused_only_where_listed(
    method, path, body, status, ddb, calls, jwks, monkeypatch
):
    """A session with no org is the person's tenant on every door, and may provision it (see the container cases).
    It reads its own, empty data; the doors in REFUSED refuse it, and nothing is written or sent for it."""
    for name in ("LINK_CLIENT_ID", "LINK_CLIENT_SECRET", "LINK_PUBLISHABLE_KEY"):  # the wallet doors exist
        monkeypatch.setattr(settings, name, "test")
    person = Tenant("user_solo", None)
    timeline = await ok(person, "GET", "/api/v1/voice/timeline")
    assert not any(v for v in timeline.values() if isinstance(v, list))
    assert (await ok(person, "GET", "/api/v1/tasks"))["tasks"] == []
    calls.clear()
    room = await ok(person, "POST", "/api/v1/messaging/rooms", {"clientRoomId": str(uuid.uuid4()), "name": "mine",
                                                               "kind": "channel", "members": []})  # fmt: skip
    ids = {"b_tenant": person.user_id, "room": room["roomId"], "new": str(uuid.uuid4())}
    r = await call(person, method, fill(path, ids), fill(body, ids))
    assert r.status_code == status, r.text[:300]
    assert not [c for c in calls if c[0] in ("stripe", "http", "fake")]
    assert await container_repo.get_by_owner_id(person.user_id) is None
    assert await billing_repo.get_by_owner_id(person.user_id) is None
    assert await company_slug_repo.get_by_owner(person.user_id) is None  # no company slug
    assert await agent_email_repo.query_by_owner(person.user_id) == []  # no mailbox
    assert await integration_secret_store.get(person.user_id, "aws") is None  # no ExternalId


# --- the Phone setup doors a person passes on their own tenant ----------------------------------------------


async def test_person_mail_address_reads_none_claimed_and_mints_nothing(ddb, calls, jwks):
    """A person's address read never makes a slug or mailbox: before the claim it is `none_claimed`; a claimed slug
    with no mailbox row is still `none_claimed` (the claim door makes the row); with the row it is theirs."""
    person = Tenant("user_solo", None)
    none = {"address": None, "invite_address": None, "status": "none_claimed"}
    assert await ok(person, "GET", "/api/v1/agent-mail/address") == none
    assert await company_slug_repo.get_by_owner(person.user_id) is None
    assert await company_slug_repo.hold(slug="solo-co", user_id=person.user_id)
    assert await company_slug_repo.claim(slug="solo-co", user_id=person.user_id, owner_id=person.user_id)
    assert await ok(person, "GET", "/api/v1/agent-mail/address") == none
    assert await agent_email_repo.query_by_owner(person.user_id) == []
    await agent_email_repo.put(address="agents@solo-co.mail.test", owner_id=person.user_id, verified_senders=[])
    assert (await ok(person, "GET", "/api/v1/agent-mail/address"))["invite_address"] == "agents@solo-co.mail.test"
    assert not [c for c in calls if c[0] in ("stripe", "http", "fake")]


async def test_go_live_buys_only_with_a_voice_plan(tenants, calls, monkeypatch):
    """Where numbers are bought, Go live with no voice plan is 402 for a person as for an org: nothing is bought."""
    _, b = tenants
    monkeypatch.setattr(settings, "VOICE_BUY_NUMBERS", True)
    await workspaces(b)
    calls.clear()
    r = await call(b, "PUT", "/api/v1/voice/agent", {"config": {"live": True}})
    assert r.status_code == 402 and r.json()["detail"]["code"] == "voice_payment_required", r.text[:300]
    assert not [c for c in calls if c[0] in ("stripe", "http", "fake")]
    assert not (await store.get_voice_agent(b.tenant) or {}).get("live")


async def test_go_live_cannot_take_another_tenants_line(tenants, ddb, calls, monkeypatch):
    """Where numbers are not bought, Go live gives the one shared line to the first tenant; B gets 409 and A's line
    and Agent page are unchanged."""
    a, b = tenants
    monkeypatch.setattr(settings, "VOICE_BUY_NUMBERS", False)
    monkeypatch.setattr(settings, "VOICE_LINE_NUMBER", "+15550100009")
    await workspaces(a, b)
    assert (await ok(a, "PUT", "/api/v1/voice/agent", {"config": {"live": True}}))["config"]["number"]
    before = snapshot(ddb, a.tenant, a.user_id, "+15550100009")
    r = await call(b, "PUT", "/api/v1/voice/agent", {"config": {"live": True}})
    assert r.status_code == 409 and r.json()["detail"] == {"code": "number_unavailable", "reason": "taken"}
    assert snapshot(ddb, a.tenant, a.user_id, "+15550100009") == before
    assert not (await store.get_voice_agent(b.tenant) or {}).get("live")


# --- the website read and the voice plan a person passes on their own tenant ---------------------------------


def _bedrock(seen: list):
    """Bedrock's converse, answering one Agent page draft and recording each call."""

    class Client:
        def converse(self, **kwargs):
            seen.append(kwargs)
            text = json.dumps({"staff": ["Ana"], "phone": "", "toAdd": ["Parking?"]})
            return {"output": {"message": {"content": [{"text": text}]}}}

    return Client()


async def test_person_draft_reads_a_public_site_and_refuses_a_private_one(ddb, calls, jwks, monkeypatch):
    """A person's website read runs the repo's SSRF-safe fetcher and one model call, as for an org; a private,
    loopback or link-local address, or a redirect to one, is never fetched and makes no model call."""
    from core.services import link_preview
    from core.voice.phone import voice_onboarding

    fetched, model = [], []

    def site(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        return httpx.Response(200, html="<p>Ana cuts hair</p>", headers={"content-type": "text/html"})

    monkeypatch.setattr(link_preview, "_TRANSPORT", httpx.MockTransport(site))
    monkeypatch.setattr(voice_onboarding, "_bedrock", lambda: _bedrock(model))
    person = Tenant("user_solo", None)
    draft = (await ok(person, "POST", "/api/v1/voice/onboarding/draft", {"website": "http://1.1.1.1/"}))["draft"]
    assert draft["read"] is True and draft["staff"] == ["Ana"] and len(model) == 1
    assert fetched == ["http://1.1.1.1/"]
    for private in ("http://169.254.169.254/latest", "http://127.0.0.1/", "http://10.0.0.1/", "http://1.1.1.1/moved"):
        fetched.clear()
        draft = (await ok(person, "POST", "/api/v1/voice/onboarding/draft", {"website": private}))["draft"]
        assert draft["read"] is False, private
        assert all(urlsplit(u).hostname == "1.1.1.1" for u in fetched), fetched  # only the public hop, never 127/8
    assert len(model) == 1


async def test_person_draft_is_one_read_at_a_time_per_tenant(ddb, calls, jwks, monkeypatch):
    """While a tenant's website read runs, a second for the same tenant is 409 `draft_in_progress` and fetches
    nothing; another tenant's read is not held up, and the tenant reads again once the first ends."""
    fake(monkeypatch, calls, "core.voice.phone.voice_onboarding.draft", {"read": True})
    person, other = Tenant("user_solo", None), Tenant("user_other", None)
    token = await container_repo.try_claim_owner_lifecycle(f"voice-draft#{person.user_id}", stale_s=120)
    calls.clear()
    r = await call(person, "POST", "/api/v1/voice/onboarding/draft", {"website": "example.com"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "draft_in_progress"
    assert not [c for c in calls if c[0] == "fake"]
    assert (await ok(other, "POST", "/api/v1/voice/onboarding/draft", {"website": "example.com"}))["draft"]
    await container_repo.release_owner_lifecycle_claim(f"voice-draft#{person.user_id}", token=token)
    assert (await ok(person, "POST", "/api/v1/voice/onboarding/draft", {"website": "example.com"}))["draft"]


async def test_person_who_opens_checkout_and_never_pays_keeps_the_free_trial(ddb, calls, jwks, monkeypatch):
    """A person whose first billing action is the voice-plan checkout gets the tenant's trial row (unarmed) with the
    Stripe customer joined to it; with no webhook (they never pay) it stays the free trial."""
    await seed_billing(Tenant("user_a", None), Tenant("user_b", None), monkeypatch, calls)  # the plan is sold
    person = Tenant("user_solo", None)
    plan = fill(VOICE_PLAN, {"b_tenant": person.user_id})
    for _ in range(2):
        assert (await ok(person, "POST", "/api/v1/billing/voice-plan/checkout", plan))["status"] == "checkout"
    row = await billing_repo.get_by_owner_id(person.user_id, consistent_read=True)
    assert row["subscription_status"] == "trial_local" and "trial_end" not in row and row["stripe_customer_id"]
    assert "owner_type" not in row
    assert (await ok(person, "GET", "/api/v1/billing/voice-plan"))["status"] == "not_enrolled"


def _voice_sub(sub_id: str, customer: str, owner: str, **extra) -> dict:
    return {
        "id": sub_id, "object": "subscription", "customer": customer, "status": "active",
        "metadata": {"purpose": "voice_plan", "workspace_id": owner, "accepted_by": owner, "plan_version": "x"},
        "items": {"data": [{"price": {"id": settings.STRIPE_VOICE_PLAN_PRICE_ID}}]}, **extra,
    }  # fmt: skip


async def _webhook(event: dict, monkeypatch) -> None:
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda body, sig, secret: event)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v1/billing/webhooks/stripe", content="{}", headers={"stripe-signature": "x"})
    assert r.status_code == 200, r.text[:300]


def _stripe_posts(calls: list, path: str) -> list[dict]:
    """The form bodies of the Stripe POSTs to `path`, each as {field: value}."""
    return [
        {k: v[0] for k, v in parse_qs(c[2] or "").items()}
        for c in calls
        if c[0] == "stripe" and c[1].startswith("post ") and urlsplit(c[1][5:]).path == path
    ]


async def test_person_buys_goes_live_and_cancels_their_own_voice_plan_and_spark_is_untouched(
    ddb, calls, jwks, monkeypatch
):
    """A person checks out the voice plan on a Stripe customer of their own tenant (never Spark's), the plan's
    webhook enrolls them, Go live buys exactly one number, the portal and the cancel reach only that customer and
    subscription; their Spark subscription row is never written, and Spark's own webhook leaves the plan alone."""
    from core.billing import voice_plan
    from core.services import provision_dispatch
    from core.voice.phone import voice_onboarding as vo
    from tests.unit.services.test_voice_onboarding import NEW, _buying, _fake_twilio

    await seed_billing(Tenant("user_a", None), Tenant("user_b", None), monkeypatch, calls)  # the plan is sold
    monkeypatch.setattr(settings, "STRIPE_SPARK_PRICE_ID", "price_spark")
    person = Tenant("user_solo", None)
    await workspaces(person)
    users = dynamodb.get_table("users")
    spark = {"user_id": person.user_id, "os_trial_ends_at": 4102444800, "os_stripe_customer_id": "cus_spark",
             "os_subscription_id": "sub_spark", "os_subscription_status": "active"}  # fmt: skip
    await run_in_thread(users.put_item, Item=spark)

    def spark_row():
        return users.get_item(Key={"user_id": person.user_id})["Item"]

    plan = {**fill(VOICE_PLAN, {"b_tenant": person.user_id})}
    wrong = await call(person, "POST", "/api/v1/billing/voice-plan/checkout", {**plan, "expectedOwnerId": "user_a"})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "workspace_changed"
    calls.clear()
    assert (await ok(person, "POST", "/api/v1/billing/voice-plan/checkout", plan))["status"] == "checkout"
    (customer,) = _stripe_posts(calls, "/v1/customers")
    assert customer["metadata[workspace_id]"] == person.user_id
    (session,) = _stripe_posts(calls, "/v1/checkout/sessions")
    account = await billing_repo.get_by_owner_id(person.user_id, consistent_read=True)
    assert account["subscription_status"] == "trial_local" and "trial_end" not in account  # the trial, unarmed
    voice_customer = account["stripe_customer_id"]
    assert voice_customer != "cus_spark" and session["customer"] == voice_customer
    assert session["metadata[workspace_id]"] == person.user_id and session["metadata[purpose]"] == "voice_plan"
    assert not mentions(json.dumps(calls, default=str), ["cus_spark", "sub_spark"]) and spark_row() == spark

    # Stripe's side of the plan's webhook: the subscription, and Metronome's usage contract for it.
    subs = {"sub_voice": _voice_sub("sub_voice", voice_customer, person.user_id)}
    subs["sub_spark"] = {"id": "sub_spark", "object": "subscription", "customer": "cus_spark", "status": "active",
                         "metadata": {"product": "spark", "user_id": person.user_id}, "items": {"data": []}}  # fmt: skip

    def modify(sub_id, **kwargs):
        subs[sub_id] |= {"cancel_at_period_end": True, "cancel_at": 4102444800}
        return subs[sub_id]

    async def enroll(owner_id, acct, client):
        await billing_repo.set_voice_plan(owner_id, customer_id="m_cus", contract_id="m_contract", usage_key="k",
                                          started_at="2026-11-01T00:00:00+00:00", billing_anchor="2026-11-01")  # fmt: skip

    monkeypatch.setattr(stripe.Subscription, "retrieve", lambda sub_id, **k: subs[sub_id])
    monkeypatch.setattr(stripe.Subscription, "modify", modify)
    monkeypatch.setattr(voice_plan, "_enroll", enroll)
    monkeypatch.setattr(provision_dispatch, "dispatch_reconcile", AsyncMock(return_value=True))
    event = {"id": "evt_voice", "type": "customer.subscription.created", "data": {"object": subs["sub_voice"]}}
    await _webhook(event, monkeypatch)
    assert (await ok(person, "GET", "/api/v1/billing/voice-plan"))["status"] == "active" and spark_row() == spark
    assert (await billing_repo.get_by_owner_id(person.user_id))["subscription_status"] == "trial_local"

    paid_row = billing_repo.get_by_owner_id
    _buying(monkeypatch, True)
    monkeypatch.setattr(vo.billing_repo, "get_by_owner_id", paid_row)  # the real row the webhook made paid
    twilio = _fake_twilio(monkeypatch)
    live = {"live": True, "numberChoice": {"path": "new", "areaCode": "610"}}
    config = (await ok(person, "PUT", "/api/v1/voice/agent", {"config": live}))["config"]
    assert config["number"] == NEW
    assert (await ok(person, "PUT", "/api/v1/voice/agent", {"config": config}))["config"]["number"] == NEW
    assert [c.args for c in twilio.await_args_list].count(("POST", "IncomingPhoneNumbers.json")) == 1

    before = await billing_repo.get_by_owner_id(person.user_id, consistent_read=True)
    spark_event = {"id": "evt_spark", "type": "customer.subscription.updated", "data": {"object": subs["sub_spark"]}}
    await _webhook(spark_event, monkeypatch)
    assert await billing_repo.get_by_owner_id(person.user_id, consistent_read=True) == before
    assert spark_row()["os_subscription_id"] == "sub_spark"

    calls.clear()
    assert (await ok(person, "POST", "/api/v1/billing/portal"))["portal_url"]
    (portal,) = _stripe_posts(calls, "/v1/billing_portal/sessions")
    assert portal["customer"] == voice_customer
    bad = await call(person, "DELETE", "/api/v1/billing/voice-plan", query={"expectedOwnerId": "user_a"})
    assert bad.status_code == 409 and not subs["sub_voice"].get("cancel_at_period_end")
    r = await call(person, "DELETE", "/api/v1/billing/voice-plan", query={"expectedOwnerId": person.user_id})
    assert r.status_code == 200 and r.json()["status"] == "canceling", r.text[:300]
    assert subs["sub_voice"]["cancel_at_period_end"] and "cancel_at_period_end" not in subs["sub_spark"]
    assert spark_row() == {**spark} and not mentions(json.dumps(calls, default=str), ["cus_spark", "sub_spark"])


async def test_voice_plan_doors_refuse_another_tenants_expected_owner(tenants, ddb, calls, monkeypatch):
    """B naming A's tenant as `expectedOwnerId` is refused at checkout and cancel (409 workspace_changed): no Stripe
    call is made and A's rows are unchanged, for a person as for an org."""
    a, b = tenants
    await seed_billing(a, b, monkeypatch, calls)
    before = snapshot(ddb, MARKER, a.user_id, a.tenant)
    calls.clear()
    plan = fill(VOICE_PLAN, {"b_tenant": a.tenant})
    for method, path, body, query in (
        ("POST", "/api/v1/billing/voice-plan/checkout", plan, None),
        ("DELETE", "/api/v1/billing/voice-plan", None, {"expectedOwnerId": a.tenant}),
    ):
        r = await call(b, method, path, body, query)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "workspace_changed", r.text[:300]
    assert not [c for c in calls if c[0] == "stripe"]
    assert snapshot(ddb, MARKER, a.user_id, a.tenant) == before


# --- a person with no organization uses the product end to end ------------------------------------------------


async def test_person_provisions_posts_and_dispatches_only_on_their_own_tenant(ddb, calls, jwks, monkeypatch):
    """A person with no org provisions their tenant (the provision runner's own steps; only AgentCore, the outermost
    client, is faked), posts in their own room, and the turn's dispatch row is written for their tenant and room. A
    second person can neither read nor post in that room."""
    from core.services import provision_dispatch
    from scripts import provision_runner

    for name in ("prepare_hermes_workspace", "provision_hermes_runtime", "reconcile_runtime"):
        fake(monkeypatch, calls, f"core.services.hermes_provisioning.{name}")

    async def run_provision(owner_id: str) -> bool:  # the Step Functions execution, run in-process
        assert await provision_runner.run(owner_id) == provision_runner.EXIT_OK
        return True

    monkeypatch.setattr(provision_dispatch, "dispatch_provision", run_provision)
    monkeypatch.setattr("routers.container.dispatch_provision", run_provision)
    person, other = Tenant("user_solo", None), Tenant("user_other", None)
    await ok(person, "POST", "/api/v1/container/provision")
    assert (await billing_repo.get_by_owner_id(person.user_id))["subscription_status"] == "trial_local"
    rooms = (await ok(person, "GET", "/api/v1/messaging/rooms"))["rooms"]
    room = next(r for r in rooms if r.get("kind") == "channel")
    assert room["workspaceId"] == (await ok(person, "GET", "/api/v1/messaging/live-config"))["workspaceId"]
    text = {"clientMessageId": str(uuid.uuid4()), "text": "What should we do first?", "replyToMessageId": None}
    sent = await ok(person, "POST", f"/api/v1/messaging/rooms/{room['roomId']}/messages",
                    {**text, "threadRootMessageId": None})  # fmt: skip
    table = messaging_repo._table()
    work = [i for i in table.scan()["Items"] if str(i.get("sk", "")).startswith("WORK#")]
    (admit,) = work  # the turn's admission row: the work consumer runs it on the person's own runtime
    assert admit["sk"] == messaging_repo.work_admit_sk(sent["messageSeq"]) and admit["pk"] == f"ROOM#{room['roomId']}"
    assert admit["payload"] == {"kind": "admit", "roomId": room["roomId"], "throughSeq": sent["messageSeq"]}
    assert admit["messageGroupId"] == messaging_repo._sha256(f"admit|{person.user_id}|{room['roomId']}")
    assert (await messaging_repo.get_room(room["roomId"]))["ownerId"] == person.user_id

    before = snapshot(ddb, person.user_id, room["roomId"])
    read = await call(other, "GET", f"/api/v1/messaging/rooms/{room['roomId']}/messages")
    post = await call(other, "POST", f"/api/v1/messaging/rooms/{room['roomId']}/messages",
                      {**text, "clientMessageId": str(uuid.uuid4()), "threadRootMessageId": None})  # fmt: skip
    assert read.status_code in (403, 404) and post.status_code in (403, 404), (read.text[:200], post.text[:200])
    assert snapshot(ddb, person.user_id, room["roomId"]) == before


async def test_person_workspace_checkout_bills_no_seats(ddb, calls, jwks, monkeypatch):
    """A person's tenant is one human: the Team checkout carries no seat item."""
    await seed_billing(Tenant("user_a", None), Tenant("user_b", None), monkeypatch, calls)
    person = Tenant("user_solo", None)
    calls.clear()
    assert (await call(person, "POST", "/api/v1/billing/checkout", {"plan": "workspace"})).status_code == 200
    (session,) = _stripe_posts(calls, "/v1/checkout/sessions")
    assert "price_seat" not in session.values() and session["metadata[workspace_id]"] == person.user_id
    row = await billing_repo.get_by_owner_id(person.user_id, consistent_read=True)
    assert row["subscription_status"] == "trial_local"


@pytest.mark.parametrize("paid", [False, True])
async def test_a_free_person_cannot_widen_their_mailbox_and_a_paid_one_can(paid, ddb, calls, jwks, monkeypatch):
    """A person on no paid plan cannot let their mailbox send to anyone or clear a complaint pause (402
    `paid_plan_required`), and every other setting still saves; on a paid plan both pass."""
    monkeypatch.setattr("routers.agent_mail._owner_email", AsyncMock(return_value=None))
    person, other = Tenant("user_solo", None), Tenant("user_other", None)
    await billing_rows(other, person, b_paid=paid)  # the person has a billing row, paid or not
    address = await _mail(person, "solo-co")
    await agent_email_repo.update_settings(address, send_to_anyone=False)  # closed by the owner (new rows are open)
    for body in ({"send_to_anyone": True}, {"outbound_disabled": None}):
        r = await call(person, "PATCH", "/api/v1/agent-mail/address", body)
        assert r.status_code == (200 if paid else 402), r.text[:300]
        if not paid:
            assert r.json()["detail"] == {"status": "paid_plan_required"}
    assert (await agent_email_repo.get_by_address(address)).get("send_to_anyone", False) is paid
    assert (await ok(person, "PATCH", "/api/v1/agent-mail/address", {"footer_enabled": False}))["address"] == address
    assert (await ok(person, "PATCH", "/api/v1/agent-mail/address", {"send_to_anyone": False}))["address"] == address


async def test_pending_oauth_states_are_deleted_for_exactly_one_owner(ddb, calls, jwks, monkeypatch):
    """The wipe's `oauth_state` step: a person's pending Link wallet connect is deleted, another person's stays, so a
    callback after the wipe cannot store a credential for the deleted account."""
    from core.repositories import oauth_state_repo

    for name in ("LINK_CLIENT_ID", "LINK_CLIENT_SECRET", "LINK_PUBLISHABLE_KEY"):
        monkeypatch.setattr(settings, name, "test")
    person, other = Tenant("user_solo", None), Tenant("user_solo2", None)
    await billing_rows(person, other)
    for t in (person, other):
        assert (await ok(t, "POST", "/api/v1/company/link-wallet/connect"))["authorizeUrl"]
    states = {i["owner_id"]: i["state"] for i in dynamodb.get_table("oauth-state").scan()["Items"]}
    assert set(states) == {person.user_id, other.user_id}
    assert await oauth_state_repo.delete_all_for_owner(person.user_id) == 1
    assert await oauth_state_repo.consume(states[person.user_id]) is None
    assert (await oauth_state_repo.consume(states[other.user_id]))["owner_id"] == other.user_id
