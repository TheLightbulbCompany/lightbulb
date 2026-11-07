"""reset-session and clear-error answer the agent row (no runtime session or error state since Paperclip runs went
away; behaviour in tests/unit/services/test_agent_registry_writes.py). The body still forbids smuggled fields."""

import pydantic
import pytest

from routers.teams.schemas import ResetAgentSessionBody


def test_reset_agent_session_body_forbids_extra_and_aliases():
    body = ResetAgentSessionBody(task_key="t1")
    assert body.model_dump(by_alias=True, exclude_none=True) == {"taskKey": "t1"}
    with pytest.raises(pydantic.ValidationError):
        ResetAgentSessionBody(task_key="t1", adapterType="openclaw_gateway")
