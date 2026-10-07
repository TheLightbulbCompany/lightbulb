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
            "channels.status",
            "config.get",
            "config.patch",
            "config.set",
            "health",
            "models.authStatus",
            "models.list",
            "skills.proposals.list",
            "skills.status",
            "skills.update",
        }
    )
    for method in rpc._PROXY_ALLOWED_METHODS:
        rpc._guard_method(method)  # must not raise
