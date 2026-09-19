"""M6 task 10-C (scan rows 2/9/23/27/31): ``models.list`` on an AgentCore row, and the
overhead-model write it validates against.

The model picker used to have nothing to pick from (``config.models.providers`` does not exist
in a Hermes config) and 400'd on anything it did send (the closed ``standard``/``premium``
vocabulary). M6-R50: the list is the models the owner can ACTUALLY invoke -- their own Bedrock
application inference profiles on a platform-billed workspace (served here, no microVM wake),
Hermes's own provider catalog via the bridge otherwise.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock

import pytest

from core.services import runtime_rpc as rpc
from core.services.messaging import ApiProblem

_PAID_AIPS = {"opus": "arn:aip:opus", "sonnet": "arn:aip:sonnet", "haiku": "arn:aip:haiku"}


def _wire_owner(monkeypatch, *, provider_choice="bedrock_claude", aip_arns=_PAID_AIPS):
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice",
        AsyncMock(return_value=(provider_choice, None)),
    )
    monkeypatch.setattr(
        rpc.container_repo, "get_by_owner_id", AsyncMock(return_value={"bedrock_aip_arns": dict(aip_arns)})
    )


def _no_bridge(monkeypatch):
    call = AsyncMock(side_effect=AssertionError("models.list must not wake a microVM for Bedrock"))
    monkeypatch.setattr(rpc.container_rpc, "call", call)
    return call


async def _models_list(owner_id="org_a"):
    return await rpc.execute_sync(owner_id=owner_id, user_id="u1", request_id="r1", method="models.list", params={})


@pytest.mark.asyncio
async def test_bedrock_owner_gets_their_own_aips_by_name_with_no_microvm_wake(monkeypatch):
    _wire_owner(monkeypatch)
    _no_bridge(monkeypatch)

    out = await _models_list()

    assert out["models"] == [
        {"id": "arn:aip:opus", "name": "Claude Opus", "provider": "bedrock", "tier": "premium"},
        {"id": "arn:aip:sonnet", "name": "Claude Sonnet", "provider": "bedrock", "tier": "standard"},
        {"id": "arn:aip:haiku", "name": "Claude Haiku", "provider": "bedrock", "tier": "cheap"},
    ]
    # The roster default is the standard tier -- the same one a fresh non-CEO profile is written
    # with, so the picker's "default" row and provisioning can never disagree.
    assert out["defaultId"] == "arn:aip:sonnet"


@pytest.mark.asyncio
async def test_a_trial_owner_is_offered_only_the_one_profile_they_hold(monkeypatch):
    """A trial owner holds exactly one AIP. Offering opus/haiku would hand them a model the
    bedrock-gate refuses on every turn."""
    _wire_owner(monkeypatch, provider_choice="bedrock_trial", aip_arns={"sonnet": "arn:aip:sonnet"})
    _no_bridge(monkeypatch)

    out = await _models_list()

    assert [row["id"] for row in out["models"]] == ["arn:aip:sonnet"]
    assert out["defaultId"] == "arn:aip:sonnet"


@pytest.mark.asyncio
async def test_no_aips_on_the_row_yet_is_an_empty_list_not_an_error(monkeypatch):
    _wire_owner(monkeypatch, aip_arns={})
    _no_bridge(monkeypatch)

    assert await _models_list() == {"models": [], "defaultId": ""}


@pytest.mark.asyncio
async def test_byo_owner_falls_through_to_the_bridges_hermes_catalog(monkeypatch):
    """Only the runtime holds Hermes's own provider catalog, so a BYO/OAuth workspace's list
    comes from the bridge -- passed through unchanged."""
    _wire_owner(monkeypatch, provider_choice="byo_key", aip_arns={})
    payload = {"models": [{"id": "claude-sonnet-5", "name": "claude-sonnet-5", "provider": "anthropic"}]}
    call = AsyncMock(return_value=payload)
    monkeypatch.setattr(rpc.container_rpc, "call", call)

    assert await _models_list() == payload
    assert call.await_args.args[1] == "models.list"


# ---------------------------------------------------------------------------
# The overhead-model write (rows 4/23) -- one id in, the whole auxiliary block out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overhead_model_patch_expands_into_every_auxiliary_task(monkeypatch):
    from core.services.hermes_profiles import AUXILIARY_TASKS

    _wire_owner(monkeypatch)
    out = await rpc._expand_agentcore_config_patch("org_a", {"auxiliary": {"model": "arn:aip:haiku"}})

    aux = out["auxiliary"]
    # The pseudo-key is never stored, and the provider + task list come from the backend writer.
    assert "model" not in aux
    assert set(aux) == set(AUXILIARY_TASKS)
    assert all(
        entry == {"provider": "bedrock", "model": "arn:aip:haiku", "fallback_chain": []} for entry in aux.values()
    )


@pytest.mark.asyncio
async def test_overhead_model_patch_refuses_a_model_the_workspace_cannot_use(monkeypatch):
    _wire_owner(monkeypatch)
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


@pytest.mark.asyncio
async def test_migrated_trial_inventory_does_not_expose_paid_tiers(monkeypatch):
    _wire_owner(monkeypatch, provider_choice="bedrock_trial")
    out = await _models_list()
    assert [model["id"] for model in out["models"]] == ["arn:aip:sonnet"]
    assert out["defaultId"] == "arn:aip:sonnet"
    with pytest.raises(ApiProblem):
        await rpc._validated_model_id("org_a", "arn:aip:opus", method="agents.update")
