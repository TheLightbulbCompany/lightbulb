"""Tests for paperclip_agent_lifecycle (marketplace deploy only, deleted in task 8b.6) — meta kwarg threading."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services.paperclip_agent_lifecycle import create_paperclip_for_marketplace  # noqa: E402
from core.services.paperclip_meta import PaperclipMeta  # noqa: E402


@pytest.fixture(autouse=True)
def _provisioned_container(monkeypatch):
    """Both create paths build their adapter through ``resolve_agent_adapter``, which reads the
    owner's container row for its hook token -- and the shared builder REFUSES to write an
    adapter without one (every run would 401 at the facade, reported per RUN rather than as a
    broken agent). These meta-kwarg-threading tests are not about that guard, so give them a
    provisioned row."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook-tok"}))


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
        mock_repo.resolve_owner_row = AsyncMock(return_value=MagicMock(company_id="co"))
        mock_repo_cls.return_value = mock_repo

        await create_paperclip_for_marketplace(
            buyer_user_id="u",
            owner_id="org_u",
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
