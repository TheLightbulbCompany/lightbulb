"""agent_email: slug-derived address building + best-effort idempotent mint."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_email  # noqa: E402


def test_primary_address_is_just_the_slug():
    """The company's main address is its CEO's — {slug}@domain."""
    addr = agent_email.build_agent_email_address(
        slug="acme", agent_name="Friday", domain="agents.isol8.co", is_primary=True
    )
    assert addr == "acme@agents.isol8.co"


def test_secondary_address_appends_the_agent():
    addr = agent_email.build_agent_email_address(
        slug="acme", agent_name="Atlas", domain="agents.isol8.co", is_primary=False
    )
    assert addr == "acme-atlas@agents.isol8.co"


def test_address_is_lowercased_and_agent_name_slugified():
    addr = agent_email.build_agent_email_address(
        slug="Acme",
        agent_name="  Chief  Ops!! ",
        domain="Agents.Isol8.co",
        is_primary=False,
    )
    assert addr == "acme-chief-ops@agents.isol8.co"


def test_address_is_deterministic():
    kwargs = dict(slug="acme", agent_name="Friday", domain="agents.isol8.co", is_primary=True)
    assert agent_email.build_agent_email_address(**kwargs) == (agent_email.build_agent_email_address(**kwargs))


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
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
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
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
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

    assert address == "acme@agents.isol8.co"
    assert len(puts) == 1
    assert puts[0]["owner_id"] == "user_1"
    assert puts[0]["agent_id"] == "agent_friday"
    assert puts[0]["verified_senders"] == ["o@example.com"]


@pytest.mark.asyncio
async def test_mint_is_idempotent_when_row_exists_for_the_same_owner(monkeypatch):
    """Re-minting your own address is a no-op re-affirmation, not a write."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    puts = []

    async def fake_get(address):
        return {"address": address, "owner_id": "user_1"}

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
    assert address == "acme@agents.isol8.co"
    assert puts == []


@pytest.mark.asyncio
async def test_mint_refuses_and_logs_when_address_held_by_a_different_owner(monkeypatch, caplog):
    """A collision must be LOUD, not a silent success — the caller must not
    believe minting worked when it wrote nothing."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
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
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")

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
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")

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
async def test_mint_never_raises_on_bad_agent_name_when_not_primary(monkeypatch):
    """`agent_name` is only dereferenced (via `_agent_slug`) on the non-primary
    path — probe that path directly rather than leaving `is_primary` at its
    default `True`, which never touches `agent_name` at all."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_atlas",
        agent_name=None,
        owner_email="o@example.com",
        slug="acme",
        is_primary=False,
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


def test_secondary_local_part_respects_rfc5321_limit():
    """RFC 5321 caps the local part at 64 octets.

    The company slug is capped at 32, but an agent name is not (agent names
    accept up to 80 chars upstream), so ``{slug}-{agent}`` could otherwise
    emit an address no mail system will accept — and the mint would happily
    persist it.
    """
    addr = agent_email.build_agent_email_address(
        slug="a" * 32,
        agent_name="Chief " * 20,  # slugifies far past the remaining budget
        domain="agents.isol8.co",
        is_primary=False,
    )
    local = addr.split("@", 1)[0]
    assert len(local) <= agent_email.MAX_LOCAL_PART_LEN
    assert not local.endswith("-")
    assert "--" not in local


def test_secondary_local_part_untouched_when_short():
    """Ordinary names must not be truncated by the cap."""
    addr = agent_email.build_agent_email_address(
        slug="acme", agent_name="Atlas", domain="agents.isol8.co", is_primary=False
    )
    assert addr == "acme-atlas@agents.isol8.co"
