import pytest
from pydantic import ValidationError

from routers.teams.schemas import CreateAgentBody, PatchAgentBody


def test_create_agent_accepts_minimal_payload():
    body = CreateAgentBody(name="alice", role="ceo")
    assert body.name == "alice"
    assert body.role == "ceo"
    assert body.title is None


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "adapterType",
        "adapterConfig",
        "url",
        "headers",
        "authToken",
        "password",
        "deviceToken",
    ],
)
def test_create_agent_rejects_forbidden_fields(forbidden_field):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CreateAgentBody(name="x", role="r", **{forbidden_field: "evil"})


def test_create_agent_rejects_empty_name():
    with pytest.raises(ValidationError):
        CreateAgentBody(name="", role="r")


def test_patch_agent_only_allows_safe_fields():
    body = PatchAgentBody(name="renamed", title="New Title")
    assert body.name == "renamed"

    with pytest.raises(ValidationError):
        PatchAgentBody(adapterType="process")


def test_strict_base_emits_camel_case_via_by_alias():
    """The _Strict base is the BFF↔Paperclip wire-shape contract: snake_case
    Python attributes, camelCase JSON keys. Pydantic must dump camelCase
    when by_alias=True is passed and accept either name on input."""
    from routers.teams.schemas import PatchIssueBody

    # Construction with snake_case attribute names (internal Python callers).
    body = PatchIssueBody(assignee_agent_id="agt_1", project_id="proj_1")
    dumped = body.model_dump(exclude_unset=True, by_alias=True)
    assert dumped == {"assigneeAgentId": "agt_1", "projectId": "proj_1"}


def test_strict_base_accepts_camel_case_input():
    """Frontend posts camelCase JSON; the BFF must parse it without 422."""
    from routers.teams.schemas import PatchIssueBody

    body = PatchIssueBody.model_validate({"assigneeAgentId": "agt_1"})
    assert body.assignee_agent_id == "agt_1"


def test_strict_base_still_rejects_unknown_fields():
    """alias_generator must not weaken extra='forbid' — adapterType
    smuggling must still 422."""
    from routers.teams.schemas import PatchIssueBody

    with pytest.raises(ValidationError):
        PatchIssueBody.model_validate({"adapterType": "evil"})
