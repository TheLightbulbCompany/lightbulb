from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import paperclip_agent_lifecycle as lifecycle


@pytest.mark.asyncio
async def test_prepare_delete_reassigns_open_issues_and_preserves_routines():
    admin = MagicMock()
    admin.list_agents = AsyncMock(
        return_value=[
            {"id": "friday", "role": "ceo"},
            {"id": "atlas", "role": "general"},
            {"id": "retiring", "role": "engineer"},
        ]
    )
    admin.list_routines = AsyncMock(
        return_value=[
            {"id": "routine-live", "assigneeAgentId": "retiring", "status": "active"},
            {"id": "routine-old", "assigneeAgentId": "retiring", "status": "archived"},
            {"id": "routine-other", "assigneeAgentId": "atlas", "status": "active"},
        ]
    )
    admin.patch_routine = AsyncMock(return_value={})
    admin.list_issues = AsyncMock(
        side_effect=[
            [
                {"id": "issue-open", "status": "in_review"},
                {"id": "issue-done", "status": "done"},
            ],
            [
                {"id": "issue-cancelled", "status": "cancelled"},
            ],
        ]
    )
    admin.patch_issue = AsyncMock(return_value={})
    http = MagicMock()
    http.aclose = AsyncMock()

    with (
        patch.object(lifecycle, "resolve_company_id_for_owner", new=AsyncMock(return_value="company")),
        patch.object(lifecycle, "PaperclipAdminClient", return_value=admin),
        patch.object(lifecycle, "get_admin_session_cookie", new=AsyncMock(return_value="cookie")),
        patch.object(lifecycle.httpx, "AsyncClient", return_value=http),
        patch.object(lifecycle, "_ISSUE_PAGE_SIZE", 2),
        patch(
            "core.services.paperclip_openclaw_pairing.get_seeded_agent_ids",
            new=AsyncMock(return_value={"friday", "atlas"}),
        ),
    ):
        result = await lifecycle.prepare_paperclip_agent_delete(
            owner_id="owner",
            agent_id="retiring",
        )

    assert result == {
        "replacement_agent_id": "friday",
        "issues_reassigned": 1,
        "routines_archived": 2,
    }
    assert admin.patch_routine.await_args_list[0].kwargs["body"] == {
        "assigneeAgentId": "friday",
        "status": "archived",
    }
    assert admin.patch_routine.await_args_list[1].kwargs["body"] == {
        "assigneeAgentId": "friday",
    }
    admin.patch_issue.assert_awaited_once_with(
        issue_id="issue-open",
        body={"assigneeAgentId": "friday"},
        session_cookie="cookie",
    )
    assert [call.kwargs["params"]["offset"] for call in admin.list_issues.await_args_list] == ["0", "2"]


# ---------------------------------------------------------------------------
# delete_agent_everywhere: the S3 profile prefix is the agent's whole durable record.
#
# The ECS-shaped halves this used to also drive are gone with that runtime: the gateway
# `agents.list` probe and the `agents.delete` RPC it might trigger (a parked microVM has no
# gateway RPC to probe, and probing one paid a 30s timeout for an answer the branch never used),
# the `openclaw.json` agent-entry and channel-account strip, the EFS `workspace-{id}/` cleanup,
# the `.template` sidecar removal, and the `openclaw.json` file-lock retry helper the delete
# wrapped all of it in.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_everywhere_deletes_the_profile_before_the_paperclip_row():
    """Order is the recovery story: if the profile delete fails, the Paperclip row stays VISIBLE
    and a retry reconciles. Deleting the row first would leave a live profile with nothing in
    Teams to delete it by."""
    events: list[str] = []

    async def record_paperclip(**_kwargs):
        events.append("paperclip")

    with (
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch(
            "core.services.hermes_profiles.delete_profile", MagicMock(side_effect=lambda *_: events.append("profile"))
        ) as delete_profile,
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock(side_effect=record_paperclip)),
    ):
        result = await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    assert result["deleted"] is True
    assert events == ["profile", "paperclip"]
    delete_profile.assert_called_once_with("owner", "retiring")


@pytest.mark.asyncio
async def test_delete_everywhere_keeps_the_paperclip_row_when_the_profile_delete_fails():
    """A throttled S3 delete must not take the row with it -- the owner needs something in Teams
    to retry against."""
    with (
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.services.hermes_profiles.delete_profile", MagicMock(side_effect=OSError("s3 throttled"))),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()) as delete_paperclip,
    ):
        with pytest.raises(OSError, match="s3 throttled"):
            await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    delete_paperclip.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_everywhere_carries_the_preserved_fields_back_to_the_caller():
    """``prepare_paperclip_agent_delete`` reassigns the agent's open issues and records what it
    preserved (routines, starter-roster uuid). The caller renders that, so dropping it silently
    tells the owner nothing was carried over."""
    with (
        patch.object(
            lifecycle,
            "prepare_paperclip_agent_delete",
            new=AsyncMock(return_value={"reassigned_issues": 3, "preserved_routines": 2}),
        ),
        patch("core.services.hermes_profiles.delete_profile", MagicMock()),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()),
    ):
        result = await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring")

    assert result == {"deleted": True, "agent_id": "retiring", "reassigned_issues": 3, "preserved_routines": 2}


@pytest.mark.asyncio
async def test_delete_everywhere_never_probes_a_runtime():
    """M6-R37: probing a possibly-parked microVM paid a 30s timeout for an answer this path never
    used, and on AgentCore it would also WAKE the workspace to delete an agent from it."""
    with (
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.services.hermes_profiles.delete_profile", MagicMock()),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()),
        patch(
            "core.services.container_rpc.call",
            new=AsyncMock(side_effect=AssertionError("the runtime must never be probed to delete an agent")),
        ),
    ):
        assert (await lifecycle.delete_agent_everywhere(owner_id="owner", agent_id="retiring"))["deleted"] is True
