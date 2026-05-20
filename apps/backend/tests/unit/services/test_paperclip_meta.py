"""Tests for paperclip_meta — read/write of params.paperclip on agent entries."""

import pytest

from core.services.paperclip_meta import (
    PAPERCLIP_AGENT_ROLES,
    PaperclipMeta,
    read_from_agent_entry,
    write_to_agent_entry,
)


def test_paperclip_agent_roles_matches_upstream_enum():
    # Mirrors paperclip/packages/shared/src/constants.ts AGENT_ROLES.
    # If this fails, Paperclip upstream changed the enum and the
    # constant needs to be re-synced.
    assert PAPERCLIP_AGENT_ROLES == (
        "ceo",
        "cto",
        "cmo",
        "cfo",
        "security",
        "engineer",
        "designer",
        "pm",
        "qa",
        "devops",
        "researcher",
        "general",
    )


def test_read_from_missing_params_returns_default_meta():
    """Agent entry without ``params`` returns a meta with role=general."""
    meta = read_from_agent_entry({"id": "agent_x", "name": "X"})
    assert meta.role == "general"
    assert meta.title is None
    assert meta.capabilities is None
    assert meta.icon is None


def test_read_from_params_without_paperclip_block_returns_default():
    """Agent entry with ``params`` but no ``paperclip`` sub-block returns
    default meta — the params bag is used by multiple consumers."""
    meta = read_from_agent_entry({"id": "x", "params": {"other_integration": {"k": "v"}}})
    assert meta.role == "general"
    assert meta.title is None


def test_read_from_full_paperclip_block_extracts_all_fields():
    entry = {
        "id": "ray",
        "name": "Ray",
        "params": {
            "paperclip": {
                "role": "general",
                "title": "Restaurant booking specialist",
                "capabilities": "Books reservations on Resy.",
                "icon": "calendar",
            }
        },
    }
    meta = read_from_agent_entry(entry)
    assert meta.role == "general"
    assert meta.title == "Restaurant booking specialist"
    assert meta.capabilities == "Books reservations on Resy."
    assert meta.icon == "calendar"


def test_read_with_invalid_role_coerces_to_general():
    """A role string outside the closed enum is coerced to 'general'
    rather than raising — the agent entry is a free-form bag and
    upstream Paperclip would 422 on an invalid role, so we sanitize at
    the read boundary."""
    entry = {"id": "x", "params": {"paperclip": {"role": "recruiter"}}}
    meta = read_from_agent_entry(entry)
    assert meta.role == "general"


def test_write_creates_params_paperclip_block_when_absent():
    """Writing onto an entry without params builds the nested structure
    without mutating any other keys."""
    entry = {"id": "x", "name": "X"}
    meta = PaperclipMeta(role="engineer", title="Backend dev", capabilities="Writes Python.", icon=None)
    out = write_to_agent_entry(entry, meta)
    assert out is entry  # writes in place AND returns the entry
    assert entry["params"]["paperclip"]["role"] == "engineer"
    assert entry["params"]["paperclip"]["title"] == "Backend dev"
    assert entry["params"]["paperclip"]["capabilities"] == "Writes Python."
    # icon is None → must NOT serialize as JSON null in the params bag
    # (keeps the params dict small and matches "unset" semantics).
    assert "icon" not in entry["params"]["paperclip"]
    # Original keys preserved
    assert entry["id"] == "x"
    assert entry["name"] == "X"


def test_write_merges_with_existing_paperclip_block():
    """Writing a partial meta (only some fields set) preserves prior
    values for other fields — supports incremental edit flows where
    the UI sends only what changed."""
    entry = {
        "id": "x",
        "params": {"paperclip": {"role": "engineer", "title": "Original title", "capabilities": "Original caps."}},
    }
    meta = PaperclipMeta(role="engineer", title="New title", capabilities=None, icon=None)
    write_to_agent_entry(entry, meta)
    assert entry["params"]["paperclip"]["title"] == "New title"
    # capabilities=None means "no change", not "clear". Prior value kept.
    assert entry["params"]["paperclip"]["capabilities"] == "Original caps."


def test_write_preserves_other_params_subkeys():
    """The params bag is shared with other integrations — we MUST not
    blow away unrelated subkeys."""
    entry = {
        "id": "x",
        "params": {"other_integration": {"k": "v"}, "paperclip": {"role": "general"}},
    }
    write_to_agent_entry(entry, PaperclipMeta(role="cmo", title="Marketing lead", capabilities=None, icon=None))
    assert entry["params"]["other_integration"] == {"k": "v"}
    assert entry["params"]["paperclip"]["role"] == "cmo"


def test_write_invalid_role_raises():
    """Writing — unlike reading — is strict. Catching a typo at write
    time prevents bad data from getting into openclaw.json."""
    with pytest.raises(ValueError, match="role"):
        write_to_agent_entry({"id": "x"}, PaperclipMeta(role="bogus", title=None, capabilities=None, icon=None))
