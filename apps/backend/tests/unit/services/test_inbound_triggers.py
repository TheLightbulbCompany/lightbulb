"""Tests for the Automations Phase 2 trigger-seed service.

Mirrors the ``_resolver_env`` fake-collaborator idiom from
``test_paperclip_trigger_proxy.py``: patch ``PaperclipRepo`` (company
resolution), ``get_admin_session_cookie``, and ``PaperclipAdminClient`` so
nothing here touches a real Paperclip deployment. GitHub's auto-register HTTP
call is faked with an ``httpx.MockTransport``-backed client swapped in for
``_github_client_factory`` — same convention ``test_paperclip_trigger_proxy.py``
uses for its own ``_client_factory``, since respx mismatches httpx 0.28 URLs.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services.inbound_triggers import cleanup_github_hook, seed_trigger_routine


@pytest.fixture(autouse=True)
def mock_secret_put():
    """Every seeded trigger writes a small durable record via
    integration_secret_store.put — the stripe-edge-seeded marker (stripe_edge
    signing_mode) and the GitHub hook id/repo retention (auto registration).
    Autouse keeps every OTHER test in this file off real boto3; tests that
    care about the write itself take this fixture as an argument."""
    with patch("core.services.inbound_triggers.integration_secret_store.put", AsyncMock(return_value="arn")) as m:
        yield m


def _fake_admin(*, friday_id="friday_1", routine_id="rt_1", public_id="pub_1"):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[{"id": friday_id, "role": "ceo"}])
    admin.create_routine = AsyncMock(return_value={"id": routine_id})
    admin.create_routine_trigger = AsyncMock(
        return_value={
            "trigger": {"publicId": public_id},
            "secretMaterial": {"webhookUrl": "https://example.test/hook", "webhookSecret": "s"},
        }
    )
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
async def test_seed_trigger_routine_stripe_guided(mock_secret_put):
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

    # The stripe-edge-seeded marker — the ONLY thing PUT
    # /automations/{public_id}/webhook-secret (routers/automations.py) may
    # gate on, NOT Paperclip signingMode == "none" (also Phase 1's own
    # deliberately-unsigned option). Written to the same Secrets Manager
    # slot the whsec_ itself later occupies.
    mock_secret_put.assert_awaited_once_with("owner_1", "stripe-trigger-pub_1", {"stripe_edge_seeded": True})


@pytest.mark.asyncio
async def test_seed_trigger_routine_stripe_marker_write_failure_rolls_back_routine(mock_secret_put):
    """If the stripe-edge-seeded marker can't be written, the routine+trigger
    already created upstream are an orphan the owner could never legitimately
    arm (PUT would always 400 not_a_stripe_edge_trigger) — roll back like any
    other post-creation failure."""
    admin = _fake_admin(routine_id="rt_orphan_marker")
    mock_secret_put.side_effect = RuntimeError("Secrets Manager throttled")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(RuntimeError, match="Secrets Manager throttled"):
        await seed_trigger_routine("owner_1", "stripe", "invoice.payment_failed")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_marker"


@pytest.mark.asyncio
async def test_seed_trigger_routine_builds_expected_bodies():
    """The routine and trigger bodies sent upstream — title/description carry
    the interpolated event label, the routine assigns to the resolved Friday
    id, and the webhook trigger is armed with Paperclip signingMode "none"
    (stripe_edge verifies at the Isol8 edge instead — see stripe_edge.py)."""
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
    # Registry signing_mode "stripe_edge" must NOT reach Paperclip verbatim —
    # it arms "none" because verification happens at the Isol8 edge instead.
    assert trigger_body["signingMode"] == "none"
    assert trigger_body["replayWindowSec"] == 300
    assert trigger_body["label"] == "Stripe: Charge refunded"


@pytest.mark.asyncio
async def test_seed_trigger_routine_bails_when_provider_has_no_inbound():
    # google has no `inbound:` block. (github DOES as of Task 4 — see the
    # github-specific tests below — so it can no longer stand in here.)
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
    leave the routine it already created orphaned — this happens BEFORE the
    guided/auto branch split, so the rollback has to cover both."""
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
        await seed_trigger_routine("owner_1", "github", "workflow_run", repo="acme/widgets")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_malformed"


# ---------------------------------------------------------------------------
# GitHub auto-registration (Task 4) — webhook_registration: "auto"
# ---------------------------------------------------------------------------


class _StubGitHubTransport(httpx.AsyncBaseTransport):
    def __init__(self, captured: dict, *, status: int = 201, content: bytes = b'{"id": 123}'):
        self.captured = captured
        self.status = status
        self.content = content

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.captured["url"] = str(request.url)
        self.captured["body"] = request.content
        self.captured["headers"] = dict(request.headers)
        return httpx.Response(self.status, content=self.content)


def _stub_github_client(captured: dict, **kw):
    transport = _StubGitHubTransport(captured, **kw)
    return patch(
        "core.services.inbound_triggers._github_client_factory",
        lambda: httpx.AsyncClient(transport=transport),
    )


def _github_token(token: str | None = "tok_gh"):
    value = {"access_token": token} if token else None
    return patch("core.services.inbound_triggers.integration_secret_store.get", AsyncMock(return_value=value))


@pytest.mark.asyncio
async def test_seed_trigger_routine_github_auto_registers_webhook(mock_secret_put):
    admin = _fake_admin(routine_id="rt_gh", public_id="pub_gh")
    p_repo, p_cookie, p_client = _env(admin)
    captured: dict = {}
    with p_repo, p_cookie, p_client, _github_token(), _stub_github_client(captured):
        out = await seed_trigger_routine("owner_1", "github", "workflow_run", repo="acme/widgets")

    assert out["guided"] is False
    assert out["instructions"] is None
    assert out["proxy_url"].endswith("/api/v1/hooks/t/owner_1/pub_gh")

    assert captured["url"] == "https://api.github.com/repos/acme/widgets/hooks"
    assert captured["headers"]["authorization"] == "Bearer tok_gh"
    body = json.loads(captured["body"])
    assert body["name"] == "web"
    assert body["active"] is True
    assert body["config"]["url"] == out["proxy_url"]
    assert body["config"]["secret"] == "s"  # secretMaterial.webhookSecret from _fake_admin
    assert body["config"]["content_type"] == "json"
    # ONLY the selected event, never the provider's whole inbound.events list
    # — see _auto_register_webhook's docstring for why (cross-firing routines).
    assert body["events"] == ["workflow_run"]
    admin.delete_routine.assert_not_awaited()

    # The registered hook's id + repo are retained keyed by public_id, so a
    # later routine/trigger delete can unregister it (cleanup_github_hook).
    # _StubGitHubTransport's default response body is {"id": 123}.
    mock_secret_put.assert_awaited_once_with("owner_1", "github-hook-pub_gh", {"hook_id": 123, "repo": "acme/widgets"})


@pytest.mark.asyncio
async def test_seed_trigger_routine_github_two_events_register_two_single_event_hooks():
    """Two automations on the same repo (one per event) must each register a
    SEPARATE hook subscribed to only its own event — never the provider's
    whole event list — or every push fires both routines (duplicate agent
    runs + duplicate spend)."""
    captured_calls: list[dict] = []

    class _RecordingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured_calls.append({"url": str(request.url), "body": request.content})
            return httpx.Response(201, content=b'{"id": 1}')

    def _client_factory():
        return httpx.AsyncClient(transport=_RecordingTransport())

    for event_id, public_id in (("workflow_run", "pub_wr"), ("issues", "pub_is")):
        admin = _fake_admin(routine_id=f"rt_{event_id}", public_id=public_id)
        p_repo, p_cookie, p_client = _env(admin)
        with (
            p_repo,
            p_cookie,
            p_client,
            _github_token(),
            patch("core.services.inbound_triggers._github_client_factory", _client_factory),
        ):
            await seed_trigger_routine("owner_1", "github", event_id, repo="acme/widgets")

    assert len(captured_calls) == 2
    assert [json.loads(c["body"])["events"] for c in captured_calls] == [["workflow_run"], ["issues"]]


@pytest.mark.asyncio
async def test_seed_trigger_routine_github_missing_repo_rolls_back_routine():
    admin = _fake_admin(routine_id="rt_orphan_repo")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, pytest.raises(ValueError, match="repo"):
        await seed_trigger_routine("owner_1", "github", "workflow_run")  # no repo=
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_repo"


@pytest.mark.asyncio
async def test_seed_trigger_routine_github_missing_token_rolls_back_routine():
    admin = _fake_admin(routine_id="rt_orphan_token")
    p_repo, p_cookie, p_client = _env(admin)
    with p_repo, p_cookie, p_client, _github_token(None), pytest.raises(ValueError, match="no github credential"):
        await seed_trigger_routine("owner_1", "github", "workflow_run", repo="acme/widgets")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_token"


@pytest.mark.asyncio
async def test_seed_trigger_routine_github_missing_secret_material_rolls_back_routine():
    """If Paperclip's create_routine_trigger returns no
    secretMaterial.webhookSecret (None), registering the GitHub hook anyway
    would create a secret-less hook that 401s every real event forever while
    this call still reports success. Must raise instead of silently
    proceeding, and roll back the routine it already created — same as any
    other auto-register failure."""
    admin = _fake_admin(routine_id="rt_orphan_secret")
    admin.create_routine_trigger = AsyncMock(
        return_value={"trigger": {"publicId": "pub_1"}, "secretMaterial": {"webhookUrl": "u", "webhookSecret": None}}
    )
    p_repo, p_cookie, p_client = _env(admin)
    with (
        p_repo,
        p_cookie,
        p_client,
        _github_token(),
        pytest.raises(ValueError, match="no trigger secret"),
    ):
        await seed_trigger_routine("owner_1", "github", "workflow_run", repo="acme/widgets")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_secret"


@pytest.mark.asyncio
async def test_seed_trigger_routine_github_api_error_rolls_back_routine():
    admin = _fake_admin(routine_id="rt_orphan_api")
    p_repo, p_cookie, p_client = _env(admin)
    captured: dict = {}
    with (
        p_repo,
        p_cookie,
        p_client,
        _github_token(),
        _stub_github_client(captured, status=422, content=b'{"message":"Hook already exists"}'),
        pytest.raises(ValueError, match="GitHub webhook registration failed"),
    ):
        await seed_trigger_routine("owner_1", "github", "workflow_run", repo="acme/widgets")
    admin.delete_routine.assert_awaited_once()
    assert admin.delete_routine.await_args.kwargs["routine_id"] == "rt_orphan_api"


# ---------------------------------------------------------------------------
# GitHub hook cleanup on delete (Codex P2 finding) — cleanup_github_hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_github_hook_noop_when_nothing_retained():
    """A guided_paste (Stripe) trigger, a schedule trigger, or one predating
    this retention never has a record at this slot — cleanup is a silent
    no-op, never an error."""
    with patch("core.services.inbound_triggers.integration_secret_store.get", AsyncMock(return_value=None)) as get:
        await cleanup_github_hook("owner_1", "pub_1")
    get.assert_awaited_once_with("owner_1", "github-hook-pub_1")


@pytest.mark.asyncio
async def test_cleanup_github_hook_unregisters_and_clears_retention():
    """A retained hook is unregistered via DELETE /repos/{repo}/hooks/{id}
    using the owner's stored GitHub token, and the retention record is
    dropped afterward."""
    captured: dict = {}

    class _Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            return httpx.Response(204)

    async def _get(owner_id: str, provider: str):
        if provider == "github-hook-pub_1":
            return {"hook_id": 42, "repo": "acme/widgets"}
        assert provider == "github"
        return {"access_token": "tok_gh"}

    delete_mock = AsyncMock()
    with (
        patch("core.services.inbound_triggers.integration_secret_store.get", AsyncMock(side_effect=_get)),
        patch("core.services.inbound_triggers.integration_secret_store.delete", delete_mock),
        patch(
            "core.services.inbound_triggers._github_client_factory", lambda: httpx.AsyncClient(transport=_Transport())
        ),
    ):
        await cleanup_github_hook("owner_1", "pub_1")

    assert captured["method"] == "DELETE"
    assert captured["url"] == "https://api.github.com/repos/acme/widgets/hooks/42"
    assert captured["headers"]["authorization"] == "Bearer tok_gh"
    delete_mock.assert_awaited_once_with("owner_1", "github-hook-pub_1")


@pytest.mark.asyncio
async def test_cleanup_github_hook_swallows_github_failure_and_still_clears_retention():
    """A GitHub-side failure (expired token, repo renamed/deleted, hook
    already gone) must never raise — it's cleanup running ahead of an
    already-decided delete. The retention record is dropped regardless,
    since this is best-effort, not a retryable job."""

    class _FailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

    async def _get(owner_id: str, provider: str):
        if provider == "github-hook-pub_1":
            return {"hook_id": 42, "repo": "acme/widgets"}
        return {"access_token": "tok_gh"}

    delete_mock = AsyncMock()
    with (
        patch("core.services.inbound_triggers.integration_secret_store.get", AsyncMock(side_effect=_get)),
        patch("core.services.inbound_triggers.integration_secret_store.delete", delete_mock),
        patch(
            "core.services.inbound_triggers._github_client_factory",
            lambda: httpx.AsyncClient(transport=_FailTransport()),
        ),
    ):
        await cleanup_github_hook("owner_1", "pub_1")  # must not raise

    delete_mock.assert_awaited_once_with("owner_1", "github-hook-pub_1")
