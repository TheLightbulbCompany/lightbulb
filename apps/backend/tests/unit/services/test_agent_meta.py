"""agent_meta: reading params.paperclip on agent entries."""

from core.services.agent_meta import AgentMeta, read_from_agent_entry, resolve_create_agent_fields


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
    """Happy path — a fully-populated paperclip sub-block round-trips
    into AgentMeta with every field set."""
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


def test_resolve_create_agent_fields_treats_blank_role_as_unset():
    """Codex P2: empty-string role would forward as "" and 422 against
    Paperclip's AGENT_ROLES enum. Coerce blank/whitespace to the meta
    fallback (or "general")."""

    # Empty string role + no meta → "general" default
    fields = resolve_create_agent_fields(role="", meta=None)
    assert fields["role"] == "general"

    # Whitespace-only role + meta with engineer → "engineer" (meta wins)
    fields = resolve_create_agent_fields(role="   ", meta=AgentMeta(role="engineer"))
    assert fields["role"] == "engineer"
