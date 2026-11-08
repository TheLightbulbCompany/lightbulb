"""Tests for core.services.owner_facts."""

import json

import pytest

from core.services import owner_facts


def test_build_owner_facts_trial_local():
    # A REAL trial_local row carries no provider_choice at all — the provider
    # resolves to the internal managed OpenRouter mode.
    facts = owner_facts.build_owner_facts("user_abc", {"subscription_status": "trial_local"})
    assert facts["owner_kind"] == "personal"
    assert facts["subscription_status"] == "trial_local"
    assert "free" in facts["plan"].lower()
    assert facts["provider_choice"] == "openrouter_managed"
    assert "managed" in facts["provider"].lower()
    assert "generated_at" in facts


def test_build_owner_facts_active_row_without_choice_defaults_to_managed():
    facts = owner_facts.build_owner_facts("user_abc", {"subscription_status": "active"})
    assert facts["provider_choice"] == "openrouter_managed"
    assert "managed" in facts["provider"]


def test_build_owner_facts_lapsed_subscription_is_not_reported_as_paid():
    facts = owner_facts.build_owner_facts("user_abc", {"subscription_status": "past_due"})
    assert "not active" in facts["plan"]
    assert "past_due" in facts["plan"]


def test_build_owner_facts_legacy_row_without_status_counts_as_subscribed():
    # Mirrors provision_gate.is_subscription_active's legacy fallback.
    facts = owner_facts.build_owner_facts(
        "user_abc", {"stripe_subscription_id": "sub_1", "provider_choice": "openrouter_managed"}
    )
    assert facts["plan"] == "Team ($50/mo for up to 5 people, $20/mo per member from the 6th)"


def test_build_owner_facts_paid_byo_org():
    facts = owner_facts.build_owner_facts(
        "org_123",
        {
            "subscription_status": "active",
            "provider_choice": "byo_key",
            "byo_provider": "anthropic",
        },
    )
    assert facts["owner_kind"] == "org"
    assert facts["plan"] == "Team ($50/mo for up to 5 people, $20/mo per member from the 6th)"
    assert facts["provider"] == "BYO Anthropic key"


def test_build_owner_facts_no_account():
    facts = owner_facts.build_owner_facts("user_abc", None)
    assert facts["subscription_status"] is None
    assert "no billing" in facts["plan"].lower()


@pytest.mark.asyncio
async def test_write_owner_facts_overrides_beat_stale_row(monkeypatch):
    # The row re-read after an update_item is eventually consistent — the
    # caller's just-committed values must win over whatever the read returns.
    # Every owner is AgentCore now, so the write lands via hermes_profiles
    # rather than EFS — the override-wins assertion is what survives.
    from core.services import hermes_profiles

    written = {}

    async def stale_get(owner_id, *, consistent_read=False):
        return {"subscription_status": "active", "provider_choice": "openrouter_managed"}

    async def fake_roster(owner_id):
        return [{"id": "agent-ceo"}]

    def fake_write_facts(owner_id, agent_ids, facts_json):
        written["content"] = facts_json

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", stale_get)
    monkeypatch.setattr("core.services.messaging.load_agent_roster", fake_roster)
    monkeypatch.setattr(hermes_profiles, "write_facts_to_profiles", fake_write_facts)

    await owner_facts.write_owner_facts(
        "user_abc", account_overrides={"provider_choice": "chatgpt_oauth", "byo_provider": None}
    )

    facts = json.loads(written["content"])
    assert facts["provider_choice"] == "chatgpt_oauth"
    assert "ChatGPT" in facts["provider"]


@pytest.mark.asyncio
async def test_write_owner_facts_never_raises(monkeypatch):
    async def boom(owner_id, *, consistent_read=False):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", boom)
    await owner_facts.write_owner_facts("user_abc")  # must not raise


@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected_substring",
    [
        ("openrouter_managed", None, "Lightbulb-managed credits"),
        ("chatgpt_oauth", None, "ChatGPT"),
        ("byo_key", "anthropic", "BYO Anthropic"),
        ("byo_key", "openai", "BYO OpenAI"),
    ],
)
def test_provider_label_for_each_choice(provider_choice, byo_provider, expected_substring):
    """format_provider_label maps each provider tuple to a human label."""
    label = owner_facts.format_provider_label(provider_choice, byo_provider)
    assert expected_substring in label


def test_build_owner_facts_uses_ultra_plan_label():
    """The agent-visible label follows the workspace plan."""
    facts = owner_facts.build_owner_facts(
        "org_team",
        {
            "subscription_status": "active",
            "plan": "ultra_pro",
            "provider_choice": "openrouter_managed",
        },
    )
    assert facts["plan"] == "Ultra ($150/mo)"


def _facts_env(monkeypatch, owner_facts, address, written):
    """Shared doubles: billing row, hermes-profile write capture, the team address.

    The write lands via ``hermes_profiles.write_facts_to_profiles``; there is no EFS branch
    and nothing to classify. ``address`` may be a callable that raises.
    """
    from core.services import hermes_profiles

    async def fake_billing(owner_id, *, consistent_read=False):
        return {"subscription_status": "active", "provider_choice": "openrouter_managed"}

    async def fake_team_address(owner_id):
        return address(owner_id) if callable(address) else address

    async def fake_roster(owner_id):
        return [{"id": "agent-ceo"}]

    def fake_write_facts(owner_id, agent_ids, facts_json):
        written["content"] = facts_json

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", fake_billing)
    monkeypatch.setattr(owner_facts, "public_address", fake_team_address)
    monkeypatch.setattr("core.services.messaging.load_agent_roster", fake_roster)
    monkeypatch.setattr(hermes_profiles, "write_facts_to_profiles", fake_write_facts)


@pytest.mark.asyncio
async def test_write_owner_facts_email_address_is_the_team_address(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}
    _facts_env(monkeypatch, owner_facts, "agents@acme.isol8.co", written)

    await owner_facts.write_owner_facts("user_abc")

    assert json.loads(written["content"])["email_address"] == "agents@acme.isol8.co"


@pytest.mark.asyncio
async def test_write_owner_facts_email_address_none_without_a_slug(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}
    _facts_env(monkeypatch, owner_facts, None, written)

    await owner_facts.write_owner_facts("user_abc")

    assert json.loads(written["content"])["email_address"] is None


@pytest.mark.asyncio
async def test_write_owner_facts_survives_email_lookup_failure(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}

    def boom(owner_id):
        raise RuntimeError("dynamo down")

    _facts_env(monkeypatch, owner_facts, boom, written)

    await owner_facts.write_owner_facts("user_abc")

    facts = json.loads(written["content"])
    assert facts["email_address"] is None
    assert facts["plan"]  # the rest of the facts still wrote


# --- Facts tri-state -------------------------------------------------------
#
# `email_address` alone cannot distinguish "email is off for this owner" from
# "you can send; your address is created by your first send". SKILL.md's
# null-means-unprovisioned rule would otherwise deadlock first contact,
# because the address is minted BY the send.


def test_facts_carry_owner_id_and_email_enabled_when_domain_set(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    facts = owner_facts.build_owner_facts("user_abc", {}, email_address=None)

    assert facts["owner_id"] == "user_abc"
    assert facts["email_enabled"] is True
    assert facts["email_address"] is None


def test_email_disabled_when_domain_unset(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "")
    facts = owner_facts.build_owner_facts("user_abc", {}, email_address=None)

    assert facts["email_enabled"] is False
    assert facts["email_address"] is None


def test_email_enabled_and_minted(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    facts = owner_facts.build_owner_facts("user_abc", {}, email_address="friday@acme.isol8.co")

    assert facts["email_enabled"] is True
    assert facts["email_address"] == "friday@acme.isol8.co"


# ---------------------------------------------------------------------------
# Row #15 / Ruling M6-R30 — an AgentCore owner has no EFS: write_owner_facts
# stages the file into every roster profile in S3 instead.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_owner_facts_agentcore_owner_stages_every_profile(monkeypatch):
    from core.services import hermes_profiles

    async def fake_get(owner_id, *, consistent_read=False):
        return {"subscription_status": "active", "provider_choice": "openrouter_managed"}

    async def fake_roster(owner_id):
        return [{"id": "agent-ceo"}, {"id": "agent-eng"}]

    calls = []

    def fake_write_facts(owner_id, agent_ids, facts_json):
        calls.append((owner_id, agent_ids, facts_json))

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", fake_get)
    monkeypatch.setattr("core.services.messaging.load_agent_roster", fake_roster)
    monkeypatch.setattr(hermes_profiles, "write_facts_to_profiles", fake_write_facts)

    await owner_facts.write_owner_facts("org_agentcore")

    assert len(calls) == 1
    owner_id, agent_ids, facts_json = calls[0]
    assert owner_id == "org_agentcore"
    assert agent_ids == ["agent-ceo", "agent-eng"]
    facts = json.loads(facts_json)
    assert facts["provider_choice"] == "openrouter_managed"
    # The EFS write this used to also prove was never reached is gone with the mount; the
    # module holds no workspace handle at all.
    assert not hasattr(owner_facts, "get_workspace")


@pytest.mark.asyncio
async def test_write_owner_facts_agentcore_write_failure_is_swallowed(monkeypatch):
    async def fake_get(owner_id, *, consistent_read=False):
        return {"subscription_status": "active"}

    async def fake_roster(owner_id):
        return [{"id": "agent-ceo"}]

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", fake_get)
    monkeypatch.setattr("core.services.messaging.load_agent_roster", fake_roster)

    def boom(*a, **k):
        raise RuntimeError("s3 down")

    from core.services import hermes_profiles

    monkeypatch.setattr(hermes_profiles, "write_facts_to_profiles", boom)

    await owner_facts.write_owner_facts("org_agentcore")  # must not raise


def test_facts_carry_this_environments_app_url(monkeypatch):
    """The bundled skill's links say thelightbulb.company (one image serves dev and prod); agents swap
    in this host so a dev workspace is never sent to the production app."""
    monkeypatch.setattr(owner_facts.settings, "FRONTEND_URL", "https://dev.thelightbulb.company/", raising=False)
    facts = owner_facts.build_owner_facts("org_abc", {"subscription_status": "trial_local"})
    assert facts["app_url"] == "https://dev.thelightbulb.company"


def test_base_and_trial_are_labelled_as_what_they_are():
    base = owner_facts.build_owner_facts("org_a", {"subscription_status": "active", "plan": "base"})
    assert base["plan"] == "Base ($20/mo)"
    trial = owner_facts.build_owner_facts("org_a", {"subscription_status": "trial_local"})
    assert "DeepSeek" in trial["plan"] and "Bedrock" not in trial["plan"]


@pytest.mark.asyncio
async def test_owner_timezone_is_the_org_creators(monkeypatch):
    from unittest.mock import AsyncMock

    from core.repositories import user_repo
    from core.services import clerk_admin

    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={"created_by": "user_owner"}))
    get_tz = AsyncMock(return_value="Asia/Kolkata")
    monkeypatch.setattr(user_repo, "get_timezone", get_tz, raising=False)
    written = {}
    _facts_env(monkeypatch, owner_facts, None, written)

    await owner_facts.write_owner_facts("org_1")

    get_tz.assert_awaited_once_with("user_owner")
    assert json.loads(written["content"])["owner_timezone"] == "Asia/Kolkata"


@pytest.mark.asyncio
@pytest.mark.parametrize("org", [None, {}, RuntimeError("clerk down")])
async def test_owner_timezone_falls_back_to_utc(monkeypatch, org):
    from unittest.mock import AsyncMock

    from core.services import clerk_admin

    lookup = AsyncMock(side_effect=org) if isinstance(org, Exception) else AsyncMock(return_value=org)
    monkeypatch.setattr(clerk_admin, "get_organization", lookup)
    assert await owner_facts._lookup_owner_timezone("org_1") == "UTC"
    assert await owner_facts._lookup_owner_timezone("user_1") == "UTC"  # never a personal owner


@pytest.mark.asyncio
async def test_owner_timezone_getter_failure_is_utc(monkeypatch):
    from unittest.mock import AsyncMock

    from core.repositories import user_repo
    from core.services import clerk_admin

    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={"created_by": "user_owner"}))
    monkeypatch.setattr(user_repo, "get_timezone", AsyncMock(side_effect=RuntimeError("ddb")), raising=False)
    assert await owner_facts._lookup_owner_timezone("org_1") == "UTC"


# ---- declared manifest: teammates + connected_integrations ----------------------------------
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402


def test_build_owner_facts_manifest_defaults_empty():
    facts = owner_facts.build_owner_facts("org_1", None)
    assert facts["teammates"] == []
    assert facts["connected_integrations"] == []


def test_teammates_use_starter_role_key_and_flag_the_ceo():
    roster = [
        {"id": "a1", "name": "Friday", "role": "general"},
        {"id": "a2", "name": "Fin", "role": "cfo"},
        {"id": "a3", "name": "Custom", "role": "designer"},
    ]
    out = owner_facts._teammates_from_roster(roster, {"ceo": "a1", "finance": "a2"})
    assert out == [
        {"id": "a1", "name": "Friday", "role": "ceo", "is_ceo": True},
        {"id": "a2", "name": "Fin", "role": "finance", "is_ceo": False},
        {"id": "a3", "name": "Custom", "role": "designer", "is_ceo": False},
    ]


def test_teammates_legacy_roster_root_ceo_and_empty():
    legacy = [{"id": "a1", "name": "Boss", "role": "ceo"}, {"id": "a2", "name": "X", "role": "ceo", "reportsTo": "a1"}]
    out = owner_facts._teammates_from_roster(legacy, None)
    assert [t["is_ceo"] for t in out] == [True, False]
    assert owner_facts._teammates_from_roster([], {"ceo": "a1"}) == []


@pytest.mark.asyncio
async def test_connected_integrations_names_and_rails_only_no_secret_material():
    rows = [
        {"owner_id": "o", "provider": "github", "status": "connected", "identity": "me@x.com", "tenant_id": "t"},
        {"owner_id": "o", "provider": "linear", "status": "connected"},
        {
            "owner_id": "o",
            "provider": "projects/stripe",
            "status": "connected",
            "provider_resources": [{"env_keys": ["K"]}],
        },
        {"owner_id": "o", "provider": "google", "status": "reconnect_required"},
        {"owner_id": "o", "provider": "projects/posthog", "status": "connected", "provider_resources": []},
        {"owner_id": "o", "provider": "slack", "status": "connected"},
    ]
    reg = {"github": MagicMock(actor="cli"), "linear": MagicMock(actor="mcp")}
    with (
        patch("core.repositories.integrations_repo.list_for_owner", AsyncMock(return_value=rows)),
        patch("core.services.credentials.registry.all_providers", return_value=reg),
    ):
        out = await owner_facts._lookup_connected_integrations("o")
    assert out == [
        {"provider": "github", "rail": "cli"},
        {"provider": "linear", "rail": "mcp"},
        {"provider": "stripe", "rail": "projects"},
    ]
    blob = json.dumps(out)
    assert "me@x.com" not in blob and "env_keys" not in blob


@pytest.mark.asyncio
async def test_connected_integrations_empty_and_failure_are_empty_lists():
    with patch("core.repositories.integrations_repo.list_for_owner", AsyncMock(return_value=[])):
        assert await owner_facts._lookup_connected_integrations("o") == []
    with patch("core.repositories.integrations_repo.list_for_owner", AsyncMock(side_effect=RuntimeError("boom"))):
        assert await owner_facts._lookup_connected_integrations("o") == []


@pytest.mark.asyncio
async def test_write_owner_facts_writes_the_manifest_to_every_profile():
    written = {}
    roster = [{"id": "a1", "name": "Friday", "role": "general"}]
    company = MagicMock(starter_agent_ids={"ceo": "a1"})
    with (
        patch.object(owner_facts.billing_repo, "get_by_owner_id", AsyncMock(return_value={})),
        patch.object(owner_facts, "_lookup_email_address", AsyncMock(return_value=None)),
        patch.object(owner_facts, "_lookup_provisioning", AsyncMock(return_value=[])),
        patch.object(owner_facts, "_lookup_owner_timezone", AsyncMock(return_value="UTC")),
        patch.object(
            owner_facts,
            "_lookup_connected_integrations",
            AsyncMock(return_value=[{"provider": "github", "rail": "cli"}]),
        ),
        patch("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster)),
        patch("core.repositories.workspace_repo.WorkspaceRepo.resolve_owner_row", AsyncMock(return_value=company)),
        patch(
            "core.services.hermes_profiles.write_facts_to_profiles",
            side_effect=lambda o, ids, body: written.update(ids=ids, body=body),
        ),
    ):
        await owner_facts.write_owner_facts("org_1")
    facts = json.loads(written["body"])
    assert written["ids"] == ["a1"]
    assert facts["teammates"] == [{"id": "a1", "name": "Friday", "role": "ceo", "is_ceo": True}]
    assert facts["connected_integrations"] == [{"provider": "github", "rail": "cli"}]


@pytest.mark.asyncio
async def test_delete_agent_everywhere_refreshes_facts_for_every_caller():
    from core.services import agent_lifecycle as lc, agent_registry

    with (
        patch.object(agent_registry, "roster", AsyncMock(return_value=[])),
        patch.object(agent_registry, "delete", AsyncMock()),
        patch(
            "core.repositories.workspace_repo.WorkspaceRepo",
            MagicMock(return_value=MagicMock(remove_starter_agent_id=AsyncMock())),
        ),
        patch("core.services.network_member.publish_sessions", AsyncMock()),
        patch("core.services.hermes_profiles.delete_profile"),
        patch("core.services.owner_facts.write_owner_facts", AsyncMock()) as facts,
    ):
        await lc.delete_agent_everywhere(owner_id="org_1", agent_id="a1")
    facts.assert_awaited_once_with("org_1")
