"""Unit tests for the Paperclip proxy adapter-surface request filter.

The filter is the sole gate stopping a tenant from persisting a non-gateway
adapter (host shell / SSRF) or reaching an adapter-management / host-exec route
on the shared Paperclip task. See docs/audit-2026-05-02-paperclip-routes.md.

Two directions are tested with equal weight:
  * it BLOCKS the RCE + the dangerous management/exec/auth/instance routes;
  * it does NOT brick the Teams Inbox — the read + lifecycle routes the UI
    actually uses must pass through.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from routers.paperclip_proxy import (
    _ALLOWED_ADAPTER_TYPES,
    _assert_paperclip_request_allowed,
    _filter_request_headers,
    _top_level_adapter_type,
)


class _FakeRequest:
    """Minimal stand-in exposing ``.headers`` (mirrors the sibling test file)."""

    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def _blocked(method: str, path: str, body: bytes = b"") -> bool:
    try:
        _assert_paperclip_request_allowed(method, path, body)
        return False
    except HTTPException as exc:
        assert exc.status_code == 403
        return True


# ---------------------------------------------------------------
# The RCE — non-gateway adapter on create / hire
# ---------------------------------------------------------------


@pytest.mark.parametrize("adapter", ["process", "http", "claude_local", "codex_local", "hermes_gateway"])
def test_create_agent_with_non_gateway_adapter_is_blocked(adapter):
    body = json.dumps({"name": "x", "adapterType": adapter}).encode()
    assert _blocked("POST", "api/companies/c1/agents", body)


def test_create_agent_missing_adapter_type_is_blocked():
    # Absent adapterType defaults to "process" upstream — must be rejected.
    assert _blocked("POST", "api/companies/c1/agents", json.dumps({"name": "x"}).encode())
    assert _blocked("POST", "api/companies/c1/agents", b"")


def test_agent_hire_with_non_gateway_adapter_is_blocked():
    body = json.dumps({"adapterType": "process", "adapterConfig": {"command": "/bin/sh"}}).encode()
    assert _blocked("POST", "api/companies/c1/agent-hires", body)


def test_create_agent_with_gateway_adapter_is_allowed():
    body = json.dumps({"name": "x", "adapterType": "openclaw_gateway"}).encode()
    assert not _blocked("POST", "api/companies/c1/agents", body)


def test_agent_hire_with_gateway_adapter_is_allowed():
    body = json.dumps({"adapterType": "openclaw_gateway"}).encode()
    assert not _blocked("POST", "api/companies/c1/agent-hires", body)


def test_present_but_non_gateway_adapter_blocked_on_any_write_route():
    # An update route that never appears in the create list is still caught by
    # the "present ⇒ must be allowlisted" rule.
    body = json.dumps({"adapterType": "process"}).encode()
    assert _blocked("PATCH", "api/agents/a1", body)


# ---------------------------------------------------------------
# Dangerous management / exec / auth / instance routes
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "api/adapters/install"),  # npm install on host
        ("DELETE", "api/adapters/process"),  # npm uninstall on host
        ("GET", "api/adapters"),  # inventory disclosure
        ("POST", "api/plugins/install"),  # npm install on host
        ("POST", "api/plugins/p1/config"),  # plugin config eval
        ("GET", "_plugins/p1/ui/main.js"),  # plugin-served JS
        ("POST", "api/auth/sign-up/email"),  # Paperclip's own auth
        ("GET", "api/auth/get-session"),
        ("GET", "api/instance/scheduler-heartbeats"),  # cross-tenant leak
        ("GET", "api/instance/settings/general"),
        ("GET", "api/admin/users"),
        ("POST", "api/companies"),  # company create
        ("DELETE", "api/companies/c1"),  # company delete
        ("POST", "api/companies/import"),  # portability import
        ("POST", "api/companies/c1/imports/apply"),
        ("POST", "api/companies/c1/environments"),  # SSH driver create
        ("POST", "api/companies/c1/environments/probe-config"),  # SSRF probe
        ("POST", "api/environments/e1/probe"),
        ("POST", "api/agents/a1/claude-login"),  # claude_local subprocess
        ("GET", "api/invites/tok/test-resolution"),  # outbound HEAD
    ],
)
def test_dangerous_routes_are_blocked(method, path):
    assert _blocked(method, path)


def test_company_scoped_adapter_route_blocked_for_non_gateway_type():
    assert _blocked("GET", "api/companies/c1/adapters/process/models")
    assert _blocked("GET", "api/companies/c1/adapters/codex_local/detect-model")


def test_company_scoped_adapter_route_allowed_for_gateway_type():
    assert not _blocked("GET", "api/companies/c1/adapters/openclaw_gateway/models")


def test_leading_slash_is_tolerated():
    # The proxy passes ``path`` without a leading slash, but never rely on that.
    assert _blocked("POST", "/api/adapters/install")


# ---------------------------------------------------------------
# Must NOT brick the Teams Inbox — real read + lifecycle traffic
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "api/companies/c1/agents", b""),  # list agents
        ("GET", "api/agents/a1", b""),  # agent detail
        ("GET", "api/companies/c1/issues", b""),  # issues list
        ("GET", "api/companies/c1/environments", b""),  # env read (probe blocked, read fine)
        ("PATCH", "api/companies/c1", b'{"name":"New"}'),  # company update
        ("GET", "api/companies/c1", b""),  # company read
        ("POST", "api/agents/a1/wakeup", b'{"source":"ui"}'),  # no adapterType
        ("POST", "api/agents/a1/pause", b""),
        ("POST", "api/approvals/ap1/approve", b'{"decisionNote":"ok"}'),
        ("POST", "api/companies/c1/issues", b'{"title":"bug"}'),  # issue create, no adapter
        ("PATCH", "api/agents/a1/permissions", b'{"canAssignTasks":true}'),
        ("GET", "api/companies/c1/threads", b""),
    ],
)
def test_teams_inbox_routes_pass_through(method, path, body):
    assert not _blocked(method, path, body)


def test_non_json_body_on_write_route_passes():
    # A binary/multipart body carries no forgeable adapterType.
    assert not _blocked("POST", "api/agents/a1/instructions-bundle/file", b"\x00\x01\x02")


# ---------------------------------------------------------------
# _top_level_adapter_type helper
# ---------------------------------------------------------------


def test_top_level_adapter_type_reads_string():
    assert _top_level_adapter_type(b'{"adapterType":"openclaw_gateway"}') == "openclaw_gateway"


@pytest.mark.parametrize(
    "body",
    [b"", b"{}", b'{"other":1}', b"not-json", b'["adapterType"]', b'{"adapterType":123}'],
)
def test_top_level_adapter_type_returns_none(body):
    assert _top_level_adapter_type(body) is None


def test_allowlist_is_gateway_only():
    # Guard against an accidental widening of the allowlist.
    assert _ALLOWED_ADAPTER_TYPES == frozenset({"openclaw_gateway"})


# ---------------------------------------------------------------
# x-paperclip-* header stripping (SEC-3)
# ---------------------------------------------------------------


def test_paperclip_trusted_headers_are_stripped():
    req = _FakeRequest(
        headers={
            "User-Agent": "test/1.0",
            "X-Paperclip-Cloud-User-Id": "victim",
            "x-paperclip-cloud-stack-role": "owner",
            "X-Paperclip-Run-Id": "forged",
            "X-Forwarded-Host": "company.isol8.co",
        }
    )
    out = _filter_request_headers(req)
    assert "User-Agent" in out
    assert "X-Forwarded-Host" in out
    assert not any(k.lower().startswith("x-paperclip-") for k in out)
