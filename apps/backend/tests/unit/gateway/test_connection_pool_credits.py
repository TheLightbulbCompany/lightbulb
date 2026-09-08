"""Tests for the credit-aware gateway hooks.

Two surfaces:

- Pool-level ``GatewayConnectionPool.gate_chat`` — pre-chat hard-stop for
  card-3 users when balance ≤ 0; cards 1+2 + legacy users always pass.

- Connection-level ``GatewayConnection._fetch_and_record_usage`` — invoked
  on the chat-end lifecycle event; after the meter cutover it records the
  analytics usage counters ONLY. Credit deduction moved OUT of the
  container path: the AWS Bedrock invocation-log meter is now the sole
  biller (every-run, cache-correct, exactly-once on requestId).

``provider_choice`` is read from the ``billing_accounts`` row (per
owner_id), NOT from the ``users`` row. Credit balance reads stay keyed on
the container owner.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.gateway.connection_pool import GatewayConnection, GatewayConnectionPool


# --------- Task 4: pool.gate_chat ---------


@pytest.mark.asyncio
async def test_card3_blocked_by_metronome_gate():
    pool = GatewayConnectionPool(management_api=None)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "bedrock_claude",
                }
            ),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=True)),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1", session_key="sk_test")
    assert result["blocked"] is True
    assert result["code"] == "out_of_credits"
    assert "Top up" in result["message"]


@pytest.mark.asyncio
async def test_card3_not_blocked_when_gate_clear():
    pool = GatewayConnectionPool(management_api=None)
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "bedrock_claude",
                }
            ),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=AsyncMock(return_value=False)),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1", session_key="sk_test")
    assert result == {"blocked": False}


@pytest.mark.asyncio
async def test_card3_gate_keyed_on_owner_for_org_credits_pool():
    """Both provider_choice (from billing_repo) AND the credits gate are
    keyed on owner_id. Org members all draw from one pool funded by the
    admin — without this, members get blocked with out_of_credits.
    """
    pool = GatewayConnectionPool(management_api=None)
    billing_mock = AsyncMock(
        return_value={
            "subscription_status": "active",
            "provider_choice": "bedrock_claude",
        }
    )
    blocked = AsyncMock(return_value=False)
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=billing_mock),
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked),
    ):
        result = await pool.gate_chat(user_id="member_X", owner_id="org_Y", session_key="sk_test")

    assert result == {"blocked": False}
    billing_mock.assert_awaited_once_with("org_Y")
    blocked.assert_awaited_once_with("org_Y")


@pytest.mark.asyncio
async def test_card1_oauth_user_never_gated_by_credits():
    pool = GatewayConnectionPool(management_api=None)
    blocked = AsyncMock()
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "chatgpt_oauth",
                }
            ),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1", session_key="sk_test")
    assert result == {"blocked": False}
    blocked.assert_not_called()


@pytest.mark.asyncio
async def test_card2_byo_user_never_gated_by_credits():
    pool = GatewayConnectionPool(management_api=None)
    blocked = AsyncMock()
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "subscription_status": "active",
                    "provider_choice": "byo_key",
                }
            ),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", new=blocked),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="o_1", session_key="sk_test")
    assert result == {"blocked": False}
    blocked.assert_not_called()


@pytest.mark.asyncio
async def test_billing_row_without_provider_choice_never_gated():
    """Legacy billing rows without provider_choice shouldn't be locked
    out — they keep working until provider is set."""
    pool = GatewayConnectionPool(management_api=None)
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                # legacy row carries stripe_subscription_id, no
                # subscription_status, no provider_choice
                "stripe_subscription_id": "sub_legacy",
            }
        ),
    ):
        result = await pool.gate_chat(user_id="u_legacy", owner_id="o_legacy", session_key="sk_test")
    assert result == {"blocked": False}


# --------- Audit M1: subscription-status gate (any provider) ---------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["canceled", "incomplete", "incomplete_expired", "unpaid", "paused", "past_due"],
)
async def test_inactive_subscription_blocks_chat_regardless_of_provider(status):
    """A canceled / past_due / unpaid subscription blocks chat for ANY
    provider — including chatgpt_oauth and byo_key, where the LLM cost
    is on the user but the container compute is on us."""
    pool = GatewayConnectionPool(management_api=None)
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value={"subscription_status": status}),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="u_1", session_key="sk_test")
    assert result["blocked"] is True
    assert result["code"] == "subscription_inactive"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "trialing"])
async def test_active_or_trialing_subscription_does_not_trigger_sub_gate(status):
    """active/trialing pass the subscription gate (then fall through to
    the provider_choice + balance check). Without provider_choice set,
    Layer-2 returns blocked=False."""
    pool = GatewayConnectionPool(management_api=None)
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "subscription_status": status,
                "provider_choice": "chatgpt_oauth",
            }
        ),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="u_1", session_key="sk_test")
    assert result == {"blocked": False}


@pytest.mark.asyncio
async def test_missing_billing_row_blocks_chat_when_owner_id_provided():
    """An authenticated user whose billing row doesn't exist at all
    (never went through trial-checkout, or got cleaned up out-of-band)
    must be blocked. Otherwise non-Bedrock providers would chat freely
    with us still on the hook for container compute."""
    pool = GatewayConnectionPool(management_api=None)
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="u_1", session_key="sk_test")
    assert result["blocked"] is True
    assert result["code"] == "subscription_inactive"


@pytest.mark.asyncio
async def test_legacy_billing_row_without_status_does_not_block():
    """A pre-Plan-3 billing row may have stripe_subscription_id but no
    subscription_status backfilled — those should NOT be locked out
    mid-deploy."""
    pool = GatewayConnectionPool(management_api=None)
    with patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(
            return_value={
                "stripe_subscription_id": "sub_legacy",
                "provider_choice": "chatgpt_oauth",
            }
        ),
    ):
        result = await pool.gate_chat(user_id="u_1", owner_id="u_1", session_key="sk_test")
    assert result == {"blocked": False}


# ------ _fetch_and_record_usage is analytics-only (Metronome bills) ------


def _make_connection_with_rpc(session: dict, user_id: str = "u_1") -> GatewayConnection:
    """A GatewayConnection whose sessions.describe RPC returns *session*."""
    conn = object.__new__(GatewayConnection)
    conn.user_id = user_id
    conn.send_rpc = AsyncMock()
    conn.wait_for_response = AsyncMock(return_value={"session": session})
    return conn


def _bedrock_session() -> dict:
    import uuid

    return {
        "key": f"agent:main:{uuid.uuid4().hex}",
        "inputTokens": 10,
        "outputTokens": 5,
        "model": "amazon-bedrock/anthropic.claude-sonnet-4-6",
        "estimatedCostUsd": 1.5,
    }


@pytest.mark.asyncio
async def test_fetch_and_record_usage_writes_analytics_only():
    """Chat completion records the analytics counters and nothing else —
    billing flows from the meter Lambda straight to Metronome, so no
    deduct/ledger call can exist on this path."""
    session = _bedrock_session()
    conn = _make_connection_with_rpc(session)
    record_mock = AsyncMock()

    with patch("core.services.usage_service.record_usage", new=record_mock):
        await conn._fetch_and_record_usage(session["key"], member_user_id="member_1")

    record_mock.assert_awaited_once()
    _, kwargs = record_mock.call_args
    assert kwargs["owner_id"] == "u_1"
    assert kwargs["user_id"] == "member_1"


@pytest.mark.asyncio
async def test_usage_lookup_is_a_keyed_describe_not_a_store_scan():
    """The per-turn lookup must be ``sessions.describe {key}``.

    ``sessions.list {}`` serialised the owner's WHOLE session store on the
    owner's own container after every chat turn, only to linear-scan it for
    one row. Assert the exact RPC and params — and that no scan is sent at
    all on the happy path, since a describe hit must not be followed by one.
    """
    session = _bedrock_session()
    conn = _make_connection_with_rpc(session)
    record_mock = AsyncMock()

    with patch("core.services.usage_service.record_usage", new=record_mock):
        await conn._fetch_and_record_usage(session["key"], member_user_id="member_1")

    methods = [call.args[1] for call in conn.send_rpc.await_args_list]
    assert methods == ["sessions.describe"]
    assert conn.send_rpc.await_args_list[0].args[2] == {"key": session["key"]}

    _, kwargs = record_mock.call_args
    assert kwargs["model"] == session["model"]
    assert kwargs["input_tokens"] == 10
    assert kwargs["output_tokens"] == 5
    # estimatedCostUsd 1.5 -> microcents; the authoritative cache-inclusive cost.
    assert kwargs["raw_cost_microcents"] == 1_500_000


@pytest.mark.asyncio
async def test_describe_miss_falls_back_to_the_case_insensitive_scan():
    """A describe MISS keeps the old scan as the cold path.

    OpenClaw folds a mixed-case session key down to its lowercased legacy
    alias but never the reverse, so a keyed lookup cannot reproduce the
    case-insensitive match the scan did. Dropping that would silently lose
    usage analytics for those sessions, so the scan stays — behind the miss.
    """
    session = _bedrock_session()
    session["key"] = "agent:main:User_Bob"
    conn = _make_connection_with_rpc(session)
    conn.wait_for_response = AsyncMock(side_effect=[{"session": None}, {"sessions": [session]}])
    record_mock = AsyncMock()

    with patch("core.services.usage_service.record_usage", new=record_mock):
        await conn._fetch_and_record_usage("agent:main:user_bob", member_user_id="member_1")

    methods = [call.args[1] for call in conn.send_rpc.await_args_list]
    assert methods == ["sessions.describe", "sessions.list"]
    record_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_describe_failure_records_nothing_and_never_raises():
    """A failing lookup must not break the turn.

    ``_fetch_and_record_usage`` runs as a background task off the gateway
    reader loop; an exception escaping it would surface as an unhandled task
    error, and the run itself is already finished and delivered. Matches the
    old behaviour when ``sessions.list`` failed: swallow, log, record nothing.
    """
    conn = _make_connection_with_rpc(_bedrock_session())
    conn.wait_for_response = AsyncMock(side_effect=TimeoutError("gateway rpc timeout"))
    record_mock = AsyncMock()

    with patch("core.services.usage_service.record_usage", new=record_mock):
        await conn._fetch_and_record_usage("agent:main:user_bob", member_user_id="member_1")

    record_mock.assert_not_awaited()
    # Failure is NOT retried against the scan — a broken RPC channel stays broken.
    assert [call.args[1] for call in conn.send_rpc.await_args_list] == ["sessions.describe"]


@pytest.mark.asyncio
async def test_unknown_session_records_nothing():
    """Neither describe nor the scan finds the key: yield nothing, quietly."""
    conn = _make_connection_with_rpc(_bedrock_session())
    conn.wait_for_response = AsyncMock(side_effect=[{"session": None}, {"sessions": []}])
    record_mock = AsyncMock()

    with patch("core.services.usage_service.record_usage", new=record_mock):
        await conn._fetch_and_record_usage("agent:main:ghost", member_user_id="member_1")

    record_mock.assert_not_awaited()


def test_legacy_deduct_method_is_deleted():
    """The DDB-ledger deduct died with the cutover — the method must not
    creep back onto the connection."""
    assert not hasattr(GatewayConnection, "_maybe_deduct_credits")
