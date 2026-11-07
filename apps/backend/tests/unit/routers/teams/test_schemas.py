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
