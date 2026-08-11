"""Pure classification logic for the non-gateway adapter audit (no AWS)."""

from __future__ import annotations

import pytest

from scripts.audit_paperclip_adapter_types import (
    _is_activate_branch,
    classify_agents,
    classify_approvals,
    classify_join_requests,
    is_dangerous,
)


@pytest.mark.parametrize("adapter", ["openclaw_gateway", "hermes_gateway"])
def test_gateway_adapters_are_safe(adapter):
    assert not is_dangerous(adapter)


@pytest.mark.parametrize("adapter", ["process", "http", "claude_local", "codex_local", "cursor", None])
def test_host_exec_and_null_adapters_are_dangerous(adapter):
    assert is_dangerous(adapter)


def test_classify_agents_flags_only_dangerous_and_is_remediable():
    rows = [
        {"id": "a1", "company_id": "c1", "name": "Friday", "adapter_type": "openclaw_gateway"},
        {"id": "a2", "company_id": "c1", "name": "evil", "adapter_type": "process"},
        {"id": "a3", "company_id": "c2", "name": "hermes", "adapter_type": "hermes_gateway"},
        {"id": "a4", "company_id": "c2", "name": "shell", "adapter_type": None},
    ]
    found = classify_agents(rows)
    assert {f.row_id for f in found} == {"a2", "a4"}
    assert all(f.remediable for f in found)  # wakeup can't be blocked -> we quarantine


@pytest.mark.parametrize(
    "agent_id,agent_id_type,activate",
    [
        ("ag_1", "string", True),  # truthy string -> activate (safe)
        ("", "string", False),  # empty string -> create branch
        (None, None, False),  # absent -> create branch
        ("false", "boolean", False),  # JSON false renders as "false" text
        ("0", "number", False),  # JSON 0 renders as "0" text
        ("null", "null", False),  # JSON null
    ],
)
def test_is_activate_branch_matches_js_truthiness(agent_id, agent_id_type, activate):
    assert _is_activate_branch(agent_id, agent_id_type) is activate


def _hire(id_, adapter_type, *, status="pending", agent_id=None, agent_id_type=None):
    return {
        "id": id_,
        "company_id": "c1",
        "type": "hire_agent",
        "status": status,
        "adapter_type": adapter_type,
        "agent_id": agent_id,
        "agent_id_type": agent_id_type,
    }


def test_classify_approvals_only_pending_hire_with_dangerous_payload():
    rows = [
        _hire("p1", None),  # null adapter, create branch -> flag
        _hire("p2", "process", status="revision_requested"),  # flag
        _hire("p3", "openclaw_gateway"),  # safe adapter -> ignore
        _hire("p4", "process", status="approved"),  # wrong status -> ignore
        {"id": "p5", "company_id": "c1", "type": "budget_increase", "status": "pending", "adapter_type": None},
        _hire("p6", None, agent_id="ag_1", agent_id_type="string"),  # activate branch -> ignore
        _hire("p7", None, agent_id="", agent_id_type="string"),  # empty string -> flag
        _hire("p8", None, agent_id="false", agent_id_type="boolean"),  # JSON false -> flag
        _hire("p9", None, agent_id="0", agent_id_type="number"),  # JSON 0 -> flag
    ]
    found = classify_approvals(rows)
    # every falsy-agentId create-branch hire flagged; safe-adapter / wrong-status /
    # non-hire / truthy-string-activate ignored.
    assert {f.row_id for f in found} == {"p1", "p2", "p7", "p8", "p9"}
    assert all(f.remediable for f in found)


def test_classify_join_requests_dangerous_but_route_blocked():
    rows = [
        {"id": "j1", "company_id": "c1", "request_type": "human", "adapter_type": None, "status": "pending_approval"},
        {"id": "j2", "company_id": "c1", "request_type": "agent", "adapter_type": None, "status": "pending_approval"},
        {
            "id": "j3",
            "company_id": "c1",
            "request_type": "agent",
            "adapter_type": "process",
            "status": "pending_approval",
        },
    ]
    found = classify_join_requests(rows)
    assert {f.row_id for f in found} == {"j2", "j3"}
    assert all(not f.remediable for f in found)  # /approve is blocked by #1673 -> audit only
