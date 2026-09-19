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


@pytest.fixture(autouse=True)
def _provisioned_container(monkeypatch):
    """Both create paths build their adapter through ``resolve_agent_adapter``, which reads the
    owner's container row for its hook token -- and the shared builder REFUSES to write an
    adapter without one (every run would 401 at the facade, reported per RUN rather than as a
    broken agent). These meta-kwarg-threading tests are not about that guard, so give them a
    provisioned row; ``test_paperclip_agent_lifecycle.py`` covers the refusal."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook-tok"}))


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
    ):
        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co-1"))
        mock_repo_cls.return_value = mock_repo

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
    assert kwargs["idempotency_key"].startswith("lightbulb-agent-create:v1:chat:")


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
    ):
        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))
        mock_repo_cls.return_value = mock_repo

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
async def test_chat_retry_reuses_the_key_across_a_rebuilt_adapter_config():
    """The idempotency key is derived from logical INTENT, never from the adapter config -- so a
    second attempt resolves to the same Paperclip agent rather than minting a duplicate. It used
    to matter because the config carried a freshly minted service JWT and a new device key pair
    on every call; the native adapter's config is stable, which makes the key's independence from
    it easy to break silently."""

    mock_admin = MagicMock()
    mock_admin.create_agent = AsyncMock(return_value={"id": "same-uuid"})

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
    ):
        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))
        mock_repo_cls.return_value = mock_repo

        for _ in range(2):
            await create_paperclip_for_chat(
                creator_user_id="member",
                owner_id="org",
                name="Forge",
                role="engineer",
                model=None,
            )

    first, second = mock_admin.create_agent.await_args_list
    assert first.kwargs["idempotency_key"] == second.kwargs["idempotency_key"]
    # Rebuilt from scratch on each call, and carrying no per-call credential: the device private
    # key and the freshly minted service JWT that made this config volatile are both gone.
    assert first.kwargs["adapter_config"] == second.kwargs["adapter_config"]
    assert "devicePrivateKeyPem" not in first.kwargs["adapter_config"]
    assert "authToken" not in first.kwargs["adapter_config"]


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
    ):
        mock_repo = MagicMock()
        mock_repo.get = AsyncMock(return_value=MagicMock(company_id="co"))
        mock_repo_cls.return_value = mock_repo

        await create_paperclip_for_marketplace(
            buyer_user_id="u",
            owner_id="u",
            name="Ray",
            role=None,
            meta=meta,
            idempotency_key="marketplace-install-1",
        )

    kwargs = mock_admin.create_agent.await_args.kwargs
    assert kwargs["role"] == "cmo"
    assert kwargs["title"] == "Marketing lead"
    assert kwargs["capabilities"] == "Campaign work."
    assert kwargs["idempotency_key"] == "marketplace-install-1"
