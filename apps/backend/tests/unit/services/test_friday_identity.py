"""Unit tests for friday_identity: the workspace-state seed and the
per-agent Paperclip identity files."""

import re
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.services import friday_identity

# JS-canonical ISO-8601: exactly 3 fractional digits and a literal ``Z`` —
# what OpenClaw 7.2's workspace-state migration accepts (``new Date(v).
# toISOString() === v``). Python's ``.isoformat()`` (6-digit micros + ``+00:00``)
# does NOT match this and is rejected by the migration.
_JS_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _assert_js_canonical_roundtrip(ts: str) -> None:
    """The timestamp is JS-canonical AND survives a strptime→strftime round-trip
    byte-identically (3-digit millis preserved, literal ``Z``)."""
    assert _JS_ISO_RE.match(ts), f"{ts!r} is not JS-canonical (YYYY-MM-DDTHH:MM:SS.mmmZ)"
    parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
    reformatted = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"
    assert reformatted == ts



def test_js_iso_now_is_js_canonical_and_roundtrips():
    """The shared helper emits ``YYYY-MM-DDTHH:MM:SS.mmmZ`` — byte-identical to
    JS ``Date.prototype.toISOString`` — so OpenClaw 7.2's workspace-state
    migration (``new Date(v).toISOString() === v``) accepts it."""
    _assert_js_canonical_roundtrip(friday_identity.js_iso_now())



# ---------------------------------------------------------------------------
# seed_workspace_state — one-shot semantics + stale-copy heal (#1429)
# ---------------------------------------------------------------------------
#
# Verified against the deployed fork tag v2026.7.2-isol8.1: the runtime gate
# (assertNoUnmigratedWorkspaceState) is PRESENCE-ONLY — if the file exists,
# every agent run throws. First boot's doctor imports it into the shared
# SQLite store, records a sha256 receipt, and deletes the file; a re-seed
# (fresh timestamp → new sha) makes every later doctor pass refuse cleanup
# ("retired source now conflicts") — a permanent brick. Exercised on a REAL
# Workspace over tmp_path: the one-shot gate is filesystem-shape-sensitive
# and a MagicMock would happily pass states the fix exists to distinguish.


@pytest.fixture
def real_workspace(tmp_path, monkeypatch):
    from core.containers.workspace import Workspace

    # Skip the EFS access-point chown (uid 1000) — not permitted on dev boxes.
    monkeypatch.setattr("core.config.settings.ENVIRONMENT", "local")
    return Workspace(mount_path=str(tmp_path))


def test_seed_workspace_state_seeds_only_a_fresh_workspace(real_workspace, tmp_path):
    state = tmp_path / "user_x" / "workspace-uuid-f" / ".openclaw" / "isol8-setup-seed.json"
    legacy = tmp_path / "user_x" / "workspace-uuid-f" / ".openclaw" / "workspace-state.json"

    # Fresh workspace: seeded, with the JS-canonical timestamp contract.
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "seeded"
    import json as _json

    payload = _json.loads(state.read_text())
    # The NATIVE sentinel carries no legacy ``version`` key, and the legacy file
    # is never written -- so the #1429 presence gate has nothing to trip on.
    assert set(payload.keys()) == {"bootstrapSeededAt", "setupCompletedAt"}
    assert not legacy.exists()
    _assert_js_canonical_roundtrip(payload["setupCompletedAt"])
    _assert_js_canonical_roundtrip(payload["bootstrapSeededAt"])

    # Re-run before any boot (retried provision): the workspace exists and the
    # original seed is preserved byte-for-byte — it migrates cleanly on boot #1.
    original = state.read_text()
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "skipped"
    assert state.read_text() == original


def test_seed_workspace_state_never_rewrites_after_migration(real_workspace, tmp_path):
    """THE #1429 fix: boot #1's doctor migrated the file into SQLite and
    deleted it. A reprovision (the trial->paid conversion path) must NOT
    re-write it — the re-seeded copy's sha no longer matches the migration
    receipt, doctor refuses cleanup forever, and the presence-only runtime
    gate bricks every agent run."""
    state = tmp_path / "user_x" / "workspace-uuid-f" / ".openclaw" / "isol8-setup-seed.json"
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "seeded"

    # Boot #1: the fork's hook converts + deletes the sentinel (and the shared
    # DB exists once OpenClaw has booted).
    state.unlink()
    (tmp_path / "user_x" / "state").mkdir()
    (tmp_path / "user_x" / "state" / "openclaw.sqlite").write_bytes(b"")

    # Reprovision: the old write_file_if_missing found the path empty and
    # re-wrote it. One-shot must skip instead.
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "skipped"
    assert not state.exists()


def test_seed_workspace_state_heals_an_already_bricked_workspace(real_workspace, tmp_path):
    """Pre-fix cohort: the file was re-seeded into an already-migrated
    workspace (shared state DB present). The stale copy's only runtime effect
    is tripping the presence gate — delete it."""
    # A container seeded BEFORE the native-sentinel cutover carries the legacy
    # file; we no longer write it, so plant it the way that cohort has it.
    state = tmp_path / "user_x" / "workspace-uuid-f" / ".openclaw" / "workspace-state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"version": 1, "bootstrapSeededAt": "x", "setupCompletedAt": "x"}')
    (tmp_path / "user_x" / "state").mkdir()
    (tmp_path / "user_x" / "state" / "openclaw.sqlite").write_bytes(b"")

    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "deleted_stale"
    assert not state.exists()

    # And the heal is itself one-shot: the next reprovision is a plain skip.
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "skipped"


def test_seed_workspace_state_keeps_an_unmigrated_seed(real_workspace, tmp_path):
    """No shared state DB == the container never booted since seeding: the
    file is the original un-migrated seed and must be left for boot #1's
    doctor to import (deleting it would only lose the setup-completed marker)."""
    state = tmp_path / "user_x" / "workspace-uuid-f" / ".openclaw" / "isol8-setup-seed.json"
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "seeded"

    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "skipped"
    assert state.is_file()


# ---------------------------------------------------------------------------
# pre_stage_paperclip_identities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paperclip_identities_writes_per_agent_file():
    """Each agent with a minted key gets its own claimed-key file in its own
    workspace with the exact Isol8-defined shape."""
    import json as _json

    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=True)

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "agent-friday", "role": "ceo", "name": "Friday"},
            {"id": "agent-atlas", "role": "general", "name": "Atlas"},
        ],
        agent_keys={"agent-friday": "key-f", "agent-atlas": "key-a"},
        company_id="comp-1",
        api_url="http://paperclip.internal:3100",
        workspace=workspace,
    )

    calls = {c.args[1]: c.args[2] for c in workspace.write_file_if_missing.call_args_list}
    assert set(calls) == {
        "workspace-agent-friday/paperclip-claimed-api-key.json",
        "workspace-agent-atlas/paperclip-claimed-api-key.json",
    }
    friday_doc = _json.loads(calls["workspace-agent-friday/paperclip-claimed-api-key.json"])
    assert friday_doc == {
        "token": "key-f",
        "agentId": "agent-friday",
        "companyId": "comp-1",
        "apiUrl": "http://paperclip.internal:3100",
    }
    atlas_doc = _json.loads(calls["workspace-agent-atlas/paperclip-claimed-api-key.json"])
    assert atlas_doc["token"] == "key-a"
    assert atlas_doc["agentId"] == "agent-atlas"


@pytest.mark.asyncio
async def test_paperclip_identities_skips_agents_without_a_minted_key():
    """An agent whose key mint failed (absent from agent_keys) gets no file."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=True)

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "agent-friday", "role": "ceo"},
            {"id": "agent-nokey", "role": "general"},
        ],
        agent_keys={"agent-friday": "key-f"},  # agent-nokey missing
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    paths = [c.args[1] for c in workspace.write_file_if_missing.call_args_list]
    assert paths == ["workspace-agent-friday/paperclip-claimed-api-key.json"]


@pytest.mark.asyncio
async def test_paperclip_identities_skipped_when_no_keys():
    """No minted keys → nothing staged (Paperclip unreachable at provision)."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[{"id": "agent-friday", "role": "ceo"}],
        agent_keys={},
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_paperclip_identities_skipped_when_no_company_or_url():
    """Missing company_id or api_url → skip entirely (incomplete context)."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[{"id": "agent-friday", "role": "ceo"}],
        agent_keys={"agent-friday": "key-f"},
        company_id=None,
        api_url="http://pc:3100",
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_paperclip_identities_per_file_failure_does_not_block_rest():
    """One agent's write raising must not stop the others (best-effort)."""
    workspace = MagicMock()
    # First write raises, second succeeds.
    workspace.write_file_if_missing = MagicMock(side_effect=[OSError("disk full"), True])

    # Should not raise.
    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "agent-a", "role": "ceo"},
            {"id": "agent-b", "role": "general"},
        ],
        agent_keys={"agent-a": "ka", "agent-b": "kb"},
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )


# ---------------------------------------------------------------------------
# stage_paperclip_identity_for_agent (single-agent, post-create path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_single_agent_writes_identity_file():
    """The single-agent seeder writes one claimed-key file with the exact
    Isol8-defined shape into the agent's own workspace."""
    import json as _json

    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=True)

    await friday_identity.stage_paperclip_identity_for_agent(
        owner_id="user_abc",
        agent_id="agent-live",
        token="key-live",
        company_id="comp-1",
        api_url="http://paperclip.internal:3100",
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_called_once()
    rel_path = workspace.write_file_if_missing.call_args.args[1]
    assert rel_path == "workspace-agent-live/paperclip-claimed-api-key.json"
    doc = _json.loads(workspace.write_file_if_missing.call_args.args[2])
    assert doc == {
        "token": "key-live",
        "agentId": "agent-live",
        "companyId": "comp-1",
        "apiUrl": "http://paperclip.internal:3100",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,company_id,api_url",
    [
        ("", "comp-1", "http://pc:3100"),  # key mint failed
        ("key", None, "http://pc:3100"),  # company unresolved
        ("key", "comp-1", None),  # paperclip url unset
    ],
)
async def test_stage_single_agent_skips_on_incomplete_context(token, company_id, api_url):
    """A missing token/company_id/api_url skips staging (no file written);
    the next reprovision self-heals."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.stage_paperclip_identity_for_agent(
        owner_id="user_abc",
        agent_id="agent-live",
        token=token,
        company_id=company_id,
        api_url=api_url,
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_stage_single_agent_write_failure_is_swallowed():
    """A write error must not raise — best-effort, the next reprovision retries."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(side_effect=OSError("disk full"))

    # Should not raise.
    await friday_identity.stage_paperclip_identity_for_agent(
        owner_id="user_abc",
        agent_id="agent-live",
        token="key",
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    # The single write was attempted (and its error swallowed).
    workspace.write_file_if_missing.assert_called_once()
