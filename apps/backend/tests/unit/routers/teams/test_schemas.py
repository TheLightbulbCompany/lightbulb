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


def test_patch_issue_accepts_hidden_at_for_hide_and_restore():
    """Hide/restore: hiddenAt must reach the wire. An ISO string hides the
    task (upstream drops hiddenAt-set rows from the list); an explicit null
    restores it. exclude_unset keeps the explicit null so a restore PATCH is
    not silently dropped."""
    from routers.teams.schemas import PatchIssueBody

    hidden = PatchIssueBody(hidden_at="2026-06-24T00:00:00Z")
    assert hidden.model_dump(exclude_unset=True, by_alias=True) == {"hiddenAt": "2026-06-24T00:00:00Z"}

    restored = PatchIssueBody.model_validate({"hiddenAt": None})
    assert restored.model_dump(exclude_unset=True, by_alias=True) == {"hiddenAt": None}


def test_create_issue_accepts_work_mode_and_status():
    """The composer can file a task straight into a status and pick an
    execution work mode — both forward camelCase to upstream's
    createIssueSchema (workMode default 'standard', status default 'backlog')."""
    from routers.teams.schemas import CreateIssueBody

    body = CreateIssueBody(title="ship it", status="todo", work_mode="planning")
    assert body.model_dump(exclude_none=True, by_alias=True) == {
        "title": "ship it",
        "status": "todo",
        "workMode": "planning",
    }


def test_create_issue_rejects_unknown_work_mode_and_status():
    from routers.teams.schemas import CreateIssueBody

    with pytest.raises(ValidationError):
        CreateIssueBody(title="x", work_mode="turbo")
    with pytest.raises(ValidationError):
        CreateIssueBody(title="x", status="frozen")


def test_patch_issue_accepts_work_mode():
    from routers.teams.schemas import PatchIssueBody

    body = PatchIssueBody(work_mode="ask")
    assert body.model_dump(exclude_unset=True, by_alias=True) == {"workMode": "ask"}


def test_patch_issue_accepts_parent_user_assignee_and_labels():
    from routers.teams.schemas import PatchIssueBody

    body = PatchIssueBody(parent_id="par_1", assignee_user_id="usr_1", label_ids=["lbl_1", "lbl_2"])
    assert body.model_dump(exclude_unset=True, by_alias=True) == {
        "parentId": "par_1",
        "assigneeUserId": "usr_1",
        "labelIds": ["lbl_1", "lbl_2"],
    }


def test_patch_issue_forwards_execution_policy_monitor():
    """The monitor schedule rides on executionPolicy. The alias generator only
    renames model fields, so the nested monitor dict's already-camelCase keys
    (nextCheckAt) pass through verbatim — upstream updateIssueSchema validates
    the inner shape."""
    from routers.teams.schemas import PatchIssueBody

    body = PatchIssueBody(execution_policy={"monitor": {"nextCheckAt": "2026-07-01T09:00:00Z"}})
    assert body.model_dump(exclude_unset=True, by_alias=True) == {
        "executionPolicy": {"monitor": {"nextCheckAt": "2026-07-01T09:00:00Z"}}
    }


def test_patch_issue_forwards_null_execution_policy_to_clear_monitor():
    """An explicit null executionPolicy clears the monitor. exclude_unset keeps
    the explicit null so the clear PATCH is not silently dropped."""
    from routers.teams.schemas import PatchIssueBody

    body = PatchIssueBody.model_validate({"executionPolicy": None})
    assert body.model_dump(exclude_unset=True, by_alias=True) == {"executionPolicy": None}


def test_create_goal_accepts_level_and_status():
    from routers.teams.schemas import CreateGoalBody

    body = CreateGoalBody(title="grow revenue", level="team", status="active")
    assert body.model_dump(exclude_none=True, by_alias=True) == {
        "title": "grow revenue",
        "level": "team",
        "status": "active",
    }
    with pytest.raises(ValidationError):
        CreateGoalBody(title="x", level="galactic")


def test_patch_project_accepts_status_and_goal_ids():
    from routers.teams.schemas import PatchProjectBody

    body = PatchProjectBody(status="in_progress", goal_ids=["goal_1"])
    assert body.model_dump(exclude_unset=True, by_alias=True) == {
        "status": "in_progress",
        "goalIds": ["goal_1"],
    }
    with pytest.raises(ValidationError):
        PatchProjectBody(status="frozen")


def test_patch_project_accepts_lead_agent_id():
    from routers.teams.schemas import PatchProjectBody

    body = PatchProjectBody(lead_agent_id="a" * 8)
    assert body.model_dump(exclude_unset=True, by_alias=True) == {
        "leadAgentId": "a" * 8,
    }
