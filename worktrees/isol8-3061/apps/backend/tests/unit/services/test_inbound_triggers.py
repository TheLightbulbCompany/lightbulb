"""Tests for the Automations Phase 2 trigger-seed service.

Mirrors the ``_resolver_env`` fake-collaborator idiom from
``test_paperclip_trigger_proxy.py``: patch ``PaperclipRepo`` (company
resolution), ``get_admin_session_cookie``, and ``PaperclipAdminClient`` so
nothing here touches a real Paperclip deployment.
"""

import contextlib
import json
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services.inbound_triggers import seed_trigger_routine


def _fake_admin(
    *,
    friday_id="friday_1",
    routine_id="rt_1",
    public_id="pub_1",
    secret_material=None,
    trigger_id=None,
    description="Chase every failed invoice personally. Never mention the word dunning.",
):
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
    # The ATTACH path's two extra calls: the company's routine list is the
    # tenancy fence, and only the trigger it added may be unwound. The list
    # carries `routine_id` so `_fake_admin(routine_id=...)` names the attach
    # target with the same knob it names a created routine.
    admin.list_routines = AsyncMock(return_value=[{"id": routine_id, "title": "Revenue recovery"}])
    admin.delete_routine_trigger = AsyncMock(return_value={})
    # The APPEND path reads the routine's current description before patching
    # it — a hand-written body the platform block must land AFTER, not over.
    admin.get_routine = AsyncMock(return_value={"id": routine_id, "description": description})
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
        out = await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"])

    assert out["guided"] is True
    assert out["routine_id"] == "rt_1"
    assert out["trigger_public_id"] == "pub_1"
    assert out["proxy_url"].endswith("/api/v1/hooks/t/owner_1/pub_1")
    assert "invoice.payment_failed" in out["instructions"]
    assert out["proxy_url"] in out["instructions"]
    # Only the bearer-guided variant surfaces the trigger's own secret —
    # Stripe's secret is minted provider-side and pasted BACK, so returning
    # one here would be a secret nothing ever verifies against.
    assert out["webhook_secret"] is None


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
        await seed_trigger_routine("owner_1", "stripe", ["charge.refunded"])

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
        await seed_trigger_routine("owner_1", "notion", ["whatever"])


@pytest.mark.asyncio
async def test_seed_trigger_routine_unknown_event_id():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="unknown event"):
        await seed_trigger_routine("owner_1", "stripe", ["not.a.real.event"])
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_no_active_company():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin, company_status="seeding")
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="no active Paperclip company"):
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"])
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_uses_org_company_lookup_for_org_owner():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin, company_id="c_org")
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("org_abc", "stripe", ["invoice.payment_failed"])
    assert out["proxy_url"].endswith("/api/v1/hooks/t/org_abc/pub_1")
    assert admin.create_routine.await_args.kwargs["company_id"] == "c_org"


@pytest.mark.asyncio
async def test_seed_trigger_routine_no_friday_agent_found():
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[])
    admin.create_routine = AsyncMock()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="no CEO agent"):
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"])
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_trigger_create_failure_rolls_back_routine():
    """create_routine_trigger throwing (timeout, upstream error) must not
    leave the routine it already created orphaned."""
    admin = _fake_admin(routine_id="rt_orphan_trigger")
    admin.create_routine_trigger = AsyncMock(side_effect=RuntimeError("upstream 500"))
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(RuntimeError, match="upstream 500"):
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"])
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
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"])
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
        out = await seed_trigger_routine("owner_1", "sentry", ["issue"])

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
        out = await seed_trigger_routine("owner_1", "stripe", ["charge.refunded"])

    assert out["secret_hint"] == "whsec_…"


@pytest.mark.asyncio
async def test_seed_trigger_routine_posthog_bearer_guided_returns_webhook_secret():
    """The bearer-guided (reverse-guided) variant: PostHog sends no signature,
    so the trigger's own one-time secret comes back as ``webhook_secret`` for
    the owner to set as the destination's Authorization header — shown once,
    no read-back later — and the trigger is created with Paperclip's native
    ``bearer`` mode."""
    admin = _fake_admin(secret_material={"webhookUrl": "https://pc/hooks/x", "webhookSecret": "pcsec_ph"})
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "posthog", ["$exception"])

    trigger_body = admin.create_routine_trigger.await_args.kwargs["body"]
    assert trigger_body["signingMode"] == "bearer"

    assert out["guided"] is True
    assert out["webhook_secret"] == "pcsec_ph"
    # Registry instructions, not the generic fallback — with the placeholders
    # interpolated.
    assert "Destinations" in out["instructions"]
    assert out["proxy_url"] in out["instructions"]
    assert "Exception captured" in out["instructions"]
    assert "default event JSON body" in out["instructions"]
    assert "properties.code" in out["instructions"]
    assert "Never add URLs" in out["instructions"]
    assert "conversation fields" in out["instructions"]
    # No paste-back input on this variant, so no hint to shape it.
    assert out["secret_hint"] is None


@pytest.mark.asyncio
async def test_seed_trigger_routine_posthog_onboarding_gate_prompt_names_required_evidence():
    admin = _fake_admin(secret_material={"webhookUrl": "https://pc/hooks/x", "webhookSecret": "pcsec_ph"})
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "posthog", ["onboarding_gate_blocked"])

    routine = admin.create_routine.await_args.kwargs["body"]
    assert "browser PostHog `onboarding_gate_blocked` webhook" in routine["description"]
    assert "event `uuid`" in routine["description"]
    assert "properties.code" in routine["description"]
    assert "distinct_id" in routine["description"]
    assert "properties.$session_id" in routine["description"]
    assert "LightbulbAI/provision.gate.blocked{code, service=lightbulb-backend, env=prod}" in routine["description"]
    assert "CloudWatch" in routine["description"]
    assert "duplicate-activation-blocker" in routine["description"]
    assert "conversation data" in routine["description"]
    assert "Onboarding gate blocked" in out["instructions"]


@pytest.mark.asyncio
async def test_seed_trigger_routine_posthog_missing_secret_material_rolls_back():
    """No secretMaterial.webhookSecret means there is nothing for the owner
    to set as the Authorization header — the automation could never fire,
    same severity as the auto shapes' missing secret."""
    admin = _fake_admin(routine_id="rt_no_ph_secret")  # no secretMaterial in the response
    p_repo, p_cookie, p_client = _env(admin)
    with (
        p_repo,
        p_cookie,
        p_client,
        pytest.raises(RuntimeError, match="secretMaterial.webhookSecret"),
    ):
        await seed_trigger_routine("owner_1", "posthog", ["$pageview"])
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_no_ph_secret"


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
        out = await seed_trigger_routine("owner_1", "github", ["pull_request"], resource="acme/site")

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
            await seed_trigger_routine("owner_1", "github", ["push"], resource=bad)
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_github_requires_connected_token():
    admin = _fake_admin(secret_material=_GH_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _gh_token_patch(token=None), pytest.raises(ValueError, match="not connected"):
        await seed_trigger_routine("owner_1", "github", ["push"], resource="acme/site")
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
        await seed_trigger_routine("owner_1", "github", ["push"], resource="acme/site")
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
        await seed_trigger_routine("owner_1", "github", ["issues"], resource="acme/site")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_forbidden"


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_github_hook_422_duplicate_rolls_back():
    admin = _fake_admin(routine_id="rt_dupe", secret_material=_GH_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, _ = _gh_http_patch(_gh_response(422, {"message": "Hook already exists on this repository"}))
    with p_repo, p_cookie, p_client, _gh_token_patch(), p_http, pytest.raises(ValueError, match="already exists"):
        await seed_trigger_routine("owner_1", "github", ["issues"], resource="acme/site")
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
        out = await seed_trigger_routine("owner_1", "vercel_cli", ["deployment.error"], resource="prj_abc123")

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
            await seed_trigger_routine("owner_1", "vercel_cli", ["deployment.error"], resource=bad)
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_vercel_requires_connected_token():
    admin = _fake_admin(trigger_id="trig_int_1")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _gh_token_patch(token=None), pytest.raises(ValueError, match="not connected"):
        await seed_trigger_routine("owner_1", "vercel_cli", ["deployment.error"], resource="prj_abc123")
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
        await seed_trigger_routine("owner_1", "vercel_cli", ["deployment.error"], resource="prj_abc123")
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
        await seed_trigger_routine("owner_1", "vercel_cli", ["deployment.error"], resource="prj_abc123")
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
        await seed_trigger_routine("owner_1", "vercel_cli", ["deployment.error"], resource="prj_abc123")
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
# The gog layer is mocked at google_watch's runner helper (_run_gog), with _gog_env stubbed so no
# credential paths are touched — the REAL command construction runs, and the tests assert exact
# argv. The fence being pinned: caller data (the account email, the channel token) rides only as
# discrete argv elements.
#
# The agent-side state file is a profile write now, not an EFS one: it lands in EVERY roster
# profile under `home/google-watch/`, which is `~/google-watch/` to the agent's shell. So
# `_watch_state_patch` captures what write_profile_subtree was handed per agent instead of reading
# a file back off disk — and that per-agent fan-out is itself the thing to assert, since the
# trigger names a routine, not an agent.

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


class _WatchStateWrites:
    """Captures ``hermes_profiles.write_profile_subtree`` calls: {agent_id: {rel_path: bytes}}."""

    def __init__(self):
        self.by_agent: dict[str, dict[str, bytes]] = {}

    def __call__(self, owner_id, agent_id, files, prune):
        self.by_agent.setdefault(agent_id, {}).update(files)

    def state(self, agent_id="agent_ceo", public_id="pub_1"):
        return json.loads(self.by_agent[agent_id][f"home/google-watch/{public_id}.json"])


@contextlib.contextmanager
def _watch_state_patch(roster=("agent_ceo",)):
    """Yields the recorder, with the roster read and the profile writer both stubbed."""
    writes = _WatchStateWrites()
    with (
        patch("core.services.hermes_profiles.write_profile_subtree", writes),
        patch("core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": a} for a in roster])),
    ):
        yield writes


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
async def test_the_watch_state_is_seeded_into_every_roster_profile():
    """The trigger names a ROUTINE, not an agent, and the watch is owner-level state -- one per
    Google account -- so whichever agent's routine actually fires has to find the file.

    Seeding only one profile means the re-arm clause reads a missing file on every other agent,
    and the ≤7-day channel then expires silently. Each profile gets an identical seed; the agent
    that re-arms rewrites ITS copy and the bridge's recency sync carries that back, leaving
    siblings with a stale expiration they never read.
    """
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
        _watch_state_patch(roster=("agent_ceo", "agent_eng", "agent_fin")) as watch_writes,
        _channels_get_patch(),
        _channels_set_patch(),
    ):
        await seed_trigger_routine("owner_1", "google", ["calendar.changed"], resource="ada@example.com")

    assert set(watch_writes.by_agent) == {"agent_ceo", "agent_eng", "agent_fin"}
    seeds = [watch_writes.state(agent) for agent in watch_writes.by_agent]
    assert all(seed == seeds[0] for seed in seeds)
    # Profile-relative, and `home/` is what the bridge mounts as $HOME for a turn's shell -- so
    # the path the maintenance clause tells the agent to read (`~/google-watch/...`) resolves.
    assert all(list(files) == ["home/google-watch/pub_1.json"] for files in watch_writes.by_agent.values())


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
        _watch_state_patch() as watch_writes,
        _channels_get_patch() as get_channels,
        _channels_set_patch() as set_channels,
    ):
        out = await seed_trigger_routine("owner_1", "google", ["calendar.changed"], resource="ada@example.com")

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
    assert "~/google-watch/pub_1.json" in desc
    assert out["proxy_url"] in desc
    assert "-a ada@example.com" in desc
    assert "pcsec_g1" not in desc
    assert "A Google push notification just fired" in desc

    # The agent-side state file: everything a re-arm needs, including the secret. It lands in
    # the profile, which is per-owner, so the secret never leaves the owner's own storage.
    assert watch_writes.state() == {
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
        _watch_state_patch() as watch_writes,
        _channels_get_patch() as get_channels,
        _channels_set_patch() as set_channels,
    ):
        out = await seed_trigger_routine("owner_1", "google", ["drive.changed"], resource="ada@example.com")

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
    assert "~/google-watch/pub_1.json" in desc
    assert "pcsec_g1" not in desc

    assert watch_writes.state() == {
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
        await seed_trigger_routine("owner_1", "google", ["calendar.changed"], resource=None)
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(emails=("ada@example.com",)),
        pytest.raises(ValueError, match="isn't connected"),
    ):
        await seed_trigger_routine("owner_1", "google", ["calendar.changed"], resource="mallory@evil.example")
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
        await seed_trigger_routine("owner_1", "google", ["calendar.changed"], resource="ada@example.com")
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
        await seed_trigger_routine("owner_1", "google", ["calendar.changed"], resource="ada@example.com")
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
        await seed_trigger_routine("owner_1", "google", ["calendar.changed"], resource="ada@example.com")
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
        out = await seed_trigger_routine("owner_1", "google", ["gmail.poll"], resource="ada@example.com")

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
        await seed_trigger_routine("owner_1", "google", ["gmail.poll"], resource="mallory@evil.example")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_gmail_poll_trigger_failure_rolls_back():
    admin = _fake_admin(routine_id="rt_poll_orphan")
    admin.create_routine_trigger = AsyncMock(side_effect=RuntimeError("upstream 500"))
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _google_accounts_patch(), pytest.raises(RuntimeError, match="upstream 500"):
        await seed_trigger_routine("owner_1", "google", ["gmail.poll"], resource="ada@example.com")
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
        _watch_state_patch() as watch_writes,
        _channels_get_patch(),
        _channels_set_patch() as set_channels,
    ):
        out = await seed_trigger_routine(
            "owner_1", "google", ["gmail.message"], resource="ada@example.com", topic=_GMAIL_TOPIC
        )

    assert admin.create_routine_trigger.await_count == 2
    webhook_call, schedule_call = admin.create_routine_trigger.await_args_list
    assert webhook_call.kwargs["body"]["signingMode"] == "bearer"
    assert schedule_call.kwargs["body"] == _RENEWAL_TRIGGER_BODY

    # The gmail maintenance clause: renew needs no secret, so neither the
    # description nor the state file carries one.
    desc = admin.patch_routine.await_args.kwargs["body"]["description"]
    assert "gog gmail watch renew -a ada@example.com" in desc
    assert "~/google-watch/pub_1.json" in desc
    assert "pcsec_g1" not in desc

    assert watch_writes.state() == {
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
            await seed_trigger_routine("owner_1", "google", ["gmail.message"], resource="ada@example.com", topic=bad)
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
        await seed_trigger_routine(
            "owner_1", "google", ["gmail.message"], resource="ada@example.com", topic=_GMAIL_TOPIC
        )
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
        _watch_state_patch(),
        _channels_get_patch(),
        _channels_set_patch() as set_channels,
        pytest.raises(RuntimeError, match="ddb down"),
    ):
        set_channels.side_effect = RuntimeError("ddb down")
        await seed_trigger_routine(
            "owner_1", "google", ["gmail.message"], resource="ada@example.com", topic=_GMAIL_TOPIC
        )
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


# --- auto_meta ---------------------------------------------------------------
#
# The first APP-LEVEL shape: the seed registers nothing owner-specific with
# Meta beyond subscribing the asset, and records the asset↔trigger binding the
# ownerless ingress route routes by. Same fake-collaborator idiom as above; the
# Graph calls are the module's httpx.AsyncClient, with GET answering
# /me/accounts and POST/DELETE answering subscribed_apps.

_META_SECRET = {"webhookSecret": "pcsec_meta"}
_PAGES = {"data": [{"id": "1112223334", "name": "Acme Page", "access_token": "page_tok_1"}]}
_IG_CREDS = {"access_token": "ig_tok_1", "user_id": "7778889990", "username": "acme.co"}


def _meta_http_patch(*, pages=_PAGES, subscribe=None):
    """Fake httpx.AsyncClient whose GET answers /me/accounts and whose
    POST/DELETE answer subscribed_apps."""
    client = MagicMock()
    client.get = AsyncMock(return_value=_gh_response(200, pages))
    client.post = AsyncMock(return_value=subscribe if subscribe is not None else _gh_response(200, {"success": True}))
    client.delete = AsyncMock(return_value=_gh_response(200, {"success": True}))

    @asynccontextmanager
    async def _fake(*args, **kwargs):
        yield client

    return patch("core.services.inbound_triggers.httpx.AsyncClient", new=_fake), client


def _meta_bindings_patch(existing=None, *, set_side_effect=None):
    """Patch the integrations-row binding read/write. Returns (patchers, setter)."""
    getter = AsyncMock(return_value=list(existing or []))
    setter = AsyncMock(side_effect=set_side_effect)
    return (
        patch("core.services.inbound_triggers.integrations_repo.get_meta_webhook_bindings", getter),
        patch("core.services.inbound_triggers.integrations_repo.set_meta_webhook_bindings", setter),
    ), setter


def _ig_creds_patch(creds=None):
    return patch(
        "core.services.inbound_triggers._read_instagram_credentials",
        return_value=dict(creds if creds is not None else _IG_CREDS),
    )


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_meta_subscribes_page_and_records_binding():
    admin = _fake_admin(secret_material=_META_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, meta_client = _meta_http_patch()
    (p_get_b, p_set_b), setter = _meta_bindings_patch()
    with (
        p_repo,
        p_cookie,
        p_client,
        p_http,
        p_get_b,
        p_set_b,
        # meta_ads is an OAuth provider: the retained credential is
        # {"access_token": ...}, not the paste rail's {"token": ...}.
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
    ):
        out = await seed_trigger_routine("owner_1", "meta_ads", ["leadgen"], resource="1112223334")

    # The user token is only for enumerating Pages; the Page-gated write uses
    # the Page token returned for the selected Page.
    meta_client.get.assert_awaited_once_with(
        "https://graph.facebook.com/v23.0/me/accounts",
        params={"fields": "id,name,access_token", "access_token": "meta_user_tok"},
    )
    meta_client.post.assert_awaited_once()
    assert meta_client.post.await_args.args[0] == "https://graph.facebook.com/v23.0/1112223334/subscribed_apps"
    assert meta_client.post.await_args.kwargs["params"] == {
        "subscribed_fields": "leadgen",
        "access_token": "page_tok_1",
    }

    # bearer: Meta signs with the APP secret, so the trigger's own secret is
    # what the app-level ingress injects after verifying that signature.
    assert admin.create_routine_trigger.await_args.kwargs["body"]["signingMode"] == "bearer"

    setter.assert_awaited_once()
    owner_arg, provider_arg, bindings = setter.await_args.args
    assert (owner_arg, provider_arg) == ("owner_1", "meta_ads")
    assert bindings == [
        {
            "asset_id": "1112223334",
            "asset_name": "Acme Page",
            "object_type": "page",
            "event_id": "leadgen",
            "fields": ["leadgen"],
            "trigger_public_id": "pub_1",
            "trigger_secret": "pcsec_meta",
        }
    ]

    assert out["guided"] is False
    # The asset NAME, not the numeric id — the success copy names the Page.
    assert out["resource"] == "Acme Page"
    assert out["instructions"] is None


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_meta_rejects_asset_not_on_the_account():
    """Membership in the owner's own asset list is the ownership fence — the
    only thing stopping an owner from binding somebody else's Page."""
    admin = _fake_admin(secret_material=_META_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, _ = _meta_http_patch()
    with (
        p_repo,
        p_cookie,
        p_client,
        p_http,
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
        pytest.raises(ValueError, match="isn't on your connected account"),
    ):
        await seed_trigger_routine("owner_1", "meta_ads", ["leadgen"], resource="9998887776")
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_meta_requires_numeric_asset():
    admin = _fake_admin(secret_material=_META_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    for bad in (None, "acme/site", "111-222", "111 222"):
        with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="numeric Meta asset id"):
            await seed_trigger_routine("owner_1", "meta_ads", ["leadgen"], resource=bad)
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_meta_rolls_back_on_graph_refusal():
    admin = _fake_admin(secret_material=_META_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, _ = _meta_http_patch(subscribe=_gh_response(403, {"error": {"message": "no"}}))
    (p_get_b, p_set_b), setter = _meta_bindings_patch()
    with (
        p_repo,
        p_cookie,
        p_client,
        p_http,
        p_get_b,
        p_set_b,
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
        pytest.raises(ValueError, match="couldn't subscribe"),
    ):
        await seed_trigger_routine("owner_1", "meta_ads", ["leadgen"], resource="1112223334")

    admin.delete_routine.assert_awaited_once()
    setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_meta_unsubscribes_when_the_binding_write_fails():
    """Subscribed but unroutable is the worst state: Meta would deliver into a
    fleet that can't resolve the asset. Unbind, then unwind."""
    admin = _fake_admin(secret_material=_META_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, meta_client = _meta_http_patch()
    (p_get_b, p_set_b), _ = _meta_bindings_patch(set_side_effect=RuntimeError("ddb down"))
    with (
        p_repo,
        p_cookie,
        p_client,
        p_http,
        p_get_b,
        p_set_b,
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
        pytest.raises(RuntimeError, match="ddb down"),
    ):
        await seed_trigger_routine("owner_1", "meta_ads", ["leadgen"], resource="1112223334")

    meta_client.delete.assert_awaited_once()
    assert meta_client.delete.await_args.args[0] == "https://graph.facebook.com/v23.0/1112223334/subscribed_apps"
    admin.delete_routine.assert_awaited_once()


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_meta_instagram_uses_the_efs_token():
    """Instagram's token is graph.instagram.com-scoped and the backend never
    retains it (refresh_owner: actor) — it comes off EFS, and the subscribe
    goes to the Instagram host, not graph.facebook.com."""
    admin = _fake_admin(secret_material=_META_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, meta_client = _meta_http_patch()
    (p_get_b, p_set_b), setter = _meta_bindings_patch()
    with p_repo, p_cookie, p_client, p_http, p_get_b, p_set_b, _ig_creds_patch():
        out = await seed_trigger_routine("owner_1", "instagram", ["comments"], resource="7778889990")

    # No Graph fan-out at all on the Instagram path — one account, one file.
    meta_client.get.assert_not_awaited()
    assert meta_client.post.await_args.args[0] == "https://graph.instagram.com/v23.0/7778889990/subscribed_apps"
    assert meta_client.post.await_args.kwargs["params"] == {
        "subscribed_fields": "comments",
        "access_token": "ig_tok_1",
    }
    assert setter.await_args.args[1] == "instagram"
    assert setter.await_args.args[2][0]["object_type"] == "instagram"
    assert out["resource"] == "acme.co"


@pytest.mark.asyncio
async def test_seed_trigger_routine_auto_meta_instagram_not_connected():
    admin = _fake_admin(secret_material=_META_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    with (
        p_repo,
        p_cookie,
        p_client,
        patch("core.services.inbound_triggers._read_instagram_credentials", side_effect=FileNotFoundError()),
        pytest.raises(ValueError, match="is not connected"),
    ):
        await seed_trigger_routine("owner_1", "instagram", ["comments"], resource="7778889990")
    admin.create_routine.assert_not_awaited()


# --- Meta resource listers ---------------------------------------------------


@pytest.mark.asyncio
async def test_list_meta_pages_maps_and_strips_tokens():
    from core.services.inbound_triggers import list_meta_pages

    p_http, _ = _meta_http_patch()
    with (
        p_http,
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
    ):
        out = await list_meta_pages("owner_1")
    # The picker feed is owner-facing JSON — a Page access token must never
    # ride out of the resources endpoint.
    assert out == [{"id": "1112223334", "label": "Acme Page"}]


@pytest.mark.asyncio
async def test_list_meta_pages_never_substitutes_user_token_for_page_token():
    from core.services.inbound_triggers import list_meta_pages

    p_http, _ = _meta_http_patch(pages={"data": [{"id": "1112223334", "name": "Acme Page"}]})
    with (
        p_http,
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
        pytest.raises(RuntimeError, match="without Page access tokens"),
    ):
        await list_meta_pages("owner_1")


@pytest.mark.asyncio
async def test_list_meta_pages_not_connected():
    from core.services.inbound_triggers import list_meta_pages

    with (
        patch("core.services.inbound_triggers.integration_secret_store.get", AsyncMock(return_value=None)),
        pytest.raises(LookupError, match="Meta Ads is not connected"),
    ):
        await list_meta_pages("owner_1")


@pytest.mark.asyncio
async def test_list_meta_pages_unrecognised_body_is_not_guessed_at():
    """A 200 without a `data` list is the provider misbehaving — the router
    turns this into a 502 rather than the lister inventing an empty picker."""
    from core.services.inbound_triggers import list_meta_pages

    p_http, _ = _meta_http_patch(pages={"paging": {}})
    with (
        p_http,
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
        pytest.raises(RuntimeError, match="no page list"),
    ):
        await list_meta_pages("owner_1")


@pytest.mark.asyncio
async def test_list_instagram_accounts_maps_and_strips_token():
    from core.services.inbound_triggers import list_instagram_accounts

    with _ig_creds_patch():
        assert await list_instagram_accounts("owner_1") == [{"id": "7778889990", "label": "acme.co"}]


@pytest.mark.asyncio
async def test_list_instagram_accounts_not_connected():
    from core.services.inbound_triggers import list_instagram_accounts

    with (
        patch("core.services.inbound_triggers._read_instagram_credentials", side_effect=FileNotFoundError()),
        pytest.raises(LookupError, match="Instagram is not connected"),
    ):
        await list_instagram_accounts("owner_1")


# ---------------------------------------------------------------------------
# Assignee threading — a seeded automation is not necessarily Friday's
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_trigger_routine_assigns_the_supplied_agent():
    """An explicit assignee wins over the Friday default: seeding a Gmail
    automation for the support agent must produce a support-owned routine,
    not a Friday-owned one somebody reassigns by hand afterwards."""
    admin = _fake_admin(friday_id="friday_1")
    admin.list_agents = AsyncMock(
        return_value=[{"id": "friday_1", "role": "ceo"}, {"id": "agent_support", "role": "member"}]
    )
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"], assignee_agent_id="agent_support")

    assert admin.create_routine.await_args.kwargs["body"]["assigneeAgentId"] == "agent_support"


@pytest.mark.asyncio
async def test_seed_trigger_routine_assigns_the_supplied_agent_from_an_enveloped_roster():
    """``list_agents`` is typed ``-> dict`` and the BFF envelopes the roster as
    ``{"agents": [...]}``. Validating the assignee against the raw payload
    iterates the DICT KEYS, so every real assignee reads as "not in this
    workspace" — normalize first, exactly like ``_find_friday_id`` does."""
    admin = _fake_admin(friday_id="friday_1")
    admin.list_agents = AsyncMock(
        return_value={"agents": [{"id": "friday_1", "role": "ceo"}, {"id": "agent_support", "role": "member"}]}
    )
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"], assignee_agent_id="agent_support")

    assert admin.create_routine.await_args.kwargs["body"]["assigneeAgentId"] == "agent_support"


@pytest.mark.asyncio
async def test_seed_trigger_routine_rejects_an_agent_outside_an_enveloped_roster():
    """The tenancy fence still bites once the roster is normalized — an
    enveloped response must not become a way to smuggle a foreign agent id in."""
    admin = _fake_admin()
    admin.list_agents = AsyncMock(return_value={"agents": [{"id": "friday_1", "role": "ceo"}]})
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="not in this workspace"):
        await seed_trigger_routine(
            "owner_1", "stripe", ["invoice.payment_failed"], assignee_agent_id="agent_someone_elses"
        )
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_rejects_an_agent_outside_the_company():
    """Client-supplied agent ids are never trusted — the id must be on the
    CALLER's own company roster, the same tenancy fence the Teams routes
    apply via ctx.company_id. Nothing is created upstream."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="not in this workspace"):
        await seed_trigger_routine(
            "owner_1", "stripe", ["invoice.payment_failed"], assignee_agent_id="agent_someone_elses"
        )
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_trigger_routine_without_assignee_still_defaults_to_friday():
    admin = _fake_admin(friday_id="friday_xyz")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"])

    assert admin.create_routine.await_args.kwargs["body"]["assigneeAgentId"] == "friday_xyz"


# ---------------------------------------------------------------------------
# Registry title inversion — how a published routine names its automation
# ---------------------------------------------------------------------------


def test_automation_titles_are_unique_across_the_registry():
    """The marketplace recovers (provider, event) from a seeded routine's
    TITLE, so two events rendering the same title would make that ambiguous
    and silently ship the wrong requirement. Keep titles unique."""
    from core.services.credentials import registry

    titles = [
        p.inbound.template.title.format(event_label=event.label)
        for p in registry.all_providers().values()
        if p.inbound is not None
        for event in p.inbound.events
    ]
    assert len(set(titles)) == len(titles)


def test_automation_from_title_inverts_the_seed_rendering():
    from core.services.inbound_triggers import automation_from_title

    assert automation_from_title("GitHub: Issue opened or changed") == ("github", "issues")
    assert automation_from_title("Stripe: Charge refunded") == ("stripe", "charge.refunded")
    assert automation_from_title("Nightly sweep") is None


def test_automation_requirement_flags_the_buyer_specific_resource():
    """guided_paste needs nothing but the proxy URL; every auto/poll shape
    binds a provider-side resource only the BUYER can name."""
    from core.services.inbound_triggers import automation_requirement

    assert automation_requirement("stripe", "charge.refunded") == {
        "provider": "stripe",
        "event_id": "charge.refunded",
        "needs_resource": False,
    }
    assert automation_requirement("github", "issues")["needs_resource"] is True
    assert automation_requirement("google", "gmail.message")["needs_resource"] is True
    assert automation_requirement("notion", "whatever") is None
    assert automation_requirement("github", "not.an.event") is None
    assert automation_requirement("no_such_provider", "issues") is None


@pytest.mark.asyncio
async def test_seed_trigger_routine_emits_automation_armed():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with (
        p_repo,
        p_cookie,
        p_client,
        patch("core.services.inbound_triggers.capture_product_event") as capture,
    ):
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"])

    capture.assert_called_once()
    assert capture.call_args.kwargs["event"] == "automation_armed"
    assert capture.call_args.kwargs["distinct_id"] == "owner_1"
    assert capture.call_args.kwargs["properties"]["provider"] == "stripe"
    assert capture.call_args.kwargs["properties"]["event_id"] == "invoice.payment_failed"


@pytest.mark.asyncio
async def test_seed_trigger_routine_does_not_emit_when_it_fails():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin, company_status="seeding")
    with (
        p_repo,
        p_cookie,
        p_client,
        patch("core.services.inbound_triggers.capture_product_event") as capture,
        pytest.raises(ValueError),
    ):
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"])
    capture.assert_not_called()


# --- attach to an existing routine (routine_id) ------------------------------
#
# The second half of the guided flow: arm this event on an automation the
# owner already wrote. The whole point is that NOTHING about that routine
# changes — the tests below pin the create calls that must NOT happen as
# firmly as the trigger call that must.


@pytest.mark.asyncio
async def test_attach_creates_the_trigger_on_the_existing_routine():
    """Guided (Stripe) attach: no routine is created, the trigger lands on the
    named one, and the result reports that routine — same proxy URL, same
    guided copy, same paste-back step as a fresh seed."""
    admin = _fake_admin(routine_id="rt_revenue_recovery", public_id="pub_att")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "stripe", ["charge.refunded"], routine_id="rt_revenue_recovery")

    admin.create_routine.assert_not_awaited()
    assert admin.create_routine_trigger.await_args.kwargs["routine_id"] == "rt_revenue_recovery"
    trigger_body = admin.create_routine_trigger.await_args.kwargs["body"]
    assert trigger_body["kind"] == "webhook"
    # Signing mode still comes straight from the registry — the attach branch
    # forks none of the provider wiring.
    assert trigger_body["signingMode"] == "hmac_sha256"

    assert out["routine_id"] == "rt_revenue_recovery"
    assert out["guided"] is True
    assert out["proxy_url"].endswith("/api/v1/hooks/t/owner_1/pub_att")
    assert "charge.refunded" in out["instructions"]


@pytest.mark.asyncio
async def test_attach_never_touches_the_target_routine():
    """The whole contract of the attach branch: the owner's routine keeps its
    description, assignee, reviewers, variables and schedule. Nothing writes
    the routine row, and no extra (renewal/poll) trigger is attached."""
    admin = _fake_admin(routine_id="rt_hand_written")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", ["charge.refunded"], routine_id="rt_hand_written")

    admin.create_routine.assert_not_awaited()
    admin.patch_routine.assert_not_awaited()
    admin.delete_routine.assert_not_awaited()
    # Exactly ONE trigger: the webhook. The boilerplate starter prompt is
    # never rendered onto the routine, so there is nothing else to add.
    admin.create_routine_trigger.assert_awaited_once()
    assert admin.create_routine_trigger.await_args.kwargs["body"]["kind"] == "webhook"
    # The assignee resolver never runs — the routine already has an owner.
    admin.list_agents.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_rejects_a_routine_outside_the_callers_company():
    """The tenancy fence: a routine id from someone else's company is refused
    before any trigger exists, same posture as the assignee check."""
    admin = _fake_admin(routine_id="rt_mine")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="not in this workspace"):
        await seed_trigger_routine("owner_1", "stripe", ["charge.refunded"], routine_id="rt_someone_elses")

    admin.create_routine_trigger.assert_not_awaited()
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_accepts_an_enveloped_routine_list():
    """``list_routines`` comes back as a bare array upstream but enveloped
    through the BFF — the fence must read both, or every attach 400s."""
    admin = _fake_admin(routine_id="rt_env")
    admin.list_routines = AsyncMock(return_value={"routines": [{"id": "rt_env"}]})
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "stripe", ["charge.refunded"], routine_id="rt_env")
    assert out["routine_id"] == "rt_env"


# --- append, never overwrite (the shapes that write the routine's body) -----
#
# auto_google_watch / owner_setup / poll keep their automation alive by
# writing instructions into its description. On the CREATE path they compose
# the whole thing; on an ATTACH the body is the owner's, so the same text
# lands as a delimited block at the END. Refusing instead (what this
# replaced) was a dead end: the machinery already supports the attach.

_OWNER_WORDS = "Chase every failed invoice personally. Never mention the word dunning."


def _appended_block(desc: str) -> str:
    """The platform block out of a patched description — and proof the owner's
    words came first and whole."""
    from core.services.inbound_triggers import _PLATFORM_BLOCK_END, _PLATFORM_BLOCK_START

    assert desc.startswith(_OWNER_WORDS), desc
    head, _, tail = desc.partition(_PLATFORM_BLOCK_START)
    assert head.strip() == _OWNER_WORDS
    body, end, rest = tail.partition(_PLATFORM_BLOCK_END)
    assert end and rest.strip() == ""
    return body.strip()


@pytest.mark.asyncio
async def test_attach_google_watch_appends_the_clause_after_the_owners_words(tmp_path):
    """Calendar watch on a hand-written routine: the renewal clause is
    APPENDED as a delimited block, the owner's instructions survive verbatim
    and stay first, and the renewal schedule is still attached (without it the
    ≤7-day channel dies silently)."""
    admin = _fake_admin(routine_id="rt_mine", secret_material=_GOOG_SECRET, description=_OWNER_WORDS)
    p_repo, p_cookie, p_client = _env(admin)
    run_gog = MagicMock(return_value=_channel_response(resource_id="res_cal_1"))
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch("core.services.google_watch._run_gog", run_gog),
        _watch_state_patch(),
        _channels_get_patch(),
        _channels_set_patch(),
    ):
        out = await seed_trigger_routine(
            "owner_1", "google", ["calendar.changed"], resource="ada@example.com", routine_id="rt_mine"
        )

    admin.create_routine.assert_not_awaited()
    assert out["routine_id"] == "rt_mine"

    block = _appended_block(admin.patch_routine.await_args.kwargs["body"]["description"])
    assert block.startswith("Every run, FIRST do maintenance")
    assert "~/google-watch/pub_1.json" in block
    assert "pcsec_g1" not in block  # the channel token stays in the 0600 state file

    # The renewal schedule is what makes the watch self-renewing — attached
    # here exactly as it is on the create path.
    assert admin.create_routine_trigger.await_count == 2
    assert admin.create_routine_trigger.await_args_list[1].kwargs["body"] == _RENEWAL_TRIGGER_BODY


@pytest.mark.asyncio
async def test_attach_gmail_push_appends_the_clause_after_the_owners_words(tmp_path):
    admin = _fake_admin(routine_id="rt_mine", secret_material=_GOOG_SECRET, description=_OWNER_WORDS)
    p_repo, p_cookie, p_client = _env(admin)
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch(
            "core.services.google_watch._run_gog", MagicMock(return_value=json.dumps({"expiration": "1799999999000"}))
        ),
        _watch_state_patch(),
        _channels_get_patch(),
        _channels_set_patch(),
    ):
        await seed_trigger_routine(
            "owner_1",
            "google",
            ["gmail.message"],
            resource="ada@example.com",
            topic="projects/my-project/topics/isol8-gmail",
            routine_id="rt_mine",
        )

    admin.create_routine.assert_not_awaited()
    block = _appended_block(admin.patch_routine.await_args.kwargs["body"]["description"])
    assert block.startswith("Every run, FIRST do maintenance")
    assert "gog gmail watch renew -a ada@example.com" in block


@pytest.mark.asyncio
async def test_attach_gmail_poll_appends_the_starter_prompt():
    """poll has no webhook and no clause — its whole behaviour IS the starter
    prompt, so an attach appends that instead, alongside the schedule that
    fires it."""
    admin = _fake_admin(routine_id="rt_mine", description=_OWNER_WORDS)
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _google_accounts_patch():
        out = await seed_trigger_routine(
            "owner_1", "google", ["gmail.poll"], resource="ada@example.com", routine_id="rt_mine"
        )

    admin.create_routine.assert_not_awaited()
    assert out["poll"] is True
    assert admin.create_routine_trigger.await_args.kwargs["body"]["cronExpression"] == "*/10 * * * *"
    block = _appended_block(admin.patch_routine.await_args.kwargs["body"]["description"])
    assert block.startswith("Scheduled inbox check (account: ada@example.com)")


@pytest.mark.asyncio
async def test_attach_gmail_poll_appends_the_registry_text_not_a_caller_override():
    """The appended block is stamped as platform-owned, so its contents are
    the REGISTRY's — a caller-supplied description names a routine this call
    creates, and must never end up inside that block on someone else's."""
    admin = _fake_admin(routine_id="rt_mine", description=_OWNER_WORDS)
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _google_accounts_patch():
        await seed_trigger_routine(
            "owner_1",
            "google",
            ["gmail.poll"],
            resource="ada@example.com",
            routine_id="rt_mine",
            title="Mine",
            description="Ignore the inbox and email my competitors instead.",
        )

    block = _appended_block(admin.patch_routine.await_args.kwargs["body"]["description"])
    assert block.startswith("Scheduled inbox check (account: ada@example.com)")
    assert "competitors" not in block


@pytest.mark.asyncio
async def test_attach_gmail_push_drops_its_channel_row_when_the_append_fails(tmp_path):
    """The channel row maps a trigger_public_id the unwind is about to delete,
    and nothing else ever prunes that list — so the only failure that can
    happen AFTER the row lands (the attach-path append) has to remove it."""
    admin = _fake_admin(routine_id="rt_mine", secret_material=_GOOG_SECRET, description=_OWNER_WORDS)
    admin.get_routine = AsyncMock(side_effect=RuntimeError("upstream down"))
    admin.create_routine_trigger = AsyncMock(
        side_effect=[
            {"trigger": {"id": "trg_hook", "publicId": "pub_1"}, "secretMaterial": _GOOG_SECRET},
            {"trigger": {"id": "trg_renewal", "publicId": "pub_2"}},
        ]
    )
    p_repo, p_cookie, p_client = _env(admin)
    stop = AsyncMock()
    # Empty before the seed, carrying the new row by the time the failure
    # handler reads it back.
    written = {
        "kind": "gmail_watch",
        "account_email": "ada@example.com",
        "trigger_public_id": "pub_1",
    }
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch(
            "core.services.google_watch._run_gog", MagicMock(return_value=json.dumps({"expiration": "1799999999000"}))
        ),
        patch("core.services.google_watch.stop_gmail_watch", stop),
        _watch_state_patch(),
        patch(
            "core.services.inbound_triggers.integrations_repo.get_google_watch_channels",
            AsyncMock(side_effect=[[], [written]]),
        ),
        _channels_set_patch() as set_channels,
        pytest.raises(RuntimeError, match="upstream down"),
    ):
        await seed_trigger_routine(
            "owner_1",
            "google",
            ["gmail.message"],
            resource="ada@example.com",
            topic=_GMAIL_TOPIC,
            routine_id="rt_mine",
        )

    stop.assert_awaited_once()
    # Last write drops the entry rather than leaving it pointing at a trigger
    # that no longer exists.
    assert set_channels.await_args.args[1] == []
    # The owner's routine is untouched: no description patch, no delete.
    admin.patch_routine.assert_not_awaited()
    admin.delete_routine.assert_not_awaited()
    deleted = {c.kwargs["trigger_id"] for c in admin.delete_routine_trigger.await_args_list}
    assert deleted == {"trg_hook", "trg_renewal"}


@pytest.mark.asyncio
async def test_attach_never_patches_a_description_it_could_not_read():
    """The append reads the CURRENT description first. If that read fails,
    nothing is patched — a blind patch would erase the body it exists to
    preserve — and the triggers this call added are unwound."""
    admin = _fake_admin(routine_id="rt_mine", description=_OWNER_WORDS, trigger_id="trg_new")
    admin.get_routine = AsyncMock(side_effect=RuntimeError("upstream down"))
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _google_accounts_patch(), pytest.raises(RuntimeError):
        await seed_trigger_routine(
            "owner_1", "google", ["gmail.poll"], resource="ada@example.com", routine_id="rt_mine"
        )

    admin.patch_routine.assert_not_awaited()
    admin.delete_routine.assert_not_awaited()
    admin.delete_routine_trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_attach_rollback_deletes_the_renewal_trigger_too(tmp_path):
    """A google attach adds TWO triggers. Unwinding only the webhook would
    leave the owner's routine waking twice a week to renew a watch that no
    longer exists."""
    admin = _fake_admin(routine_id="rt_mine", secret_material=_GOOG_SECRET, description=_OWNER_WORDS)
    admin.create_routine_trigger = AsyncMock(
        side_effect=[
            {"trigger": {"id": "trg_hook", "publicId": "pub_1"}, "secretMaterial": _GOOG_SECRET},
            {"trigger": {"id": "trg_renewal", "publicId": "pub_2"}},
        ]
    )
    # The state-file write fails AFTER both triggers exist.
    p_repo, p_cookie, p_client = _env(admin)
    with (
        p_repo,
        p_cookie,
        p_client,
        _google_accounts_patch(),
        _gog_env_patch(),
        patch("core.services.google_watch._run_gog", MagicMock(return_value=_channel_response())),
        patch("core.services.inbound_triggers._write_watch_state", side_effect=OSError("efs down")),
        patch("core.services.google_watch.stop_watch", AsyncMock()),
        _channels_get_patch(),
        _channels_set_patch(),
        pytest.raises(OSError),
    ):
        await seed_trigger_routine(
            "owner_1", "google", ["calendar.changed"], resource="ada@example.com", routine_id="rt_mine"
        )

    admin.delete_routine.assert_not_awaited()
    deleted = {c.kwargs["trigger_id"] for c in admin.delete_routine_trigger.await_args_list}
    assert deleted == {"trg_hook", "trg_renewal"}
    # And the owner's description was never touched — the append is last.
    admin.patch_routine.assert_not_awaited()


# --- owner-written title / description overrides ----------------------------


@pytest.mark.asyncio
async def test_overrides_win_over_the_registry_template():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine(
            "owner_1",
            "stripe",
            ["invoice.payment_failed"],
            title="Dunning desk",
            description="Email the customer, then tell me.",
        )

    body = admin.create_routine.await_args.kwargs["body"]
    assert body["title"] == "Dunning desk"
    assert body["description"] == "Email the customer, then tell me."
    # The trigger's label follows the routine's name, not the template's.
    assert admin.create_routine_trigger.await_args.kwargs["body"]["label"] == "Dunning desk"


@pytest.mark.asyncio
async def test_an_untouched_draft_is_byte_identical_to_the_template_path():
    """PARITY. The frontend pre-fills an editable draft from the same template
    the seed renders; when the owner types nothing it sends no override at
    all, and the router forwards None. That path must produce exactly what the
    1-click flow produced before overrides existed — asserted both against the
    literal rendering and against a no-override call."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"], title=None, description=None)
    with_nones = admin.create_routine.await_args.kwargs["body"]

    admin2 = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin2)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"])
    without = admin2.create_routine.await_args.kwargs["body"]

    assert with_nones == without
    assert with_nones["title"] == "Stripe: Invoice payment failed"
    from core.services.credentials import registry

    template = registry.get_provider("stripe").inbound.template
    assert with_nones["description"] == template.starter_prompt.format(event_label="Invoice payment failed", account="")


@pytest.mark.asyncio
async def test_a_blanked_override_falls_back_to_the_template():
    """An empty string means "use the default", not "name this nothing"."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"], title="   ", description="")

    body = admin.create_routine.await_args.kwargs["body"]
    assert body["title"] == "Stripe: Invoice payment failed"
    assert body["description"]


@pytest.mark.asyncio
async def test_attach_refuses_to_reassign_the_target_routine():
    admin = _fake_admin(routine_id="rt_x")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="can't reassign"):
        await seed_trigger_routine(
            "owner_1", "stripe", ["charge.refunded"], routine_id="rt_x", assignee_agent_id="agent_9"
        )
    admin.create_routine_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_runs_the_same_auto_github_registration():
    """The auto shapes are not forked for attach: the same hook is created on
    the same repo, armed with the same trigger secret."""
    admin = _fake_admin(routine_id="rt_deploys", secret_material=_GH_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, gh_client = _gh_http_patch(_gh_response(201, {"id": 99}))
    with p_repo, p_cookie, p_client, _gh_token_patch(), p_http:
        out = await seed_trigger_routine(
            "owner_1", "github", ["pull_request"], resource="acme/site", routine_id="rt_deploys"
        )

    admin.create_routine.assert_not_awaited()
    gh_client.post.assert_awaited_once()
    assert gh_client.post.await_args.args[0] == "https://api.github.com/repos/acme/site/hooks"
    assert gh_client.post.await_args.kwargs["json"]["config"]["secret"] == "pcsec_1"
    assert out["routine_id"] == "rt_deploys"
    assert out["guided"] is False


@pytest.mark.asyncio
async def test_attach_rollback_deletes_the_trigger_not_the_owners_routine():
    """GitHub refusing the hook must unwind ONLY the trigger this call added.
    Deleting the routine here would destroy hand-authored work the call never
    created — the create branch's rollback is not reusable as-is."""
    admin = _fake_admin(routine_id="rt_precious", secret_material=_GH_SECRET, trigger_id="trg_new")
    p_repo, p_cookie, p_client = _env(admin)
    p_http, _ = _gh_http_patch(_gh_response(403, {"message": "Forbidden"}))
    with p_repo, p_cookie, p_client, _gh_token_patch(), p_http, pytest.raises(ValueError, match="admin access"):
        await seed_trigger_routine("owner_1", "github", ["issues"], resource="acme/site", routine_id="rt_precious")

    admin.delete_routine.assert_not_awaited()
    admin.delete_routine_trigger.assert_awaited_once()
    assert admin.delete_routine_trigger.await_args.kwargs["trigger_id"] == "trg_new"


@pytest.mark.asyncio
async def test_attach_marks_the_funnel_event_as_an_existing_routine():
    admin = _fake_admin(routine_id="rt_x")
    p_repo, p_cookie, p_client = _env(admin)
    with (
        p_repo,
        p_cookie,
        p_client,
        patch("core.services.inbound_triggers.capture_product_event") as capture,
    ):
        await seed_trigger_routine("owner_1", "stripe", ["charge.refunded"], routine_id="rt_x")
    assert capture.call_args.kwargs["properties"]["target"] == "existing_routine"


@pytest.mark.asyncio
async def test_create_branch_still_creates_and_never_lists_routines():
    """The unchanged 1-click path: omitting routine_id still seeds a routine
    from the registry template, assigns it, and never consults the routine
    list (the attach fence must not become a cost every create pays)."""
    admin = _fake_admin(friday_id="friday_1", routine_id="rt_new")
    p_repo, p_cookie, p_client = _env(admin)
    with (
        p_repo,
        p_cookie,
        p_client,
        patch("core.services.inbound_triggers.capture_product_event") as capture,
    ):
        out = await seed_trigger_routine("owner_1", "stripe", ["charge.refunded"])

    admin.create_routine.assert_awaited_once()
    body = admin.create_routine.await_args.kwargs["body"]
    assert body["title"] == "Stripe: Charge refunded"
    assert body["assigneeAgentId"] == "friday_1"
    assert body["description"]
    admin.list_routines.assert_not_awaited()
    admin.delete_routine_trigger.assert_not_awaited()
    assert out["routine_id"] == "rt_new"
    assert capture.call_args.kwargs["properties"]["target"] == "new_routine"


# ── multi-event: one trigger, many events ───────────────────────────────────
#
# A Paperclip webhook trigger has no event filter, and Stripe/GitHub/Vercel/
# Meta all let one endpoint subscribe to many events — so the founder's
# three-event Revenue recovery routine is ONE trigger and ONE dashboard paste,
# not three. The events that CAN'T share a trigger (a Google watch, a Gmail
# poll) are the 400 below.

_STRIPE_THREE = ["invoice.payment_failed", "customer.subscription.deleted", "charge.refunded"]


@pytest.mark.asyncio
async def test_multi_event_creates_one_trigger_naming_every_event():
    """Three Stripe events -> ONE routine, ONE webhook trigger, and guided
    instructions that name all three (the owner subscribes one endpoint to the
    lot in the Stripe dashboard)."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "stripe", _STRIPE_THREE)

    admin.create_routine.assert_awaited_once()
    admin.create_routine_trigger.assert_awaited_once()

    body = admin.create_routine.await_args.kwargs["body"]
    assert body["title"] == "Stripe: Invoice payment failed, Subscription canceled and Charge refunded"
    # The agent wakes for any of the three, so it is told to branch on the
    # payload's own type before doing anything.
    assert "3 events" in body["description"]
    assert "read the event type out of the payload FIRST" in body["description"]

    for event_id in _STRIPE_THREE:
        assert event_id in out["instructions"], event_id
    assert out["proxy_url"] in out["instructions"]
    assert out["guided"] is True


@pytest.mark.asyncio
async def test_multi_event_trigger_label_falls_back_when_it_blows_the_cap():
    """Paperclip rejects a trigger label over 120 chars outright. Enough
    events joined into one title passes that long before it stops being
    useful, so the label falls back to the compact form."""
    from core.services.credentials.registry import get_provider

    events = [e.id for e in get_provider("stripe").inbound.events[:8]]
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", events)

    label = admin.create_routine_trigger.await_args.kwargs["body"]["label"]
    assert label == "Stripe (8 events)"
    assert len(label) <= 120


@pytest.mark.asyncio
async def test_a_title_between_the_label_and_title_caps_splits_them():
    """The window the 8-event case skips over: a joined title of 121-200 chars
    is a legal ROUTINE title but an illegal trigger LABEL, so the routine keeps
    the descriptive name and only the label falls back."""
    from core.services.credentials.registry import get_provider

    events = [e.id for e in get_provider("stripe").inbound.events[:5]]
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", events)

    title = admin.create_routine.await_args.kwargs["body"]["title"]
    assert 120 < len(title) <= 200, len(title)
    assert title.startswith("Stripe: Invoice payment failed,")
    assert admin.create_routine_trigger.await_args.kwargs["body"]["label"] == "Stripe (5 events)"


@pytest.mark.asyncio
async def test_a_long_custom_title_on_a_single_event_keeps_its_label():
    """Parity guard for the label fallback: it is a MULTI-event concern. A
    one-event automation with a long hand-typed title must reach Paperclip
    exactly as it did before multi-select — this call must never quietly
    rename somebody's trigger to the provider name."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    custom = "Dunning desk " * 12  # 156 chars, over the label cap, under the title cap
    with p_repo, p_cookie, p_client:
        await seed_trigger_routine("owner_1", "stripe", ["charge.refunded"], title=custom)

    assert admin.create_routine_trigger.await_args.kwargs["body"]["label"] == custom.strip()


@pytest.mark.asyncio
async def test_multi_event_vercel_registers_every_event_on_one_webhook():
    """The auto shape's whole point: ONE webhook on the project, subscribed to
    the UNION of the selected events' provider_events."""
    admin = _fake_admin(trigger_id="trig_int_1")
    p_repo, p_cookie, p_client = _env(admin)
    p_http, vc_client = _gh_http_patch(_gh_response(200, _VC_WEBHOOK))
    with p_repo, p_cookie, p_client, _gh_token_patch(token="vc_tok_1"), p_http:
        await seed_trigger_routine(
            "owner_1", "vercel_cli", ["deployment.error", "deployment.succeeded"], resource="prj_abc123"
        )

    vc_client.post.assert_awaited_once()
    assert vc_client.post.await_args.kwargs["json"]["events"] == [
        "deployment.error",
        "deployment.succeeded",
    ]
    admin.create_routine_trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_event_meta_subscribes_the_union_of_fields():
    """Meta's app-level ingress routes by intersecting a delivery's fields
    with the binding's, so the union is both what gets subscribed and what
    the binding must record."""
    admin = _fake_admin(secret_material=_META_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, meta_client = _meta_http_patch()
    (p_get_b, p_set_b), setter = _meta_bindings_patch()
    with (
        p_repo,
        p_cookie,
        p_client,
        p_http,
        p_get_b,
        p_set_b,
        patch(
            "core.services.inbound_triggers.integration_secret_store.get",
            AsyncMock(return_value={"access_token": "meta_user_tok"}),
        ),
    ):
        await seed_trigger_routine("owner_1", "meta_ads", ["leadgen", "feed"], resource="1112223334")

    meta_client.post.assert_awaited_once()
    assert meta_client.post.await_args.kwargs["params"]["subscribed_fields"] == "leadgen,feed"
    assert setter.await_args.args[2][0]["fields"] == ["leadgen", "feed"]
    admin.create_routine_trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_header_identified_providers_stay_one_event_per_trigger():
    """GitHub names the fired event in X-GitHub-Event and Sentry in
    Sentry-Hook-Resource — neither reaches the agent (the proxy forwards the
    body verbatim, and the fork's firePublicTrigger passes only `payload`).
    Arming several on one trigger would wake an agent that cannot tell which
    fired, so it is refused until Isol8AI/isol8#1916 lands."""
    for provider, events in (("github", ["pull_request", "issues"]), ("sentry", ["issue", "comment"])):
        admin = _fake_admin(secret_material=_GH_SECRET)
        p_repo, p_cookie, p_client = _env(admin)
        with p_repo, p_cookie, p_client, _gh_token_patch(), pytest.raises(ValueError) as exc:
            await seed_trigger_routine("owner_1", provider, events, resource="acme/site")
        assert "can't tell which event fired from the payload alone" in str(exc.value)
        assert "separate triggers" in str(exc.value)
        # Nothing was created — the check runs before the first upstream call.
        admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_event_github_still_registers_its_provider_events():
    """The pre-existing union under ONE curated id is untouched by the gate:
    an event whose provider_events lists several names still subscribes them
    all — that was never the multi-event path."""
    admin = _fake_admin(secret_material=_GH_SECRET)
    p_repo, p_cookie, p_client = _env(admin)
    p_http, gh_client = _gh_http_patch(_gh_response(201, {"id": 99}))
    with p_repo, p_cookie, p_client, _gh_token_patch(), p_http:
        await seed_trigger_routine("owner_1", "github", ["pull_request"], resource="acme/site")

    assert gh_client.post.await_args.kwargs["json"]["events"] == ["pull_request"]


@pytest.mark.asyncio
async def test_single_event_rendering_is_byte_identical_to_the_one_click_path():
    """The parity case. A one-element list must render the title, description
    and instructions EXACTLY as the single-event flow always did — the join of
    one item is the item, and the multi-event clause is a separate branch."""
    admin_one = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin_one)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "stripe", ["invoice.payment_failed"])

    routine = admin_one.create_routine.await_args.kwargs["body"]
    assert routine["title"] == "Stripe: Invoice payment failed"
    assert routine["description"] == (
        "A Stripe webhook just fired: Invoice payment failed. Look at the event payload, "
        "figure out what happened, and take the appropriate next step (e.g. follow up with "
        "the customer, flag it to the team, or update records)."
    )
    assert "This trigger is armed for" not in routine["description"]
    assert admin_one.create_routine_trigger.await_args.kwargs["body"]["label"] == "Stripe: Invoice payment failed"
    assert "invoice.payment_failed" in out["instructions"]
    assert "Invoice payment failed" in out["instructions"]


@pytest.mark.asyncio
async def test_events_with_different_registration_shapes_are_refused():
    """A Gmail poll rides a schedule and a Calendar watch opens a push
    channel — nothing about them can share one trigger. The error names both
    so the owner knows what to split."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError) as exc:
        await seed_trigger_routine("owner_1", "google", ["gmail.poll", "calendar.changed"], resource="ada@example.com")

    msg = str(exc.value)
    assert "gmail.poll (poll)" in msg
    assert "calendar.changed (auto_google_watch)" in msg
    assert "separate triggers" in msg
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_google_watches_cannot_share_one_trigger():
    """Same shape, still one-per-trigger: each watch is its own Google push
    channel with its own state file and renewal clause."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="separate triggers"):
        await seed_trigger_routine(
            "owner_1", "google", ["calendar.changed", "drive.changed"], resource="ada@example.com"
        )
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_events_in_a_list_are_named():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="unknown event 'not.a.real.event'"):
        await seed_trigger_routine("owner_1", "stripe", ["charge.refunded", "not.a.real.event"])
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_event_list_is_refused_before_anything_exists():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="at least one event"):
        await seed_trigger_routine("owner_1", "stripe", [])
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_event_attaches_one_trigger_to_an_existing_routine():
    """The founder's actual case: three Stripe events armed on the Revenue
    recovery routine they already wrote. One trigger added, nothing they wrote
    touched."""
    admin = _fake_admin(routine_id="rt_revenue_recovery")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client:
        out = await seed_trigger_routine("owner_1", "stripe", _STRIPE_THREE, routine_id="rt_revenue_recovery")

    admin.create_routine.assert_not_awaited()
    admin.patch_routine.assert_not_awaited()
    admin.create_routine_trigger.assert_awaited_once()
    assert out["routine_id"] == "rt_revenue_recovery"
    for event_id in _STRIPE_THREE:
        assert event_id in out["instructions"], event_id


@pytest.mark.asyncio
async def test_funnel_event_records_the_whole_event_list():
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, patch("core.services.inbound_triggers.capture_product_event") as capture:
        await seed_trigger_routine("owner_1", "stripe", _STRIPE_THREE)

    props = capture.call_args.kwargs["properties"]
    assert props["event_ids"] == _STRIPE_THREE
    assert props["event_count"] == 3
    # The old single-event property still reads, for funnels built on it.
    assert props["event_id"] == "invoice.payment_failed"


@pytest.mark.asyncio
async def test_a_duplicated_event_id_is_armed_and_counted_once():
    """Dedupe is the seed's, not the router's — a direct service call with the
    same id twice arms one event AND reports one, so the funnel can't
    over-count what was actually wired."""
    admin = _fake_admin()
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, patch("core.services.inbound_triggers.capture_product_event") as capture:
        await seed_trigger_routine("owner_1", "stripe", ["charge.refunded", "charge.refunded"])

    admin.create_routine_trigger.assert_awaited_once()
    assert admin.create_routine.await_args.kwargs["body"]["title"] == "Stripe: Charge refunded"
    props = capture.call_args.kwargs["properties"]
    assert props["event_ids"] == ["charge.refunded"]
    assert props["event_count"] == 1


# --- one platform block, whatever brand armed the trigger -------------------


def test_strip_platform_blocks_removes_every_delimiter_generation():
    from core.services.inbound_triggers import _PLATFORM_BLOCK_DELIMITERS, _strip_platform_blocks

    for start, end in _PLATFORM_BLOCK_DELIMITERS:
        desc = f"{_OWNER_WORDS}\n\n{start}\nRe-arm the watch channel weekly.\n{end}"
        assert _strip_platform_blocks(desc).strip() == _OWNER_WORDS


def test_strip_platform_blocks_leaves_an_unterminated_marker_alone():
    """No end delimiter means someone edited the body. Cutting from there
    would delete their work; a duplicate block is cheaper."""
    from core.services.inbound_triggers import _PLATFORM_BLOCK_START, _strip_platform_blocks

    desc = f"{_OWNER_WORDS}\n\n{_PLATFORM_BLOCK_START}\nhalf a block the owner mangled"
    assert _strip_platform_blocks(desc) == desc


def test_strip_platform_blocks_keeps_prose_between_an_orphan_start_and_a_real_block():
    """The stacking bug this PR fixes is exactly what strands an orphaned
    start marker mid-description. Anchoring on the FIRST start and any later
    end would delete every owner word between the two — content `main` never
    touches. Only the complete block may go."""
    from core.services.inbound_triggers import (
        _PLATFORM_BLOCK_END,
        _PLATFORM_BLOCK_START,
        _strip_platform_blocks,
    )

    desc = (
        f"{_PLATFORM_BLOCK_START}\nowner deleted the end marker\n\n"
        "Owner's second paragraph, load-bearing.\n\n"
        f"{_PLATFORM_BLOCK_START}\nplatform clause\n{_PLATFORM_BLOCK_END}"
    )
    stripped = _strip_platform_blocks(desc)

    assert "Owner's second paragraph, load-bearing." in stripped
    assert "owner deleted the end marker" in stripped
    assert "platform clause" not in stripped
    assert _PLATFORM_BLOCK_END not in stripped
    # The orphan survives, so exactly one start marker is left.
    assert stripped.count(_PLATFORM_BLOCK_START) == 1


@pytest.mark.asyncio
async def test_append_after_an_orphan_start_leaves_one_complete_block():
    from core.services.inbound_triggers import (
        _PLATFORM_BLOCK_END,
        _PLATFORM_BLOCK_START,
        _append_platform_block,
    )

    admin = MagicMock()
    admin.get_routine = AsyncMock(
        return_value={
            "description": (
                f"{_PLATFORM_BLOCK_START}\nowner deleted the end marker\n\n"
                "Owner's second paragraph, load-bearing.\n\n"
                f"{_PLATFORM_BLOCK_START}\nstale clause\n{_PLATFORM_BLOCK_END}"
            )
        }
    )
    admin.patch_routine = AsyncMock()

    await _append_platform_block(admin, cookie="c", routine_id="rt_1", block="Fresh clause.")

    written = admin.patch_routine.await_args.kwargs["body"]["description"]
    assert "Owner's second paragraph, load-bearing." in written
    assert "stale clause" not in written
    assert "Fresh clause." in written
    assert written.count(_PLATFORM_BLOCK_END) == 1


@pytest.mark.asyncio
async def test_append_platform_block_replaces_a_legacy_brand_block():
    """Re-arming a trigger armed before the rename must leave ONE block. The
    retired delimiters no longer match the new ones, so without recognising
    them the owner ends up with two sets of contradicting instructions."""
    from core.services.inbound_triggers import (
        _PLATFORM_BLOCK_END,
        _PLATFORM_BLOCK_START,
        _append_platform_block,
    )

    legacy_start = "--- Added by Lightbulb AI when this trigger was armed — leave this block in place ---"
    legacy_end = "--- end Lightbulb AI block ---"
    admin = MagicMock()
    admin.get_routine = AsyncMock(
        return_value={"description": f"{_OWNER_WORDS}\n\n{legacy_start}\nStale clause.\n{legacy_end}"}
    )
    admin.patch_routine = AsyncMock()

    await _append_platform_block(admin, cookie="c", routine_id="rt_1", block="Fresh clause.")

    written = admin.patch_routine.await_args.kwargs["body"]["description"]
    assert written.count(_PLATFORM_BLOCK_START) == 1
    assert written.count(_PLATFORM_BLOCK_END) == 1
    assert legacy_start not in written
    assert "Stale clause." not in written
    assert "Fresh clause." in written
    # The owner's own words survive, first and whole.
    assert written.startswith(_OWNER_WORDS)


@pytest.mark.asyncio
async def test_append_platform_block_does_not_stack_its_own_block():
    from core.services.inbound_triggers import _PLATFORM_BLOCK_START, _append_platform_block

    admin = MagicMock()
    admin.get_routine = AsyncMock(return_value={"description": _OWNER_WORDS})
    admin.patch_routine = AsyncMock()

    await _append_platform_block(admin, cookie="c", routine_id="rt_1", block="Clause.")
    first = admin.patch_routine.await_args.kwargs["body"]["description"]

    admin.get_routine = AsyncMock(return_value={"description": first})
    await _append_platform_block(admin, cookie="c", routine_id="rt_1", block="Clause.")
    second = admin.patch_routine.await_args.kwargs["body"]["description"]

    assert second.count(_PLATFORM_BLOCK_START) == 1
    assert second == first


def test_strip_platform_blocks_scans_past_an_orphan_end_delimiter():
    """An orphaned END is content, not a stop sign. Ending the scan there
    leaves a real block behind it in place — the duplication this exists to
    prevent."""
    from core.services.inbound_triggers import (
        _PLATFORM_BLOCK_END,
        _PLATFORM_BLOCK_START,
        _strip_platform_blocks,
    )

    desc = (
        f"{_PLATFORM_BLOCK_END}\nowner deleted the start marker\n\n"
        "Owner's second paragraph, load-bearing.\n\n"
        f"{_PLATFORM_BLOCK_START}\nplatform clause\n{_PLATFORM_BLOCK_END}"
    )
    stripped = _strip_platform_blocks(desc)

    assert "Owner's second paragraph, load-bearing." in stripped
    assert "owner deleted the start marker" in stripped
    # The orphan end survives; the complete block after it does not.
    assert stripped.count(_PLATFORM_BLOCK_END) == 1
    assert _PLATFORM_BLOCK_START not in stripped
    assert "platform clause" not in stripped


@pytest.mark.asyncio
async def test_append_after_an_orphan_end_leaves_one_complete_block():
    from core.services.inbound_triggers import (
        _PLATFORM_BLOCK_END,
        _PLATFORM_BLOCK_START,
        _append_platform_block,
    )

    admin = MagicMock()
    admin.get_routine = AsyncMock(
        return_value={
            "description": (
                f"{_PLATFORM_BLOCK_END}\nowner deleted the start marker\n\n"
                "Owner's second paragraph, load-bearing.\n\n"
                f"{_PLATFORM_BLOCK_START}\nstale clause\n{_PLATFORM_BLOCK_END}"
            )
        }
    )
    admin.patch_routine = AsyncMock()

    await _append_platform_block(admin, cookie="c", routine_id="rt_1", block="Fresh clause.")

    written = admin.patch_routine.await_args.kwargs["body"]["description"]
    assert "Owner's second paragraph, load-bearing." in written
    assert "stale clause" not in written
    assert "Fresh clause." in written
    assert written.count(_PLATFORM_BLOCK_START) == 1
    # One appended block + the surviving orphan end.
    assert written.count(_PLATFORM_BLOCK_END) == 2
