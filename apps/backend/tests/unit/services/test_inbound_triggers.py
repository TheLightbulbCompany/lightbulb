"""Tests for the Automations Phase 2 trigger-seed service.

Mirrors the ``_resolver_env`` fake-collaborator idiom from
``test_paperclip_trigger_proxy.py``: patch ``PaperclipRepo`` (company
resolution), ``get_admin_session_cookie``, and ``PaperclipAdminClient`` so
nothing here touches a real Paperclip deployment.
"""

import json
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services.inbound_triggers import seed_trigger_routine


def _fake_admin(*, friday_id="friday_1", routine_id="rt_1", public_id="pub_1", secret_material=None, trigger_id=None):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[{"id": friday_id, "role": "ceo"}])
    admin.create_routine = AsyncMock(return_value={"id": routine_id})
    trigger = {"publicId": public_id}
    # The internal trigger id — the auto_vercel arm (rotate-secret) needs it.
    if trigger_id is not None:
        trigger["id"] = trigger_id
    trigger_resp = {"trigger": trigger}
    if secret_material is not None:
        trigger_resp["secretMaterial"] = secret_material
    admin.create_routine_trigger = AsyncMock(return_value=trigger_resp)
    admin.rotate_routine_trigger_secret = AsyncMock(return_value={})
    admin.patch_routine = AsyncMock(return_value={})
    admin.delete_routine = AsyncMock(return_value={})
    return admin


def _env(admin, *, company_id="c_1", company_status="active"):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id=company_id, status=company_status))
    repo.get_org_company_id = AsyncMock(return_value=company_id)
    return (
        patch("core.services.inbound_triggers.PaperclipRepo", return_value=repo),
        patch("core.services.inbound_triggers.get_admin_session_cookie", AsyncMock(return_value="s=1")),
        patch("core.services.inbound_triggers.PaperclipAdminClient", return_value=admin),
    )


@pytest.mark.asyncio
async def test_seed_trigger_routine_stripe_guided():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")

    assert out["guided"] is True
    assert out["routine_id"] == "rt_1"
    assert out["trigger_public_id"] == "pub_1"
    assert out["proxy_url"].endswith("/api/v1/hooks/t/owner_1/pub_1")
    assert "invoice.payment_failed" in out["instructions"]
    assert out["proxy_url"] in out["instructions"]


@pytest.mark.asyncio
async def test_seed_trigger_routine_builds_expected_bodies():
    """The routine and trigger bodies sent upstream — title/description carry
    the interpolated event label, the routine assigns to the resolved Friday
    id, and the webhook trigger is armed with Paperclip's native
    ``hmac_sha256`` signingMode straight from the registry (Stripe's exact
    ``{t}.{body}`` scheme) — Paperclip verifies natively now, there is no
    Isol8-side edge verification."""
    admin = _fake_admin(friday_id="friday_xyz")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", "charge.refunded")

    routine_body = admin.create_routine.await_args.kwargs["body"]
    assert routine_body["title"] == "Stripe: Charge refunded"
    assert "Charge refunded" in routine_body["description"]
    assert routine_body["assigneeAgentId"] == "friday_xyz"
    assert admin.create_routine.await_args.kwargs["company_id"] == "c_1"

    trigger_call = admin.create_routine_trigger.await_args.kwargs
    assert trigger_call["routine_id"] == "rt_1"
    trigger_body = trigger_call["body"]
    assert trigger_body["kind"] == "webhook"
    # inbound.signing_mode IS a native Paperclip signingMode, passed straight
    # through — Stripe's registry entry declares "hmac_sha256".
    assert trigger_body["signingMode"] == "hmac_sha256"
    assert trigger_body["replayWindowSec"] == 300
    assert trigger_body["label"] == "Stripe: Charge refunded"


@pytest.mark.asyncio
async def test_seed_trigger_routine_bails_when_provider_has_no_inbound():
    # notion has no `inbound:` block, so it isn't a trigger source.
    with pytest.raises(ValueError, match="not wired as an Automations trigger source"):
        await seed_trigger_routine("owner_1", "notion", "whatever")


@pytest.mark.asyncio
async def test_seed_trigger_routine_unknown_event_id():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="unknown event"):
        await seed_trigger_routine("owner_1", "stripe", "not.a.real.event")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_no_active_company():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin, company_status="seeding")
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="no active Paperclip company"):
        await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_uses_org_company_lookup_for_org_owner():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin, company_id="c_org")
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("org_abc", "stripe", "invoice.payment_failed")
    assert out["proxy_url"].endswith("/api/v1/hooks/t/org_abc/pub_1")
    assert admin.create_routine.await_args.kwargs["company_id"] == "c_org"


@pytest.mark.asyncio
async def test_seed_trigger_routine_no_friday_agent_found():
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[])
    admin.create_routine = AsyncMock()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="no CEO agent"):
        await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_trigger_create_failure_rolls_back_routine():
    """create_routine_trigger throwing (timeout, upstream error) must not
    leave the routine it already created orphaned."""
    admin = _fake_admin(routine_id="rt_orphan_trigger")
    admin.create_routine_trigger = AsyncMock(side_effect=RuntimeError("upstream 500"))
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(RuntimeError, match="upstream 500"):
        await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_trigger"


@pytest.mark.asyncio
async def test_seed_trigger_routine_trigger_missing_public_id_rolls_back_routine():
    """A malformed create_routine_trigger response (no trigger.publicId) is
    also a trigger-create failure — the routine it already created must be
    rolled back the same way an outright exception would be."""
    admin = _fake_admin(routine_id="rt_orphan_malformed")
    admin.create_routine_trigger = AsyncMock(return_value={"trigger": {}})
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(RuntimeError, match="no trigger.publicId"):
        await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_malformed"


@pytest.mark.asyncio
async def test_seed_trigger_routine_sentry_uses_registry_copy_and_github_hmac():
    """A Tier-2 raw-body-HMAC provider (Sentry) seeds with Paperclip's native
    ``github_hmac`` mode and surfaces its registry-authored guided copy —
    Sentry's webhook lives on an internal integration, not a plain
    "add a webhook endpoint" page, so the per-provider instructions and
    secret_hint are what keep the owner from getting lost mid-setup."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "sentry", "issue")

    trigger_body = admin.create_routine_trigger.await_args.kwargs["body"]
    assert trigger_body["signingMode"] == "github_hmac"

    assert out["guided"] is True
    # Registry instructions, not the generic fallback — with the placeholders
    # interpolated.
    assert "Developer Settings" in out["instructions"]
    assert out["proxy_url"] in out["instructions"]
    assert "Issue created or changed" in out["instructions"]
    assert out["secret_hint"] == "Client Secret from the integration"


@pytest.mark.asyncio
async def test_seed_trigger_routine_stripe_returns_secret_hint():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "stripe", "charge.refunded")

    assert out["secret_hint"] == "whsec_…"


# --- auto_github ------------------------------------------------------------
#
# GitHub HTTP is mocked by patching the module's httpx.AsyncClient directly
# (same approach as test_clerk_admin.py — respx 0.20.2 + newer httpx have a
# route-matching incompat). The seed opens TWO clients: the Paperclip one
# (whose requests never fire — the admin client and session are mocked) and
# the GitHub one, so a single fake client serves both.

_GH_SECRET = {"webhookUrl": "https://pc/hooks/x", "webhookSecret": "pcsec_1"}


def _gh_token_patch(token="gh_tok_1"):
    credential = {"token": token} if token else None
    return patch(
        "core.services.inbound_triggers.integration_secret_store.get",
        AsyncMock(return_value=credential),
    )


def _gh_response(status_code: int, json_body=None) -> httpx.Response:
    # A real Response (with a request attached) so raise_for_status() works.
    return httpx.Response(
        status_code,
        json=json_body if json_body is not None else {},
        request=httpx.Request("POST", "https://api.github.com/x"),
    )


def _gh_http_patch(response: httpx.Response):
    """Patch httpx.AsyncClient in the module with a fake whose get/post/delete
    answer ``response``. Returns (patcher, client) so tests can assert on the
    call."""
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    client.delete = AsyncMock(return_value=response)

    @asynccontextmanager
    async def _fake(*args, **kwargs):
        yield client

    return patch("core.services.inbound_triggers.httpx.AsyncClient", new=_fake), client


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_github_registers_hook():
    """The auto shape: the GitHub hook is created on the named repo, pointed
    at the proxy URL, armed with the trigger create's one-time secretMaterial
    secret, and subscribed to the registry's provider_events — no guided
    paste step remains, the result is live."""
    admin = _fake_admin(secret_material=_GH_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, gh_client = _gh_http_patch(_gh_response(201, {"id": 99}))
    with p_repo, p_cookie, p_client, _gh_token_patch(), p_http:
        out = await seed_trigger_routine("owner_1", "github", "pull_request", resource="acme/site")

    gh_client.post.assert_awaited_once()
    hook_url = gh_client.post.await_args.args[0]
    assert hook_url == "https://api.github.com/repos/acme/site/hooks"
    hook_body = gh_client.post.await_args.kwargs["json"]
    assert hook_body["config"]["url"] == out["proxy_url"]
    assert hook_body["config"]["secret"] == "pcsec_1"
    assert hook_body["config"]["content_type"] == "json"
    assert hook_body["events"] == ["pull_request"]
    assert hook_body["active"] is True
    headers = gh_client.post.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer gh_tok_1"

    trigger_body = admin.create_routine_trigger.await_args.kwargs["body"]
    assert trigger_body["signingMode"] == "github_hmac"

    assert out["guided"] is False
    assert out["resource"] == "acme/site"
    assert out["instructions"] is None
    assert out["secret_hint"] is None


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_github_requires_resource():
    """A missing or malformed repo fails BEFORE anything exists upstream."""
    admin = _fake_admin(secret_material=_GH_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    for bad in (None, "no-slash", "a/b/c", "own er/repo"):
        with p_repo, p_cookie, p_client, _gh_token_patch(), pytest.raises(ValueError, match="owner/repo"):
            await seed_trigger_routine("owner_1", "github", "push", resource=bad)
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_github_requires_connected_token():
    admin = _fake_admin(secret_material=_GH_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _gh_token_patch(token=None), pytest.raises(ValueError, match="not connected"):
        await seed_trigger_routine("owner_1", "github", "push", resource="acme/site")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_github_missing_secret_material_rolls_back():
    """No secretMaterial.webhookSecret in the trigger create response means
    there is nothing to arm the GitHub hook with — same severity as a missing
    publicId: roll the routine back, and never touch GitHub."""
    admin = _fake_admin(routine_id="rt_no_secret")  # no secretMaterial in the response
    p_repo, p_cookie, p_client = _env(admin)
    p_http, gh_client = _gh_http_patch(_gh_response(201, {"id": 99}))
    with (
        p_repo,
        p_cookie,
        p_client,
        _gh_token_patch(),
        p_http,
        pytest.raises(RuntimeError, match="secretMaterial.webhookSecret"),
    ):
        await seed_trigger_routine("owner_1", "github", "push", resource="acme/site")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_no_secret"
    gh_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_github_hook_403_rolls_back():
    """GitHub refusing the hook (no admin on the repo) unwinds the routine
    and surfaces a caller-facing ValueError, not an opaque 500."""
    admin = _fake_admin(routine_id="rt_forbidden", secret_material=_GH_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, _ = _gh_http_patch(_gh_response(403, {"message": "Forbidden"}))
    with p_repo, p_cookie, p_client, _gh_token_patch(), p_http, pytest.raises(ValueError, match="admin access"):
        await seed_trigger_routine("owner_1", "github", "issues", resource="acme/site")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_forbidden"


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_github_hook_422_duplicate_rolls_back():
    admin = _fake_admin(routine_id="rt_dupe", secret_material=_GH_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, _ = _gh_http_patch(_gh_response(422, {"message": "Hook already exists on this repository"}))
    with p_repo, p_cookie, p_client, _gh_token_patch(), p_http, pytest.raises(ValueError, match="already exists"):
        await seed_trigger_routine("owner_1", "github", "issues", resource="acme/site")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_dupe"


@pytest.mark.asyncio
async def test_list_github_repos_maps_full_names():
    from core.services.inbound_triggers import list_github_repos

    p_http, gh_client = _gh_http_patch(_gh_response(200, [{"full_name": "acme/site"}, {"full_name": "acme/api"}]))
    with _gh_token_patch(), p_http:
        out = await list_github_repos("owner_1")
    assert out == [
        {"id": "acme/site", "label": "acme/site"},
        {"id": "acme/api", "label": "acme/api"},
    ]
    assert gh_client.get.await_args.args[0] == "https://api.github.com/user/repos"
    assert gh_client.get.await_args.kwargs["params"] == {"per_page": 100, "sort": "updated"}


@pytest.mark.asyncio
async def test_list_github_repos_not_connected_raises_lookup_error():
    from core.services.inbound_triggers import list_github_repos

    with _gh_token_patch(token=None), pytest.raises(LookupError, match="not connected"):
        await list_github_repos("owner_1")


# --- auto_vercel ------------------------------------------------------------
#
# Same mocking approach as auto_github above. `_gh_token_patch` is reused for
# the Vercel credential — it patches the shared integration_secret_store.get,
# and the paste rail stores the same {"token": ...} shape for vercel_cli.

_VC_WEBHOOK = {"id": "hook_1", "secret": "vcsec_1"}


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_vercel_registers_webhook_and_arms():
    """The Vercel auto shape: the webhook is created on the named project,
    pointed at the proxy URL and subscribed to the registry's
    provider_events — then the trigger is ARMED with the secret Vercel
    generated (the opposite direction from GitHub). No paste step remains,
    the result is live."""
    admin = _fake_admin(trigger_id="trig_int_1")
    p_repo, p_cookie, p_client = _env(admin)
    p_http, vc_client = _gh_http_patch(_gh_response(200, _VC_WEBHOOK))
    with p_repo, p_cookie, p_client, _gh_token_patch(token="vc_tok_1"), p_http:
        out = await seed_trigger_routine("owner_1", "vercel_cli", "deployment.error", resource="prj_abc123")

    vc_client.post.assert_awaited_once()
    assert vc_client.post.await_args.args[0] == "https://api.vercel.com/v1/webhooks"
    webhook_body = vc_client.post.await_args.kwargs["json"]
    assert webhook_body == {
        "url": out["proxy_url"],
        "events": ["deployment.error"],
        "projectIds": ["prj_abc123"],
    }
    assert vc_client.post.await_args.kwargs["headers"] == {"Authorization": "Bearer vc_tok_1"}

    trigger_body = admin.create_routine_trigger.await_args.kwargs["body"]
    assert trigger_body["signingMode"] == "hmac_sha1"

    admin.rotate_routine_trigger_secret.assert_awaited_once_with(
        session_cookie="s=1", trigger_id="trig_int_1", secret="vcsec_1"
    )

    assert out["guided"] is False
    assert out["resource"] == "prj_abc123"
    assert out["instructions"] is None
    assert out["secret_hint"] is None


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_vercel_requires_resource():
    """A missing or malformed project id fails BEFORE anything exists upstream."""
    admin = _fake_admin(trigger_id="trig_int_1")
    p_repo, p_cookie, p_client = _env(admin)
    for bad in (None, "prj/abc", "prj abc", "prj-abc!"):
        with (
            p_repo,
            p_cookie,
            p_client,
            _gh_token_patch(token="vc_tok_1"),
            pytest.raises(ValueError, match="project id"),
        ):
            await seed_trigger_routine("owner_1", "vercel_cli", "deployment.error", resource=bad)
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_vercel_requires_connected_token():
    admin = _fake_admin(trigger_id="trig_int_1")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _gh_token_patch(token=None), pytest.raises(ValueError, match="not connected"):
        await seed_trigger_routine("owner_1", "vercel_cli", "deployment.error", resource="prj_abc123")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_vercel_missing_trigger_id_rolls_back():
    """No trigger.id in the create response means the trigger can never be
    armed with Vercel's secret — same severity as a missing publicId: roll
    the routine back, and never touch Vercel."""
    admin = _fake_admin(routine_id="rt_no_trigger_id")  # no trigger.id in the response
    p_repo, p_cookie, p_client = _env(admin)
    p_http, vc_client = _gh_http_patch(_gh_response(200, _VC_WEBHOOK))
    with (
        p_repo,
        p_cookie,
        p_client,
        _gh_token_patch(token="vc_tok_1"),
        p_http,
        pytest.raises(RuntimeError, match="trigger.id"),
    ):
        await seed_trigger_routine("owner_1", "vercel_cli", "deployment.error", resource="prj_abc123")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_no_trigger_id"
    vc_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_vercel_create_403_rolls_back():
    """Vercel refusing the webhook (plan/token can't create them) unwinds the
    routine and surfaces a caller-facing ValueError, not an opaque 500."""
    admin = _fake_admin(routine_id="rt_forbidden_vc", trigger_id="trig_int_1")
    p_repo, p_cookie, p_client = _env(admin)
    p_http, _ = _gh_http_patch(_gh_response(403, {"error": {"code": "forbidden"}}))
    with (
        p_repo,
        p_cookie,
        p_client,
        _gh_token_patch(token="vc_tok_1"),
        p_http,
        pytest.raises(ValueError, match="plan or token"),
    ):
        await seed_trigger_routine("owner_1", "vercel_cli", "deployment.error", resource="prj_abc123")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_forbidden_vc"
    admin.rotate_routine_trigger_secret.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_vercel_arm_failure_deletes_webhook_and_rolls_back():
    """The ARM (rotate-secret) failing after the Vercel webhook exists must
    delete the webhook AND unwind the routine — a live webhook pointed at a
    trigger still holding its random placeholder would deliver into signature
    failures forever."""
    admin = _fake_admin(routine_id="rt_arm_fail", trigger_id="trig_int_1")
    admin.rotate_routine_trigger_secret = AsyncMock(side_effect=RuntimeError("upstream 500"))
    p_repo, p_cookie, p_client = _env(admin)
    p_http, vc_client = _gh_http_patch(_gh_response(200, _VC_WEBHOOK))
    with (
        p_repo,
        p_cookie,
        p_client,
        _gh_token_patch(token="vc_tok_1"),
        p_http,
        pytest.raises(RuntimeError, match="upstream 500"),
    ):
        await seed_trigger_routine("owner_1", "vercel_cli", "deployment.error", resource="prj_abc123")
    vc_client.delete.assert_awaited_once()
    assert vc_client.delete.await_args.args[0] == "https://api.vercel.com/v1/webhooks/hook_1"
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_arm_fail"


@pytest.mark.asyncio
async def test_list_vercel_projects_maps_ids_and_names():
    from core.services.inbound_triggers import list_vercel_projects

    p_http, vc_client = _gh_http_patch(
        _gh_response(200, {"projects": [{"id": "prj_1", "name": "site"}, {"id": "prj_2", "name": "api"}]})
    )
    with _gh_token_patch(token="vc_tok_1"), p_http:
        out = await list_vercel_projects("owner_1")
    assert out == [
        {"id": "prj_1", "label": "site"},
        {"id": "prj_2", "label": "api"},
    ]
    assert vc_client.get.await_args.args[0] == "https://api.vercel.com/v9/projects"


@pytest.mark.asyncio
async def test_list_vercel_projects_not_connected_raises_lookup_error():
    from core.services.inbound_triggers import list_vercel_projects

    with _gh_token_patch(token=None), pytest.raises(LookupError, match="not connected"):
        await list_vercel_projects("owner_1")


# --- auto_google_watch --------------------------------------------------------
#
# The gog layer is mocked at google_watch's runner helper (_run_gog), with
# _gog_env stubbed so no EFS paths are touched — the REAL command construction
# runs, and the tests assert exact argv. The fence being pinned: caller data
# (the account email, the channel token) rides only as discrete argv elements.
# The agent-side state file is written for real, under tmp_path via
# _efs_root_patch (atomic_write skips its chown on a non-root euid, same as
# the materializer tests).

_GOOG_SECRET = {"webhookUrl": "https://pc/hooks/x", "webhookSecret": "pcsec_g1"}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# The renewal schedule-trigger body every google shape attaches second.
_RENEWAL_TRIGGER_BODY = {
    "kind": "schedule",
    "cronExpression": "0 6 * * 1,4",
    "timezone": "UTC",
    "enabled": True,
    "label": "Keep the Google watch alive",
}


def _google_accounts_patch(emails=("ada@example.com",)):
    return patch(
        "core.services.inbound_triggers.google_accounts_repo.list_accounts",
        AsyncMock(return_value=[{"email": e} for e in emails]),
    )


def _gog_env_patch():
    return patch("core.services.google_watch._gog_env", return_value={"FAKE": "env"})


def _efs_root_patch(tmp_path):
    return patch("core.services.inbound_triggers.owner_efs_root", lambda owner_id: tmp_path / owner_id)


def _channels_get_patch(existing=()):
    return patch(
        "core.services.inbound_triggers.integrations_repo.get_google_watch_channels",
        AsyncMock(return_value=list(existing)),
    )


def _channels_set_patch():
    return patch(
        "core.services.inbound_triggers.integrations_repo.set_google_watch_channels",
        new_callable=AsyncMock,
    )


def _channel_response(resource_id="res_1", expiration="1799999999000"):
    return json.dumps({"kind": "api#channel", "resourceId": resource_id, "expiration": expiration})


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_google_watch_calendar(tmp_path):
    """The calendar shape: events.watch via gog's generic Discovery caller on
    the picked account's primary calendar, channel token = the trigger's own
    one-time secret, bearer signingMode — then the self-renewal seed: a
    second (schedule) trigger on the same routine, the agent-side state file
    on the owner's EFS, and the routine description patched to open with the
    maintenance clause. The integrations row is NOT touched: nothing
    platform-side reads calendar/drive channels anymore."""
    admin = _fake_admin(secret_material=_GOOG_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    run_gog = MagicMock(return_value=_channel_response(resource_id="res_cal_1"))
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch("core.services.google_watch._run_gog", run_gog),
        _efs_root_patch(tmp_path),
        _channels_get_patch() as get_channels,
        _channels_set_patch() as set_channels,
    ):
        out = await seed_trigger_routine("owner_1", "google", "calendar.changed", resource="ada@example.com")

    # Two triggers on the same routine: the webhook first, then the renewal
    # schedule.
    assert admin.create_routine_trigger.await_count == 2
    webhook_call, schedule_call = admin.create_routine_trigger.await_args_list
    assert webhook_call.kwargs["body"]["kind"] == "webhook"
    assert webhook_call.kwargs["body"]["signingMode"] == "bearer"
    assert schedule_call.kwargs["routine_id"] == "rt_1"
    assert schedule_call.kwargs["body"] == _RENEWAL_TRIGGER_BODY

    routine_body = admin.create_routine.await_args.kwargs["body"]
    assert "ada@example.com" in routine_body["description"]

    run_gog.assert_called_once()
    argv = run_gog.call_args.args[0]
    assert argv[:6] == ["gog", "api", "call", "calendar", "v3", "events.watch"]
    assert argv[6:8] == ["--params", '{"calendarId":"primary"}']
    assert argv[8] == "--body"
    watch_body = json.loads(argv[9])
    assert _UUID_RE.match(watch_body["id"])
    assert watch_body["type"] == "web_hook"
    assert watch_body["address"] == out["proxy_url"]
    assert watch_body["token"] == "pcsec_g1"  # channel token = the trigger's own secret
    assert watch_body["params"] == {"ttl": "604800"}
    assert argv[10:] == ["--allow-write", "--force", "-a", "ada@example.com"]

    # The maintenance clause is patched onto the description, ahead of the
    # act-on-change half, with the concrete path/address/account interpolated
    # — and NO secret (the description is visible in the Teams UI).
    patch_call = admin.patch_routine.await_args
    assert patch_call.kwargs["routine_id"] == "rt_1"
    desc = patch_call.kwargs["body"]["description"]
    assert desc.startswith("Every run, FIRST do maintenance")
    assert "/home/node/.openclaw/google-watch/pub_1.json" in desc
    assert out["proxy_url"] in desc
    assert "-a ada@example.com" in desc
    assert "pcsec_g1" not in desc
    assert "A Google push notification just fired" in desc

    # The agent-side state file: everything a re-arm needs, including the
    # secret — 0600 on the owner's own EFS.
    state_file = tmp_path / "owner_1" / "google-watch" / "pub_1.json"
    assert json.loads(state_file.read_text()) == {
        "kind": "calendar",
        "account": "ada@example.com",
        "calendar_id": "primary",
        "address": out["proxy_url"],
        "channel_token": "pcsec_g1",
        "channel_id": watch_body["id"],
        "resource_id": "res_cal_1",
        "expiration_ms": 1799999999000,
    }  # no page_token key — calendar watches have none

    get_channels.assert_not_awaited()
    set_channels.assert_not_awaited()

    assert out["guided"] is False
    assert out["resource"] == "ada@example.com"
    assert out["instructions"] is None
    assert out["secret_hint"] is None


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_google_watch_drive(tmp_path):
    """The drive shape: a fresh changes start-token first, then changes.watch
    with the token/webhook-url/channel-id/channel-token flags — the start
    pageToken kept in the state file (a drive re-arm needs it), the renewal
    trigger attached, and no integrations-row write."""
    admin = _fake_admin(secret_material=_GOOG_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    run_gog = MagicMock(
        side_effect=[
            json.dumps({"startPageToken": "pt_77"}),
            _channel_response(resource_id="res_drv_1"),
        ]
    )
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch("core.services.google_watch._run_gog", run_gog),
        _efs_root_patch(tmp_path),
        _channels_get_patch() as get_channels,
        _channels_set_patch() as set_channels,
    ):
        out = await seed_trigger_routine("owner_1", "google", "drive.changed", resource="ada@example.com")

    assert run_gog.call_count == 2
    token_argv = run_gog.call_args_list[0].args[0]
    assert token_argv == ["gog", "drive", "changes", "start-token", "-a", "ada@example.com"]

    watch_argv = run_gog.call_args_list[1].args[0]
    assert watch_argv[:4] == ["gog", "drive", "changes", "watch"]
    assert watch_argv[4] == "--token=pt_77"
    assert watch_argv[5] == f"--webhook-url={out['proxy_url']}"
    channel_id = watch_argv[6].removeprefix("--channel-id=")
    assert _UUID_RE.match(channel_id)
    assert watch_argv[7] == "--channel-token=pcsec_g1"  # = the trigger's own secret
    assert watch_argv[8].startswith("--expiration-ms=")
    assert int(watch_argv[8].removeprefix("--expiration-ms=")) > 0
    assert watch_argv[9:] == ["-a", "ada@example.com"]

    assert admin.create_routine_trigger.await_count == 2
    assert admin.create_routine_trigger.await_args_list[1].kwargs["body"] == _RENEWAL_TRIGGER_BODY

    # The drive maintenance clause names the drive re-arm command.
    desc = admin.patch_routine.await_args.kwargs["body"]["description"]
    assert "gog drive changes watch" in desc
    assert "/home/node/.openclaw/google-watch/pub_1.json" in desc
    assert "pcsec_g1" not in desc

    state_file = tmp_path / "owner_1" / "google-watch" / "pub_1.json"
    assert json.loads(state_file.read_text()) == {
        "kind": "drive",
        "account": "ada@example.com",
        "address": out["proxy_url"],
        "channel_token": "pcsec_g1",
        "channel_id": channel_id,
        "resource_id": "res_drv_1",
        "expiration_ms": 1799999999000,
        "page_token": "pt_77",
    }  # no calendar_id key — that's the calendar shape's

    get_channels.assert_not_awaited()
    set_channels.assert_not_awaited()

    assert out["guided"] is False
    assert out["resource"] == "ada@example.com"


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_google_watch_requires_connected_account():
    """A missing resource, or an email that is NOT in the owner's connected-
    accounts inventory (the argv injection fence), fails BEFORE anything
    exists upstream."""
    admin = _fake_admin(secret_material=_GOOG_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        pytest.raises(ValueError, match="connected Google account"),
    ):
        await seed_trigger_routine("owner_1", "google", "calendar.changed", resource=None)
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(emails=("ada@example.com",)),
        pytest.raises(ValueError, match="isn't connected"),
    ):
        await seed_trigger_routine("owner_1", "google", "calendar.changed", resource="mallory@evil.example")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_google_watch_gog_failure_rolls_back():
    """gog refusing the watch (broken grant, revoked account) unwinds the
    routine; no channel entry is ever recorded."""
    admin = _fake_admin(routine_id="rt_gog_fail", secret_material=_GOOG_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    run_gog = MagicMock(side_effect=ValueError("Google refused the watch request: invalid_grant"))
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch("core.services.google_watch._run_gog", run_gog),
        _channels_get_patch(),
        _channels_set_patch() as set_channels,
        pytest.raises(ValueError, match="Google refused"),
    ):
        await seed_trigger_routine("owner_1", "google", "calendar.changed", resource="ada@example.com")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_gog_fail"
    set_channels.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_google_watch_renewal_trigger_failure_stops_channel():
    """The renewal schedule-trigger create failing after the channel exists
    must stop the channel (best-effort) AND unwind the routine — without its
    maintenance tick the watch silently dies within a week."""
    admin = _fake_admin(routine_id="rt_renew_fail", secret_material=_GOOG_SECRET)
    admin.create_routine_trigger = AsyncMock(
        side_effect=[
            {"trigger": {"publicId": "pub_1"}, "secretMaterial": _GOOG_SECRET},
            RuntimeError("upstream 500"),
        ]
    )
    p_repo, p_cookie, p_client = _env(admin)
    run_gog = MagicMock(return_value=_channel_response(resource_id="res_x"))
    stop_watch = AsyncMock()
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch("core.services.google_watch._run_gog", run_gog),
        patch("core.services.google_watch.stop_watch", stop_watch),
        pytest.raises(RuntimeError, match="upstream 500"),
    ):
        await seed_trigger_routine("owner_1", "google", "calendar.changed", resource="ada@example.com")
    stop_watch.assert_awaited_once()
    stop_kwargs = stop_watch.await_args.kwargs
    assert stop_kwargs["resource_id"] == "res_x"
    assert stop_kwargs["account_email"] == "ada@example.com"
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_renew_fail"


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_google_watch_missing_secret_material_rolls_back():
    """No secretMaterial.webhookSecret means there is nothing to arm the
    channel token with — roll the routine back, never touch Google."""
    admin = _fake_admin(routine_id="rt_no_goog_secret")  # no secretMaterial
    p_repo, p_cookie, p_client = _env(admin)
    run_gog = MagicMock()
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch("core.services.google_watch._run_gog", run_gog),
        pytest.raises(RuntimeError, match="secretMaterial.webhookSecret"),
    ):
        await seed_trigger_routine("owner_1", "google", "calendar.changed", resource="ada@example.com")
    admin.delete_routine.assert_awaited_once()
    run_gog.assert_not_called()


@pytest.mark.asyncio
async def test_list_google_accounts_maps_emails():
    from core.services.inbound_triggers import list_google_accounts

    with _google_accounts_patch(emails=("ada@example.com", "team@example.com")):
        out = await list_google_accounts("owner_1")
    assert out == [
        {"id": "ada@example.com", "label": "ada@example.com"},
        {"id": "team@example.com", "label": "team@example.com"},
    ]


@pytest.mark.asyncio
async def test_list_google_accounts_not_connected_raises_lookup_error():
    from core.services.inbound_triggers import list_google_accounts

    with _google_accounts_patch(emails=()), pytest.raises(LookupError, match="not connected"):
        await list_google_accounts("owner_1")


def test_google_watch_stop_argv():
    """stop_watch's fixed argv for both surfaces — drive uses the dedicated
    stop command, calendar rides the generic channels.stop Discovery call."""
    from core.services import google_watch

    run_gog = MagicMock(return_value="{}")
    with (
        patch("core.services.google_watch._gog_env", return_value={"FAKE": "env"}),
        patch("core.services.google_watch._run_gog", run_gog),
    ):
        google_watch._stop_watch_sync("owner_1", "ada@example.com", "drive.changed", "chan_1", "res_1")
        assert run_gog.call_args.args[0] == [
            "gog",
            "drive",
            "changes",
            "stop",
            "chan_1",
            "res_1",
            "-a",
            "ada@example.com",
        ]

        google_watch._stop_watch_sync("owner_1", "ada@example.com", "calendar.changed", "chan_2", "res_2")
        argv = run_gog.call_args.args[0]
        assert argv[:6] == ["gog", "api", "call", "calendar", "v3", "channels.stop"]
        assert argv[6] == "--body"
        assert json.loads(argv[7]) == {"id": "chan_2", "resourceId": "res_2"}
        assert argv[8:] == ["--allow-write", "--force", "-a", "ada@example.com"]


# --- gmail (per-event registration: poll / owner_setup) -----------------------
#
# Both shapes ride the google block; the event's `registration` overrides the
# block-level auto_google_watch. Same mocking approach as the watch tests.

_GMAIL_TOPIC = "projects/my-project/topics/isol8-gmail"


@pytest.mark.asyncio
async def test_seed_trigger_routine_gmail_poll_creates_enabled_schedule_trigger():
    """gmail.poll: no webhook at all — the routine gets an ENABLED */10 UTC
    schedule trigger, the description carries the per-event prompt (a
    scheduled gog search, not the push-notification template), and the result
    has poll=True with no trigger_public_id/proxy_url."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _google_accounts_patch():
        out = await seed_trigger_routine("owner_1", "google", "gmail.poll", resource="ada@example.com")

    trigger_call = admin.create_routine_trigger.await_args.kwargs
    assert trigger_call["routine_id"] == "rt_1"
    assert trigger_call["body"] == {
        "kind": "schedule",
        "cronExpression": "*/10 * * * *",
        "timezone": "UTC",
        "enabled": True,
    }

    routine_body = admin.create_routine.await_args.kwargs["body"]
    assert "ada@example.com" in routine_body["description"]
    # The per-event starter_prompt override, not the push-notification template.
    assert "gog gmail search" in routine_body["description"]

    assert out == {
        "routine_id": "rt_1",
        "trigger_public_id": None,
        "proxy_url": None,
        "guided": False,
        "instructions": None,
        "secret_hint": None,
        "resource": "ada@example.com",
        "poll": True,
    }


@pytest.mark.asyncio
async def test_seed_trigger_routine_gmail_poll_requires_connected_account():
    """The connected-accounts inventory fence applies to the poll shape too —
    the email rides into the routine description the agent executes."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _google_accounts_patch(), pytest.raises(ValueError, match="isn't connected"):
        await seed_trigger_routine("owner_1", "google", "gmail.poll", resource="mallory@evil.example")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_gmail_poll_trigger_failure_rolls_back():
    admin = _fake_admin(routine_id="rt_poll_orphan")
    admin.create_routine_trigger = AsyncMock(side_effect=RuntimeError("upstream 500"))
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _google_accounts_patch(), pytest.raises(RuntimeError, match="upstream 500"):
        await seed_trigger_routine("owner_1", "google", "gmail.poll", resource="ada@example.com")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_poll_orphan"


@pytest.mark.asyncio
async def test_seed_trigger_routine_gmail_push_starts_watch_and_records_entry(tmp_path):
    """gmail.message: bearer trigger, `gog gmail watch start` against the
    owner's own topic (INBOX only), the self-renewal seed (schedule trigger +
    secretless state file + maintenance clause), a kind="gmail_watch" entry
    on the google_watch_channels list carrying the trigger's secret (the
    proxy's OIDC bearer lookup still reads it), and the one-time Pub/Sub
    setup instructions in the result."""
    admin = _fake_admin(secret_material=_GOOG_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    run_gog = MagicMock(return_value=json.dumps({"historyId": "1234", "expiration": "1799999999000"}))
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch("core.services.google_watch._run_gog", run_gog),
        _efs_root_patch(tmp_path),
        _channels_get_patch(),
        _channels_set_patch() as set_channels,
    ):
        out = await seed_trigger_routine(
            "owner_1", "google", "gmail.message", resource="ada@example.com", topic=_GMAIL_TOPIC
        )

    assert admin.create_routine_trigger.await_count == 2
    webhook_call, schedule_call = admin.create_routine_trigger.await_args_list
    assert webhook_call.kwargs["body"]["signingMode"] == "bearer"
    assert schedule_call.kwargs["body"] == _RENEWAL_TRIGGER_BODY

    # The gmail maintenance clause: renew needs no secret, so neither the
    # description nor the state file carries one.
    desc = admin.patch_routine.await_args.kwargs["body"]["description"]
    assert "gog gmail watch renew -a ada@example.com" in desc
    assert "/home/node/.openclaw/google-watch/pub_1.json" in desc
    assert "pcsec_g1" not in desc

    state_file = tmp_path / "owner_1" / "google-watch" / "pub_1.json"
    assert json.loads(state_file.read_text()) == {
        "kind": "gmail",
        "account": "ada@example.com",
        "topic": _GMAIL_TOPIC,
        "expiration_ms": 1799999999000,
    }

    run_gog.assert_called_once()
    assert run_gog.call_args.args[0] == [
        "gog",
        "gmail",
        "watch",
        "start",
        "--topic",
        _GMAIL_TOPIC,
        "--label-ids",
        "INBOX",
        "-a",
        "ada@example.com",
    ]

    assert set_channels.await_args.args[0] == "owner_1"
    (entry,) = set_channels.await_args.args[1]
    assert entry == {
        "kind": "gmail_watch",
        "account_email": "ada@example.com",
        "topic": _GMAIL_TOPIC,
        "trigger_public_id": "pub_1",
        "proxy_url": out["proxy_url"],
        "channel_token": "pcsec_g1",  # = the trigger's own secret
        "expiration_ms": 1799999999000,
    }

    assert out["guided"] is False
    assert "poll" not in out
    assert out["resource"] == "ada@example.com"
    assert out["secret_hint"] is None
    # The finish dialog's one-time setup steps, concretely interpolated.
    assert _GMAIL_TOPIC in out["instructions"]
    assert out["proxy_url"] in out["instructions"]
    assert "gmail-api-push@system.gserviceaccount.com" in out["instructions"]


@pytest.mark.asyncio
async def test_seed_trigger_routine_gmail_push_requires_valid_topic():
    """A missing or malformed topic fails BEFORE anything exists upstream —
    the regex is also the argv/instructions injection fence."""
    admin = _fake_admin(secret_material=_GOOG_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    for bad in (
        None,
        "isol8-gmail",  # bare name, not fully qualified
        "projects/My_Proj/topics/isol8",  # project charset violation
        "projects/my-project/topics/",  # empty topic name
        "projects/my-project/topics/a b",  # whitespace injection
    ):
        with (
            p_repo,
            p_cookie,
            p_client,
            _google_accounts_patch(),
            pytest.raises(ValueError, match="Pub/Sub topic"),
        ):
            await seed_trigger_routine("owner_1", "google", "gmail.message", resource="ada@example.com", topic=bad)
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_gmail_push_gog_failure_rolls_back():
    """gog refusing the watch (broken grant, topic Gmail can't publish to)
    unwinds the routine; no entry is ever recorded."""
    admin = _fake_admin(routine_id="rt_gmail_fail", secret_material=_GOOG_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    run_gog = MagicMock(side_effect=ValueError("Google refused the watch request: topicNotFound"))
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch("core.services.google_watch._run_gog", run_gog),
        _channels_get_patch(),
        _channels_set_patch() as set_channels,
        pytest.raises(ValueError, match="Google refused"),
    ):
        await seed_trigger_routine("owner_1", "google", "gmail.message", resource="ada@example.com", topic=_GMAIL_TOPIC)
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_gmail_fail"
    set_channels.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_gmail_push_row_write_failure_stops_watch(tmp_path):
    """The entry write failing after the watch exists must stop the watch
    (best-effort) AND unwind the routine — without its entry the proxy has no
    bearer secret to inject and deliveries would 401 forever."""
    admin = _fake_admin(routine_id="rt_gmail_row_fail", secret_material=_GOOG_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    run_gog = MagicMock(return_value=json.dumps({"historyId": "1", "expiration": "1799999999000"}))
    stop = AsyncMock()
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch("core.services.google_watch._run_gog", run_gog),
        patch("core.services.google_watch.stop_gmail_watch", stop),
        _efs_root_patch(tmp_path),
        _channels_get_patch(),
        _channels_set_patch() as set_channels,
        pytest.raises(RuntimeError, match="ddb down"),
    ):
        set_channels.side_effect = RuntimeError("ddb down")
        await seed_trigger_routine("owner_1", "google", "gmail.message", resource="ada@example.com", topic=_GMAIL_TOPIC)
    stop.assert_awaited_once_with(owner_id="owner_1", account_email="ada@example.com")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_gmail_row_fail"


def test_google_watch_gmail_argv():
    """The fixed argv for the gmail watch pair — caller data rides only as
    discrete argv elements — and the expiration parse. (Renewal has no
    backend argv: the routine's own agent runs `gog gmail watch renew`
    in-container.)"""
    from core.services import google_watch

    run_gog = MagicMock(return_value=json.dumps({"historyId": "7", "expiration": "1799999999000"}))
    with (
        patch("core.services.google_watch._gog_env", return_value={"FAKE": "env"}),
        patch("core.services.google_watch._run_gog", run_gog),
    ):
        out = google_watch._start_gmail_watch_sync("owner_1", "ada@example.com", _GMAIL_TOPIC)
        assert out == {"expiration_ms": 1799999999000}
        assert run_gog.call_args.args[0] == [
            "gog",
            "gmail",
            "watch",
            "start",
            "--topic",
            _GMAIL_TOPIC,
            "--label-ids",
            "INBOX",
            "-a",
            "ada@example.com",
        ]

        google_watch._stop_gmail_watch_sync("owner_1", "ada@example.com")
        assert run_gog.call_args.args[0] == ["gog", "gmail", "watch", "stop", "-a", "ada@example.com"]


def test_parse_gmail_watch_requires_expiration():
    """A watch response without `expiration` is as fatal as the call failing —
    the seeded state file could never say when to re-arm."""
    from core.services.google_watch import _parse_gmail_watch

    with pytest.raises(RuntimeError, match="no expiration"):
        _parse_gmail_watch(json.dumps({"historyId": "7"}))
