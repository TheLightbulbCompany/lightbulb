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

    with (
        patch("core.services.credentials.integration_secret_store.get", side_effect=get),
        patch("core.services.credentials.integration_secret_store.put", side_effect=put),
        patch("core.services.credentials.integration_secret_store.delete", side_effect=delete),
        patch("core.services.inbound_triggers.httpx.AsyncClient", new=fake_client),
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
        yield MagicMock(temporal=temporal, secrets=secrets, http=http, funnel=funnel)


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
            "core.services.automation_hooks.integrations_repo.get_meta_webhook_bindings",
            AsyncMock(return_value=bindings),
        ),
        patch("core.services.automation_hooks.integrations_repo.set_meta_webhook_bindings", AsyncMock()) as setb,
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


async def test_delete_meta_keeps_a_sibling_hooks_subscription(env):
    """Two automations on one Page share Meta's one subscription: deleting the first keeps the Page
    subscribed with the sibling's fields; deleting the last unsubscribes it."""
    asset = {"id": "1112223334", "label": "Acme Page", "object_type": "page", "graph_base": "g", "token": "t"}
    bindings: list[dict] = []
    with (
        patch.dict(inbound_triggers._META_ASSET_LISTERS, {"meta_ads": AsyncMock(return_value=[asset])}),
        patch("core.services.inbound_triggers._subscribe_meta_asset", AsyncMock()) as subscribe,
        patch("core.services.inbound_triggers._unsubscribe_meta_asset", AsyncMock()) as unsubscribe,
        patch(
            "core.services.automation_hooks.integrations_repo.get_meta_webhook_bindings",
            AsyncMock(side_effect=lambda *a: list(bindings)),
        ),
        patch(
            "core.services.automation_hooks.integrations_repo.set_meta_webhook_bindings",
            AsyncMock(side_effect=lambda owner, provider, rows: bindings.__setitem__(slice(None), list(rows))),
        ),
    ):
        first = await _seed("meta_ads", ["leadgen"], resource="1112223334")
        second = await _seed("meta_ads", ["feed"], resource="1112223334")
        assert len(bindings) == 2
        first_fields, second_fields = bindings[0]["fields"], bindings[1]["fields"]
        assert first_fields and second_fields and set(first_fields) != set(second_fields)
        # the second seed subscribes the UNION, not just its own fields (it would overwrite the first's)
        assert subscribe.await_args.args == (asset, sorted({*first_fields, *second_fields}))
        await automation_hooks.delete(WS, first["trigger_public_id"])
        assert not unsubscribe.called
        assert subscribe.await_args.args == (asset, sorted(second_fields))  # narrowed to the sibling's fields
        assert [b["trigger_public_id"] for b in bindings] == [second["trigger_public_id"]]
        await automation_hooks.delete(WS, second["trigger_public_id"])
        unsubscribe.assert_awaited_once_with(asset)
        assert bindings == []


async def test_delete_guided_hook_needs_no_temporal(env, monkeypatch):
    from core.temporal import client as tc

    out = await _seed("stripe", ["invoice.payment_failed"])
    monkeypatch.setattr(tc, "_client", None)  # a Stripe hook has no keeper: Temporal is never asked
    await automation_hooks.delete(WS, out["trigger_public_id"])
    assert await repo.get_hook(WS, out["trigger_public_id"]) is None


async def test_seed_link_conflict_unwinds_the_hook(env):
    from core.repositories.tasks_repo import VersionConflict

    with patch("core.services.automation_hooks.repo.update_schedule", AsyncMock(side_effect=VersionConflict("x"))):
        with pytest.raises(VersionConflict):
            await _seed("github", ["pull_request"], resource="acme/site")
    assert await repo.list_schedules(WS) == []
    assert not [k for k in env.secrets if k[1].startswith("hook-")]
    assert not env.http.post.called  # nothing registered provider-side either


async def test_attach_refuses_to_reassign(env):
    first = await _seed("stripe", ["invoice.payment_failed"])
    with pytest.raises(ValueError, match="can't reassign"):
        await _seed("stripe", ["charge.refunded"], routine_id=first["automation_id"], assignee_agent_id="agent_x")


# --- the webhook-secret route --------------------------------------------------------------------


@pytest.fixture
def api(env):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


def _as_org_admin():
    from core.auth import AuthContext

    return AuthContext(user_id=USER, org_id=WS, org_role="org:admin")


def test_hook_row_fields_in_schema():
    fields = ts.Hook.model_fields
    assert {"resource", "topic", "page_token"} <= set(fields)


# --- refused before anything exists (ported from the Paperclip-era test_inbound_triggers.py) ------------------
# Each goes through automation_hooks.seed -> inbound_triggers._resolve_registration: the ownership and injection
# fences on the resource the owner names. Nothing may be written or registered.


async def _refused(provider, events, match, **kw):
    with pytest.raises(ValueError, match=match):
        await _seed(provider, events, **kw)
    assert await repo.list_schedules(WS) == []


async def test_meta_asset_not_on_the_owners_account_is_refused(env):
    mine = {"id": "1112223334", "label": "Acme Page", "object_type": "page", "graph_base": "g", "token": "t"}
    with (
        patch.dict(inbound_triggers._META_ASSET_LISTERS, {"meta_ads": AsyncMock(return_value=[mine])}),
        patch("core.services.inbound_triggers._subscribe_meta_asset", AsyncMock()) as subscribe,
    ):
        await _refused("meta_ads", ["leadgen"], "isn't on your connected account", resource="9998887776")
    subscribe.assert_not_awaited()


@pytest.mark.parametrize("resource", [None, "", "12ab", "123/../../me", "1 OR 1"])
async def test_non_numeric_meta_id_is_refused(env, resource):
    lister = AsyncMock(return_value=[])
    with patch.dict(inbound_triggers._META_ASSET_LISTERS, {"meta_ads": lister}):
        await _refused("meta_ads", ["leadgen"], "numeric Meta asset id", resource=resource)
    lister.assert_not_awaited()


async def test_instagram_not_connected_is_refused(env):
    lister = AsyncMock(side_effect=LookupError("no instagram"))
    with patch.dict(inbound_triggers._META_ASSET_LISTERS, {"instagram": lister}):
        await _refused("instagram", ["comments"], "not connected", resource="1112223334")


@pytest.mark.parametrize("resource", [None, "bob@example.com", "ada@example.com; rm -rf /"])
async def test_google_account_not_connected_is_refused(env, resource):
    await _refused("google", ["calendar.changed"], "Google account", resource=resource)
    assert env.temporal.started == []


@pytest.mark.parametrize("resource", [None, "not-a-repo", "acme/site; curl evil", "../etc/passwd"])
async def test_github_needs_an_owner_repo(env, resource):
    await _refused("github", ["pull_request"], "owner/repo", resource=resource)
    env.http.post.assert_not_awaited()


async def test_github_needs_a_connected_token(env):
    env.secrets.pop((WS, "github"))
    await _refused("github", ["pull_request"], "GitHub is not connected", resource="acme/site")
    env.http.post.assert_not_awaited()


@pytest.mark.parametrize("resource", [None, "prj 1; curl evil", "prj_1/../x"])
async def test_vercel_needs_a_project_id(env, resource):
    await _refused("vercel_cli", ["deployment.error"], "Vercel project id", resource=resource)
    env.http.post.assert_not_awaited()


async def test_vercel_needs_a_connected_token(env):
    env.secrets.pop((WS, "vercel_cli"))
    await _refused("vercel_cli", ["deployment.error"], "Vercel is not connected", resource="prj_1")
    env.http.post.assert_not_awaited()
