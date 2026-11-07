"""agent_email: the workspace's one mailbox — create-on-first-use and the per-agent From."""

import os
from email.utils import formataddr

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

TEAM = "agents@acme.thelightbulb.company"


from core.services.agent_email import sender_allowed  # noqa: E402


def test_sender_allowed_exact_and_domain():
    row = {"verified_senders": ["owner@example.com", "@clientco.com"]}
    assert sender_allowed(row, "owner@example.com")
    assert sender_allowed(row, "Owner@Example.com")
    assert sender_allowed(row, "anyone@clientco.com")
    assert not sender_allowed(row, "anyone@notclientco.com")
    assert not sender_allowed(row, "anyone@sub.clientco.com")
    assert not sender_allowed({}, "owner@example.com")


from unittest.mock import AsyncMock  # noqa: E402

from core.services import agent_email as ae  # noqa: E402


@pytest.mark.asyncio
async def test_append_agent_mail_footer_is_default_on_and_escapes_html(monkeypatch):
    monkeypatch.setattr(ae.agent_email_repo, "get_by_address", AsyncMock(return_value={}))
    monkeypatch.setattr(ae, "workspace_name", AsyncMock(return_value="Acme & Co"))
    monkeypatch.setattr(ae.settings, "FRONTEND_URL", "https://lightbulb.example/")

    text, rendered_html = await ae.append_agent_mail_footer(
        owner_id="owner_1",
        agent_id="agent_friday",
        address=TEAM,
        body_text="Hello",
        body_html="<p>Hello</p>",
        agent_name="Friday <CEO>",
    )

    assert text == (
        "Hello\n\nSent by Friday <CEO> for Acme & Co with Lightbulb (https://lightbulb.example?utm_source=agent_email)."
    )
    assert rendered_html == (
        "<p>Hello</p><p>Sent by Friday &lt;CEO&gt; for Acme &amp; Co with "
        '<a href="https://lightbulb.example?utm_source=agent_email">Lightbulb</a>.</p>'
    )


@pytest.mark.asyncio
async def test_append_agent_mail_footer_honors_owner_opt_out(monkeypatch):
    monkeypatch.setattr(
        ae.agent_email_repo,
        "get_by_address",
        AsyncMock(return_value={"footer_enabled": False}),
    )

    assert await ae.append_agent_mail_footer(
        owner_id="owner_1",
        agent_id="agent_friday",
        address=TEAM,
        body_text="Hello",
        body_html="<p>Hello</p>",
    ) == ("Hello", "<p>Hello</p>")


def _registry(rows: dict):
    """``agent_registry.get`` over ``{(ws, agent_id): row}``."""
    return AsyncMock(side_effect=lambda ws, agent_id: rows.get((ws, agent_id)))


@pytest.mark.asyncio
async def test_resolve_agent_name_reads_the_workspace_registry(monkeypatch):
    rows = {("o", "a1"): {"agentId": "a1", "name": " Ada "}, ("o", "gone"): {"name": "Old", "status": "deleted"}}
    monkeypatch.setattr(ae.agent_registry, "get", _registry(rows))
    assert await ae.resolve_agent_name(owner_id="o", agent_id="a1") == "Ada"
    assert await ae.resolve_agent_name(owner_id="o", agent_id="gone") is None
    # Another workspace's agent id never resolves: the registry is keyed by workspace.
    assert await ae.resolve_agent_name(owner_id="other", agent_id="a1") is None
    monkeypatch.setattr(ae.agent_registry, "get", AsyncMock(side_effect=RuntimeError("ddb")))
    assert await ae.resolve_agent_name(owner_id="o", agent_id="a1") is None


@pytest.fixture
def team(monkeypatch):
    monkeypatch.setattr(ae.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(ae.company_slug_repo, "get_by_owner", AsyncMock(return_value={"slug": "acme"}))
    monkeypatch.setattr(ae, "workspace_name", AsyncMock(return_value="Acme"))
    monkeypatch.setattr(ae.agent_email_repo, "get_by_address", AsyncMock(return_value={"address": TEAM}))


@pytest.mark.asyncio
async def test_ensure_team_address_reuses_the_existing_row(monkeypatch, team):
    monkeypatch.setattr(ae.agent_email_repo, "get_by_address", AsyncMock(return_value={"owner_id": "o"}))
    put = AsyncMock()
    monkeypatch.setattr(ae.agent_email_repo, "put", put)
    assert await ae.ensure_team_address("o", "o@x.y") == (TEAM, "ok")
    put.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_team_address_creates_the_one_row(monkeypatch, team):
    monkeypatch.setattr(ae.agent_email_repo, "get_by_address", AsyncMock(return_value=None))
    monkeypatch.setattr(ae.agent_email_repo, "query_by_owner", AsyncMock(return_value=[]))
    put = AsyncMock(return_value={"owner_id": "o"})
    monkeypatch.setattr(ae.agent_email_repo, "put", put)
    assert await ae.ensure_team_address("o", "o@x.y") == (TEAM, "ok")
    assert put.await_args.kwargs == {
        "address": TEAM,
        "owner_id": "o",
        "verified_senders": ["o@x.y"],
        "send_to_anyone": True,
    }


@pytest.mark.asyncio
async def test_ensure_team_address_refuses_another_owners_row(monkeypatch, team):
    monkeypatch.setattr(ae.agent_email_repo, "get_by_address", AsyncMock(return_value={"owner_id": "other"}))
    assert await ae.ensure_team_address("o", "o@x.y") == (None, "mint_failed")


@pytest.mark.asyncio
async def test_ensure_team_address_slug_derives_from_workspace_name(monkeypatch, team):
    """No slug yet: allocated from the workspace name."""
    monkeypatch.setattr(ae.company_slug_repo, "get_by_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(ae, "workspace_name", AsyncMock(return_value="Renamed By Friday"))
    slug = AsyncMock(return_value="renamed")
    monkeypatch.setattr(ae, "allocate_company_slug", slug)
    monkeypatch.setattr(ae.agent_email_repo, "get_by_address", AsyncMock(return_value={"owner_id": "o"}))
    assert await ae.ensure_team_address("o", None) == ("agents@renamed.thelightbulb.company", "ok")
    assert slug.await_args.kwargs["name"] == "Renamed By Friday"


@pytest.mark.asyncio
async def test_ensure_team_address_reasons(monkeypatch, team):
    monkeypatch.setattr(ae.company_slug_repo, "get_by_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(ae, "allocate_company_slug", AsyncMock(return_value=None))
    assert await ae.ensure_team_address("o", None) == (None, "slug_allocation_failed")
    monkeypatch.setattr(ae.settings, "AGENT_EMAIL_DOMAIN", "")
    assert await ae.ensure_team_address("o", None) == (None, "disabled")


@pytest.mark.asyncio
async def test_from_header_is_the_agent_on_the_team_address(monkeypatch, team):
    monkeypatch.setattr(ae, "resolve_agent_name", AsyncMock(return_value="Friday"))
    header = await ae.from_header("o", "agent_friday", TEAM)
    assert header == formataddr(("Friday · Acme agents", TEAM))
    assert ae.resolve_agent_name.await_args.kwargs == {"owner_id": "o", "agent_id": "agent_friday"}


@pytest.mark.asyncio
async def test_agent_mail_from_uses_registry_name(monkeypatch, team):
    monkeypatch.setattr(ae.agent_registry, "get", _registry({("o", "a1"): {"agentId": "a1", "name": "Ada"}}))
    assert await ae.from_header("o", "a1", TEAM) == formataddr(("Ada · Acme agents", TEAM))


@pytest.mark.asyncio
async def test_team_address_minted_for_workspace_without_company_id(monkeypatch):
    """A workspace provisioned natively has no company_id and maybe no saved name: the slug
    comes from the owner id and the address is still minted."""
    monkeypatch.setattr(ae.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(ae.company_slug_repo, "get_by_owner", AsyncMock(return_value=None))
    from core.services import workspace_name as wn

    monkeypatch.setattr(wn.PaperclipRepo, "get_workspace", AsyncMock(return_value=None))
    monkeypatch.setattr(wn.clerk_admin, "get_organization", AsyncMock(return_value=None))
    slug = AsyncMock(return_value="org-1")
    monkeypatch.setattr(ae, "allocate_company_slug", slug)
    monkeypatch.setattr(ae.agent_email_repo, "get_by_address", AsyncMock(return_value={"owner_id": "org_1"}))
    assert await ae.ensure_team_address("org_1", None) == ("agents@org-1.thelightbulb.company", "ok")
    assert slug.await_args.kwargs["name"] == "org_1"


@pytest.mark.asyncio
async def test_from_header_falls_back_to_the_bare_address(monkeypatch, team):
    monkeypatch.setattr(ae, "resolve_agent_name", AsyncMock(return_value=None))
    assert await ae.from_header("o", "agent_x", TEAM) == TEAM


PUBLIC = "agents@thelightbulb.company"


@pytest.mark.asyncio
async def test_from_header_sends_as_the_public_address(monkeypatch, team):
    monkeypatch.setattr(
        ae.agent_email_repo, "get_by_address", AsyncMock(return_value={"address": TEAM, "public_address": PUBLIC})
    )
    monkeypatch.setattr(ae, "resolve_agent_name", AsyncMock(return_value="Friday"))
    assert await ae.from_header("o", "agent_friday", TEAM) == formataddr(("Friday · Acme agents", PUBLIC))
    monkeypatch.setattr(ae, "resolve_agent_name", AsyncMock(return_value=None))
    assert await ae.from_header("o", "agent_x", TEAM) == PUBLIC


@pytest.mark.asyncio
async def test_public_address_is_the_team_address_without_one(monkeypatch, team):
    assert await ae.public_address("o") == TEAM
    monkeypatch.setattr(ae.agent_email_repo, "get_by_address", AsyncMock(side_effect=RuntimeError("ddb")))
    assert await ae.public_for(TEAM) == TEAM  # a failed read still sends, from the routing address
    monkeypatch.setattr(ae.agent_email_repo, "get_by_address", AsyncMock(return_value={"public_address": PUBLIC}))
    assert await ae.public_address("o") == PUBLIC


def test_strictest_ignores_alias_rows():
    alias = {"address": PUBLIC, "alias_of": TEAM, "owner_id": "o"}
    assert ae.strictest([{"send_to_anyone": True, "verified_senders": []}, alias])["send_to_anyone"] is True


def test_strictest_ignores_text_rows():
    text = {"address": "+15551230000", "kind": "text", "owner_id": "o"}
    assert ae.strictest([{"send_to_anyone": True, "verified_senders": []}, text])["send_to_anyone"] is True


# ---- mail_changed: the Mail panel's live nudge ----

import asyncio  # noqa: E402
from unittest.mock import patch  # noqa: E402

from core.services.agent_email import mail_changed as real_mail_changed  # noqa: E402  (conftest stubs the attribute)


async def test_mail_changed_tells_the_workspace_people_to_reread_mail():
    with patch("core.services.live_events.publish_owner_users", AsyncMock()) as pub:
        await real_mail_changed("org_1")
        await asyncio.gather(*ae._nudges)
    owner, event = pub.await_args.args
    assert owner == "org_1"
    assert event["type"] == "runtime.resource.invalidated" and event["payload"] == {"resource": "mail"}


async def test_mail_changed_swallows_a_failed_publish():
    with patch("core.services.live_events.publish_owner_users", AsyncMock(side_effect=RuntimeError("appsync"))):
        await real_mail_changed("org_1")
        await asyncio.gather(*ae._nudges)  # the background publish never raises


async def test_mail_changed_never_waits_on_the_publish_and_bounds_it(monkeypatch):
    async def stall(*_):
        await asyncio.sleep(60)

    monkeypatch.setattr(ae, "_MAIL_CHANGED_TIMEOUT_S", 0.01)
    with patch("core.services.live_events.publish_owner_users", stall):
        await asyncio.wait_for(real_mail_changed("org_1"), 0.005)  # returns before any publish work
        await asyncio.wait_for(asyncio.gather(*ae._nudges), 1)  # and the publish gives up
