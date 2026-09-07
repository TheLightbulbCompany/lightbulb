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

    async def _greet(token, user):
        calls.append(f"greet:{token}:{user}")

    async def _patch(owner, patch, *, agent_for_account=None):
        calls.append("patch")
        assert patch["channels"]["slack"]["accounts"]["T1"]["mode"] == "http"
        assert patch["channels"]["slack"]["enabled"] is True
        assert patch["channels"]["slack"]["streaming"] == {"mode": "progress"}
        assert agent_for_account == {"T1": "agent_f"}

    mgr = MagicMock()
    mgr.refresh_task_definition = _refresh
    with (
        patch("core.services.slack_install_service.integration_secret_store.put", _put_secret),
        patch("core.services.slack_install_service.slack_installs_repo.put", _put_row),
        patch("core.services.slack_install_service._mark_connected", _status),
        patch("core.services.slack_install_service.get_ecs_manager", lambda: mgr),
        patch("core.services.slack_install_service.patch_openclaw_config", _patch),
        patch("core.services.slack_install_service._greet_installer", _greet),
    ):
        row = await svc.apply_install(**_install_kwargs())
    assert row["status"] == "active"
    assert calls == [
        "secret:org_1:slack/T1:xoxb-1",
        "row",
        "status",
        "refresh:SLACK_BOT_TOKEN_T1:arn:secret:bot_token::",
        "patch",
        "greet:xoxb-1:U_ADMIN",
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
async def test_apply_install_rolls_back_row_and_secret_when_patch_fails():
    mgr = MagicMock()
    mgr.refresh_task_definition = AsyncMock(return_value="arn:td")

    with (
        patch("core.services.slack_install_service.integration_secret_store.put", AsyncMock(return_value="arn:secret")),
        patch(
            "core.services.slack_install_service.slack_installs_repo.put", AsyncMock(return_value={"status": "active"})
        ),
        patch("core.services.slack_install_service._mark_connected", AsyncMock()),
        patch("core.services.slack_install_service.get_ecs_manager", lambda: mgr),
        patch(
            "core.services.slack_install_service.patch_openclaw_config",
            AsyncMock(side_effect=RuntimeError("efs down")),
        ),
        patch("core.services.slack_install_service.revoke_install", AsyncMock(return_value=True)) as revoke,
    ):
        with pytest.raises(RuntimeError, match="efs down"):
            await svc.apply_install(**_install_kwargs())
    revoke.assert_awaited_once_with("T1")


@pytest.mark.asyncio
async def test_revoke_install_tears_down_everything():
    row = {"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}
    removed = {}
    order: list[str] = []

    async def _remove(owner, path, predicate):
        removed["pred"] = predicate

    async def _refresh(owner, *, drop_secrets=None):
        order.append("refresh")
        return "arn:td"

    async def _delete_secret(owner, provider):
        order.append("delete_secret")

    mgr = MagicMock()
    mgr.refresh_task_definition = AsyncMock(side_effect=_refresh)

    with (
        patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=row)),
        patch("core.services.slack_install_service.slack_installs_repo.set_status", AsyncMock()) as set_status,
        patch("core.services.slack_install_service.get_ecs_manager", lambda: mgr),
        patch(
            "core.services.slack_install_service.integration_secret_store.delete", AsyncMock(side_effect=_delete_secret)
        ) as del_secret,
        patch("core.services.slack_install_service.delete_openclaw_config_path", AsyncMock()) as del_path,
        patch("core.services.slack_install_service.remove_from_openclaw_config_list", _remove),
        patch(
            "core.services.slack_install_service.channel_link_repo.sweep_by_owner_provider_agent",
            AsyncMock(return_value=2),
        ),
        patch("core.services.slack_install_service.slack_installs_repo.list_for_owner", AsyncMock(return_value=[])),
        patch("core.services.slack_install_service._mark_disconnected", AsyncMock()) as mark_disconnected,
    ):
        assert await svc.revoke_install("T1") is True
    mark_disconnected.assert_awaited_once_with("org_1")
    set_status.assert_awaited_once_with("T1", "revoked")
    mgr.refresh_task_definition.assert_awaited_once_with("org_1", drop_secrets=["SLACK_BOT_TOKEN_T1"])
    del_secret.assert_awaited_once_with("org_1", "slack/T1")
    del_path.assert_awaited_once_with("org_1", ["channels", "slack", "accounts", "T1"])
    assert order == ["refresh", "delete_secret"]
    assert removed["pred"]({"type": "route", "agentId": "x", "match": {"channel": "slack", "accountId": "T1"}}) is True
    assert removed["pred"]({"type": "route", "agentId": "x", "match": {"channel": "slack", "accountId": "T9"}}) is False


@pytest.mark.asyncio
async def test_revoke_continues_when_taskdef_refresh_fails():
    """An ECS hiccup mid-revoke must not abort the rest of teardown — only the
    secret delete is skipped (a dead ARN in a task-def is worse than an
    orphaned secret, which is harmless)."""
    row = {"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}

    mgr = MagicMock()
    mgr.refresh_task_definition = AsyncMock(side_effect=RuntimeError("ecs down"))

    with (
        patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=row)),
        patch("core.services.slack_install_service.slack_installs_repo.set_status", AsyncMock()) as set_status,
        patch("core.services.slack_install_service.get_ecs_manager", lambda: mgr),
        patch("core.services.slack_install_service.integration_secret_store.delete", AsyncMock()) as del_secret,
        patch("core.services.slack_install_service.delete_openclaw_config_path", AsyncMock()) as del_path,
        patch("core.services.slack_install_service.remove_from_openclaw_config_list", AsyncMock()) as del_binding,
        patch(
            "core.services.slack_install_service.channel_link_repo.sweep_by_owner_provider_agent",
            AsyncMock(return_value=2),
        ),
        patch("core.services.slack_install_service.slack_installs_repo.list_for_owner", AsyncMock(return_value=[])),
        patch("core.services.slack_install_service._mark_disconnected", AsyncMock()) as mark_disconnected,
    ):
        assert await svc.revoke_install("T1") is True
    del_secret.assert_not_awaited()
    del_path.assert_awaited_once_with("org_1", ["channels", "slack", "accounts", "T1"])
    del_binding.assert_awaited_once()
    set_status.assert_awaited_once_with("T1", "revoked")
    mark_disconnected.assert_awaited_once_with("org_1")


@pytest.mark.asyncio
async def test_revoke_keeps_slack_connected_while_another_workspace_remains():
    """The owner has ONE Slack status row for every workspace; revoking one of
    two must not flip it, or catalog requirement checks report Slack missing
    while the other workspace is still transporting."""
    row = {"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}
    remaining = [{"team_id": "T2", "owner_id": "org_1", "agent_id": "agent_g", "status": "active"}]
    mgr = MagicMock()
    mgr.refresh_task_definition = AsyncMock(return_value="arn:td")

    with (
        patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=row)),
        patch("core.services.slack_install_service.slack_installs_repo.set_status", AsyncMock()),
        patch("core.services.slack_install_service.get_ecs_manager", lambda: mgr),
        patch("core.services.slack_install_service.integration_secret_store.delete", AsyncMock()),
        patch("core.services.slack_install_service.delete_openclaw_config_path", AsyncMock()),
        patch("core.services.slack_install_service.remove_from_openclaw_config_list", AsyncMock()),
        patch(
            "core.services.slack_install_service.channel_link_repo.sweep_by_owner_provider_agent",
            AsyncMock(return_value=0),
        ),
        patch(
            "core.services.slack_install_service.slack_installs_repo.list_for_owner", AsyncMock(return_value=remaining)
        ),
        patch("core.services.slack_install_service._mark_disconnected", AsyncMock()) as mark_disconnected,
    ):
        assert await svc.revoke_install("T1") is True
    mark_disconnected.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_ignores_a_stale_gsi_echo_of_the_revoked_row():
    """The by-owner GSI is eventually consistent; the row just flipped to
    revoked can still read as active. It must not count as "remaining"."""
    row = {"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}
    stale = [{"team_id": "T1", "owner_id": "org_1", "agent_id": "agent_f", "status": "active"}]
    mgr = MagicMock()
    mgr.refresh_task_definition = AsyncMock(return_value="arn:td")

    with (
        patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=row)),
        patch("core.services.slack_install_service.slack_installs_repo.set_status", AsyncMock()),
        patch("core.services.slack_install_service.get_ecs_manager", lambda: mgr),
        patch("core.services.slack_install_service.integration_secret_store.delete", AsyncMock()),
        patch("core.services.slack_install_service.delete_openclaw_config_path", AsyncMock()),
        patch("core.services.slack_install_service.remove_from_openclaw_config_list", AsyncMock()),
        patch(
            "core.services.slack_install_service.channel_link_repo.sweep_by_owner_provider_agent",
            AsyncMock(return_value=0),
        ),
        patch("core.services.slack_install_service.slack_installs_repo.list_for_owner", AsyncMock(return_value=stale)),
        patch("core.services.slack_install_service._mark_disconnected", AsyncMock()) as mark_disconnected,
    ):
        assert await svc.revoke_install("T1") is True
    mark_disconnected.assert_awaited_once_with("org_1")


@pytest.mark.asyncio
async def test_revoke_unknown_team_is_false():
    with patch("core.services.slack_install_service.slack_installs_repo.get", AsyncMock(return_value=None)):
        assert await svc.revoke_install("T_NOPE") is False


def test_manifest_scopes_match_service_scopes():
    import pathlib

    import yaml

    manifest = yaml.safe_load(pathlib.Path("scripts/slack-app-manifest.yaml").read_text())
    assert sorted(manifest["oauth_config"]["scopes"]["bot"]) == sorted(svc.BOT_SCOPES)
    assert len(svc.BOT_SCOPES) == 23
    assert manifest["settings"]["socket_mode_enabled"] is False
    assert manifest["settings"]["interactivity"]["is_enabled"] is True
    assert manifest["features"]["shortcuts"]
    # The plugin handles both Agent View events; the manifest must declare
    # them or Slack never delivers them (no new scope needed).
    bot_events = manifest["settings"]["event_subscriptions"]["bot_events"]
    assert "assistant_thread_started" in bot_events
    assert "assistant_thread_context_changed" in bot_events


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Client:
    """Minimal httpx.AsyncClient stand-in recording the one POST we make."""

    def __init__(self, sent, body=None, raises=None):
        self._sent, self._body, self._raises = sent, body or {"ok": True}, raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, headers, json):
        if self._raises:
            raise self._raises
        self._sent.append((url, headers, json))
        return _Resp(self._body)


@pytest.mark.asyncio
async def test_greet_installer_dms_the_installer_with_the_bot_token():
    sent = []
    with patch("core.services.slack_install_service.httpx.AsyncClient", lambda **kw: _Client(sent)):
        await svc._greet_installer("xoxb-abc", "U_ADMIN")
    assert len(sent) == 1
    url, headers, body = sent[0]
    assert url == svc.SLACK_POST_MESSAGE_URL
    assert headers["Authorization"] == "Bearer xoxb-abc"
    # A user id as `channel` is what opens the IM — that IS the fix.
    assert body["channel"] == "U_ADMIN"
    assert body["text"] == svc.INSTALL_GREETING


@pytest.mark.asyncio
async def test_greet_installer_is_a_noop_without_an_installer_id():
    sent = []
    with patch("core.services.slack_install_service.httpx.AsyncClient", lambda **kw: _Client(sent)):
        await svc._greet_installer("xoxb-abc", "")
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw",
    [{"body": {"ok": False, "error": "channel_not_found"}}, {"raises": RuntimeError("slack down")}],
)
async def test_greet_installer_never_raises(kw):
    """The workspace is already connected by the time we greet — a failed
    greeting must not turn a good install into a rollback."""
    with patch("core.services.slack_install_service.httpx.AsyncClient", lambda **k: _Client([], **kw)):
        await svc._greet_installer("xoxb-abc", "U_ADMIN")
