from unittest.mock import AsyncMock

import pytest
from core.config import settings
from core.services.container_provision import provider_choice_from_row
from core.services.hermes_profiles import build_profile_config
from core.services import trial_cap


def test_muse_resolution_and_profile(monkeypatch):
    monkeypatch.setattr(settings, "MUSE_ENABLED", True)
    assert provider_choice_from_row({"subscription_status": "trial_local"}) == ("opencode_zen", None)
    assert provider_choice_from_row(
        {"subscription_status": "active", "plan": "entry", "provider_choice": "byo_key"}
    ) == ("opencode_zen", None)
    config = build_profile_config(agent_role="ceo", provider_choice="opencode_zen", is_ceo=True)
    assert config["model"]["provider"] == "opencode-zen"
    assert config["model"]["default"] == "muse-spark-1.3"
    assert config["model"]["base_url"] == "${LIGHTBULB_MUSE_BASE_URL}"
    for task in config["auxiliary"].values():
        if isinstance(task, dict):
            assert task["provider"] == "opencode-zen"
            assert task["model"] == "muse-spark-1.3"
            assert task["fallback_chain"] == []
            assert task["api_mode"] == "codex_responses"


def test_entry_never_falls_back_when_muse_disabled(monkeypatch):
    monkeypatch.setattr(settings, "MUSE_ENABLED", False)
    assert provider_choice_from_row({"plan": "entry"}) == ("opencode_zen", None)
    assert provider_choice_from_row({"subscription_status": "trial_local"}) == ("bedrock_trial", None)


@pytest.mark.asyncio
async def test_muse_trial_is_not_bedrock_capped(monkeypatch):
    monkeypatch.setattr(settings, "MUSE_ENABLED", True)
    spent = AsyncMock(side_effect=AssertionError("Muse must not read Bedrock allowance"))
    monkeypatch.setattr(trial_cap.billing_repo, "get_trial_daily_spend", spent)
    assert (
        await trial_cap.reason_for_account("org_one", {"subscription_status": "trial_local", "trial_end": 9999999999})
        is None
    )


@pytest.mark.asyncio
async def test_entry_rejects_key_storage_and_oauth_activation(monkeypatch):
    from core.services import provision_gate, oauth_service, key_service
    from fastapi import HTTPException

    monkeypatch.setattr(
        provision_gate.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"plan": "entry", "subscription_status": "active"}),
    )
    monkeypatch.setattr(
        key_service, "_validate_llm_key", AsyncMock(side_effect=AssertionError("vendor must not be called"))
    )
    with pytest.raises(HTTPException, match="403"):
        await key_service.KeyService().set_key("org_one", "openai", "never-submit")
    with pytest.raises(HTTPException, match="403"):
        await oauth_service.request_device_code(user_id="user_one", bound_owner_id="org_one")
    from types import SimpleNamespace

    monkeypatch.setattr(
        oauth_service,
        "_table",
        lambda: SimpleNamespace(get_item=lambda **_: {"Item": {"state": "pending", "bound_owner_id": "org_one"}}),
    )
    with pytest.raises(HTTPException, match="403"):
        await oauth_service.poll_device_code(user_id="user_one")


@pytest.mark.asyncio
async def test_provider_refresh_rewrites_existing_runtime_then_stops_warm_session(monkeypatch):
    from core.services import hermes_provisioning as hp

    calls = []
    monkeypatch.setattr(settings, "AGENTCORE_HERMES_IMAGE", "image:new")
    monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr(
        hp.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"status": "running", "agentcore_runtime_arn": "arn:runtime", "needs_provision": True}),
    )
    monkeypatch.setattr(hp, "_load_role_and_aips", AsyncMock(return_value=("role", {})))
    monkeypatch.setattr(hp, "_wait_for_idle_session", AsyncMock(return_value=True))
    monkeypatch.setattr(hp, "refresh_provider_profiles", AsyncMock(side_effect=lambda *a: calls.append("profiles")))
    monkeypatch.setattr(hp.browser_sessions, "ensure_browser_profile", AsyncMock(return_value=""))
    monkeypatch.setattr(hp, "_runtime_image_uri", lambda _: "image:new")
    monkeypatch.setattr(hp, "build_runtime_request_for_owner", lambda *a, **k: {})
    monkeypatch.setattr(hp, "_update_runtime_image", lambda *a: calls.append("runtime-env"))
    monkeypatch.setattr(hp, "_wait_for_runtime_ready", lambda *a: None)
    monkeypatch.setattr(hp, "_stop_runtime_session", lambda *a: calls.append("stop"))
    update = AsyncMock()
    monkeypatch.setattr(hp.container_repo, "update_container_leased", update)
    monkeypatch.setattr(hp.billing_repo, "arm_trial_if_unset", AsyncMock())
    await hp.provision_hermes_runtime("org_one", claim_id="claim")
    assert calls == ["profiles", "runtime-env", "stop"]
    assert update.call_args.args[2]["needs_provision"] is False


@pytest.mark.asyncio
async def test_provider_projection_reads_committed_billing_choice(monkeypatch):
    from core.services import container_provision

    async def billing_read(owner_id, *, consistent_read=False):
        # An eventual replica still has the pre-switch provider.
        return {"provider_choice": "opencode_zen" if consistent_read else "bedrock_claude"}

    monkeypatch.setattr(container_provision.billing_repo, "get_by_owner_id", billing_read)
    assert await container_provision.resolve_provider_choice("org_one") == ("opencode_zen", None)


@pytest.mark.asyncio
async def test_provider_refresh_preserves_owner_content_and_pins_delegation(monkeypatch):
    import yaml
    from types import SimpleNamespace
    from core.services import hermes_provisioning as hp, hermes_profiles, messaging

    monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr(
        hp,
        "resolve_profile_inputs",
        AsyncMock(
            return_value=SimpleNamespace(
                provider_choice="opencode_zen",
                byo_provider=None,
                byo_api_key=None,
                aip_arns={},
            )
        ),
    )
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": "agent", "role": "engineer"}]))
    monkeypatch.setattr(hp, "_stage_owner_facts", AsyncMock())
    monkeypatch.setattr(
        hermes_profiles,
        "read_profile_config",
        lambda *a, **k: {
            "model": {"provider": "openai"},
            "mcp_servers": {"custom": {"command": "mine"}},
            "terminal": {"timeout": 42},
        },
    )
    monkeypatch.setattr(hermes_profiles, "_require_text", lambda *a: "CUSTOM=keep\nOPENAI_API_KEY=old\n")
    writes = {}
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, value: writes.__setitem__(key, value))
    await hp.refresh_provider_profiles("org_one", {})
    config = yaml.safe_load(writes["org_one/hermes/agent/config.yaml"])
    assert config["model"]["provider"] == "opencode-zen"
    assert config["delegation"]["api_mode"] == "codex_responses"
    assert config["fallback_providers"] == []
    assert config["mcp_servers"] == {"custom": {"command": "mine"}}
    assert config["terminal"] == {"timeout": 42}
    assert writes["org_one/hermes/agent/.env"] == "CUSTOM=keep\n"
    assert not any(key.endswith("SOUL.md") for key in writes)
