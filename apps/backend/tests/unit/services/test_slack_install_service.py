import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import slack_install_service as svc  # noqa: E402


def test_account_block_is_http_with_secret_refs_and_no_plaintext():
    block = svc.account_block("T0123ABC")
    assert block["mode"] == "http"
    assert block["webhookPath"] == "/slack/events/T0123ABC"
    assert block["botToken"] == {"source": "env", "provider": "default", "id": "SLACK_BOT_TOKEN_T0123ABC"}
    assert block["signingSecret"] == {"source": "env", "provider": "default", "id": "OPENCLAW_HOOK_TOKEN"}
    assert "appToken" not in block
    assert block["dmPolicy"] == "open" and block["allowFrom"] == ["*"]
    assert block["slashCommand"] == {"enabled": True, "name": "lightbulb"}


def test_route_binding_targets_agent_by_account():
    assert svc.route_binding("T1", "agent_f") == {
        "type": "route",
        "agentId": "agent_f",
        "match": {"channel": "slack", "accountId": "T1"},
    }


def _install_kwargs(**over):
    base = dict(
        owner_id="org_1",
        agent_id="agent_f",
        team_id="T1",
        team_name="Acme",
        bot_token="xoxb-1",
        bot_user_id="U_BOT",
        app_id="A1",
        scopes=["chat:write"],
        installer_slack_user_id="U_ADMIN",
        installer_member_id="user_1",
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_apply_install_writes_secret_row_taskdef_and_config_in_order():
    calls: list[str] = []

    async def _put_secret(owner, provider, cred):
        calls.append(f"secret:{owner}:{provider}:{cred['bot_token']}")
        return "arn:secret"

    async def _put_row(**kw):
        calls.append("row")
        return {**kw, "status": "active"}

    async def _status(*a, **k):
        calls.append("status")

    async def _refresh(owner, *, extra_secrets):
        calls.append(f"refresh:{extra_secrets[0]['name']}:{extra_secrets[0]['valueFrom']}")
        return "arn:td"

    async def _patch(owner, patch):
        calls.append("patch")
        assert patch["channels"]["slack"]["accounts"]["T1"]["mode"] == "http"
        assert patch["channels"]["slack"]["enabled"] is True
        assert patch["channels"]["slack"]["streaming"] == {"mode": "progress"}

    async def _remove(owner, path, predicate):
        calls.append("unbind-auto")

    async def _append(owner, path, entries):
        calls.append(f"bindings:{entries['agentId']}")

    mgr = MagicMock()
    mgr.refresh_task_definition = _refresh
    mgr._secret_arn = AsyncMock(return_value="arn:secret")
    with (
        patch("core.services.slack_install_service.integration_secret_store.put", _put_secret),
        patch("core.services.slack_install_service.slack_installs_repo.put", _put_row),
        patch("core.services.slack_install_service._mark_connected", _status),
        patch("core.services.slack_install_service.get_ecs_manager", lambda: mgr),
        patch("core.services.slack_install_service.patch_openclaw_config", _patch),
        patch("core.services.slack_install_service.remove_from_openclaw_config_list", _remove),
        patch("core.services.slack_install_service.append_to_openclaw_config_list", _append),
    ):
        row = await svc.apply_install(**_install_kwargs())
    assert row["status"] == "active"
    assert calls == [
        "secret:org_1:slack/T1:xoxb-1",
        "row",
        "status",
        "refresh:SLACK_BOT_TOKEN_T1:arn:secret",
        "patch",
        "unbind-auto",
        "bindings:agent_f",
    ]


@pytest.mark.asyncio
async def test_apply_install_conflict_raises_and_deletes_secret():
    deleted = []

    async def _delete(owner, provider):
        deleted.append(provider)

    with (
        patch("core.services.slack_install_service.integration_secret_store.put", AsyncMock(return_value="arn")),
        patch("core.services.slack_install_service.integration_secret_store.delete", _delete),
        patch("core.services.slack_install_service.slack_installs_repo.put", AsyncMock(return_value=None)),
    ):
        with pytest.raises(svc.SlackInstallConflict):
            await svc.apply_install(**_install_kwargs())
    assert deleted == ["slack/T1"]


@pytest.mark.asyncio
async def test_revoke_install_tears_down_everything():
    row = {"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}
    removed = {}

    async def _remove(owner, path, predicate):
        removed["pred"] = predicate

    with (
        patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=row)),
        patch("core.services.slack_install_service.slack_installs_repo.set_status", AsyncMock()) as set_status,
        patch("core.services.slack_install_service.integration_secret_store.delete", AsyncMock()) as del_secret,
        patch("core.services.slack_install_service.delete_openclaw_config_path", AsyncMock()) as del_path,
        patch("core.services.slack_install_service.remove_from_openclaw_config_list", _remove),
        patch(
            "core.services.slack_install_service.channel_link_repo.sweep_by_owner_provider_agent",
            AsyncMock(return_value=2),
        ),
        patch("core.services.slack_install_service._mark_disconnected", AsyncMock()),
    ):
        assert await svc.revoke_install("T1") is True
    set_status.assert_awaited_once_with("T1", "revoked")
    del_secret.assert_awaited_once_with("org_1", "slack/T1")
    del_path.assert_awaited_once_with("org_1", ["channels", "slack", "accounts", "T1"])
    assert removed["pred"]({"type": "route", "agentId": "x", "match": {"channel": "slack", "accountId": "T1"}}) is True
    assert removed["pred"]({"type": "route", "agentId": "x", "match": {"channel": "slack", "accountId": "T9"}}) is False


@pytest.mark.asyncio
async def test_revoke_unknown_team_is_false():
    with patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=None)):
        assert await svc.revoke_install("T_NOPE") is False
