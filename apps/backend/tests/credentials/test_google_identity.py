"""Tests for per-agent Google-account identity seeding (google_identity).

``seed_agent_google_identity`` rewrites a delimited managed block inside the
agent's IDENTITY.md telling it which Google account to act as (``gog -a
<email>``). The block is updateable on reassignment because IDENTITY.md is
otherwise seeded write-once; the managed-block markers let the seed rewrite
only its own region and leave the surrounding persona untouched.

Seeding is user-action-driven — only the assign API and the disconnect path
call it. It is deliberately a no-op when IDENTITY.md does not exist yet: the
agent simply isn't seeded, and the seeder never creates the file (the
friday/atlas pre-stage owns first-write of IDENTITY.md).

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


@pytest.fixture(autouse=True)
def _ecs_owner_by_default(monkeypatch):
    """The seeder now refuses on an AgentCore workspace, and the real predicate reads DynamoDB."""
    monkeypatch.setattr(
        "core.services.runtime_transport.is_agentcore_owner", AsyncMock(return_value=False), raising=False
    )


@pytest.fixture
def patch_resolver(monkeypatch):
    """Patch get_assignment to return a controllable email.

    The seeder reflects ONLY an agent's explicit assignment (no default
    fallback), so the block is driven by ``get_assignment``: an email seeds
    the block, None removes it.
    """

    def _set(email):
        monkeypatch.setattr(
            google_identity.google_accounts_repo,
            "get_assignment",
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
    # The email sits inside the markers, not loose elsewhere in the file.
    begin = written.index(BEGIN_MARKER)
    end = written.index(END_MARKER)
    assert begin < written.index("alice@gmail.com") < end


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
async def test_rerun_same_email_writes_nothing(patch_resolver):
    """Re-running with the same account is idempotent: no second write."""
    patch_resolver("alice@gmail.com")
    base = "# IDENTITY.md\n\n- **Name:** Friday\n"
    ws = _make_workspace(base)

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    first = ws._state["content"]
    assert ws.write_file.call_count == 1

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    second = ws._state["content"]

    assert first == second
    # No churn: the second run detected no change and skipped the write.
    assert ws.write_file.call_count == 1


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
async def test_missing_identity_with_email_is_noop(patch_resolver):
    """IDENTITY.md not seeded yet → no write even when an account exists.

    The seeder never creates IDENTITY.md; first-write belongs to the
    friday/atlas pre-stage. An agent with no base file simply isn't seeded.
    """
    patch_resolver("alice@gmail.com")
    ws = _make_workspace(None)  # read_file raises (file missing)

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)

    ws.write_file.assert_not_called()


@pytest.mark.asyncio
async def test_missing_identity_without_email_is_noop(patch_resolver):
    """No account + no IDENTITY.md yet → nothing to write, no write attempted."""
    patch_resolver(None)
    ws = _make_workspace(None)  # read_file raises (file missing)

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)

    ws.write_file.assert_not_called()


@pytest.fixture
def real_repo(monkeypatch):
    """Run the seeder against the REAL repo backed by an in-memory table.

    Lets us prove the finding-2 flow end-to-end without mocking get_assignment:
    the seeder reads the actual explicit-assignment row, so unassigning truly
    drives the block away rather than to a default account.
    """
    from core.repositories import google_accounts_repo

    store = {}

    class _T:
        def put_item(self, Item):
            store[(Item["owner_id"], Item["provider"])] = Item

        def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, **kw):
            item = store.setdefault((Key["owner_id"], Key["provider"]), dict(Key))
            if "connected_at" not in item:
                item["connected_at"] = (ExpressionAttributeValues or {})[":now"]

        def get_item(self, Key):
            it = store.get((Key["owner_id"], Key["provider"]))
            return {"Item": it} if it else {}

        def delete_item(self, Key):
            store.pop((Key["owner_id"], Key["provider"]), None)

    monkeypatch.setattr(google_accounts_repo, "_table", lambda: _T())
    return google_accounts_repo


@pytest.mark.asyncio
async def test_unassign_then_disconnect_leaves_no_stale_block(real_repo):
    # Finding-2: with B explicitly assigned to the agent, unassigning must
    # REMOVE the block — not silently rewrite it to the owner's default account
    # A. So when A is later disconnected, there is nothing stale left behind.
    repo = real_repo
    await repo.add_account("org_1", "alice@gmail.com")  # default (oldest) A
    await repo.add_account("org_1", "bob@gmail.com")  # B
    await repo.set_assignment("org_1", "agent_x", "bob@gmail.com")

    base = "# IDENTITY.md\n\n- **Name:** Friday\n"
    ws = _make_workspace(base)

    # Assign B → block names bob.
    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    assert "bob@gmail.com" in ws._state["content"]

    # Unassign → block removed, NOT rewritten to the default alice.
    await repo.remove_assignment("org_1", "agent_x")
    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    after_unassign = ws._state["content"]
    assert BEGIN_MARKER not in after_unassign
    assert "alice@gmail.com" not in after_unassign  # never rewritten to default
    assert "bob@gmail.com" not in after_unassign

    # Disconnect the default A. The agent has no assignment, so re-seeding it
    # is still a no-op block-wise: there is nothing stale to clean up.
    await repo.remove_account("org_1", "alice@gmail.com")
    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)
    final = ws._state["content"]
    assert BEGIN_MARKER not in final
    assert "alice@gmail.com" not in final
    assert final.startswith(base.rstrip("\n"))


@pytest.fixture
def agentcore(monkeypatch):
    """An AgentCore owner + a stand-in for the profile cli-env writer."""
    monkeypatch.setattr(
        "core.services.runtime_transport.is_agentcore_owner", AsyncMock(return_value=True), raising=False
    )
    setter = AsyncMock()
    monkeypatch.setattr("core.services.credentials.hermes_cli.set_profile_cli_env", setter, raising=False)
    return setter


@pytest.mark.asyncio
async def test_agentcore_assignment_is_delivered_as_gog_account_env(agentcore, patch_resolver):
    """The Hermes image ships ``gog`` and the bridge carries per-profile shell values, so the
    binding is the env var gog itself reads — not an IDENTITY.md instruction (there is no
    IDENTITY.md on a profile, and the EFS file nothing reads was the old no-op)."""
    patch_resolver("alice@gmail.com")
    ws = _make_workspace("# IDENTITY.md\n")

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)

    agentcore.assert_awaited_once_with("org_1", "agent_x", {"GOG_ACCOUNT": "alice@gmail.com"})
    # The EFS leg stays untouched: an AgentCore owner has no workspace file to seed.
    ws.write_file.assert_not_called()
    ws.read_file.assert_not_called()


@pytest.mark.asyncio
async def test_agentcore_unassigned_deletes_the_key(agentcore, patch_resolver):
    patch_resolver(None)
    ws = _make_workspace("# IDENTITY.md\n")

    await seed_agent_google_identity("org_1", "agent_x", workspace=ws)

    agentcore.assert_awaited_once_with("org_1", "agent_x", {"GOG_ACCOUNT": None})
