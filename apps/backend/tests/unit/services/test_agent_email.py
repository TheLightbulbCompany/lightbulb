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
    monkeypatch.setattr(ae, "PaperclipRepo", _onboarding_repo())
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value="Acme & Co"))
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
        "Hello\n\nSent by Friday <CEO> for Acme & Co with Lightbulb "
        "(https://lightbulb.example?utm_source=agent_email)."
    )
    assert rendered_html == (
        '<p>Hello</p><p>Sent by Friday &lt;CEO&gt; for Acme &amp; Co with '
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


def _onboarding_repo(company_id="co_1", friday="agent_friday"):
    """A stand-in for ``PaperclipRepo`` whose ``get_onboarding_state`` returns
    a fixed onboarding row."""

    class _Repo:
        def __init__(self, table_name): ...

        async def get_onboarding_state(self, owner_id):
            return {"company_id": company_id, "friday_agent_id": friday}

    return _Repo


def _admin_returning(agent: dict):
    """A stand-in for ``shared_admin_client()`` whose ``get_agent`` returns a
    fixed agent dict."""

    class _Admin:
        _http = None

        async def get_agent(self, *, session_cookie, agent_id):
            return agent

    return _Admin


@pytest.mark.asyncio
async def test_resolve_agent_name_reads_current_company_scoped_name_for_legacy_seed(monkeypatch):
    monkeypatch.setattr(
        ae,
        "shared_admin_client",
        lambda: _admin_returning({"name": "Ada", "companyId": "co_1"})(),
    )
    monkeypatch.setattr(ae, "get_admin_session_cookie", AsyncMock(return_value="cookie"))

    assert (
        await ae.resolve_agent_name(
            agent_id="agent_friday",
            company_id="co_1",
        )
        == "Ada"
    )


@pytest.mark.asyncio
async def test_resolve_agent_name_rejects_agent_from_a_different_company(monkeypatch):
    """get_agent rides the shared admin cookie and is not company-scoped — a
    container-supplied agent_id from a different owner's company must not
    put a foreign agent's name on this owner's mail."""

    monkeypatch.setattr(
        ae, "shared_admin_client", lambda: _admin_returning({"name": "Mallory", "companyId": "co_other"})()
    )
    monkeypatch.setattr(ae, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    assert await ae.resolve_agent_name(agent_id="agent_mallory", company_id="co_1") is None


@pytest.mark.asyncio
async def test_resolve_agent_name_rejects_agent_with_no_companyid(monkeypatch):
    """A record with no ``companyId`` at all is unresolved, not authorized —
    the cross-company guard must not treat a falsy companyId as a pass."""
    monkeypatch.setattr(ae, "shared_admin_client", lambda: _admin_returning({"name": "Nameless"})())
    monkeypatch.setattr(ae, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    assert await ae.resolve_agent_name(agent_id="agent_nameless", company_id="co_1") is None


@pytest.fixture
def team(monkeypatch):
    monkeypatch.setattr(ae.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(ae.company_slug_repo, "get_by_owner", AsyncMock(return_value={"slug": "acme"}))
    monkeypatch.setattr(ae, "PaperclipRepo", _onboarding_repo())
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
async def test_ensure_team_address_slug_derives_from_live_company_name(monkeypatch, team):
    """No slug yet: allocated from the company name the agent chose, read live."""
    monkeypatch.setattr(ae.company_slug_repo, "get_by_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value="Renamed By Friday"))
    slug = AsyncMock(return_value="renamed")
    monkeypatch.setattr(ae, "allocate_company_slug", slug)
    monkeypatch.setattr(ae.agent_email_repo, "get_by_address", AsyncMock(return_value={"owner_id": "o"}))
    assert await ae.ensure_team_address("o", None) == ("agents@renamed.thelightbulb.company", "ok")
    assert slug.await_args.kwargs["name"] == "Renamed By Friday"


@pytest.mark.asyncio
async def test_ensure_team_address_reasons(monkeypatch, team):
    monkeypatch.setattr(ae.company_slug_repo, "get_by_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value=None))
    assert await ae.ensure_team_address("o", None) == (None, "slug_allocation_failed")
    monkeypatch.setattr(ae.settings, "AGENT_EMAIL_DOMAIN", "")
    assert await ae.ensure_team_address("o", None) == (None, "disabled")


@pytest.mark.asyncio
async def test_from_header_is_the_agent_on_the_team_address(monkeypatch, team):
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value="Acme"))
    monkeypatch.setattr(ae, "resolve_agent_name", AsyncMock(return_value="Friday"))
    header = await ae.from_header("o", "agent_friday", TEAM)
    assert header == formataddr(("Friday · Acme agents", TEAM))
    assert ae.resolve_agent_name.await_args.kwargs == {"agent_id": "agent_friday", "company_id": "co_1"}


@pytest.mark.asyncio
async def test_from_header_falls_back_to_the_bare_address(monkeypatch, team):
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value="Acme"))
    monkeypatch.setattr(ae, "resolve_agent_name", AsyncMock(return_value=None))
    assert await ae.from_header("o", "agent_x", TEAM) == TEAM


PUBLIC = "agents@thelightbulb.company"


@pytest.mark.asyncio
async def test_from_header_sends_as_the_public_address(monkeypatch, team):
    monkeypatch.setattr(
        ae.agent_email_repo, "get_by_address", AsyncMock(return_value={"address": TEAM, "public_address": PUBLIC})
    )
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value="Acme"))
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
