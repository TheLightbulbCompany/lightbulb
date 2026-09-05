import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.containers import config as cfg  # noqa: E402
from core.containers import ecs_manager as em  # noqa: E402
from tests.unit.containers.helpers import minimal_build_kwargs  # noqa: E402


@pytest.mark.asyncio
async def test_provision_channel_accounts_returns_accounts_and_channel_bindings():
    on_disk = {
        "channels": {
            "slack": {"accounts": {"T1": {"mode": "http"}}},
            "telegram": {"accounts": {"agent_a": {"botToken": "x"}}},
        },
        "bindings": [
            {"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}},
            {"type": "route", "agentId": "agent_a", "match": {"channel": "telegram", "accountId": "agent_a"}},
            {"type": "route", "agentId": "agent_x", "match": {"channel": "email", "accountId": "e"}},
        ],
    }
    with patch("core.containers.ecs_manager.read_openclaw_config_from_efs", AsyncMock(return_value=on_disk)):
        accounts, bindings = await em._provision_channel_accounts("org_1")
    assert accounts == {"slack": {"T1": {"mode": "http"}}, "telegram": {"agent_a": {"botToken": "x"}}}
    assert bindings == on_disk["bindings"][:2]


@pytest.mark.asyncio
async def test_provision_channel_accounts_is_fail_safe():
    with patch("core.containers.ecs_manager.read_openclaw_config_from_efs", AsyncMock(side_effect=RuntimeError("efs"))):
        assert await em._provision_channel_accounts("org_1") == ({}, [])


def _build(**kw):
    """Minimal build_openclaw_config_dict call — merges minimal_build_kwargs()
    (paid bedrock_claude provider so is_bedrock_trial is False) with the
    per-test overrides."""
    return cfg.build_openclaw_config_dict(**{**minimal_build_kwargs(), **kw})


def test_carried_binding_wins_over_synthesized_one():
    out = _build(
        channel_accounts={"slack": {"T1": {"mode": "http"}}},
        channel_bindings=[{"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}}],
    )
    slack_bindings = [b for b in out["bindings"] if b["match"]["channel"] == "slack"]
    assert slack_bindings == [{"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}}]
    assert out["channels"]["slack"]["accounts"]["T1"] == {"mode": "http"}


def test_legacy_account_without_carried_binding_still_synthesizes():
    out = _build(channel_accounts={"telegram": {"agent_a": {"botToken": "x"}}}, channel_bindings=[])
    assert {"type": "route", "agentId": "agent_a", "match": {"channel": "telegram", "accountId": "agent_a"}} in out[
        "bindings"
    ]


def test_stale_binding_for_missing_account_is_dropped():
    out = _build(
        channel_accounts={"slack": {"T1": {"mode": "http"}}},
        channel_bindings=[
            {"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}},
            {"type": "route", "agentId": "agent_old", "match": {"channel": "slack", "accountId": "T_GONE"}},
        ],
    )
    slack_bindings = [b for b in out["bindings"] if b["match"]["channel"] == "slack"]
    assert slack_bindings == [{"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}}]
