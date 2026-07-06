"""Unit tests for the shared, brand-blind ``resolve_owner_facts``.

The resolver is the fact-gathering half extracted out of OpenClaw's
``_resolve_owner_profile`` (Clerk + billing lookups). These tests mock Clerk /
billing / owner-email and assert the resolved ``OwnerFacts`` carry the right
values + fallbacks for both personal and org owners. Byte-identical parity with
OpenClaw's rendered output is proven by the (unchanged) ``_resolve_owner_profile``
tests in ``test_friday_identity.py`` — this file proves the FACTS themselves.
"""

import pytest

from core.runtime.base import OwnerFacts
from core.services.owner_facts import resolve_owner_facts


@pytest.mark.asyncio
async def test_personal_owner_facts_from_clerk(monkeypatch):
    from core.services import clerk_admin, container_provision

    async def fake_get_user(user_id):
        assert user_id == "user_abc"
        return {
            "first_name": "Sam",
            "last_name": "Rivers",
            "email_addresses": [{"id": "ea_1", "email_address": "sam@example.com"}],
            "primary_email_address_id": "ea_1",
        }

    async def fake_resolve_provider(owner_id):
        return ("bedrock_claude", None)

    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)
    monkeypatch.setattr(container_provision, "resolve_provider_choice", fake_resolve_provider)

    facts = await resolve_owner_facts("user_abc")
    assert isinstance(facts, OwnerFacts)
    assert facts.owner_type == "personal"
    assert facts.provider_choice == "bedrock_claude"
    assert facts.byo_provider is None
    assert facts.display_name == "Sam"
    assert facts.first_name == "Sam"
    assert facts.full_name == "Sam Rivers"
    assert facts.email == "sam@example.com"
    assert facts.org_name is None


@pytest.mark.asyncio
async def test_personal_owner_facts_fall_back_to_email_local_part(monkeypatch):
    from core.services import clerk_admin, container_provision

    async def fake_get_user(user_id):
        return {
            "first_name": None,
            "last_name": None,
            "email_addresses": [{"id": "ea_1", "email_address": "tao@example.com"}],
            "primary_email_address_id": "ea_1",
        }

    async def fake_resolve_provider(owner_id):
        return ("byo_key", "anthropic")

    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)
    monkeypatch.setattr(container_provision, "resolve_provider_choice", fake_resolve_provider)

    facts = await resolve_owner_facts("user_abc")
    assert facts.first_name == "tao"
    assert facts.display_name == "tao"
    assert facts.full_name == "(unknown)"
    assert facts.email == "tao@example.com"
    assert facts.byo_provider == "anthropic"


@pytest.mark.asyncio
async def test_personal_owner_facts_degrade_when_clerk_returns_none(monkeypatch):
    from core.services import clerk_admin, container_provision

    async def fake_get_user(user_id):
        return None

    async def fake_resolve_provider(owner_id):
        return ("bedrock_claude", None)

    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)
    monkeypatch.setattr(container_provision, "resolve_provider_choice", fake_resolve_provider)

    facts = await resolve_owner_facts("user_abc")
    assert facts.email == "(unknown)"
    assert facts.full_name == "(unknown)"
    assert facts.first_name == "there"  # no name, no usable email
    assert facts.display_name == "there"


@pytest.mark.asyncio
async def test_org_owner_facts_from_clerk_and_owner_email(monkeypatch):
    from core.services import clerk_admin, container_provision, paperclip_owner_email

    async def fake_get_org(org_id):
        assert org_id == "org_xyz"
        return {"name": "Acme Inc", "created_by": "user_founder_123"}

    async def fake_lookup_owner_email(*, org_id, fallback_user_id):
        assert org_id == "org_xyz"
        assert fallback_user_id == "user_founder_123"
        return "founder@acme.com"

    async def fake_resolve_provider(owner_id):
        return ("bedrock_claude", None)

    monkeypatch.setattr(clerk_admin, "get_organization", fake_get_org)
    monkeypatch.setattr(paperclip_owner_email, "lookup_owner_email", fake_lookup_owner_email)
    monkeypatch.setattr(container_provision, "resolve_provider_choice", fake_resolve_provider)

    facts = await resolve_owner_facts("org_xyz")
    assert facts.owner_type == "org"
    assert facts.org_name == "Acme Inc"
    assert facts.display_name == "Acme Inc"
    assert facts.first_name == "team"
    assert facts.full_name == "Acme Inc"
    assert facts.email == "founder@acme.com"


@pytest.mark.asyncio
async def test_org_owner_facts_fall_back_when_no_org(monkeypatch):
    from core.services import clerk_admin, container_provision, paperclip_owner_email

    async def fake_get_org(org_id):
        return None

    async def fake_lookup_owner_email(*, org_id, fallback_user_id):
        # fallback_user_id should be None when the org has no created_by.
        assert fallback_user_id is None
        return None

    async def fake_resolve_provider(owner_id):
        return ("bedrock_claude", None)

    monkeypatch.setattr(clerk_admin, "get_organization", fake_get_org)
    monkeypatch.setattr(paperclip_owner_email, "lookup_owner_email", fake_lookup_owner_email)
    monkeypatch.setattr(container_provision, "resolve_provider_choice", fake_resolve_provider)

    facts = await resolve_owner_facts("org_xyz")
    assert facts.org_name == "your team"
    assert facts.display_name == "your team"
    assert facts.email == "(unknown)"


def test_owner_facts_is_frozen_pure_dataclass():
    import dataclasses

    facts = OwnerFacts(
        owner_type="personal",
        provider_choice="bedrock_claude",
        byo_provider=None,
        display_name="Sam",
        first_name="Sam",
        full_name="Sam Rivers",
        email="sam@example.com",
    )
    assert dataclasses.is_dataclass(facts) and facts.__dataclass_params__.frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        facts.email = "x"  # type: ignore[misc]


def test_owner_facts_import_stays_light():
    """base.OwnerFacts is a pure DTO — importing it (and the resolver module's
    types) must not drag core.containers (AWS clients) onto the import path."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys\n"
        "from core.services.owner_facts import resolve_owner_facts  # noqa: F401\n"
        "from core.runtime.base import OwnerFacts\n"
        "OwnerFacts(owner_type='personal', provider_choice='bedrock_claude',\n"
        "           byo_provider=None, display_name='a', first_name='a',\n"
        "           full_name='a', email='a')\n"
        "leaked = [m for m in sys.modules if m.startswith('core.containers')]\n"
        "assert not leaked, f'core.containers leaked into owner_facts import: {leaked}'\n"
    )
    backend_dir = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
