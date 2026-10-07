"""_sanitize_agent_create never requires or derives `workspace`.

An ECS agent's EFS path is assigned downstream, from the Paperclip agent's own
id (paperclip_openclaw_pairing's `workspace=f"/home/node/.openclaw/workspace-{pc_uuid}"`),
never from this client-supplied param; a Hermes/AgentCore agent has no EFS
path at all. So `_sanitize_agent_create` just has to make sure a client can't
smuggle a `workspace` value through — every OTHER existing validation
(name length, role/model shape, the params.paperclip allowlist + length cap)
must keep behaving exactly as before.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest

from core.services.messaging import ApiProblem
from core.services.runtime_rpc import _sanitize_agent_create


def test_workspace_is_never_forwarded_when_absent():
    out = _sanitize_agent_create({"name": "Ada"})
    assert "workspace" not in out
    assert out == {"name": "Ada"}


def test_a_client_supplied_workspace_is_dropped_not_trusted():
    out = _sanitize_agent_create({"name": "Ada", "workspace": "/etc"})
    assert "workspace" not in out
    assert out == {"name": "Ada"}


def test_name_validation_is_unchanged():
    with pytest.raises(ApiProblem) as exc:
        _sanitize_agent_create({"name": "   "})
    assert exc.value.error.code == "INVALID_REQUEST"
    with pytest.raises(ApiProblem):
        _sanitize_agent_create({"name": "x" * 81})
    assert _sanitize_agent_create({"name": " Ada "}) == {"name": "Ada"}


def test_role_and_model_validation_is_unchanged():
    with pytest.raises(ApiProblem):
        _sanitize_agent_create({"name": "Ada", "role": 5})
    with pytest.raises(ApiProblem):
        _sanitize_agent_create({"name": "Ada", "model": "x" * 257})
    out = _sanitize_agent_create({"name": "Ada", "role": "engineer", "model": "sonnet"})
    assert out == {"name": "Ada", "role": "engineer", "model": "sonnet"}


def test_paperclip_params_allowlist_and_cap_are_unchanged():
    with pytest.raises(ApiProblem):
        _sanitize_agent_create({"name": "Ada", "params": {"unexpected": True}})
    with pytest.raises(ApiProblem):
        _sanitize_agent_create({"name": "Ada", "params": {"paperclip": {"role": "x" * 4001}}})
    out = _sanitize_agent_create(
        {"name": "Ada", "params": {"paperclip": {"role": " engineer "}}},
    )
    assert out["params"] == {"paperclip": {"role": "engineer"}}
