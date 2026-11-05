"""Hooks from integrations in super-agent workspaces (lane A, Task 8): a `HOOK#` row plus a secret in
Secrets Manager instead of a Paperclip routine, the provider registered with the same helpers as today."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core import tasks_schema as ts
from core.repositories import automations_repo as repo
from core.services import automation_hooks, inbound_triggers
from core.services.credentials import registry
from core.services.inbound_triggers import seed_trigger_routine

WS = "org_1"
USER = "user_sam"


class FakeTemporal:
    def __init__(self):
        self.started: list[tuple] = []
        self.cancelled: list[str] = []

    async def start_workflow(self, wf, *args, id, task_queue, **kw):
        self.started.append((wf, tuple(kw.get("args") or args), id))

    def get_workflow_handle(self, wid):
        handle = MagicMock()
        handle.cancel = AsyncMock(side_effect=lambda: self.cancelled.append(wid))
        return handle


def _response(status: int, body=None) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {}, request=httpx.Request("POST", "https://x/"))


_real_get_provider = registry.get_provider


def _gmail_provider():
    """The google block with the Gmail events back (removed from the registry in #4323; see the PR)."""
    google = _real_get_provider("google")
    inbound = google.inbound.model_copy(
        update={
            "events": [
                *google.inbound.events,
                registry.InboundEvent(id="gmail.message", label="New email", registration="owner_setup"),
                registry.InboundEvent(id="gmail.poll", label="Inbox check", registration="poll"),
            ]
        }
    )
    return google.model_copy(update={"inbound": inbound})


@pytest.fixture
def env(table, monkeypatch):
    """Switch on, moto rows, in-memory Secrets Manager, fake provider HTTP, fake Temporal, Paperclip spied."""
    from core.temporal import client as tc

    temporal = FakeTemporal()
    monkeypatch.setattr(tc, "_client", temporal)
    secrets: dict[tuple[str, str], dict] = {
        (WS, "github"): {"token": "gh_tok_1"},
        (WS, "vercel_cli"): {"token": "v_tok"},
    }

    async def get(owner, name):
        return secrets.get((owner, name))

    async def put(owner, name, value):
        secrets[(owner, name)] = dict(value)
        return "arn"

    async def delete(owner, name):
        return secrets.pop((owner, name), None) is not None

    http = MagicMock()
    http.get = AsyncMock(return_value=_response(200))
    http.post = AsyncMock(return_value=_response(201, {"id": 99}))
    http.delete = AsyncMock(return_value=_response(204))

    @asynccontextmanager
    async def fake_client(*args, **kwargs):
        yield http

    paperclip = MagicMock()
    with (
        patch("core.services.inbound_triggers.flags_for", AsyncMock(return_value=True)),
        patch("core.services.credentials.integration_secret_store.get", side_effect=get),
        patch("core.services.credentials.integration_secret_store.put", side_effect=put),
        patch("core.services.credentials.integration_secret_store.delete", side_effect=delete),
        patch("core.services.inbound_triggers.httpx.AsyncClient", new=fake_client),
        patch("core.services.inbound_triggers.PaperclipAdminClient", paperclip),
        patch("core.services.inbound_triggers.get_admin_session_cookie", AsyncMock(return_value="s=1")),
        patch(
            "core.services.inbound_triggers.google_accounts_repo.list_accounts",
            AsyncMock(return_value=[{"email": "ada@example.com"}]),
        ),
        patch("core.services.google_watch._gog_env", return_value={"FAKE": "env"}),
        patch("core.services.inbound_triggers.capture_product_event") as funnel,
        patch(
            "core.services.inbound_triggers.registry.get_provider",
            side_effect=lambda key: _gmail_provider() if key == "google" else _real_get_provider(key),
        ),
    ):
        yield MagicMock(temporal=temporal, secrets=secrets, http=http, paperclip=paperclip, funnel=funnel)


async def _seed(provider, events, **kw):
    return await seed_trigger_routine(WS, provider, events, actor_user_id=USER, **kw)


async def _rows():
    scheds = await repo.list_schedules(WS)
    assert len(scheds) == 1
    sched = scheds[0]
    hooks = [await repo.get_hook(WS, pid) for pid in sched["hookPublicIds"]]
    return sched, hooks


async def test_seed_super_agent_writes_hook_and_secret_not_paperclip(env):
    out = await _seed("stripe", ["invoice.payment_failed", "charge.refunded"])

    assert not env.paperclip.called
    sched, (hook,) = await _rows()
    assert sched["status"] == "draft"
    assert sched["ownerUserId"] == USER and sched["createdBy"] == "user"
    assert sched["cron"] == [] and sched["name"] == "Stripe: Invoice payment failed and Charge refunded"
    assert hook["publicId"] == out["trigger_public_id"] and len(hook["publicId"]) == 24
    assert hook["scheduleId"] == sched["scheduleId"] == out["automation_id"]
    assert hook["provider"] == "stripe" and hook["signingMode"] == "hmac_sha256"
    assert hook["events"] == ["invoice.payment_failed", "charge.refunded"]
    assert hook["armed"] is False and hook["secretRef"] == f"hook-{hook['publicId']}"
    # A guided provider pastes its secret later: nothing in Secrets Manager yet.
    assert (WS, hook["secretRef"]) not in env.secrets
    assert env.temporal.started == []


async def test_seed_returns_same_shape(env):
    out = await _seed("stripe", ["invoice.payment_failed"])
    assert set(out) >= {"routine_id", "trigger_public_id", "proxy_url", "guided", "instructions", "secret_hint"}
    assert out["routine_id"] == out["automation_id"]
    assert out["guided"] is True and out["secret_hint"] == "whsec_…"
    assert out["proxy_url"].endswith(f"/api/v1/hooks/t/{WS}/{out['trigger_public_id']}")
    assert out["proxy_url"] in out["instructions"] and "invoice.payment_failed" in out["instructions"]
    assert out["webhook_secret"] is None
    env.funnel.assert_called_once()
    assert env.funnel.call_args.kwargs["event"] == "automation_armed"


async def test_seed_github_registers_hook_with_minted_secret(env):
    out = await _seed("github", ["pull_request"], resource="acme/site")

    sched, (hook,) = await _rows()
    secret = env.secrets[(WS, hook["secretRef"])]
    assert secret["mode"] == "github_hmac" and len(secret["secret"]) >= 32
    body = env.http.post.await_args.kwargs["json"]
    assert env.http.post.await_args.args[0] == "https://api.github.com/repos/acme/site/hooks"
    assert body["config"] == {"url": out["proxy_url"], "content_type": "json", "secret": secret["secret"]}
    assert body["events"] == ["pull_request"]
    assert hook["armed"] is True and hook["providerHookId"] == "99" and hook["resource"] == "acme/site"
    assert out["guided"] is False and out["resource"] == "acme/site"


async def test_seed_vercel_stores_vercel_secret(env):
    env.http.post = AsyncMock(return_value=_response(200, {"id": "hook_v1", "secret": "vsec_1"}))
    await _seed("vercel_cli", ["deployment.error"], resource="prj_1")
    _, (hook,) = await _rows()
    assert env.secrets[(WS, hook["secretRef"])] == {"secret": "vsec_1", "mode": "hmac_sha1"}
    assert hook["armed"] is True and hook["providerHookId"] == "hook_v1"
    assert hook["signatureHeader"] == "x-vercel-signature"


async def test_seed_meta_records_binding_for_the_app_ingress(env):
    asset = {"id": "1112223334", "label": "Acme Page", "object_type": "page", "graph_base": "g", "token": "t"}
    bindings: list[dict] = []
    with (
        patch.dict(inbound_triggers._META_ASSET_LISTERS, {"meta_ads": AsyncMock(return_value=[asset])}),
        patch("core.services.inbound_triggers._subscribe_meta_asset", AsyncMock()) as subscribe,
        patch(
            "core.services.inbound_triggers.integrations_repo.get_meta_webhook_bindings",
            AsyncMock(return_value=bindings),
        ),
        patch("core.services.inbound_triggers.integrations_repo.set_meta_webhook_bindings", AsyncMock()) as setb,
    ):
        out = await _seed("meta_ads", ["leadgen"], resource="1112223334")
    subscribe.assert_awaited_once()
    written = setb.await_args.args[2]
    assert written[0]["trigger_public_id"] == out["trigger_public_id"] and written[0]["asset_id"] == "1112223334"
    _, (hook,) = await _rows()
    assert hook["resource"] == "1112223334" and hook["armed"] is True
    assert out["resource"] == "Acme Page"


def _channel(resource_id="res_1", expiration="1799999999000"):
    return json.dumps({"kind": "api#channel", "resourceId": resource_id, "expiration": expiration})


async def test_seed_google_calendar_starts_watch_and_keeper(env):
    run_gog = MagicMock(return_value=_channel())
    with patch("core.services.google_watch._run_gog", run_gog):
        out = await _seed("google", ["calendar.changed"], resource="ada@example.com")

    _, (hook,) = await _rows()
    secret = env.secrets[(WS, hook["secretRef"])]
    assert secret["mode"] == "bearer"
    argv = run_gog.call_args.args[0]
    assert argv[:6] == ["gog", "api", "call", "calendar", "v3", "events.watch"]
    watch_body = json.loads(argv[9])
    assert watch_body["address"] == out["proxy_url"] and watch_body["token"] == secret["secret"]
    assert hook["account"] == hook["resource"] == "ada@example.com"
    assert hook["channelId"] == watch_body["id"] and hook["resourceId"] == "res_1"
    assert hook["expiresAt"] == "2027-01-15T07:59:59+00:00"
    assert hook["armed"] is True
    assert env.temporal.started == [
        ("GoogleWatchKeeperWorkflow", (WS, hook["publicId"], 1799999999000), f"gwatch:{WS}:{hook['publicId']}")
    ]


async def test_seed_gmail_push_uses_oidc_mode_and_topic(env):
    run_gog = MagicMock(return_value=json.dumps({"historyId": "7", "expiration": "1799999999000"}))
    topic = "projects/my-project/topics/lb-gmail"
    with patch("core.services.google_watch._run_gog", run_gog):
        out = await _seed("google", ["gmail.message"], resource="ada@example.com", topic=topic)

    _, (hook,) = await _rows()
    assert hook["signingMode"] == "google_oidc" and hook["topic"] == topic and hook["account"] == "ada@example.com"
    assert hook["expiresAt"] == "2027-01-15T07:59:59+00:00" and hook["armed"] is True
    assert run_gog.call_args.args[0][:6] == ["gog", "gmail", "watch", "start", "--topic", topic]
    assert topic in out["instructions"] and out["proxy_url"] in out["instructions"]
    assert env.temporal.started[0][2] == f"gwatch:{WS}:{hook['publicId']}"


async def test_gmail_poll_refused_in_super_agent_workspace(env):
    with pytest.raises(ValueError, match="use_gmail_push"):
        await _seed("google", ["gmail.poll"], resource="ada@example.com")
    assert await repo.list_schedules(WS) == []


async def test_seed_attaches_to_existing_automation(env):
    first = await _seed("stripe", ["invoice.payment_failed"])
    second = await _seed("stripe", ["charge.refunded"], routine_id=first["automation_id"])
    sched, hooks = await _rows()
    assert second["automation_id"] == first["automation_id"]
    assert [h["publicId"] for h in hooks] == [first["trigger_public_id"], second["trigger_public_id"]]
    with pytest.raises(ValueError, match="not in this workspace"):
        await _seed("stripe", ["charge.refunded"], routine_id="nope")


async def test_seed_provider_refusal_unwinds_row_hook_and_secret(env):
    env.http.post = AsyncMock(return_value=_response(403))
    with pytest.raises(ValueError, match="admin access"):
        await _seed("github", ["pull_request"], resource="acme/site")
    assert await repo.list_schedules(WS) == []
    assert not [k for k in env.secrets if k[1].startswith("hook-")]
    assert env.temporal.started == []


async def test_seed_specialist_and_title_override(env):
    out = await _seed("stripe", ["invoice.payment_failed"], assignee_agent_id="agent_fin", title="Chase invoices")
    sched, _ = await _rows()
    assert sched["name"] == "Chase invoices" and sched["specialist"] == "agent_fin"
    assert out["automation_id"] == sched["scheduleId"]


async def test_delete_unregisters_github_hook_and_secret(env):
    out = await _seed("github", ["pull_request"], resource="acme/site")
    pid = out["trigger_public_id"]
    await automation_hooks.delete(WS, pid)
    assert env.http.delete.await_args.args[0] == "https://api.github.com/repos/acme/site/hooks/99"
    assert (WS, f"hook-{pid}") not in env.secrets
    assert await repo.get_hook(WS, pid) is None
    assert (await repo.get_schedule(WS, out["automation_id"]))["hookPublicIds"] == []
    await automation_hooks.delete(WS, pid)  # already gone is fine


async def test_delete_google_stops_watch_and_cancels_keeper(env):
    run_gog = MagicMock(return_value=_channel())
    with patch("core.services.google_watch._run_gog", run_gog):
        out = await _seed("google", ["calendar.changed"], resource="ada@example.com")
        pid = out["trigger_public_id"]
        await automation_hooks.delete(WS, pid)
    argv = run_gog.call_args.args[0]
    assert argv[:6] == ["gog", "api", "call", "calendar", "v3", "channels.stop"]
    assert "res_1" in argv[7]
    assert env.temporal.cancelled == [f"gwatch:{WS}:{pid}"]


async def test_arm_stores_secret_and_arms(env):
    out = await _seed("stripe", ["invoice.payment_failed"])
    pid = out["trigger_public_id"]
    assert await automation_hooks.arm(WS, pid, "whsec_live") is True
    assert env.secrets[(WS, f"hook-{pid}")] == {"secret": "whsec_live", "mode": "hmac_sha256"}
    assert (await repo.get_hook(WS, pid))["armed"] is True
    assert await automation_hooks.arm(WS, "0" * 24, "x") is False


async def test_switch_off_seed_unchanged(env):
    with (
        patch("core.services.inbound_triggers.flags_for", AsyncMock(return_value=False)),
        patch("core.services.inbound_triggers._seed_trigger_routine", AsyncMock(return_value={"routine_id": "rt"})),
        patch("core.services.automation_hooks.seed", AsyncMock()) as sa_seed,
    ):
        out = await seed_trigger_routine(WS, "stripe", ["invoice.payment_failed"], actor_user_id=USER)
    assert out == {"routine_id": "rt"} and not sa_seed.called
    assert await repo.list_schedules(WS) == []


# --- the webhook-secret route --------------------------------------------------------------------


@pytest.fixture
def api(env):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


def _as_org_admin():
    from core.auth import AuthContext

    return AuthContext(user_id=USER, org_id=WS, org_role="org:admin")


async def test_webhook_secret_put_super_agent_branch(env, api, monkeypatch):
    from core.auth import get_current_user
    from main import app

    out = await _seed("stripe", ["invoice.payment_failed"])
    pid = out["trigger_public_id"]
    app.dependency_overrides[get_current_user] = _as_org_admin
    try:
        with (
            patch("routers.automations.flags_for", AsyncMock(return_value=True)),
            patch("routers.automations._resolve_trigger_id", AsyncMock()) as resolve,
        ):
            r = api.put(f"/api/v1/automations/{pid}/webhook-secret", json={"webhook_secret": "whsec_live"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 200 and r.json() == {"status": "stored"}
    assert not resolve.called
    assert env.secrets[(WS, f"hook-{pid}")]["secret"] == "whsec_live"
    assert (await repo.get_hook(WS, pid))["armed"] is True


async def test_webhook_secret_put_switch_off_unchanged(env, api):
    from core.auth import get_current_user
    from main import app

    out = await _seed("stripe", ["invoice.payment_failed"])
    pid = out["trigger_public_id"]
    app.dependency_overrides[get_current_user] = _as_org_admin
    try:
        with (
            patch("routers.automations.flags_for", AsyncMock(return_value=False)),
            patch("routers.automations._resolve_trigger_id", AsyncMock(return_value=None)) as resolve,
        ):
            r = api.put(f"/api/v1/automations/{pid}/webhook-secret", json={"webhook_secret": "whsec_live"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 404 and resolve.called  # today's Paperclip path
    assert (await repo.get_hook(WS, pid))["armed"] is False


def test_hook_row_fields_in_schema():
    fields = ts.Hook.model_fields
    assert {"resource", "topic", "page_token"} <= set(fields)
