"""Tests for core.services.owner_facts."""

import json

import pytest

from core.services import owner_facts


def test_build_owner_facts_trial_local():
    facts = owner_facts.build_owner_facts(
        "user_abc",
        {"subscription_status": "trial_local", "provider_choice": "bedrock_claude"},
    )
    assert facts["owner_kind"] == "personal"
    assert facts["subscription_status"] == "trial_local"
    assert "free" in facts["plan"].lower()
    assert "generated_at" in facts


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
