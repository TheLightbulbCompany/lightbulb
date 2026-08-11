"""Pure classification logic for the non-gateway adapter audit (no AWS)."""

from __future__ import annotations

import pytest

from scripts.audit_paperclip_adapter_types import classify_agents, classify_join_requests, is_dangerous


@pytest.mark.parametrize("adapter", ["openclaw_gateway", "hermes_gateway"])
def test_gateway_adapters_are_safe(adapter):
    assert not is_dangerous(adapter)


@pytest.mark.parametrize("adapter", ["process", "http", "claude_local", "codex_local", "cursor", None])
def test_host_exec_and_null_adapters_are_dangerous(adapter):
    assert is_dangerous(adapter)


def test_classify_agents_flags_only_dangerous():
    rows = [
        {"id": "a1", "company_id": "c1", "name": "Friday", "adapter_type": "openclaw_gateway"},
        {"id": "a2", "company_id": "c1", "name": "evil", "adapter_type": "process"},
        {"id": "a3", "company_id": "c2", "name": "hermes", "adapter_type": "hermes_gateway"},
        {"id": "a4", "company_id": "c2", "name": "shell", "adapter_type": None},
    ]
    ids = {f.row_id for f in classify_agents(rows)}
    assert ids == {"a2", "a4"}


def test_classify_join_requests_only_agent_type_and_dangerous():
    rows = [
        {"id": "j1", "company_id": "c1", "request_type": "human", "adapter_type": None, "status": "pending_approval"},
        {"id": "j2", "company_id": "c1", "request_type": "agent", "adapter_type": None, "status": "pending_approval"},
        {
            "id": "j3",
            "company_id": "c1",
            "request_type": "agent",
            "adapter_type": "openclaw_gateway",
            "status": "pending_approval",
        },
        {
            "id": "j4",
            "company_id": "c1",
            "request_type": "agent",
            "adapter_type": "process",
            "status": "pending_approval",
        },
    ]
    ids = {f.row_id for f in classify_join_requests(rows)}
    # human join ignored (no adapter surface); gateway agent ignored; null + process flagged.
    assert ids == {"j2", "j4"}
