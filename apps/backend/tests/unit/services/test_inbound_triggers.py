"""Tests for the Automations Phase 2 trigger-seed service.

Mirrors the ``_resolver_env`` fake-collaborator idiom from
``test_paperclip_trigger_proxy.py``: patch ``PaperclipRepo`` (company
resolution), ``get_admin_session_cookie``, and ``PaperclipAdminClient`` so
nothing here touches a real Paperclip deployment.
"""

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
    # google has no `inbound:` block, so it isn't a trigger source.
    with pytest.raises(ValueError, match="not wired as an Automations trigger source"):
        await seed_trigger_routine("owner_1", "google", "whatever")


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
