import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.containers import config as cfg  # noqa: E402
from core.containers import ecs_manager as em  # noqa: E402
from core.services.slack_install_service import account_block, route_binding  # noqa: E402
from tests.unit.containers.helpers import minimal_build_kwargs  # noqa: E402


def _no_installs():
    return patch("core.repositories.slack_installs_repo.list_for_owner", AsyncMock(return_value=[]))


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
    with (
        patch("core.containers.ecs_manager.read_openclaw_config_from_efs", AsyncMock(return_value=on_disk)),
        _no_installs(),
    ):
        accounts, bindings = await em._provision_channel_accounts("org_1")
    assert accounts == {"slack": {"T1": {"mode": "http"}}, "telegram": {"agent_a": {"botToken": "x"}}}
    assert bindings == on_disk["bindings"][:2]


@pytest.mark.asyncio
async def test_provision_channel_accounts_is_fail_safe():
    with patch("core.containers.ecs_manager.read_openclaw_config_from_efs", AsyncMock(side_effect=RuntimeError("efs"))):
        assert await em._provision_channel_accounts("org_1") == ({}, [])


@pytest.mark.asyncio
async def test_provision_channel_accounts_unions_installs_table():
    """A reprovision after an EFS wipe has nothing on disk for the platform
    Slack app — the installs table is the source of truth there, and it wins
    over the file for the same team."""
    rows = [
        {"team_id": "T1", "status": "active", "agent_id": "agent_f"},
        {"team_id": "T2", "status": "active", "agent_id": None},
        {"team_id": "T3", "status": "revoked", "agent_id": "agent_g"},
    ]
    with (
        patch("core.containers.ecs_manager.read_openclaw_config_from_efs", AsyncMock(return_value={})),
        patch("core.repositories.slack_installs_repo.list_for_owner", AsyncMock(return_value=rows)),
    ):
        accounts, bindings = await em._provision_channel_accounts("org_1")
    assert accounts == {"slack": {"T1": account_block("T1")}}
    assert bindings == [route_binding("T1", "agent_f")]


def _build(**kw):
    """Minimal build_openclaw_config_dict call — merges minimal_build_kwargs()
    (paid bedrock_claude provider so is_bedrock_trial is False) with the
    per-test overrides."""
    return cfg.build_openclaw_config_dict(**{**minimal_build_kwargs(), **kw})


def test_carried_binding_wins_over_synthesized_one():
    out = _build(
        paperclip_agents=[{"id": "agent_f", "role": "ceo"}],
        channel_accounts={"slack": {"T1": {"mode": "http"}}},
        channel_bindings=[{"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}}],
    )
    slack_bindings = [b for b in out["bindings"] if b["match"]["channel"] == "slack"]
    assert slack_bindings == [{"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}}]
    assert out["channels"]["slack"]["accounts"]["T1"] == {"mode": "http"}


def test_legacy_account_without_carried_binding_still_synthesizes():
    # The legacy `agentId == accountId` convention only holds because such an
    # account is keyed BY an agent id — so that agent has to exist.
    out = _build(
        paperclip_agents=[{"id": "agent_a", "role": "ceo"}],
        channel_accounts={"telegram": {"agent_a": {"botToken": "x"}}},
        channel_bindings=[],
    )
    assert {"type": "route", "agentId": "agent_a", "match": {"channel": "telegram", "accountId": "agent_a"}} in out[
        "bindings"
    ]


def test_stale_binding_for_missing_account_is_dropped():
    out = _build(
        paperclip_agents=[{"id": "agent_f", "role": "ceo"}],
        channel_accounts={"slack": {"T1": {"mode": "http"}}},
        channel_bindings=[
            {"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}},
            {"type": "route", "agentId": "agent_old", "match": {"channel": "slack", "accountId": "T_GONE"}},
        ],
    )
    slack_bindings = [b for b in out["bindings"] if b["match"]["channel"] == "slack"]
    assert slack_bindings == [{"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}}]


def test_no_binding_is_written_for_an_account_that_is_not_an_agent():
    """Upstream normalizes channel-root settings into an `accounts.default`
    husk (ours held only dmPolicy). Carrying it and applying the legacy
    `agentId == accountId` rule emitted `agentId: "default"`, which OpenClaw
    rejects at load — `bindings.N.agentId: Unknown agent id "default"` — so the
    container refuses the WHOLE config and keeps running the previous one.
    A binding must name an agent that exists."""
    out = _build(
        paperclip_agents=[{"id": "agent_f", "role": "ceo"}],
        channel_accounts={"slack": {"T1": {"mode": "http"}, "default": {"dmPolicy": "pairing"}}},
        channel_bindings=[{"type": "route", "agentId": "agent_f", "match": {"channel": "slack", "accountId": "T1"}}],
    )
    known = set(out["agents"].get("entries") or {k["id"]: k for k in (out["agents"].get("list") or [])})
    assert all(b["agentId"] in known for b in out["bindings"]), out["bindings"]
    assert not any(b["match"]["accountId"] == "default" for b in out["bindings"])
    # The account itself is still carried — only the bogus binding is withheld.
    assert "default" in out["channels"]["slack"]["accounts"]


def test_carried_binding_naming_a_dead_agent_is_dropped():
    """Same invariant on the carry side: a binding already on disk that names a
    deleted agent must not be re-emitted, or the config stays unloadable
    forever — regeneration would keep carrying its own bad output."""
    out = _build(
        paperclip_agents=[{"id": "agent_f", "role": "ceo"}],
        channel_accounts={"slack": {"T1": {"mode": "http"}}},
        channel_bindings=[
            {"type": "route", "agentId": "deleted_agent", "match": {"channel": "slack", "accountId": "T1"}}
        ],
    )
    assert not any(b["agentId"] == "deleted_agent" for b in out["bindings"])


@pytest.mark.asyncio
async def test_credential_less_account_husk_is_not_carried():
    """OpenClaw persists provider-level settings back to disk as a synthetic
    `accounts.default`. Ours was left holding only dmPolicy after a BYO bot was
    deleted (routers/channels.py strips the root credentials, never the husk).
    Carrying it re-injects a bot that does not exist."""
    cfg_on_disk = {
        "channels": {
            "slack": {
                "accounts": {
                    "T1": {"mode": "http", "webhookPath": "/slack/events/T1"},
                    "default": {"dmPolicy": "pairing"},
                }
            }
        },
        "bindings": [],
    }
    with (
        patch("core.containers.ecs_manager.read_openclaw_config_from_efs", AsyncMock(return_value=cfg_on_disk)),
        patch("core.repositories.slack_installs_repo.list_for_owner", AsyncMock(return_value=[])),
    ):
        accounts, _ = await em._provision_channel_accounts("org_1")
    assert list(accounts["slack"]) == ["T1"]


@pytest.mark.asyncio
async def test_provider_with_only_husks_is_dropped_entirely():
    cfg_on_disk = {"channels": {"slack": {"accounts": {"default": {"dmPolicy": "pairing"}}}}, "bindings": []}
    with (
        patch("core.containers.ecs_manager.read_openclaw_config_from_efs", AsyncMock(return_value=cfg_on_disk)),
        patch("core.repositories.slack_installs_repo.list_for_owner", AsyncMock(return_value=[])),
    ):
        accounts, _ = await em._provision_channel_accounts("org_1")
    assert "slack" not in accounts
