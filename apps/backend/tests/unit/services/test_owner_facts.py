"""Tests for core.services.owner_facts."""

import json

import pytest

from core.services import owner_facts


def test_build_owner_facts_trial_local():
    # A REAL trial_local row carries no provider_choice at all — the provider
    # must short-circuit to the bedrock_trial config mode, not "unknown".
    facts = owner_facts.build_owner_facts("user_abc", {"subscription_status": "trial_local"})
    assert facts["owner_kind"] == "personal"
    assert facts["subscription_status"] == "trial_local"
    assert "free" in facts["plan"].lower()
    assert facts["provider_choice"] == "bedrock_trial"
    assert "free trial" in facts["provider"]
    assert "generated_at" in facts


def test_build_owner_facts_active_row_without_choice_defaults_to_bedrock():
    facts = owner_facts.build_owner_facts("user_abc", {"subscription_status": "active"})
    assert facts["provider_choice"] == "bedrock_claude"
    assert "Bedrock" in facts["provider"]


def test_build_owner_facts_lapsed_subscription_is_not_reported_as_paid():
    facts = owner_facts.build_owner_facts("user_abc", {"subscription_status": "past_due"})
    assert "not active" in facts["plan"]
    assert "past_due" in facts["plan"]


def test_build_owner_facts_legacy_row_without_status_counts_as_subscribed():
    # Mirrors provision_gate.is_subscription_active's legacy fallback.
    facts = owner_facts.build_owner_facts(
        "user_abc", {"stripe_subscription_id": "sub_1", "provider_choice": "bedrock_claude"}
    )
    assert facts["plan"].startswith("Starter")


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
    assert facts["plan"].startswith("Starter")
    assert facts["provider"] == "BYO Anthropic key"


def test_build_owner_facts_no_account():
    facts = owner_facts.build_owner_facts("user_abc", None)
    assert facts["subscription_status"] is None
    assert "no billing" in facts["plan"].lower()


@pytest.mark.asyncio
async def test_write_owner_facts_writes_json(monkeypatch):
    written = {}

    class FakeWs:
        def write_file(self, user_id, path, content):
            written["user_id"] = user_id
            written["path"] = path
            written["content"] = content

    async def fake_get(owner_id, *, consistent_read=False):
        return {"subscription_status": "active", "provider_choice": "bedrock_claude"}

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", fake_get)
    monkeypatch.setattr(owner_facts, "get_workspace", lambda: FakeWs())

    await owner_facts.write_owner_facts("user_abc")

    assert written["user_id"] == "user_abc"
    assert written["path"] == "isol8-facts.json"
    facts = json.loads(written["content"])
    assert facts["provider_choice"] == "bedrock_claude"
    assert facts["plan"] == "Starter ($50/mo flat fee)"


@pytest.mark.asyncio
async def test_write_owner_facts_overrides_beat_stale_row(monkeypatch):
    # The row re-read after an update_item is eventually consistent — the
    # caller's just-committed values must win over whatever the read returns.
    written = {}

    class FakeWs:
        def write_file(self, user_id, path, content):
            written["content"] = content

    async def stale_get(owner_id, *, consistent_read=False):
        return {"subscription_status": "active", "provider_choice": "bedrock_claude"}

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", stale_get)
    monkeypatch.setattr(owner_facts, "get_workspace", lambda: FakeWs())

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
        ("bedrock_claude", None, "Bedrock"),
        ("bedrock_trial", None, "free trial"),
        ("chatgpt_oauth", None, "ChatGPT"),
        ("byo_key", "anthropic", "BYO Anthropic"),
        ("byo_key", "openai", "BYO OpenAI"),
    ],
)
def test_provider_label_for_each_choice(provider_choice, byo_provider, expected_substring):
    """format_provider_label maps each provider tuple to a human label."""
    label = owner_facts.format_provider_label(provider_choice, byo_provider)
    assert expected_substring in label


def test_build_owner_facts_team_seat_subscription_is_not_labeled_flat_fee():
    # set_team_subscription stamps subscription_item_id + seat_count — a
    # per-seat Teams plan must not be reported as the $50 flat fee.
    facts = owner_facts.build_owner_facts(
        "org_team",
        {
            "subscription_status": "active",
            "subscription_item_id": "si_1",
            "seat_count": 4,
            "provider_choice": "bedrock_claude",
        },
    )
    assert "per-seat" in facts["plan"]
    assert "4 seats" in facts["plan"]
    assert "flat fee" not in facts["plan"]


def _facts_env(monkeypatch, owner_facts, rows, written):
    """Shared doubles: billing row, workspace capture, address rows."""

    class FakeWs:
        def write_file(self, user_id, path, content):
            written["content"] = content

    async def fake_billing(owner_id, *, consistent_read=False):
        return {"subscription_status": "active", "provider_choice": "bedrock_claude"}

    async def fake_query_by_owner(owner_id):
        if callable(rows):
            return rows(owner_id)
        return rows

    monkeypatch.setattr(owner_facts.billing_repo, "get_by_owner_id", fake_billing)
    monkeypatch.setattr(owner_facts, "get_workspace", lambda: FakeWs())
    monkeypatch.setattr(owner_facts.agent_email_repo, "query_by_owner", fake_query_by_owner)


@pytest.mark.asyncio
async def test_write_owner_facts_includes_email_address(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}
    _facts_env(
        monkeypatch,
        owner_facts,
        [{"address": "acme@agents.isol8.co", "status": "active", "created_at": "2026-01-01T00:00:00Z"}],
        written,
    )

    await owner_facts.write_owner_facts("user_abc")

    assert json.loads(written["content"])["email_address"] == "acme@agents.isol8.co"


@pytest.mark.asyncio
async def test_write_owner_facts_ignores_alias_rows(monkeypatch):
    """An alias still routes, but it is not the address we advertise."""
    import json

    from core.services import owner_facts

    written = {}
    _facts_env(
        monkeypatch,
        owner_facts,
        [
            {"address": "old@agents.isol8.co", "status": "alias", "created_at": "2026-01-01T00:00:00Z"},
            {"address": "acme@agents.isol8.co", "status": "active", "created_at": "2026-02-01T00:00:00Z"},
        ],
        written,
    )

    await owner_facts.write_owner_facts("user_abc")

    assert json.loads(written["content"])["email_address"] == "acme@agents.isol8.co"


@pytest.mark.asyncio
async def test_write_owner_facts_picks_oldest_active_deterministically(monkeypatch):
    """Two active rows can exist after a rare re-provision; the choice must
    not depend on DynamoDB's arbitrary return order."""
    import json

    from core.services import owner_facts

    written = {}
    _facts_env(
        monkeypatch,
        owner_facts,
        [
            {"address": "second@agents.isol8.co", "status": "active", "created_at": "2026-03-01T00:00:00Z"},
            {"address": "first@agents.isol8.co", "status": "active", "created_at": "2026-01-01T00:00:00Z"},
        ],
        written,
    )

    await owner_facts.write_owner_facts("user_abc")

    assert json.loads(written["content"])["email_address"] == "first@agents.isol8.co"


@pytest.mark.asyncio
async def test_write_owner_facts_email_address_none_when_unminted(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}
    _facts_env(monkeypatch, owner_facts, [], written)

    await owner_facts.write_owner_facts("user_abc")

    assert json.loads(written["content"])["email_address"] is None


@pytest.mark.asyncio
async def test_write_owner_facts_survives_email_lookup_failure(monkeypatch):
    import json

    from core.services import owner_facts

    written = {}

    def boom_rows(owner_id):
        raise RuntimeError("dynamo down")

    _facts_env(monkeypatch, owner_facts, boom_rows, written)

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
