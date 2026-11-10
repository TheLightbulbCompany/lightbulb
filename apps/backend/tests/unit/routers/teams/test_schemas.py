import pytest
from pydantic import ValidationError

from routers.teams.schemas import PatchAgentBody


def test_patch_agent_only_allows_safe_fields():
    body = PatchAgentBody(name="renamed", title="New Title")
    assert body.name == "renamed"

    with pytest.raises(ValidationError):
        PatchAgentBody(adapterType="process")
