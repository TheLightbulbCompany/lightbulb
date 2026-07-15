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

    async def fake_get(owner_id):
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

    async def stale_get(owner_id):
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
    async def boom(owner_id):
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
