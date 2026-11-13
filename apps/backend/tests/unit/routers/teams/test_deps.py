import dataclasses


from routers.teams.deps import (
    TeamsContext,
    envelope_list,
)


# ─── envelope_list ────────────────────────────────────────────────────────


def test_envelope_list_wraps_bare_array():
    rows = [{"id": "a"}, {"id": "b"}]
    assert envelope_list(rows, "goals") == {"goals": rows}


def test_envelope_list_passes_through_matching_envelope():
    upstream = {"goals": [{"id": "a"}]}
    assert envelope_list(upstream, "goals") == {"goals": [{"id": "a"}]}


def test_envelope_list_renames_items_envelope_to_target_key():
    upstream = {"items": [{"id": "a"}]}
    assert envelope_list(upstream, "goals") == {"goals": [{"id": "a"}]}


def test_envelope_list_drops_non_dict_rows():
    rows = [{"id": "a"}, "stray-string", None, {"id": "b"}]
    assert envelope_list(rows, "x") == {"x": [{"id": "a"}, {"id": "b"}]}


def test_envelope_list_handles_garbage_response():
    assert envelope_list(None, "x") == {"x": []}
    assert envelope_list("not-a-list", "x") == {"x": []}
    assert envelope_list(42, "x") == {"x": []}


def test_teams_ctx_has_no_session():
    """Phase 1 task 8b.8: no Paperclip session, user or company rides on the context."""
    assert [f.name for f in dataclasses.fields(TeamsContext)] == ["user_id", "org_id", "owner_id"]
