"""Guard policy for core.services.runtime_rpc's browser runtime RPC endpoint.

``_guard_method`` is one synchronous gate that raises ``ApiProblem``: run-dispatching
methods are pointed at their product endpoint, and anything outside the allowlist of
methods something actually serves is refused.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest

from core.services.messaging import ApiProblem
from core.services import runtime_rpc as rpc


def _rejects(method: str) -> ApiProblem:
    with pytest.raises(ApiProblem) as exc_info:
        rpc._guard_method(method)
    return exc_info.value


# --- Run-dispatch refusal (chat has exactly one entrance) ------------------
#
# The generic RPC proxy used to forward *any* method to the per-user
# container -- including chat.send. That is a second door into chat: it skips
# gate_chat, mark_run_active, and the streaming UX. These pin the refusal.


@pytest.mark.parametrize(
    "method,expected_use",
    [
        ("chat.send", "/api/v1/messaging/rooms/{roomId}/messages"),
        ("sessions.send", "/api/v1/messaging/rooms/{roomId}/messages"),
        ("sessions.steer", "/api/v1/messaging/runs/{runId}/steer"),
        ("agent", "/api/v1/messaging/rooms/{roomId}/messages"),
    ],
)
def test_run_dispatching_method_is_refused(method, expected_use):
    err = _rejects(method)
    assert err.error.code == "INVALID_REQUEST"
    assert err.error.details["method"] == method
    assert err.error.details["use"] == expected_use
    assert method in err.error.message


def test_run_dispatch_refusal_set_is_small_and_principled():
    """Guard against the set silently growing into a maintained allowlist."""
    assert rpc._RUN_DISPATCH_METHODS == frozenset({"chat.send", "sessions.send", "sessions.steer", "agent"})


@pytest.mark.parametrize("method", ["chat.abort", "agent.wait", "tools.invoke", "cron.list"])
def test_run_adjacent_methods_are_denied_by_the_allowlist(method):
    """Not run-dispatching, and also not on the proxy allowlist -- refused,
    but without the run-dispatch ``use`` pointer (no client sends these; aborts
    ride the typed agent_abort message)."""
    err = _rejects(method)
    assert err.error.code == "INVALID_REQUEST"
    assert err.error.details["method"] == method
    assert "use" not in err.error.details


@pytest.mark.parametrize(
    "method",
    [
        "sessions.list",
        "sessions.patch",
        "sessions.search",
        "chat.history",
        "doctor.memory.status",
        "tools.catalog",
        "skills.install",
        "skills.proposals.inspect",
        "agent.identity.get",
        "browser.request",
        "channels.status",
        "skills.update",
        "skills.proposals.list",
        "provider.freeze",
    ],
)
def test_openclaw_methods_the_bridge_never_registered_are_refused(method):
    """The Hermes bridge answers these with UNAVAILABLE; refusing them here saves the invoke."""
    err = _rejects(method)
    assert err.error.code == "INVALID_REQUEST"
    assert err.error.details == {"method": method}


def test_allowlist_is_what_the_backend_or_bridge_actually_serves():
    """Backend-served (agents.*, config.*, models.list) plus bridge/server.py's RPC_METHODS.
    Growing this means the method got a real handler first."""
    assert rpc._PROXY_ALLOWED_METHODS == frozenset(
        {
            "agents.create",
            "agents.delete",
            "agents.list",
            "agents.update",
            "config.get",
            "config.patch",
            "health",
            "models.authStatus",
            "models.list",
            "skills.status",
        }
    )
    for method in rpc._PROXY_ALLOWED_METHODS:
        rpc._guard_method(method)  # must not raise


# --- Profile config is admin-only (same gate as routers/config.py) ----------


def _rpc_request(method: str):
    import uuid

    from core.services.messaging import RuntimeRpcRequest

    return RuntimeRpcRequest(request_id=uuid.uuid4(), method=method, params={})


@pytest.mark.parametrize("method", ["config.get", "config.patch"])
async def test_a_member_cannot_read_or_write_the_profile_config(monkeypatch, method):
    from fastapi import HTTPException

    from core.auth import AuthContext

    async def _must_not_run(**_kwargs):
        raise AssertionError("a member's config call reached execute_sync")

    monkeypatch.setattr(rpc, "execute_sync", _must_not_run)
    member = AuthContext(user_id="u_member", org_id="org_a", org_role="org:member")
    with pytest.raises(HTTPException) as exc_info:
        await rpc.execute(member, _rpc_request(method))
    assert exc_info.value.status_code == 403


@pytest.mark.parametrize("method", ["config.get", "config.patch", "agents.list"])
async def test_an_admin_and_a_member_reading_agents_are_admitted(monkeypatch, method):
    from core.auth import AuthContext

    async def _execute_sync(**kwargs):
        return {"method": kwargs["method"]}

    async def _touch(_owner_id):
        return None

    monkeypatch.setattr(rpc, "execute_sync", _execute_sync)
    monkeypatch.setattr(rpc.container_repo, "touch_last_interactive", _touch)
    role = "org:member" if method == "agents.list" else "org:admin"
    status, response = await rpc.execute(AuthContext(user_id="u1", org_id="org_a", org_role=role), _rpc_request(method))
    assert status == 200
    assert response.payload == {"method": method}


def test_config_set_whole_document_replace_is_not_served():
    """It skipped the raw-``model`` refusal config.patch applies, and nothing called it."""
    err = _rejects("config.set")
    assert err.error.code == "INVALID_REQUEST"
