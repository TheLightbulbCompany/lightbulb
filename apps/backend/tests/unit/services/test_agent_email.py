"""agent_email: slug-derived address building + best-effort idempotent mint."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_email  # noqa: E402


def test_address_is_agent_at_company_subdomain():
    """Each agent is a mailbox on its own company subdomain —
    {agent}@{slug}.{domain}."""
    addr = agent_email.build_agent_email_address(slug="acme-robotics", agent_name="Friday", domain="isol8.co")
    assert addr == "friday@acme-robotics.isol8.co"


def test_different_agent_name_yields_different_local_part():
    addr = agent_email.build_agent_email_address(slug="acme-robotics", agent_name="Atlas", domain="isol8.co")
    assert addr == "atlas@acme-robotics.isol8.co"


def test_agent_name_is_slugified_into_the_local_part():
    addr = agent_email.build_agent_email_address(slug="acme-robotics", agent_name="Chief Ops", domain="isol8.co")
    assert addr == "chief-ops@acme-robotics.isol8.co"


def test_address_is_lowercased_and_trimmed():
    addr = agent_email.build_agent_email_address(slug=" Acme ", agent_name="  Chief  Ops!! ", domain="ISOL8.co")
    assert addr == "chief-ops@acme.isol8.co"


def test_address_is_deterministic():
    kwargs = dict(slug="acme-robotics", agent_name="Friday", domain="isol8.co")
    assert agent_email.build_agent_email_address(**kwargs) == (agent_email.build_agent_email_address(**kwargs))


def test_two_agents_of_the_same_company_get_different_addresses():
    """Each agent is its own mailbox on the company subdomain, so two agents
    of the same owner trivially never collide — no truncation/disambiguation
    machinery is needed (the prior per-agent address-collision bug is gone
    with the machinery that caused it)."""
    friday = agent_email.build_agent_email_address(slug="acme-robotics", agent_name="Friday", domain="isol8.co")
    atlas = agent_email.build_agent_email_address(slug="acme-robotics", agent_name="Atlas", domain="isol8.co")
    assert friday != atlas
    assert friday == "friday@acme-robotics.isol8.co"
    assert atlas == "atlas@acme-robotics.isol8.co"


@pytest.mark.asyncio
async def test_mint_skips_when_domain_unset(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "")
    calls = []

    async def fake_put(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_mint_skips_when_slug_missing(monkeypatch):
    """No slug means no address — an owner whose slug allocation failed."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    calls = []

    async def fake_put(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="",
    )
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_mint_creates_row_with_owner_as_verified_sender(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    puts = []

    async def fake_get(address):
        return None

    async def fake_put(**kwargs):
        puts.append(kwargs)
        return kwargs

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    address = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )

    assert address == "friday@acme.isol8.co"
    assert len(puts) == 1
    assert puts[0]["owner_id"] == "user_1"
    assert puts[0]["agent_id"] == "agent_friday"
    assert puts[0]["verified_senders"] == ["o@example.com"]


@pytest.mark.asyncio
async def test_mint_is_idempotent_when_row_exists_for_the_same_owner(monkeypatch):
    """Re-minting your own address is a no-op re-affirmation, not a write."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    puts = []

    async def fake_get(address):
        return {"address": address, "owner_id": "user_1", "agent_id": "agent_friday"}

    async def fake_put(**kwargs):
        puts.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    address = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )
    assert address == "friday@acme.isol8.co"
    assert puts == []


@pytest.mark.asyncio
async def test_mint_refuses_when_same_owner_holds_the_address_for_another_agent(monkeypatch, caplog):
    """The address is derived from the agent NAME, so two same-owner agents
    named alike map to the same mailbox. Returning it would hand agent B
    agent A's mailbox — including A's inbound mail. Refuse, loudly."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    puts = []

    async def fake_get(address):
        return {"address": address, "owner_id": "user_1", "agent_id": "agent_a"}

    async def fake_put(**kwargs):
        puts.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    with caplog.at_level("ERROR"):
        result = await agent_email.mint_agent_email_address(
            owner_id="user_1",
            agent_id="agent_b",
            agent_name="Scout",
            owner_email="o@example.com",
            slug="acme",
        )

    assert result is None
    assert puts == []
    assert any("scout@acme.isol8.co" in r.message for r in caplog.records)
    assert any("agent_a" in r.message and "agent_b" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_mint_refuses_and_logs_when_address_held_by_a_different_owner(monkeypatch, caplog):
    """A collision must be LOUD, not a silent success — the caller must not
    believe minting worked when it wrote nothing."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    puts = []

    async def fake_get(address):
        return {"address": address, "owner_id": "user_other"}

    async def fake_put(**kwargs):
        puts.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    with caplog.at_level("ERROR"):
        result = await agent_email.mint_agent_email_address(
            owner_id="user_1",
            agent_id="agent_friday",
            agent_name="Friday",
            owner_email="o@example.com",
            slug="acme",
        )

    assert result is None
    assert puts == []
    assert any("collision" in r.message for r in caplog.records)
    assert any("user_1" in r.message and "user_other" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_mint_returns_none_when_conditional_put_loses_a_race(monkeypatch):
    """`agent_email_repo.put` returning None (lost the conditional put race)
    must be treated as a failed mint, not surfaced as a minted address."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")

    async def fake_get(address):
        return None

    async def fake_put(**kwargs):
        return None

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None


@pytest.mark.asyncio
async def test_mint_never_raises_on_repo_failure(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")

    async def boom(*args, **kwargs):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", boom)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None


@pytest.mark.asyncio
async def test_mint_never_raises_on_bad_agent_name(monkeypatch):
    """`agent_name` is always dereferenced via `_agent_slug` now (there is no
    more primary/secondary distinction) — a bad `agent_name` (e.g. `None`)
    must not blow up minting, just fail it."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_atlas",
        agent_name=None,
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None


@pytest.mark.asyncio
async def test_mint_never_raises_on_non_string_domain(monkeypatch):
    """`AGENT_EMAIL_DOMAIN` misconfigured as a non-string (e.g. an int from a
    bad env parse) must not blow up the pre-try domain read."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", 12345)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None


from core.services.agent_email import sender_allowed  # noqa: E402


def test_sender_allowed_exact_and_domain():
    row = {"verified_senders": ["owner@example.com", "@clientco.com"]}
    assert sender_allowed(row, "owner@example.com")
    assert sender_allowed(row, "Owner@Example.com")
    assert sender_allowed(row, "anyone@clientco.com")
    assert not sender_allowed(row, "anyone@notclientco.com")
    assert not sender_allowed(row, "anyone@sub.clientco.com")
    assert not sender_allowed({}, "owner@example.com")


import pytest  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402
from core.services import agent_email as ae  # noqa: E402


def _onboarding_repo(company_id="co_1", friday="agent_friday"):
    """A stand-in for ``PaperclipRepo`` whose ``get_onboarding_state`` returns
    a fixed onboarding row — the same shape every ``ensure_agent_address``
    test needs."""

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
async def test_ensure_agent_address_fast_path_uses_existing_row(monkeypatch):
    monkeypatch.setattr(
        ae.agent_email_repo,
        "query_by_owner",
        AsyncMock(
            return_value=[
                {"agent_id": "agent_x", "status": "active", "address": "atlas@acme.isol8.co"},
            ]
        ),
    )
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(side_effect=AssertionError("must not be called")))
    assert await ae.ensure_agent_address(owner_id="o", agent_id="agent_x", owner_email="o@x.y") == (
        "atlas@acme.isol8.co",
        "ok",
    )


@pytest.mark.asyncio
async def test_ensure_agent_address_mints_for_non_friday_agent(monkeypatch):
    monkeypatch.setattr(ae.agent_email_repo, "query_by_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(ae, "PaperclipRepo", _onboarding_repo())
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value="Acme Robotics"))
    monkeypatch.setattr(ae, "allocate_company_slug", AsyncMock(return_value="acme-robotics"))
    monkeypatch.setattr(ae, "resolve_agent_name", AsyncMock(return_value="Atlas"))
    mint = AsyncMock(return_value="atlas@acme-robotics.isol8.co")
    monkeypatch.setattr(ae, "mint_agent_email_address", mint)
    out = await ae.ensure_agent_address(owner_id="o", agent_id="agent_atlas", owner_email="o@x.y")
    assert out == ("atlas@acme-robotics.isol8.co", "ok")
    assert mint.call_args.kwargs["agent_name"] == "Atlas"
    assert mint.call_args.kwargs["agent_id"] == "agent_atlas"
    assert ae.resolve_agent_name.await_args.kwargs == {
        "agent_id": "agent_atlas",
        "company_id": "co_1",
    }


@pytest.mark.asyncio
async def test_ensure_agent_address_reports_reason(monkeypatch):
    monkeypatch.setattr(ae.agent_email_repo, "query_by_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(ae, "PaperclipRepo", _onboarding_repo())
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value=None))
    assert await ae.ensure_agent_address(owner_id="o", agent_id="agent_friday", owner_email="o@x.y") == (
        None,
        "company_name_unavailable",
    )


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
    mint a foreign agent's name onto this owner's subdomain."""

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


@pytest.mark.asyncio
async def test_ensure_agent_address_rejects_agent_from_a_different_company(monkeypatch):
    monkeypatch.setattr(ae.agent_email_repo, "query_by_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(ae, "PaperclipRepo", _onboarding_repo())
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value="Acme Robotics"))
    monkeypatch.setattr(ae, "allocate_company_slug", AsyncMock(return_value="acme-robotics"))
    monkeypatch.setattr(
        ae, "shared_admin_client", lambda: _admin_returning({"name": "Mallory", "companyId": "co_other"})()
    )
    monkeypatch.setattr(ae, "get_admin_session_cookie", AsyncMock(return_value="cookie"))

    result = await ae.ensure_agent_address(owner_id="o", agent_id="agent_mallory", owner_email="o@x.y")
    assert result == (None, "agent_name_unavailable")


@pytest.mark.asyncio
async def test_ensure_agent_address_slug_derives_from_live_company_name(monkeypatch):
    """Not the provisioning placeholder — that is the whole point of minting
    late (preserves the intent of the old
    test_slug_derives_from_the_LIVE_company_name in test_agent_mail_owner_send.py,
    which can no longer express this now that owner_send delegates wholesale
    to ensure_agent_address)."""
    monkeypatch.setattr(ae.agent_email_repo, "query_by_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(ae, "PaperclipRepo", _onboarding_repo())
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value="Renamed By Friday"))
    slug_mock = AsyncMock(return_value="renamed-by-friday")
    monkeypatch.setattr(ae, "allocate_company_slug", slug_mock)
    monkeypatch.setattr(ae, "resolve_agent_name", AsyncMock(return_value="Friday"))
    monkeypatch.setattr(ae, "mint_agent_email_address", AsyncMock(return_value="friday@renamed-by-friday.isol8.co"))
    await ae.ensure_agent_address(owner_id="o", agent_id="agent_friday", owner_email="o@x.y")
    assert slug_mock.call_args.kwargs["name"] == "Renamed By Friday"
