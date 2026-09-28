"""M6 task 10-C (scan rows 2/9/23/27/31): ``models.list`` on an AgentCore row, and the
overhead-model write it validates against.

The model picker used to have nothing to pick from (``config.models.providers`` does not exist
in a Hermes config) and 400'd on anything it did send (the closed ``standard``/``premium``
vocabulary). M6-R50: the list is the models the owner can ACTUALLY invoke -- the one server
model on a managed workspace (served here, no microVM wake), Hermes's own provider catalog via
the bridge otherwise.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock

import pytest

from core.services import runtime_rpc as rpc
from core.services.messaging import ApiProblem


def _wire_owner(monkeypatch, *, provider_choice="openrouter_managed"):
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice",
        AsyncMock(return_value=(provider_choice, None)),
    )


def _no_bridge(monkeypatch):
    call = AsyncMock(side_effect=AssertionError("models.list must not wake a microVM for a managed owner"))
    monkeypatch.setattr(rpc.container_rpc, "call", call)
    return call


async def _models_list(owner_id="org_a"):
    return await rpc.execute_sync(owner_id=owner_id, user_id="u1", request_id="r1", method="models.list", params={})


@pytest.mark.asyncio
async def test_managed_openrouter_catalog_is_server_owned_and_requires_an_selected_model(monkeypatch):
    _wire_owner(monkeypatch, provider_choice="openrouter_managed")
    _no_bridge(monkeypatch)
    monkeypatch.setattr(rpc.hermes_profiles.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model")
    assert await _models_list() == {
        "models": [{"id": "candidate/model", "name": "candidate/model", "provider": "custom"}],
        "defaultId": "candidate/model",
    }
    monkeypatch.setattr(rpc.hermes_profiles.settings, "OPENROUTER_MANAGED_MODEL_ID", "")
    assert await _models_list() == {"models": [], "defaultId": ""}


@pytest.mark.asyncio
async def test_byo_owner_falls_through_to_the_bridges_hermes_catalog(monkeypatch):
    """Only the runtime holds Hermes's own provider catalog, so a BYO/OAuth workspace's list
    comes from the bridge -- passed through unchanged."""
    _wire_owner(monkeypatch, provider_choice="byo_key")
    payload = {"models": [{"id": "claude-sonnet-5", "name": "claude-sonnet-5", "provider": "anthropic"}]}
    call = AsyncMock(return_value=payload)
    monkeypatch.setattr(rpc.container_rpc, "call", call)

    assert await _models_list() == payload
    assert call.await_args.args[1] == "models.list"


# ---------------------------------------------------------------------------
# The overhead-model write (rows 4/23) -- one id in, the whole auxiliary block out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_managed_overhead_patch_keeps_every_task_on_the_gate(monkeypatch):
    from core.services.hermes_profiles import AUXILIARY_TASKS

    _wire_owner(monkeypatch, provider_choice="openrouter_managed")
    _no_bridge(monkeypatch)
    monkeypatch.setattr(rpc.hermes_profiles.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model")
    monkeypatch.setattr(rpc.hermes_profiles.settings, "OPENROUTER_GATE_URL", "https://gate.example/v1")
    out = await rpc._expand_agentcore_config_patch("org_a", {"auxiliary": {"model": "candidate/model"}})
    assert set(out["auxiliary"]) == set(AUXILIARY_TASKS) - {"vision"}  # never pinned, any provider
    assert all(
        route
        == {
            "provider": "custom",
            "model": "candidate/model",
            "base_url": "https://gate.example/v1",
            "api_key": "${LIGHTBULB_GATE_BEARER}",
            "fallback_chain": [],
        }
        for route in out["auxiliary"].values()
    )


@pytest.mark.asyncio
async def test_overhead_model_patch_refuses_a_model_the_workspace_cannot_use(monkeypatch):
    _wire_owner(monkeypatch)
    monkeypatch.setattr(rpc.hermes_profiles.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model")
    with pytest.raises(ApiProblem) as exc_info:
        await rpc._expand_agentcore_config_patch("org_a", {"auxiliary": {"model": "arn:aip:someone-else"}})
    assert exc_info.value.error.details["upstreamCode"] == "MODEL_NOT_OFFERED"


@pytest.mark.asyncio
async def test_a_patch_without_the_pseudo_key_is_passed_through_untouched(monkeypatch):
    """Every other config.patch must not pay for the two owner reads the expansion needs."""
    monkeypatch.setattr(
        rpc.container_repo, "get_by_owner_id", AsyncMock(side_effect=AssertionError("no owner read expected"))
    )
    patch = {"memory": {"memory_enabled": False}, "auxiliary": {"free_only": False}}
    assert await rpc._expand_agentcore_config_patch("org_a", patch) == patch


@pytest.mark.asyncio
async def test_config_patch_refuses_a_raw_model_block(monkeypatch):
    """Fix round 1, I4: the sibling door. `config.patch` deep-merges whatever it is handed, so a
    raw model block -- fanned out to EVERY profile when no agentId is sent -- could write an ARN
    outside the owner's grant and make the gate refuse every turn until someone reprovisions.
    `agents.update` is the only door that may set a model, and it validates."""
    with pytest.raises(ApiProblem) as exc_info:
        await rpc._expand_agentcore_config_patch("org_a", {"model": {"default": "arn:aws:bedrock:us-east-1::not-mine"}})
    assert exc_info.value.error.details["upstreamCode"] == "UNSUPPORTED_ON_AGENTCORE"
    assert exc_info.value.error.details["fields"] == ["model"]
