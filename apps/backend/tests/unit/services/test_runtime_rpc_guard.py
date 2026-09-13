"""Guard policy for core.services.runtime_rpc's browser runtime RPC endpoint.

This is "moved intact" (per the module's own docstring) from the retired WS
chat socket's per-method interceptors: ``routers/websocket_chat``'s
``_guard_seeded_delete``, ``_guard_session_key_ownership`` and
``_process_rpc_background``'s run-dispatch refusal, replaced by the
native-multiplayer cutover. The calling convention changed along with the
move: the old guards consumed a WS frame and hand-wrote an error payload over
``management_api.send_message``; ``_guard_method`` is now a single
synchronous gate that raises ``ApiProblem`` (transport-independent — the HTTP
runtime-RPC route turns it into a response, same as any other route).
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest

from core.services.messaging import ApiProblem
from core.services import runtime_rpc as rpc

_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def _rejects(method: str, params: dict, user_id: str = "user_A") -> ApiProblem:
    with pytest.raises(ApiProblem) as exc_info:
        rpc._guard_method(method, params, user_id)
    return exc_info.value


def _allows(method: str, params: dict, user_id: str = "user_A") -> None:
    rpc._guard_method(method, params, user_id)  # must not raise


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
    err = _rejects(method, {"agentId": "luna", "message": "hi"})
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
    err = _rejects(method, {"runId": "r1"})
    assert err.error.code == "INVALID_REQUEST"
    assert err.error.details["method"] == method
    assert "use" not in err.error.details


def test_sessions_reset_was_retired_not_merged_into_patch():
    """sessions.reset is gone from the proxy allowlist entirely (not folded
    into sessions.patch) -- confirm it is refused outright, not silently
    treated as an ownership-guarded alias."""
    err = _rejects("sessions.reset", {"key": f"agent:agentX:user_A:{_UUID}"})
    assert err.error.code == "INVALID_REQUEST"
    assert "use" not in err.error.details


# --- permissionMode: privilege-escalation guard -----------------------------
#
# `sessions.patch {permissionMode}` must never reach the container. This is a
# privilege-escalation vector the session-key ownership guard structurally
# cannot catch, because the caller patches THEIR OWN session -- every
# ownership check passes. OpenClaw maps the field straight onto core-tool
# policy (fork src/agents/session-permission-exec-mode.ts): full -> execMode
# "full", bypassHostApprovalFloors true. sessions.patch requires
# operator.admin to write it, and our proxy supplies exactly that on every
# caller's behalf, so on a shared per-owner container any member could hand-
# craft one frame and get exec outside their workspace with approval floors
# bypassed. A blanket reject on the FIELD (not a value blocklist) costs
# nothing.

_OWN_KEY = f"agent:agent_a:user_1:{_UUID}"


def test_permission_mode_rejected_on_the_callers_own_session():
    """The ownership check passes -- that is precisely the problem."""
    assert rpc._user_owns_session_key(_OWN_KEY, "user_1") is True
    err = _rejects("sessions.patch", {"key": _OWN_KEY, "permissionMode": "full"}, user_id="user_1")
    assert err.error.code == "INVALID_REQUEST"
    assert err.error.details["fields"] == ["permissionMode"]


@pytest.mark.parametrize("mode", ["full", "workspace", "guarded", "read-only", "anything"])
def test_every_permission_mode_value_is_rejected(mode):
    """Reject the FIELD, not a blocklist of values -- the enum is upstream's
    to change, and a value-based guard would go quietly stale."""
    err = _rejects("sessions.patch", {"key": _OWN_KEY, "permissionMode": mode}, user_id="user_1")
    assert err.error.code == "INVALID_REQUEST"


def test_ordinary_sessions_patch_still_works():
    """Renaming your own session is the common path and must not regress."""
    _allows("sessions.patch", {"key": _OWN_KEY, "label": "Q3 pricing"}, user_id="user_1")


def test_a_foreign_key_is_still_rejected_by_the_ownership_check():
    foreign = "agent:agent_a:user_2:22222222-2222-4222-8222-222222222222"
    err = _rejects("sessions.patch", {"key": foreign, "label": "nice try"}, user_id="user_1")
    assert err.error.code == "ROOM_FORBIDDEN"


def test_permission_mode_rejected_even_with_a_foreign_key():
    """Both guards fire; whichever runs first, the request must not slip
    through. The forbidden-param check runs before the ownership check."""
    foreign = "agent:agent_a:user_2:22222222-2222-4222-8222-222222222222"
    err = _rejects("sessions.patch", {"key": foreign, "permissionMode": "full"}, user_id="user_1")
    assert err.error.code == "INVALID_REQUEST"


# --- Cross-tenant IDOR guard on session-key-bearing RPCs --------------------


@pytest.mark.parametrize("method", ["sessions.patch", "sessions.delete"])
def test_member_rejected_mutating_another_users_key(method):
    # Member "user_A" cannot patch/delete "user_B"'s session on the shared
    # per-owner container.
    err = _rejects(method, {"key": f"agent:agentX:user_B:{_UUID}"})
    assert err.error.code == "ROOM_FORBIDDEN"
    assert "access" in err.error.message.lower()


@pytest.mark.parametrize(
    "key",
    [
        # Real OpenClaw key shapes (agent:<agentId>:...) for paperclip/cron
        # sessions -- see session_keys.parse_session_key. These live on
        # the shared per-owner container but are never owned by a /chat
        # member, so the ownership guard must reject a member's attempt to
        # mutate them.
        "agent:agentX:paperclip",
        f"agent:agentX:paperclip:issue:{_UUID}",
        f"agent:agentX:cron:{_UUID}",
    ],
)
def test_member_rejected_mutating_paperclip_or_cron_key(key):
    err = _rejects("sessions.delete", {"key": key})
    assert err.error.code == "ROOM_FORBIDDEN"


def test_chat_history_rejected_for_another_users_key():
    # chat.history uses `sessionKey`, not `key`. Reading another member's
    # transcript is the same IDOR class.
    err = _rejects("chat.history", {"sessionKey": f"agent:agentX:user_B:{_UUID}"})
    assert err.error.code == "ROOM_FORBIDDEN"


@pytest.mark.parametrize("method", ["sessions.patch", "sessions.delete"])
def test_member_allowed_own_named_key(method):
    # Owner of a 4-part named session may mutate it.
    _allows(method, {"key": f"agent:agentX:user_A:{_UUID}"})


def test_member_allowed_own_general_key():
    # Legacy 3-part "General" key for the caller is owned.
    _allows("sessions.patch", {"key": "agent:agentX:user_A"})


def test_prefix_boundary_blocks_user_10_masquerade():
    # user_1 must NOT be able to touch user_10's key (bare startswith would
    # false-positive; the `:` boundary check prevents it).
    err = _rejects("sessions.delete", {"key": f"agent:agentX:user_10:{_UUID}"}, user_id="user_1")
    assert err.error.code == "ROOM_FORBIDDEN"


def test_case_insensitive_own_key_allowed():
    # OpenClaw lowercases the whole key; the mixed-case Clerk id must still
    # resolve as owned.
    _allows("sessions.patch", {"key": f"agent:agentx:user_abc:{_UUID}"}, user_id="User_Abc")


def test_keyless_method_is_allowed():
    # Methods outside the key-bearing set are never touched by this check.
    _allows("sessions.list", {})


def test_session_method_without_key_param_is_allowed():
    # A sessions.patch with no `key` param carries nothing to forge -- leave
    # it to the container to reject; the guard does not block it.
    _allows("sessions.patch", {"execNode": "dev-1"})


def test_user_owns_session_key_predicate():
    assert rpc._user_owns_session_key(f"agent:x:user_A:{_UUID}", "user_A") is True
    assert rpc._user_owns_session_key("agent:x:user_A", "user_A") is True
    assert rpc._user_owns_session_key(f"agent:x:user_B:{_UUID}", "user_A") is False
    assert rpc._user_owns_session_key(f"agent:x:user_10:{_UUID}", "user_1") is False
    assert rpc._user_owns_session_key(f"paperclip:x:{_UUID}", "user_A") is False
    assert rpc._user_owns_session_key("", "user_A") is False
    assert rpc._user_owns_session_key(None, "user_A") is False


@pytest.mark.parametrize(
    "method,params",
    [
        ("sessions.rewind", {"sessionKey": f"agent:agentX:user_B:{_UUID}", "entryId": "e1"}),
        ("sessions.branches.list", {"sessionKey": f"agent:agentX:user_B:{_UUID}"}),
        ("sessions.branches.switch", {"sessionKey": f"agent:agentX:user_B:{_UUID}", "leafEntryId": "l1"}),
    ],
)
def test_rewind_and_branch_methods_rejected_for_another_users_key(method, params):
    # OpenClaw 8.1's transcript-DAG RPCs take a client-supplied sessionKey.
    # sessions.rewind and sessions.branches.switch DESTRUCTIVELY repoint the
    # transcript, and sessions.branches.list leaks another member's branch
    # headlines -- the same cross-tenant IDOR class as chat.history.
    err = _rejects(method, params)
    assert err.error.code == "ROOM_FORBIDDEN"


@pytest.mark.parametrize("method", ["sessions.rewind", "sessions.branches.list", "sessions.branches.switch"])
def test_rewind_and_branch_methods_allowed_on_own_key(method):
    _allows(method, {"sessionKey": f"agent:agentX:user_A:{_UUID}"})


# --- OpenClaw 8.1 sessions.search -------------------------------------------


def test_search_rejected_when_any_listed_key_is_foreign():
    # One foreign key in the list is enough: the container would index that
    # member's transcript for the caller.
    err = _rejects(
        "sessions.search",
        {
            "agentId": "agentX",
            "query": "salary",
            "sessionKeys": [f"agent:agentX:user_A:{_UUID}", f"agent:agentX:user_B:{_UUID}"],
        },
    )
    assert err.error.code == "ROOM_FORBIDDEN"
    assert "access" in err.error.message.lower()


def test_search_allowed_for_own_keys_and_keyless():
    _allows(
        "sessions.search",
        {"agentId": "agentX", "query": "q", "sessionKeys": [f"agent:agentX:user_A:{_UUID}"]},
    )
    # Key-less search is scoped by the response post-filter, not refused.
    _allows("sessions.search", {"agentId": "agentX", "query": "q"})


def test_search_hits_are_scoped_to_the_caller():
    # A key-less search returns hits from every session on the shared
    # container; only the caller's own survive the post-filter.
    mine = {"sessionKey": f"agent:agentX:user_a:{_UUID}", "snippet": "mine"}
    theirs = {"sessionKey": f"agent:agentX:user_b:{_UUID}", "snippet": "theirs"}
    cron = {"sessionKey": f"agent:agentX:cron:{_UUID}", "snippet": "cron"}
    out = rpc._filter_search_hits_for_user({"results": [mine, theirs, cron], "truncated": True}, "user_A")
    assert out == {"results": [mine], "truncated": True}
    # Non-dict / shapeless results pass through untouched.
    assert rpc._filter_search_hits_for_user({"indexing": True}, "user_A") == {"indexing": True}
