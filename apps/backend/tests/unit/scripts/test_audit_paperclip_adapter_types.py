"""Pure classification logic for the non-gateway adapter audit (no AWS)."""

from __future__ import annotations

import pytest

from scripts.audit_paperclip_adapter_types import (
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


def test_classify_approvals_only_pending_hire_with_dangerous_payload():
    rows = [
        {"id": "p1", "company_id": "c1", "type": "hire_agent", "status": "pending", "adapter_type": None},
        {
            "id": "p2",
            "company_id": "c1",
            "type": "hire_agent",
            "status": "revision_requested",
            "adapter_type": "process",
        },
        {"id": "p3", "company_id": "c1", "type": "hire_agent", "status": "pending", "adapter_type": "openclaw_gateway"},
        {"id": "p4", "company_id": "c1", "type": "hire_agent", "status": "approved", "adapter_type": "process"},
        {"id": "p5", "company_id": "c1", "type": "budget_increase", "status": "pending", "adapter_type": None},
        # Activate branch: agentId set, adapterType deliberately absent — a legit
        # marketplace hire of an already-created (filtered) agent. Must NOT flag.
        {
            "id": "p6",
            "company_id": "c1",
            "type": "hire_agent",
            "status": "pending",
            "adapter_type": None,
            "agent_id": "ag_1",
        },
    ]
    found = classify_approvals(rows)
    # null + process CREATE-branch hires flagged; gateway/approved/non-hire/activate ignored.
    assert {f.row_id for f in found} == {"p1", "p2"}
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
