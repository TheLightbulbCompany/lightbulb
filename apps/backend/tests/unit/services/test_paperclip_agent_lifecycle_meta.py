"""Tests for paperclip_agent_lifecycle — meta kwarg threading."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services.paperclip_agent_lifecycle import (  # noqa: E402
    create_paperclip_for_chat,
    create_paperclip_for_marketplace,
)
from core.services.paperclip_meta import PaperclipMeta  # noqa: E402


@pytest.mark.asyncio
async def test_create_paperclip_for_chat_forwards_meta_fields():
    """When meta is supplied, create_paperclip_for_chat passes
    title/capabilities/icon through to admin_client.create_agent.
    Role from meta wins when the explicit role kwarg is None
    (chat-side passes role="general" hardcoded today; meta lets that
    be overridden)."""
    meta = PaperclipMeta(
        role="engineer",
        title="Backend specialist",
        capabilities="Writes services.",
        icon="terminal",
    )
    mock_admin = MagicMock()
    mock_admin.create_agent = AsyncMock(return_value={"id": "new-uuid"})

    with (
        patch("core.services.paperclip_agent_lifecycle.PaperclipRepo") as mock_repo_cls,
        patch(
            "core.services.paperclip_agent_lifecycle.PaperclipAdminClient",
            return_value=mock_admin,
        ),
        patch(
            "core.services.paperclip_agent_lifecycle.get_admin_session_cookie",
            new=AsyncMock(return_value="sess=cookie"),
        ),
        patch("core.services.paperclip_agent_lifecycle.service_token") as mock_st,
    ):
        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co-1"))
        mock_repo_cls.return_value = mock_repo
        mock_st.mint = MagicMock(return_value="svc-token")

        await create_paperclip_for_chat(
            creator_user_id="user_creator",
            owner_id="user_owner",
            name="Ray",
            role=None,
            model=None,
            meta=meta,
        )

    mock_admin.create_agent.assert_awaited_once()
    kwargs = mock_admin.create_agent.await_args.kwargs
    assert kwargs["name"] == "Ray"
    assert kwargs["role"] == "engineer"
    assert kwargs["title"] == "Backend specialist"
    assert kwargs["capabilities"] == "Writes services."
    assert kwargs["icon"] == "terminal"


@pytest.mark.asyncio
async def test_create_paperclip_for_chat_no_meta_keeps_existing_behavior():
    """Backwards-compat: existing callers that don't pass meta still get
    role="general" and no title/capabilities (status quo before this PR).
    The kwarg defaults so existing call sites don't have to change."""
    mock_admin = MagicMock()
    mock_admin.create_agent = AsyncMock(return_value={"id": "new-uuid"})

    with (
        patch("core.services.paperclip_agent_lifecycle.PaperclipRepo") as mock_repo_cls,
        patch(
            "core.services.paperclip_agent_lifecycle.PaperclipAdminClient",
            return_value=mock_admin,
        ),
        patch(
            "core.services.paperclip_agent_lifecycle.get_admin_session_cookie",
            new=AsyncMock(return_value="c"),
        ),
        patch("core.services.paperclip_agent_lifecycle.service_token") as mock_st,
    ):
        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))
        mock_repo_cls.return_value = mock_repo
        mock_st.mint = MagicMock(return_value="t")

        await create_paperclip_for_chat(
            creator_user_id="u",
            owner_id="u",
            name="Ray",
            role=None,
            model=None,
        )

    kwargs = mock_admin.create_agent.await_args.kwargs
    # default — meta=None path
    assert kwargs["role"] == "general"
    # title/capabilities/icon either absent or None (helper doesn't pass them)
    assert kwargs.get("title") is None
    assert kwargs.get("capabilities") is None
    assert kwargs.get("icon") is None


@pytest.mark.asyncio
async def test_create_paperclip_for_marketplace_forwards_meta():
    """Same contract on the marketplace path: meta fields thread
    through to create_agent."""
    meta = PaperclipMeta(role="cmo", title="Marketing lead", capabilities="Campaign work.", icon=None)
    mock_admin = MagicMock()
    mock_admin.create_agent = AsyncMock(return_value={"id": "new-uuid"})

    with (
        patch("core.services.paperclip_agent_lifecycle.PaperclipRepo") as mock_repo_cls,
        patch(
            "core.services.paperclip_agent_lifecycle.PaperclipAdminClient",
            return_value=mock_admin,
        ),
        patch(
            "core.services.paperclip_agent_lifecycle.get_admin_session_cookie",
            new=AsyncMock(return_value="c"),
        ),
        patch("core.services.paperclip_agent_lifecycle.service_token") as mock_st,
    ):
        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))
        mock_repo_cls.return_value = mock_repo
        mock_st.mint = MagicMock(return_value="t")

        await create_paperclip_for_marketplace(
            buyer_user_id="u",
            owner_id="u",
            name="Ray",
            role=None,
            meta=meta,
        )

    kwargs = mock_admin.create_agent.await_args.kwargs
    assert kwargs["role"] == "cmo"
    assert kwargs["title"] == "Marketing lead"
    assert kwargs["capabilities"] == "Campaign work."
