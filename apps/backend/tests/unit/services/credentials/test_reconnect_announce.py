"""A grant going ``reconnect_required`` must be visible: profile reconcile + one Needs-you item."""

from unittest.mock import AsyncMock

import pytest

from core.services import activity, website_logins
from core.services.credentials import access_token, mcp_server


@pytest.fixture
def wired(monkeypatch):
    puts = AsyncMock()
    reconcile = AsyncMock()
    monkeypatch.setattr(access_token.integrations_repo, "set_status", AsyncMock())
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    monkeypatch.setattr(mcp_server, "reconcile_integrations", reconcile)
    monkeypatch.setattr(website_logins, "_humans_to_ask", AsyncMock(return_value=["user_a", "user_b"]))
    monkeypatch.setattr("core.repositories.messaging_repo.put_activity_row", puts)
    monkeypatch.setattr("core.services.live_events.publish_owner_users", AsyncMock())
    return puts, reconcile


@pytest.mark.asyncio
async def test_the_transition_reconciles_profiles_and_files_one_item_per_member(monkeypatch, wired):
    puts, reconcile = wired
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(return_value={"status": "connected"}))

    await access_token._mark_reconnect("org_a", "microsoft365")

    reconcile.assert_awaited_once_with("org_a")
    rows = [call.args[0] for call in puts.await_args_list]
    assert [r["pk"] for r in rows] == ["ACTIVITY#org_a#user_a", "ACTIVITY#org_a#user_b"]
    assert all(r["kind"] == "reconnect" and r["provider"] == "microsoft365" for r in rows)
    assert all(r["sk"].endswith("#" + activity.reconnect_item_id("microsoft365")) for r in rows)


@pytest.mark.asyncio
async def test_one_members_failed_write_does_not_stop_the_rest_or_the_invalidation(monkeypatch, wired):
    puts, _ = wired
    puts.side_effect = [RuntimeError("throttled"), None]
    publish = AsyncMock()
    monkeypatch.setattr("core.services.live_events.publish_owner_users", publish)
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(return_value={"status": "connected"}))

    await access_token._mark_reconnect("org_a", "microsoft365")

    assert puts.await_count == 2
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_repeat_while_already_reconnect_required_files_nothing(monkeypatch, wired):
    puts, reconcile = wired
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(return_value={"status": "reconnect_required"}))

    await access_token._mark_reconnect("org_a", "microsoft365")

    puts.assert_not_awaited()
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_item_is_open_until_the_integration_reconnects(monkeypatch):
    rows = [
        {"kind": "reconnect", "provider": "microsoft365", "createdAt": "2026-10-01T00:00:00+00:00"},
        {"kind": "reconnect", "provider": "microsoft365", "createdAt": "2026-10-02T00:00:00+00:00"},
    ]
    get = AsyncMock(return_value={"status": "reconnect_required"})
    monkeypatch.setattr("core.repositories.integrations_repo.get", get)

    items = await activity._render_reconnect_rows("org_a", rows, None)

    assert get.await_args.kwargs == {"consistent": True}  # a stale read would hide the fresh item
    assert len(items) == 1  # two failures, one item
    assert items[0].state == "open" and activity.needs_you(items[0])
    assert items[0].summary.title.startswith("Reconnect ")

    get.return_value = {"status": "connected"}
    assert await activity._render_reconnect_rows("org_a", rows, None) == []


@pytest.mark.asyncio
async def test_chatgpt_item_renders_from_the_token_row(monkeypatch):
    """Review of #4723: the broker files ``openai-codex`` (spec 13 §11 R5), which has no
    integrations row and no registry provider -- the item is open while the token row says
    ``reconnect_required`` and disappears once the owner reconnects."""
    rows = [{"kind": "reconnect", "provider": "openai-codex", "createdAt": "2026-10-01T00:00:00+00:00"}]
    get = AsyncMock(side_effect=AssertionError("integrations row read for ChatGPT"))
    monkeypatch.setattr("core.repositories.integrations_repo.get", get)
    monkeypatch.setattr("core.services.oauth_service.resolve_token_owner", AsyncMock(return_value="user_c"))
    needs = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.oauth_service.token_row_needs_reconnect", needs)

    items = await activity._render_reconnect_rows("org_a", rows, None)

    needs.assert_awaited_once_with("user_c", bound_to="org_a")
    assert len(items) == 1 and items[0].state == "open" and activity.needs_you(items[0])
    assert items[0].summary.title == "Reconnect ChatGPT"
    assert items[0].id == activity.reconnect_item_id("openai-codex")

    needs.return_value = False
    assert await activity._render_reconnect_rows("org_a", rows, None) == []


@pytest.mark.asyncio
async def test_announce_without_reconcile_files_the_item_only(wired):
    puts, reconcile = wired
    await access_token._announce_reconnect("org_a", "openai-codex", reconcile=False)
    reconcile.assert_not_awaited()
    assert puts.await_count == 2 and all(c.args[0]["provider"] == "openai-codex" for c in puts.await_args_list)
