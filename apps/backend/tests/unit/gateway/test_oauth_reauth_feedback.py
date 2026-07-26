"""The run-failure -> provision-gate feedback loop.

A ChatGPT OAuth credential can die AFTER onboarding (revoked in ChatGPT
settings, refresh window elapsed, refresh-token family burned). No up-front
check can catch that, and until now nothing acted on it: the container reported
the exact reason, we forwarded it to the browser, and the owner was left with a
spinner while every subsequent run failed the same way.

The container is also the ONLY party that can legitimately spend a rotating
Codex refresh token, so its verdict is the only trustworthy one available —
probing OpenAI ourselves with the stored bootstrap seed would report dead for a
healthy owner and burn the token family via reuse detection.

So ``_maybe_flag_oauth_reauth`` turns a DEFINITIVE rejection into an
``oauth_health="invalid"`` stamp, which the provision gate reads on its next
~3s poll to fire ``oauth_required`` (the "Connect ChatGPT" card).

Driven through the real ``_handle_message`` seam, mirroring
``test_run_completion_observer.py``'s fixture.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.gateway.connection_pool import GatewayConnection


# The real prod failure (2026-07-24): container running, Friday seeded, and then
# every single run died on this. "isn't accepting your saved login" is
# OpenClaw's own auth_permanent copy.
DEAD_CREDENTIAL_ERROR = (
    "openai isn't accepting your saved login. Run `openclaw models auth login "
    "--provider openai` (OAuth token refresh failed for openai: OpenAI Codex "
    'token refresh failed (401): {"error":{"code":"refresh_token_reused"}})'
)

# Same call, same shape, but OpenAI failed to ANSWER rather than refusing.
FLAKY_PROVIDER_ERROR = (
    "OAuth token refresh failed for openai: OpenAI Codex token refresh failed (503): upstream unavailable"
)


def _make_conn(owner_id="user_1"):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = owner_id
    conn.owner_id = owner_id
    conn._frontend_connections = set()
    conn._pending_rpcs = {}
    conn._management_api = MagicMock()
    conn._billing_tasks = set()
    conn._push_tasks = set()
    conn._agent_names = None
    conn._agent_names_at = 0.0
    conn._conn_svc = MagicMock()
    conn._conn_svc.query_by_owner = AsyncMock(return_value=[])
    conn._recipients_cache = None
    conn._recipients_cache_ts = 0.0
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    conn._parked = False
    conn._parked_watchdog_task = None
    conn._run_completion_callbacks = {}
    conn._run_completion_tasks = set()
    conn._oauth_reauth_flagged = False
    return conn


async def _drive_error(conn, err_msg, *, run_id="run-1"):
    """Push one chat-terminal error event through the real handler and drain
    the fire-and-forget tasks it spawns."""
    payload = {
        "sessionKey": "agent:a1:main",
        "state": "error",
        "runId": run_id,
        "errorMessage": err_msg,
    }
    with (
        patch("core.gateway.connection_pool.put_metric"),
        patch.object(conn, "_forward_to_frontends", AsyncMock()),
    ):
        await conn._handle_message({"type": "event", "event": "chat", "payload": payload})
        await asyncio.gather(*list(conn._push_tasks), *list(conn._run_completion_tasks))


@pytest.mark.asyncio
async def test_definitive_rejection_stamps_the_connector_row():
    """The fix: a dead-credential run failure marks the owner as needing
    re-auth, so the very next gate evaluation renders the Connect ChatGPT card
    instead of an unexplained spinner."""
    conn = _make_conn("org_1")
    account = {"provider_choice": "chatgpt_oauth", "oauth_connector_user_id": "user_admin"}

    with (
        patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=account)),
        patch("core.services.oauth_service.mark_oauth_invalid", AsyncMock(return_value=True)) as mark,
    ):
        await _drive_error(conn, DEAD_CREDENTIAL_ERROR)

    mark.assert_awaited_once()
    kwargs = mark.await_args.kwargs
    # Org semantics: keyed by the CONNECTOR (the admin who completed the
    # device-code flow), bound to the WORKSPACE whose container consumed it.
    assert kwargs["user_id"] == "user_admin"
    assert kwargs["bound_to"] == "org_1"
    assert "saved login" in kwargs["reason"]


@pytest.mark.asyncio
async def test_personal_owner_is_its_own_connector():
    conn = _make_conn("user_1")

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            AsyncMock(return_value={"provider_choice": "chatgpt_oauth"}),
        ),
        patch("core.services.oauth_service.mark_oauth_invalid", AsyncMock(return_value=True)) as mark,
    ):
        await _drive_error(conn, DEAD_CREDENTIAL_ERROR)

    assert mark.await_args.kwargs["user_id"] == "user_1"
    assert mark.await_args.kwargs["bound_to"] == "user_1"


@pytest.mark.asyncio
async def test_ambiguous_provider_failure_never_stamps():
    """FAIL OPEN. A 503 from OpenAI means we could not reach a verdict, not
    that the login is dead. Demanding re-auth from a working owner because
    OpenAI had a blip is a worse bug than the one being fixed — so this must
    not even reach the billing read."""
    conn = _make_conn()

    with (
        patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock()) as billing,
        patch("core.services.oauth_service.mark_oauth_invalid", AsyncMock()) as mark,
    ):
        await _drive_error(conn, FLAKY_PROVIDER_ERROR)

    billing.assert_not_awaited()
    mark.assert_not_awaited()
    assert conn._oauth_reauth_flagged is False


@pytest.mark.asyncio
async def test_ordinary_run_failure_never_stamps():
    conn = _make_conn()

    with (
        patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock()) as billing,
        patch("core.services.oauth_service.mark_oauth_invalid", AsyncMock()) as mark,
    ):
        await _drive_error(conn, "Agent run failed: tool `bash` exited with code 1")

    billing.assert_not_awaited()
    mark.assert_not_awaited()


@pytest.mark.parametrize("provider", ["bedrock_claude", "byo_key", None])
@pytest.mark.asyncio
async def test_non_chatgpt_owners_are_untouched(provider):
    """Provider scope. A BYO-key owner running an OpenAI key can emit a
    similarly-shaped auth error; it must never stamp a ChatGPT OAuth row.
    Bedrock owners have no such row at all."""
    conn = _make_conn()
    account = {"provider_choice": provider} if provider else {}

    with (
        patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=account)),
        patch("core.services.oauth_service.mark_oauth_invalid", AsyncMock()) as mark,
    ):
        await _drive_error(conn, DEAD_CREDENTIAL_ERROR)

    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeat_failures_do_not_re_stamp():
    """A dead credential fails EVERY run. The per-connection latch keeps an
    agent retry loop from spending a DDB read+write per failure."""
    conn = _make_conn()

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            AsyncMock(return_value={"provider_choice": "chatgpt_oauth"}),
        ) as billing,
        patch("core.services.oauth_service.mark_oauth_invalid", AsyncMock(return_value=True)) as mark,
    ):
        for i in range(5):
            await _drive_error(conn, DEAD_CREDENTIAL_ERROR, run_id=f"run-{i}")

    assert mark.await_count == 1
    assert billing.await_count == 1


@pytest.mark.asyncio
async def test_flagging_failure_never_breaks_the_run_and_re_arms():
    """This is a best-effort observer on the reader-loop hot path: a DDB blip
    must not propagate into chat handling, and must leave the latch open so a
    later failure gets another attempt."""
    conn = _make_conn()

    with (
        patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock(side_effect=RuntimeError("ddb down"))),
        patch("core.services.oauth_service.mark_oauth_invalid", AsyncMock()) as mark,
    ):
        await _drive_error(conn, DEAD_CREDENTIAL_ERROR)

    mark.assert_not_awaited()
    assert conn._oauth_reauth_flagged is False
