"""The wake-admission gate: Paperclip-originated agent wakes are budget-checked.

Paperclip's seed agents (Friday/Atlas) drive their runs through the **operator
relay**, which short-circuits ABOVE both the RPC proxy and the typed
``agent_chat`` handler. That made the agent-wake door the one entrance to
inference that was never budget-checked: a capped-out free-trial owner got a
wall of instantly-dead runs (12 in ~3 minutes, #1426) instead of one clear
"out for today, upgrade" signal.

The fix calls the SAME ``pool.gate_chat`` the human chat path calls. **The
provider matrix below is the point of this file** — reusing gate_chat rather
than inventing a trial-only predicate is what keeps ChatGPT-OAuth and BYOK
owners (who consume no Lightbulb AI budget) from ever being blocked on one. Those
cases run against the REAL ``gate_chat``, not a stub, so a regression in the
predicate itself fails here.
"""

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.gateway import connection_pool  # noqa: E402
from core.gateway.connection_pool import GatewayConnectionPool  # noqa: E402
from core.services import trial_cap  # noqa: E402
from routers import websocket_chat as wc  # noqa: E402

_AGENT_FRAME = {
    "type": "req",
    "id": "req-wake-1",
    "method": "agent",
    "params": {"message": "wake up", "sessionKey": "agent:friday:paperclip:issue:42"},
}


def _real_pool():
    """Patch in a REAL pool so the provider matrix exercises the real gate_chat."""
    return patch(
        "routers.websocket_chat.get_gateway_pool",
        MagicMock(return_value=GatewayConnectionPool(management_api=None)),
    )


def _billing(account: dict | None):
    return patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=account),
    )


async def _admit(body: dict | None = None):
    return await wc._admit_operator_wake(
        connection_id="conn-operator-1",
        user_id="user_1",
        owner_id="user_1",
        body=body if body is not None else dict(_AGENT_FRAME),
    )


# ---------------------------------------------------------------------------
# 1. Provider matrix — against the REAL gate_chat.
#
#    | path                  | L1 sub | L2 $3/day cap | L3 credits |
#    | free trial_local      |   ✅   |      ✅        |     —      |
#    | paid bedrock_claude   |   ✅   |    skipped    |     ✅     |
#    | ChatGPT OAuth         |   ✅   |    skipped    |  skipped   |
#    | BYO key               |   ✅   |    skipped    |  skipped   |
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trial_owner_over_daily_cap_is_blocked():
    """The #1426 case itself: a capped free-trial owner's wake is refused."""
    with (
        _real_pool(),
        _billing({"subscription_status": "trial_local"}),
        patch(
            "core.services.trial_cap.reason_for_account",
            new=AsyncMock(return_value=trial_cap.DAILY_CAP),
        ),
        patch("core.observability.product_analytics.capture_once_per_owner_per_day"),
    ):
        refusal = await _admit()

    assert refusal is not None
    assert refusal["error"]["code"] == "trial_daily_cap"


@pytest.mark.asyncio
async def test_expired_trial_owner_is_blocked():
    with (
        _real_pool(),
        _billing({"subscription_status": "trial_local", "trial_end": int(time.time()) - 60}),
    ):
        refusal = await _admit()

    assert refusal is not None
    assert refusal["error"]["code"] == "trial_expired"


@pytest.mark.asyncio
async def test_chatgpt_oauth_owner_with_zero_credits_is_ALLOWED():
    """OAuth spend lands on the user's own OpenAI account — it draws NOTHING
    from Lightbulb AI credits, so an empty credit balance must never park their
    automations. This is the case a trial-only/credit-only predicate would
    have broken."""
    with (
        _real_pool(),
        _billing({"subscription_status": "active", "provider_choice": "chatgpt_oauth"}),
        patch(
            "core.services.metronome_gate.is_owner_blocked",
            new=AsyncMock(return_value=True),
        ) as balance_mock,
    ):
        refusal = await _admit()

    assert refusal is None
    balance_mock.assert_not_awaited()  # the balance is not even consulted


@pytest.mark.asyncio
async def test_byok_owner_with_zero_credits_is_ALLOWED():
    """Same reasoning as OAuth: a BYO-key owner burns their own Anthropic /
    OpenAI key, so Lightbulb AI budget is not theirs to be blocked on."""
    with (
        _real_pool(),
        _billing({"subscription_status": "active", "provider_choice": "byo_key"}),
        patch(
            "core.services.metronome_gate.is_owner_blocked",
            new=AsyncMock(return_value=True),
        ) as balance_mock,
    ):
        refusal = await _admit()

    assert refusal is None
    balance_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_bedrock_owner_with_exhausted_credits_is_blocked():
    with (
        _real_pool(),
        _billing({"subscription_status": "active", "provider_choice": "bedrock_claude"}),
        patch(
            "core.services.metronome_gate.is_owner_blocked",
            new=AsyncMock(return_value=True),
        ),
    ):
        refusal = await _admit()

    assert refusal is not None
    assert refusal["error"]["code"] == "out_of_credits"


@pytest.mark.asyncio
async def test_bedrock_owner_with_credits_is_allowed():
    with (
        _real_pool(),
        _billing({"subscription_status": "active", "provider_choice": "bedrock_claude"}),
        patch(
            "core.services.metronome_gate.is_owner_blocked",
            new=AsyncMock(return_value=False),
        ),
    ):
        assert await _admit() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["chatgpt_oauth", "byo_key", "bedrock_claude", None])
async def test_inactive_subscription_blocks_every_provider(provider):
    """The container is on us regardless of who pays for inference, so a dead
    subscription blocks the wake on every card."""
    with (
        _real_pool(),
        _billing({"subscription_status": "canceled", "provider_choice": provider}),
    ):
        refusal = await _admit()

    assert refusal is not None
    assert refusal["error"]["code"] == "subscription_inactive"


# ---------------------------------------------------------------------------
# 2. Fail-open — the deliberate inverse of the chat path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_exception_admits_the_wake_and_emits_a_metric():
    """gate_chat raises rather than swallowing (right for a human send, which
    the user sees fail and retries). A wake has no such retry, and spend is
    fenced by the bedrock-gate's reservation anyway — so a gate-read blip must
    ADMIT, loudly."""
    pool = MagicMock()
    pool.gate_chat = AsyncMock(side_effect=RuntimeError("dynamodb boom"))

    with (
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)),
        patch("routers.websocket_chat.put_metric") as metric,
        patch("routers.websocket_chat.logger") as log,
    ):
        refusal = await _admit()

    assert refusal is None  # admitted
    metric.assert_called_once_with("wake.admission", dimensions={"outcome": "gate_error"})
    assert log.warning.called


@pytest.mark.asyncio
async def test_pool_lookup_failure_also_fails_open():
    """Fail-open covers the whole gate read, not just gate_chat's body."""
    with patch(
        "routers.websocket_chat.get_gateway_pool",
        MagicMock(side_effect=RuntimeError("no pool")),
    ):
        assert await _admit() is None


@pytest.mark.asyncio
async def test_blocked_wake_emits_the_blocked_metric():
    pool = MagicMock()
    pool.gate_chat = AsyncMock(return_value={"blocked": True, "code": "trial_daily_cap", "message": "nope"})

    with (
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)),
        patch("routers.websocket_chat.put_metric") as metric,
    ):
        assert await _admit() is not None

    metric.assert_called_once_with(
        "wake.admission",
        dimensions={"outcome": "blocked", "reason": "trial_daily_cap"},
    )


# ---------------------------------------------------------------------------
# 3. Scope — gate exactly the run-STARTING methods, nothing else.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "agent.wait",  # awaits an ALREADY-started run — gating it strands live work
        "chat.history",
        "sessions.list",
        "agents.list",
        "health",
        "chat.abort",
        "device.pair.list",
    ],
)
async def test_non_run_starting_methods_are_never_gated(method):
    """Gating a status read or a history fetch would break the Teams UI."""
    pool = MagicMock()
    pool.gate_chat = AsyncMock(return_value={"blocked": True, "code": "trial_daily_cap"})

    with patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)):
        refusal = await _admit({"type": "req", "id": "r1", "method": method, "params": {}})

    assert refusal is None
    pool.gate_chat.assert_not_awaited()


def test_gated_method_set_is_the_shared_run_dispatch_definition():
    """One definition of "this method starts a run", shared with the RPC-proxy
    refusal — the two doors must never disagree."""
    assert wc._WAKE_GATED_METHODS is wc._RUN_DISPATCH_METHODS
    # The method the Paperclip adapter actually sends to start a run.
    assert "agent" in wc._WAKE_GATED_METHODS
    # ...and the one it sends to AWAIT that run.
    assert "agent.wait" not in wc._WAKE_GATED_METHODS


@pytest.mark.asyncio
async def test_session_key_from_params_is_passed_through():
    pool = MagicMock()
    pool.gate_chat = AsyncMock(return_value={"blocked": False})

    with patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)):
        await _admit()

    assert pool.gate_chat.await_args.kwargs["session_key"] == "agent:friday:paperclip:issue:42"
    assert pool.gate_chat.await_args.kwargs["owner_id"] == "user_1"


@pytest.mark.asyncio
async def test_missing_or_malformed_params_do_not_crash_the_gate():
    pool = MagicMock()
    pool.gate_chat = AsyncMock(return_value={"blocked": False})

    with patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)):
        assert await _admit({"type": "req", "id": "r1", "method": "agent"}) is None
        assert await _admit({"type": "req", "id": "r2", "method": "agent", "params": "junk"}) is None

    assert pool.gate_chat.await_count == 2


# ---------------------------------------------------------------------------
# 4. The refusal payload — one helper owns the wire shape.
# ---------------------------------------------------------------------------


def test_refusal_payload_shape():
    """Keyed to the frame's request id and carrying the gate's own code, so the
    adapter's pending-request map resolves and the Teams surface can render one
    signal. The adapter reads error.message / error.code / error.details
    (execute.ts handleMessage)."""
    frame = wc._wake_refusal_payload(
        {"blocked": True, "code": "trial_daily_cap", "message": "You've used today's free allowance"},
        req_id="req-wake-1",
    )

    assert frame["type"] == "res"
    assert frame["id"] == "req-wake-1"
    assert frame["ok"] is False
    assert frame["error"]["code"] == "trial_daily_cap"
    assert frame["error"]["message"] == "You've used today's free allowance"
    assert frame["error"]["details"] == {"reason": "wake_admission_gate", "code": "trial_daily_cap"}


def test_refusal_message_avoids_the_adapters_transient_retry_substrings():
    """The adapter's retry classifier is a SUBSTRING match on the error message
    (execute.ts: econnrefused / econnreset / socket hang up / timeout). A gate
    message containing one of those would be re-armed as a transient failure and
    recreate #1426 from the other side."""
    transient = ("econnrefused", "econnreset", "socket hang up", "timeout")
    for code, message in connection_pool.BUDGET_BLOCK_MESSAGES.items():
        frame = wc._wake_refusal_payload({"blocked": True, "code": code, "message": message}, req_id="r")
        lowered = frame["error"]["message"].lower()
        assert not any(marker in lowered for marker in transient), code


def test_refusal_payload_tolerates_a_gate_dict_without_code_or_message():
    frame = wc._wake_refusal_payload({"blocked": True}, req_id=None)
    assert frame["ok"] is False
    assert frame["error"]["code"] == "blocked"
    assert isinstance(frame["error"]["message"], str) and frame["error"]["message"]


# ---------------------------------------------------------------------------
# 5. Wiring — the gate actually sits on the relay dispatch in POST /ws/message.
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(wc.router, prefix="/ws")
    return app


@pytest.fixture
def operator_connection():
    with patch("routers.websocket_chat.get_connection_service") as getter:
        svc = MagicMock()
        svc.get_connection.return_value = {
            "user_id": "owner-1",
            "org_id": None,
            "connection_type": "operator",
        }
        getter.return_value = svc
        yield svc


@pytest.fixture
def mgmt():
    with patch("routers.websocket_chat.get_management_api_client") as getter:
        client = MagicMock()
        client.send_message = MagicMock(return_value=True)
        client.send_message_async = AsyncMock(side_effect=client.send_message)
        getter.return_value = client
        yield client


async def _post(test_app, frame: dict):
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        return await client.post("/ws/message", headers={"x-connection-id": "conn-op"}, json=frame)


@pytest.mark.asyncio
async def test_blocked_wake_is_refused_and_never_reaches_the_container(test_app, operator_connection, mgmt):
    """The whole point: a refused wake must not start a run upstream."""
    pool = MagicMock()
    pool.gate_chat = AsyncMock(return_value={"blocked": True, "code": "trial_daily_cap", "message": "out for today"})

    with (
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)),
        patch("routers.websocket_chat.is_operator_connection", return_value=True),
        patch("routers.websocket_chat.handle_operator_message", new=AsyncMock()) as relay,
    ):
        response = await _post(test_app, dict(_AGENT_FRAME))

    assert response.status_code == 200
    relay.assert_not_awaited()  # nothing reached the container
    sent = mgmt.send_message.call_args[0][1]
    assert sent["type"] == "res"
    assert sent["id"] == "req-wake-1"
    assert sent["ok"] is False
    assert sent["error"]["code"] == "trial_daily_cap"


@pytest.mark.asyncio
async def test_admitted_wake_relays_verbatim(test_app, operator_connection, mgmt):
    pool = MagicMock()
    pool.gate_chat = AsyncMock(return_value={"blocked": False})

    with (
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)),
        patch("routers.websocket_chat.is_operator_connection", return_value=True),
        patch("routers.websocket_chat.handle_operator_message", new=AsyncMock()) as relay,
    ):
        response = await _post(test_app, dict(_AGENT_FRAME))

    assert response.status_code == 200
    relay.assert_awaited_once()
    assert relay.await_args[0][1] == _AGENT_FRAME  # forwarded unmodified
    mgmt.send_message.assert_not_called()  # no refusal


@pytest.mark.asyncio
async def test_agent_wait_relays_without_consulting_the_gate(test_app, operator_connection, mgmt):
    """The wake's second leg must pass even for a capped owner — the run it is
    awaiting has already started."""
    pool = MagicMock()
    pool.gate_chat = AsyncMock(return_value={"blocked": True, "code": "trial_daily_cap"})

    with (
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)),
        patch("routers.websocket_chat.is_operator_connection", return_value=True),
        patch("routers.websocket_chat.handle_operator_message", new=AsyncMock()) as relay,
    ):
        response = await _post(
            test_app,
            {"type": "req", "id": "w1", "method": "agent.wait", "params": {"runId": "r1"}},
        )

    assert response.status_code == 200
    relay.assert_awaited_once()
    pool.gate_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_req_frames_bypass_the_gate_entirely(test_app, operator_connection, mgmt):
    """Event / response frames flowing back over the relay are not requests and
    must never be budget-checked."""
    pool = MagicMock()
    pool.gate_chat = AsyncMock(return_value={"blocked": True, "code": "trial_daily_cap"})

    with (
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)),
        patch("routers.websocket_chat.is_operator_connection", return_value=True),
        patch("routers.websocket_chat.handle_operator_message", new=AsyncMock()) as relay,
    ):
        response = await _post(test_app, {"type": "event", "event": "agent", "payload": {}})

    assert response.status_code == 200
    relay.assert_awaited_once()
    pool.gate_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_connect_frame_is_not_gated(test_app, operator_connection, mgmt):
    """Gating the handshake would brick the relay before any run exists."""
    pool = MagicMock()
    pool.gate_chat = AsyncMock(return_value={"blocked": True, "code": "trial_daily_cap"})

    with (
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)),
        patch("routers.websocket_chat.is_operator_connection", return_value=True),
        patch("routers.websocket_chat.submit_operator_signed_connect") as submit,
    ):
        response = await _post(
            test_app,
            {"type": "req", "id": "c1", "method": "connect", "params": {"role": "operator"}},
        )

    assert response.status_code == 200
    submit.assert_called_once()
    pool.gate_chat.assert_not_awaited()


# ---------------------------------------------------------------------------
# 6. Regression — the human agent_chat path is untouched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_agent_chat_path_does_not_go_through_the_wake_gate(test_app, mgmt):
    """A chat connection must keep its own gate call, its own `error` frame
    shape (NOT the relay's `res`/`ok:false`), and never touch the wake helper."""
    with patch("routers.websocket_chat.get_connection_service") as getter:
        svc = MagicMock()
        svc.get_connection.return_value = {
            "user_id": "owner-1",
            "org_id": None,
            "connection_type": "chat",
        }
        getter.return_value = svc

        pool = MagicMock()
        pool.gate_chat = AsyncMock(
            return_value={"blocked": True, "code": "trial_daily_cap", "message": "out for today"}
        )
        pool.add_frontend_connection = MagicMock()

        ecs = MagicMock()
        ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw"}, "10.0.0.5"))

        with (
            patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=pool)),
            patch("routers.websocket_chat.get_ecs_manager", MagicMock(return_value=ecs)),
            patch("routers.websocket_chat._admit_operator_wake", new=AsyncMock()) as wake_gate,
            patch("routers.websocket_chat.handle_operator_message", new=AsyncMock()) as relay,
        ):
            response = await _post(
                test_app,
                {"type": "agent_chat", "agent_id": "friday", "message": "hello"},
            )

    assert response.status_code == 200
    wake_gate.assert_not_awaited()  # the wake gate is relay-only
    relay.assert_not_awaited()
    pool.gate_chat.assert_awaited_once()  # the chat path still gates itself

    # ...and still reports blocks in the chat wire shape, not the relay's.
    blocked = [c[0][1] for c in mgmt.send_message.call_args_list if c[0][1].get("type") == "error"]
    assert blocked, f"expected a chat `error` frame, got {mgmt.send_message.call_args_list}"
    assert blocked[0]["code"] == "trial_daily_cap"
    assert blocked[0]["agent_id"] == "friday"
