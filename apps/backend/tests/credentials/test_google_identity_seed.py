"""Tests for per-agent Google-account identity seeding (google_identity).

``seed_agent_google_identity`` rewrites a delimited managed block inside the
agent's IDENTITY.md telling it which Google account to act as (``gog -a
<email>``). The block is updateable on reassignment because IDENTITY.md is
otherwise seeded write-once; the managed-block markers let the seed rewrite
only its own region and leave the surrounding persona untouched.

These tests mock the repo resolution + the workspace read/write so no EFS or
DynamoDB is touched.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.containers.workspace import WorkspaceError
from core.services import google_identity
from core.services.google_identity import (
    BEGIN_MARKER,
    END_MARKER,
    seed_agent_google_identity,
)


def _make_workspace(initial: str | None):
    """Build a workspace stand-in backed by a single in-memory IDENTITY.md.

    ``initial=None`` models a not-yet-seeded file: ``read_file`` raises
    ``WorkspaceError`` (mirroring the real helper) and the recorded writes
    expose what the seeder chose to do.
    """
    state = {"content": initial}
    ws = MagicMock()

    def _read(owner_id, path):
        if state["content"] is None:
            raise WorkspaceError("File not found", user_id=owner_id)
        return state["content"]

    def _write(owner_id, path, content):
        state["content"] = content

    ws.read_file.side_effect = _read
    ws.write_file.side_effect = _write
    ws._state = state
    return ws


@pytest.fixture
def patch_resolver(monkeypatch):
    """Patch resolve_account_for_agent to return a controllable email."""

    def _set(email):
        monkeypatch.setattr(
            google_identity.google_accounts_repo,
            "resolve_account_for_agent",
            AsyncMock(return_value=email),
        )

    return _set


@pytest.mark.asyncio
async def test_appends_managed_block_with_resolved_email(patch_resolver):
    patch_resolver("alice@gmail.com")
    base = "# IDENTITY.md\n\n- **Name:** Friday\n"
    ws = _make_workspace(base)

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)

    written = ws._state["content"]
    # Surrounding persona is preserved verbatim.
    assert written.startswith(base)
    # The managed block is appended with both markers + the email + gog hint.
    assert BEGIN_MARKER in written
    assert END_MARKER in written
    assert "alice@gmail.com" in written
    assert "gog -a alice@gmail.com" in written


@pytest.mark.asyncio
async def test_rerun_with_different_email_replaces_block_in_place(patch_resolver):
    patch_resolver("alice@gmail.com")
    base = "# IDENTITY.md\n\n- **Name:** Friday\n"
    ws = _make_workspace(base)

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    first = ws._state["content"]
    assert "alice@gmail.com" in first

    # Reassign to a different account.
    patch_resolver("bob@gmail.com")
    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    second = ws._state["content"]

    # Old email is gone, new email present, and the block is not duplicated.
    assert "alice@gmail.com" not in second
    assert "bob@gmail.com" in second
    assert second.count(BEGIN_MARKER) == 1
    assert second.count(END_MARKER) == 1
    # Surrounding persona text survives the in-place rewrite.
    assert second.startswith(base)


@pytest.mark.asyncio
async def test_rerun_same_email_is_content_stable(patch_resolver):
    patch_resolver("alice@gmail.com")
    base = "# IDENTITY.md\n\n- **Name:** Friday\n"
    ws = _make_workspace(base)

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    first = ws._state["content"]
    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    second = ws._state["content"]

    assert first == second


@pytest.mark.asyncio
async def test_none_email_removes_the_block(patch_resolver):
    patch_resolver("alice@gmail.com")
    base = "# IDENTITY.md\n\n- **Name:** Friday\n"
    ws = _make_workspace(base)

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    assert BEGIN_MARKER in ws._state["content"]

    # Account disconnected → resolver returns None → block must be removed.
    patch_resolver(None)
    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    after = ws._state["content"]

    assert BEGIN_MARKER not in after
    assert END_MARKER not in after
    assert "alice@gmail.com" not in after
    # Persona text remains; we don't strip the file down to nothing.
    assert after.startswith(base.rstrip("\n"))


@pytest.mark.asyncio
async def test_none_email_on_unseeded_file_is_noop(patch_resolver):
    """No account + no IDENTITY.md yet → nothing to write, no write attempted."""
    patch_resolver(None)
    ws = _make_workspace(None)  # read_file raises (file missing)

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)

    ws.write_file.assert_not_called()


@pytest.mark.asyncio
async def test_missing_identity_with_email_creates_minimal_file(patch_resolver):
    """IDENTITY.md not seeded yet but an account exists → write a minimal file.

    The friday/atlas pre-stage normally lands IDENTITY.md first and this runs
    after; but for an arbitrary agent that has no base IDENTITY yet, we still
    want the gog instruction present, so we create a minimal file holding just
    the managed block.
    """
    patch_resolver("alice@gmail.com")
    ws = _make_workspace(None)

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)

    written = ws._state["content"]
    assert written is not None
    assert BEGIN_MARKER in written
    assert "gog -a alice@gmail.com" in written
