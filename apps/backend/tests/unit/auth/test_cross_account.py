"""One account cannot read or change another's data through any door that resolves a tenant.

For every (method, path) in CASES, tenant A's rows are seeded through the product's own writers (A's own HTTP
calls where a door writes them), carrying MARKER; then B, signed in, makes the call. Every case asserts:
  * A's rows are unchanged: every DynamoDB item and S3 object naming A, before and after;
  * nothing B gets back, and no outbound request B's call makes (HTTP, Stripe, a faked service), names A;
  * path id (`a_id=True`: B names A's room, task, key...): B gets 403 or 404, never 200;
  * token only: B's call succeeds, and no AWS call it makes names A either (B reads and writes only B's keys).
`ok` overrides the expected statuses for a door whose answer to B is something else (the reason is beside it);
`says` names the text that makes a refusal the tenant check rather than a generic one; `org_only` doors refuse a
person (409 workspace_required) until a person's tenant can provision.
Each case runs twice (`tenants`): as personal accounts (no `o` claim) and as two orgs.

`test_every_tenant_door_has_a_case` walks the app: a new door with no case fails (no sampling);
`test_every_router_seeds_a`: a router whose doors find nothing of A's fails unless NO_SEED says why."""

import json
import uuid
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.routing import _IncludedRouter
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient

from core import tasks_schema as ts
from core.auth import (
    AuthContext,
    get_current_user,
    get_human_session,
    require_org_admin,
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
from core.services import hermes_profiles, messaging, network_route, workspace_provisioning
from core.services.credentials import integration_secret_store
from core.services.provision_dispatch import dispatch_provision
from core.temporal import client as temporal_client
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
    org_only: bool = False  # provisions, buys or bills: a person with no org gets 409 until Task 2
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
        await billing_repo.create_if_not_exists(t.tenant, customer, owner_type="org" if t.org_id else "personal")
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
    as_a = AuthContext(user_id=a.user_id, org_id=a.org_id, human_session=True)  # the send door is org-only
    message = await messaging.create_human_message(
        as_a, rid, messaging.CreateMessageRequest(**text, threadRootMessageId=None)
    )
    message = {"messageId": str(message.message_id)}
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
    await messaging_repo.put_voice_agent(a.tenant, {"greeting": f"{MARKER} greeting"}, a.user_id)
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
    made, _ = await automations_repo.create_schedule(  # what POST /automations writes (org-only)
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
    draft |= {"status": "proposed", "created": "2026-11-20T00:00:00+00:00", "mirrored": bool(a.org_id)}
    await put_row({"pk": f"DRAFTS#{a.user_id}", "sk": "D#d-a", **draft})
    if a.org_id:
        await put_row({"pk": f"WDRAFTS#{a.org_id}", "sk": f"D#{a.user_id}#d-a", "workspace_id": a.org_id, **draft})
    return {"draft": "d-a"}


def clerk(mp, a: Tenant, b: Tenant) -> None:
    """Clerk as the two tenants: each person is the only member of their own org (when they have one)."""

    async def orgs_of(user_id, **kwargs):
        return [{"id": t.org_id, "name": t.org_id} for t in (a, b) if t.user_id == user_id and t.org_id]

    async def members_of(org_id, **kwargs):
        return [{"user_id": t.user_id, "role": "org:admin"} for t in (a, b) if t.org_id == org_id]

    mp.setattr("core.services.clerk_admin.list_user_organizations", orgs_of)
    mp.setattr("core.services.clerk_admin.list_organization_members", members_of)


async def seed_agent_keys(a, b, mp, calls) -> dict:
    clerk(mp, a, b)
    key = await ok(a, "POST", "/api/v1/settings/agent-keys", {"name": f"{MARKER} key"})
    return {"key": key["keyId"]}


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
    await _mail(a, "acme-a")  # A's claimed slug, as an org's claim leaves it (a person cannot claim one yet)
    await ok(a, "POST", "/api/v1/company/slug/hold", {"slug": "held-a"})
    return {}


async def seed_connections(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    await integrations_repo.put_status(a.tenant, "github", identity=f"{MARKER}-gh")
    await integration_secret_store.put(a.tenant, "aws", {"external_id": MARKER})  # what A's aws-setup mints
    fake(mp, calls, "routers.connections._validate_token", True)
    fake(mp, calls, "core.services.credentials.apply_connection.apply_connection")
    return {}


async def seed_network_route(a, b, mp, calls) -> dict:
    from core.services import app_wake

    await network_route._connections().put_route(key="key_a", user_id=a.user_id, generation="g1", name=f"{MARKER} mac")
    await put_row({**app_wake._key(a.user_id, "app-a"), "app": "app-a", "client": MARKER, "state": "needs_key"})
    await network_route.set_pin(b.user_id, "app-b", {"kind": "computer", "key": "key_b", "name": "b"}, "app-b", "b")
    return {"key": "key_a"}


async def seed_sessions(a, b, mp, calls) -> dict:
    upload = {"mode": "metadata", "sessions": [{"id": "claude:s-a", "harness": MARKER, "started": "2026-11-20"}]}
    await ok(a, "POST", "/api/v1/sessions", upload)
    return {"session": "claude:s-a"}


async def seed_container(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    await billing_rows(a, b)
    await a_runtime(a)
    fake(mp, calls, "core.services.provision_dispatch.dispatch_provision")
    return {}


async def seed_users(a, b, mp, calls) -> dict:
    await workspaces(a, b)
    clerk(mp, a, b)
    await user_repo.put(a.user_id, f"{MARKER}@example.com")
    await user_repo.put(b.user_id, f"{b.user_id}@example.com")  # the self-delete's typed confirmation
    return {}


async def seed_runtime_rpc(a, b, mp, calls) -> dict:
    await a_runtime(a)
    op = str(uuid.uuid4())
    await messaging_repo.create_operation(
        owner_id=a.tenant, operation_id=op, requester_user_id=a.user_id, method="health", params={"q": MARKER}
    )
    return {"op": op}


async def seed_network(a, b, mp, calls) -> dict:
    """A's computer asked to bring A's agents onto a network (a bearer code: describing it shows the computer's
    name, by design of a device flow; approving it is A's alone)."""
    from core.services import device_login, network_login

    fields = {"kind": network_login.KIND, "purpose": "agents", "computerKey": "key_a", "ticket": MARKER}
    fields |= {"machineName": "a-mac", "startedAt": 1, "sourceIp": "", "computerUser": a.user_id}
    code = await device_login.create(fields, network_login.PAGE, code_in_url=False)
    return {"code": code["userCode"]}


async def seed_oauth(a, b, mp, calls) -> dict:
    """A's pending ChatGPT sign-in. The doors take no id: each reads only the caller's own row."""
    from core.services import oauth_service

    await billing_rows(a, b)  # both paid: B's start runs to OpenAI (the network fake), with B's ids only
    row = {"user_id": a.user_id, "state": "pending", "device_auth_id": MARKER, "user_code": "AAAA-AAAA"}
    await run_in_thread(oauth_service._table().put_item, Item={**row, "interval": 5, "bound_owner_id": a.tenant})
    return {}


async def seed_devices(a, b, mp, calls) -> dict:
    await user_repo.put(a.user_id, None)
    await user_repo.set_push_token(a.user_id, "dev-a", {"token": f"ExponentPushToken[{MARKER}]", "platform": "ios"})
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


async def seed_orgs(a, b, mp, calls) -> dict:
    """An org can be joined by link; a person (user mode) has nothing joinable, so A's id is refused as any is."""
    if a.org_id:
        await ok(a, "POST", f"/api/v1/orgs/{a.org_id}/join-link")
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
    "agent_keys": seed_agent_keys,
    "agent_mail": seed_agent_mail,
    "agent_text": seed_agent_text,
    "company_slug": seed_company_slug,
    "connections": seed_connections,
    "network_route": seed_network_route,
    "sessions": seed_sessions,
    "container": seed_container,
    "users": seed_users,
    "runtime_rpc": seed_runtime_rpc,
    "network": seed_network,
    "oauth": seed_oauth,
    "devices": seed_devices,
    "debug": seed_debug,
    "config": seed_config,
    "orgs": seed_orgs,
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
    Case("messaging", "POST", "/api/v1/messaging/rooms/{room}/messages", MSG, a_id=True, org_only=True),
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
    Case("huddles", "POST", "/api/v1/huddles/{room}/join", a_id=True, org_only=True),
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
    Case("voice", "POST", "/api/v1/voice/forward-check", {"forwardFrom": "+15550100004"}, org_only=True),
    Case("voice", "POST", "/api/v1/voice/onboarding/draft", {"website": "example.com"}, org_only=True),
    Case("voice", "POST", "/api/v1/voice/number", {"path": "new", "areaCode": "415"}, org_only=True),
    # billing: tenant = caller; the owner checks out, opens the portal, cancels
    Case("billing", "GET", "/api/v1/billing/account"),
    Case("billing", "GET", "/api/v1/billing/trial-recap"),
    Case("billing", "GET", "/api/v1/billing/credits/balance"),
    Case("billing", "POST", "/api/v1/billing/portal"),
    Case("billing", "POST", "/api/v1/billing/checkout", {"plan": "workspace"}, org_only=True),
    Case("billing", "POST", "/api/v1/billing/front-desk", {}, org_only=True),
    Case("billing", "GET", "/api/v1/billing/voice-plan"),
    Case("billing", "POST", "/api/v1/billing/voice-plan/checkout", VOICE_PLAN, org_only=True),
    Case("billing", "DELETE", "/api/v1/billing/voice-plan", query={"expectedOwnerId": "{b_tenant}"}),
    # apple_iap: tenant = caller (selling is off in prod)
    Case("apple_iap", "POST", "/api/v1/billing/apple/purchase-intent", org_only=True),
    Case(
        "apple_iap",
        "POST",
        "/api/v1/billing/apple/link",
        {"signed_transaction": "not-a-jws"},
        ok=(503,),
        org_only=True,
    ),  # no Apple keys in tests: the org case proves only that nothing of A's moves
    # company_link_wallet: tenant = caller
    Case("company_link_wallet", "GET", "/api/v1/company/link-wallet/status"),
    Case("company_link_wallet", "POST", "/api/v1/company/link-wallet/connect", org_only=True),
    Case("company_link_wallet", "POST", "/api/v1/company/link-wallet/disconnect"),
    Case("company_link_wallet", "GET", "/api/v1/company/link-wallet/limits"),
    Case(
        "company_link_wallet",
        "PUT",
        "/api/v1/company/link-wallet/limits",
        {"perPurchaseCents": 5, "projectMonthlyCents": 9},
        org_only=True,
    ),
    Case("company_link_wallet", "GET", "/api/v1/company/link-wallet/purchases"),
    Case(
        "company_link_wallet",
        "POST",
        "/api/v1/company/link-wallet/purchases/{request}/approval-link",
        a_id=True,
        org_only=True,
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
    Case("automations", "POST", "/api/v1/automations", {"name": "b", "instructions": "b"}, org_only=True),
    Case(
        "automations",
        "POST",
        "/api/v1/automations/from-integration",
        {"provider": "stripe", "event_ids": ["invoice.payment_failed"]},
        org_only=True,
    ),
    Case(
        "automations",
        "PUT",
        "/api/v1/automations/{hook}/webhook-secret",
        {"webhook_secret": "whsec_b"},
        a_id=True,
        org_only=True,
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
        org_only=True,
    ),
    # memory: tenant = caller; drafts are the caller's own
    Case("memory", "POST", "/api/v1/memory/recall", {"question": "what"}, org_only=True),
    Case("memory", "GET", "/api/v1/memory/drafts"),
    Case("memory", "POST", "/api/v1/memory/drafts/{draft}/approve", a_id=True),
    Case("memory", "POST", "/api/v1/memory/drafts/{draft}/dismiss", a_id=True),
    # network: person mode is the signed-in person
    Case("network", "POST", "/api/v1/network/device/describe", {"userCode": "{code}"}, says="agents"),  # bearer code
    Case(
        "network",
        "POST",
        "/api/v1/network/device/approve",
        {"userCode": "{code}"},
        ok=(409,),
        says="lightbulb login",
        org_only=True,
    ),  # A's computer, not B's
    Case("network", "POST", "/api/v1/network/leave", ok=(409,), says="not on a network", org_only=True),
    # network_route: the person's own apps, wakes and computers
    Case("network_route", "GET", "/api/v1/network/apps"),
    Case(
        "network_route",
        "PUT",
        "/api/v1/network/apps/route",
        {"clientId": "app-b", "key": "{key}"},
        a_id=True,
        ok=(409,),
        says="offline",
    ),  # B's own app, pinned to A's computer: not one of B's
    Case("network_route", "POST", "/api/v1/network/computers/{key}/sign-out", a_id=True),
    Case("network_route", "GET", "/api/v1/network/wakes"),
    Case("network_route", "PUT", "/api/v1/network/wakes/app-a", {"key": "k"}, a_id=True),  # A's app asked A
    Case("network_route", "DELETE", "/api/v1/network/wakes/app-a"),
    # agent_keys: the caller's own keys
    Case("agent_keys", "GET", "/api/v1/settings/agent-keys"),
    Case("agent_keys", "POST", "/api/v1/settings/agent-keys", {"name": "b key"}),
    Case("agent_keys", "DELETE", "/api/v1/settings/agent-keys/{key}", a_id=True),
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
    Case("agent_mail", "GET", "/api/v1/agent-mail/address", org_only=True),  # creates the slug and mailbox
    Case("agent_mail", "PATCH", "/api/v1/agent-mail/address", {"send_to_anyone": False}, org_only=True),
    Case("agent_mail", "GET", "/api/v1/agent-mail/inbox"),
    Case("agent_mail", "GET", "/api/v1/agent-mail/inbox/new", query={"since": "2026-01-01T00:00:00+00:00"}),
    Case("agent_mail", "GET", "/api/v1/agent-mail/message", query={"key": "{key}"}, a_id=True),
    Case("agent_mail", "GET", "/api/v1/agent-mail/meetings/{meeting}/transcript", query={"start_at": 1}, a_id=True),
    # company_slug: a claim moves only the caller's own hold
    Case("company_slug", "GET", "/api/v1/company/slug-availability", query={"slug": "acme-b"}),
    Case("company_slug", "POST", "/api/v1/company/slug/hold", {"slug": "acme-a"}, a_id=True, ok=(409,)),  # taken
    Case("company_slug", "POST", "/api/v1/company/slug/claim", {"slug": "held-a"}, a_id=True, ok=(409,), org_only=True),
    Case("company_slug", "POST", "/api/v1/company/source", {"source": "web"}),
    # container: tenant = caller; the owner reads status and retries. B's own answers: an org's gate wants credits
    # (402) and it has no container (404); a person is refused a trial row (403) and passes no provision gate
    # (402) until user tenants provision (Task 2; test_no_org_caller_cannot_provision pins it).
    Case("container", "GET", "/api/v1/container/status", ok=(402,), org_only=True),
    Case("container", "POST", "/api/v1/container/provision", ok=(200, 403)),
    Case("container", "POST", "/api/v1/container/retry", ok=(403, 404)),
    Case("container", "POST", "/api/v1/browser/viewing", {}, ok=(402,)),
    Case("container", "POST", "/api/v1/browser/input", {"type": "key", "key": "a"}, ok=(409,)),  # no live page
    # connections: tenant = caller; the owner manages connections
    Case("connections", "GET", "/api/v1/connections"),
    Case("connections", "POST", "/api/v1/connections/parallel", {"token": "b"}, org_only=True),
    Case("connections", "DELETE", "/api/v1/connections/github"),
    Case(
        "connections", "GET", "/api/v1/connections/github/authorize", ok=(503,), org_only=True
    ),  # no GitHub OAuth app in tests: the org case proves only that nothing of A's moves
    Case("connections", "GET", "/api/v1/connections/aws/aws-setup", org_only=True),
    Case(
        "connections",
        "POST",
        "/api/v1/connections/aws/aws-complete",
        {"role_arn": "arn:aws:iam::123456789012:role/lightbulb-connector"},
        ok=(409,),
        says="aws-setup",
        org_only=True,
    ),  # A ran aws-setup, B did not: A's external id is not B's
    Case(
        "connections", "PUT", "/api/v1/connections/x/byo-app", {"client_id": "b", "client_secret": "b"}, org_only=True
    ),
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
    Case("config", "PATCH", "/api/v1/config", {"patch": {"x": 1}}, org_only=True),
    # orgs: unchanged for orgs; a user id in /orgs/{id}/... is not an org anyone joins or manages
    Case("orgs", "GET", "/api/v1/orgs/{a_tenant}/join-link", a_id=True),
    Case("orgs", "POST", "/api/v1/orgs/{a_tenant}/join-link", a_id=True),
    Case("orgs", "DELETE", "/api/v1/orgs/{a_tenant}/join-link", a_id=True),
    Case("orgs", "POST", "/api/v1/orgs/{a_tenant}/join", {"token": "x" * 43}, a_id=True),
    Case("orgs", "GET", "/api/v1/orgs/{a_tenant}/invitations", a_id=True),
    Case("orgs", "POST", "/api/v1/orgs/{a_tenant}/invitations", {"email": "x@example.com"}, a_id=True),
    Case("orgs", "DELETE", "/api/v1/orgs/{a_tenant}/invitations/inv_1", a_id=True),
    # users: the caller's own row; self-delete wipes only the caller
    Case("users", "POST", "/api/v1/users/sync"),
    Case("users", "GET", "/api/v1/users/me"),
    Case("users", "POST", "/api/v1/users/me/active"),
    Case("users", "GET", "/api/v1/users/me/prefs"),
    Case("users", "PUT", "/api/v1/users/me/prefs", {"timezone": "Europe/London"}),
    Case("users", "POST", "/api/v1/users/me/delete", {"confirm_email": "{b}@example.com"}),
    # sessions: the caller's own sessions
    Case("sessions", "GET", "/api/v1/sessions"),
    Case("sessions", "POST", "/api/v1/sessions", {"mode": "metadata", "sessions": []}),
    Case("sessions", "DELETE", "/api/v1/sessions/{session}", a_id=True),
    # devices, support, desktop sign-in: the caller's own
    Case(
        "devices", "POST", "/api/v1/devices", {"token": "ExponentPushToken[b]", "platform": "ios", "deviceId": "dev-b"}
    ),
    Case("devices", "DELETE", "/api/v1/devices/dev-a"),
    Case("support_tickets", "POST", "/api/v1/support/tickets", {"message": "help"}),
    Case(
        "desktop_auth", "POST", "/api/v1/auth/desktop/sign-in-token", ok=(500,)
    ),  # no CLERK_SECRET_KEY in tests: the case proves only that nothing of A's moves
    # runtime_rpc: tenant = caller
    Case(
        "runtime_rpc",
        "POST",
        "/api/v1/runtime/rpc",
        {"requestId": "{new}", "method": "health", "params": {}},
        ok=(503,),
        org_only=True,
    ),  # B has no runtime
    Case("runtime_rpc", "GET", "/api/v1/runtime/operations/{op}", a_id=True),
    # a2a: the path's workspace must be the caller's
    Case("a2a", "POST", "/a2a/{a_tenant}/{agent}", {}, a_id=True),
    Case("a2a", "GET", "/a2a/{a_tenant}/rules", a_id=True),
    Case("a2a", "PUT", "/a2a/{a_tenant}/rules", {"holders": {}, "rules": []}, a_id=True),
    Case("a2a", "GET", "/a2a/{a_tenant}/{agent}/tasks/t-a/record", a_id=True),
    Case("a2a", "POST", "/a2a/{a_tenant}/{agent}/tasks/t-a/decide", {"verdict": "allow"}, a_id=True),
    # main: the signed-in person
    Case("main", "GET", "/protected"),
]


# Routers with tenant doors and no seed for A, each with why there is nothing of A's to seed.
NO_SEED = {
    "a2a": "the path names a workspace; any caller not in it is refused before a row is read (in user mode A "
    "itself is refused: A2A is org-only until Task 9)",
    "main": "/protected echoes the caller's own id",
    "support_tickets": "one POST: it reads the caller's own billing and container rows, emails support and stores "
    "nothing; it takes no id",
    "desktop_auth": "mints a Clerk sign-in token for the caller; reads no tenant row (Clerk is the network fake)",
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
    if case.org_only and not b.org_id:
        assert r.status_code == 409 and "workspace_required" in r.text, f"{r.status_code} {r.text[:300]}"
    else:
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
    from core.services import a2a_auth
    from routers import messaging as messaging_router, questions, sessions

    signed_in = {get_current_user, get_human_session, sessions.caller, messaging_router.person_or_computer}
    signed_in |= {questions.person_only, a2a_auth.a2a_caller}  # these call get_current_user in their body

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


@pytest.mark.parametrize(  # as the doors with no org build them from a person's own session
    "ctx",
    [
        AuthContext(user_id="user_x", human_session=True),
        AuthContext("user_x", None, verified_issuer="i", human_session=True),
    ],
)
def test_admin_without_org_is_own_tenant_only(ctx):
    """A signed-in person with no org is their own tenant and its admin, and nobody else's."""
    assert resolve_owner_id(ctx) == ctx.user_id
    assert ctx.is_org_admin and require_org_admin(ctx) is ctx
    synthetic = AuthContext(user_id="user_x", org_id="org_t")  # an internal door's org context, no role
    assert resolve_owner_id(synthetic) == "org_t" and not synthetic.is_org_admin
    with pytest.raises(HTTPException) as refused:
        require_org_admin(synthetic)
    assert refused.value.status_code == 403


@pytest.mark.parametrize("token", ["oauth", "template"])
async def test_non_session_token_without_org_has_no_tenant(token, tenants):
    """An OAuth access token or a JWT-template token with no org is refused on tenant resolution as before this
    task (409 workspace_required); with an org it is that org's, as before."""
    _, b = tenants
    r = await call(replace(b, token=token), "GET", "/api/v1/voice/timeline")
    if b.org_id:
        assert r.status_code == 200
    else:
        assert r.status_code == 409 and r.json()["detail"]["code"] == "workspace_required"


async def test_org_claim_still_wins_interim(tenants):
    """Interim rule: a session token carrying an org claim (`o.id`) is that org's; without one, the person's."""
    _, b = tenants
    ctx = await get_current_user(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=b.headers["Authorization"][7:])
    )
    assert (ctx.user_id, ctx.org_id) == (b.user_id, b.org_id)
    assert resolve_owner_id(ctx) == (b.org_id or b.user_id)


# Every door that would provision, buy, bill or join a network for a person with no org, and what it answers.
REFUSED = [
    ("GET", "/api/v1/container/status", None, 409),
    ("POST", "/api/v1/container/provision", None, 403),
    ("POST", "/api/v1/container/retry", None, 403),
    ("POST", "/api/v1/debug/provision", None, 200),  # answers "provisioning"; dispatch_provision refuses
    ("POST", "/api/v1/network/device/approve", {"userCode": "{code}"}, 409),  # the person's own `agents` code
    ("POST", "/api/v1/network/leave", None, 409),
    ("POST", "/api/v1/billing/checkout", {"plan": "workspace"}, 409),
    ("POST", "/api/v1/billing/front-desk", {}, 409),
    ("POST", "/api/v1/billing/voice-plan/checkout", VOICE_PLAN, 409),
    ("POST", "/api/v1/billing/apple/purchase-intent", None, 409),
    ("POST", "/api/v1/billing/apple/link", {"signed_transaction": "x"}, 409),
    ("GET", "/api/v1/agent-mail/address", None, 409),  # would mint a company slug and an agents@ mailbox
    ("PATCH", "/api/v1/agent-mail/address", {"send_to_anyone": False}, 409),
    ("POST", "/api/v1/company/slug/claim", {"slug": "solo-co"}, 409),
    ("GET", "/api/v1/connections/aws/aws-setup", None, 409),  # mints the STS ExternalId
    ("GET", "/api/v1/connections/github/authorize", None, 409),  # starts a provider OAuth grant
    ("POST", "/api/v1/connections/parallel", {"token": "t"}, 409),  # stores a credential
    ("PUT", "/api/v1/connections/x/byo-app", {"client_id": "c", "client_secret": "s"}, 409),
    ("POST", "/api/v1/connections/aws/aws-complete", {"role_arn": "arn:aws:iam::1:role/lightbulb-connector"}, 409),
    ("POST", "/api/v1/company/link-wallet/connect", None, 409),  # links an outside wallet
    ("PUT", "/api/v1/company/link-wallet/limits", {"perPurchaseCents": 1, "projectMonthlyCents": 1}, 409),
    ("POST", "/api/v1/company/link-wallet/purchases/" + "0" * 32 + "/approval-link", None, 409),
    ("POST", "/api/v1/messaging/rooms/{room}/messages", MSG, 409),  # addressing reaches the model
    ("POST", "/api/v1/huddles/{room}/join", None, 409),  # a LiveKit room and the voice worker
    ("POST", "/api/v1/memory/recall", {"question": "q"}, 409),  # AgentCore memory retrieval
    ("PUT", "/api/v1/container/workspace/ceo/file", {"path": "USER.md", "content": "x", "tab": "config"}, 409),
    ("POST", "/api/v1/automations", {"name": "n", "instructions": "i"}, 409),  # a Temporal Schedule
    ("POST", "/api/v1/automations/from-integration", {"provider": "stripe", "event_ids": ["x"]}, 409),
    ("POST", "/api/v1/voice/forward-check", {}, 409),  # rings a phone
    ("POST", "/api/v1/voice/onboarding/draft", {"website": "example.com"}, 409),  # a model call
    ("POST", "/api/v1/runtime/rpc", {"requestId": "{new}", "method": "agents.create", "params": {}}, 409),
    ("PATCH", "/api/v1/config", {"patch": {"x": 1}}, 409),  # writes every agent's config.yaml
    ("PUT", "/api/v1/automations/hook1/webhook-secret", {"webhook_secret": "whsec_x"}, 409),
    ("POST", "/api/v1/voice/number", {}, 409),
    ("PUT", "/api/v1/voice/agent", {"config": {"live": True}}, 409),  # going live gives a number
    ("POST", "/api/v1/oauth/chatgpt/start", None, 402),  # needs a paid plan, which a person cannot buy yet
]


@pytest.mark.parametrize(("method", "path", "body", "status"), REFUSED, ids=[f"{m} {p}" for m, p, _, _ in REFUSED])
async def test_no_org_caller_reads_own_empty_data_and_cannot_provision(
    method, path, body, status, ddb, calls, jwks, monkeypatch
):
    """Accepted exposure until user tenants provision (Task 2): a session with no org is the person's tenant on
    every door. It reads its own, empty data; every door that would provision it, mint it a key or a line, bill
    it or join its agents to a network refuses, and nothing is written or sent for it."""
    from core.services import device_login, network_login

    for name in ("LINK_CLIENT_ID", "LINK_CLIENT_SECRET", "LINK_PUBLISHABLE_KEY"):  # the wallet doors exist
        monkeypatch.setattr(settings, name, "test")
    person = Tenant("user_solo", None)
    await company_slug_repo.hold(slug="solo-co", user_id=person.user_id)  # a held slug a claim would bind
    timeline = await ok(person, "GET", "/api/v1/voice/timeline")
    assert not any(v for v in timeline.values() if isinstance(v, list))
    assert (await ok(person, "GET", "/api/v1/tasks"))["tasks"] == []
    fields = {"kind": network_login.KIND, "purpose": "agents", "computerKey": "k", "ticket": "t"}
    fields |= {"machineName": "m", "startedAt": 1, "sourceIp": "", "computerUser": person.user_id}
    code = (await device_login.create(fields, network_login.PAGE, code_in_url=False))["userCode"]
    fake(monkeypatch, calls, "core.services.network_member.join_network")
    calls.clear()
    room = await ok(person, "POST", "/api/v1/messaging/rooms", {"clientRoomId": str(uuid.uuid4()), "name": "mine",
                                                               "kind": "channel", "members": []})  # fmt: skip
    ids = {"code": code, "b_tenant": person.user_id, "room": room["roomId"], "new": str(uuid.uuid4())}
    r = await call(person, method, fill(path, ids), fill(body, ids))
    assert r.status_code == status, r.text[:300]
    if status == 409:
        assert r.json()["detail"]["code"] == "workspace_required"
    assert not [c for c in calls if c[0] in ("stripe", "http", "fake")]
    assert await dispatch_provision(person.user_id) is False
    assert await container_repo.get_by_owner_id(person.user_id) is None
    assert await billing_repo.get_by_owner_id(person.user_id) is None
    assert await company_slug_repo.get_by_owner(person.user_id) is None  # no company slug
    assert await agent_email_repo.query_by_owner(person.user_id) == []  # no mailbox
    assert await integration_secret_store.get(person.user_id, "aws") is None  # no ExternalId
