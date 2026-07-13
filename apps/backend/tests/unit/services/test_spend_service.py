"""spend_service — provider-aware spend summary building blocks."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from core.billing.metronome_client import MetronomeClient
from core.services import spend_service


DRAFT_INVOICE = {
    "id": "inv-1",
    "start_timestamp": "2026-07-01T00:00:00+00:00",
    "line_items": [
        # usage line items carry our `tier` pricing group; totals are cents
        {"name": "Opus usage", "total": 900.0, "pricing_group_values": {"tier": "opus"}},
        {"name": "Sonnet usage", "total": 334.5, "pricing_group_values": {"tier": "sonnet"}},
        # commit draw-down: negative, no tier — must be excluded
        {"name": "Prepaid commit", "total": -1234.5, "pricing_group_values": None},
    ],
}


@pytest.fixture(autouse=True)
def _clear_cache():
    spend_service._spend_cache.clear()
    yield
    spend_service._spend_cache.clear()


@pytest.mark.asyncio
async def test_billed_spend_sums_tier_line_items_and_skips_credit_items():
    with (
        patch.object(spend_service, "_list_draft_invoices", new=AsyncMock(return_value=[DRAFT_INVOICE])),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        billed = await spend_service.get_billed_month_spend("owner-1")

    assert billed is not None
    assert billed.total_cents == 1234  # 900 + 334.5 rounded, credit item excluded
    assert billed.period_start == "2026-07-01T00:00:00+00:00"
    tiers = {row["tier"]: row for row in billed.by_model}
    assert tiers["opus"]["cents"] == 900
    assert tiers["sonnet"]["cents"] == 334  # round-half-even is fine; assert int
    assert tiers["opus"]["model"] == "Claude Opus"


@pytest.mark.asyncio
async def test_billed_spend_none_without_metronome_customer():
    with (
        patch.object(spend_service.billing_repo, "get_by_owner_id", new=AsyncMock(return_value={})),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_billed_spend_none_on_metronome_error():
    with (
        patch.object(
            spend_service,
            "_list_draft_invoices",
            new=AsyncMock(side_effect=RuntimeError("metronome down")),
        ),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


@pytest.mark.asyncio
async def test_billed_spend_zero_when_no_draft_invoice_yet():
    with (
        patch.object(spend_service, "_list_draft_invoices", new=AsyncMock(return_value=[])),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        billed = await spend_service.get_billed_month_spend("owner-1")
    assert billed is not None
    assert billed.total_cents == 0
    assert billed.by_model == []


@pytest.mark.asyncio
async def test_billed_spend_cached_within_ttl():
    invoices_mock = AsyncMock(return_value=[DRAFT_INVOICE])
    with (
        patch.object(spend_service, "_list_draft_invoices", new=invoices_mock),
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
    ):
        await spend_service.get_billed_month_spend("owner-1")
        await spend_service.get_billed_month_spend("owner-1")
    assert invoices_mock.await_count == 1


@pytest.mark.asyncio
async def test_billed_spend_uses_invoice_detail_when_list_omits_line_items():
    """Finding P1: Metronome's list endpoint returns invoice SUMMARIES whose
    line_items may be empty ("use the Get Invoice endpoint for detailed
    line items" per Metronome's docs) — the real ``_list_draft_invoices``
    (not mocked away here) must re-fetch each invoice by id and sum the
    DETAIL response, not the list one."""
    list_invoice = {"id": "inv-1", "start_timestamp": "2026-07-01T00:00:00+00:00", "line_items": []}
    detail_invoice = {
        "id": "inv-1",
        "start_timestamp": "2026-07-01T00:00:00+00:00",
        "line_items": [
            {"name": "Sonnet usage", "total": 500.0, "pricing_group_values": {"tier": "sonnet"}},
        ],
    }
    with (
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(return_value={"metronome_customer_id": "cus-1"}),
        ),
        patch.object(MetronomeClient, "list_customer_invoices", new=AsyncMock(return_value=[list_invoice])),
        patch.object(
            MetronomeClient, "get_customer_invoice", new=AsyncMock(return_value=detail_invoice)
        ) as get_invoice,
    ):
        billed = await spend_service.get_billed_month_spend("owner-1")

    get_invoice.assert_awaited_once_with(customer_id="cus-1", invoice_id="inv-1")
    assert billed is not None
    assert billed.total_cents == 500
    assert billed.by_model == [{"model": "Claude Sonnet", "tier": "sonnet", "cents": 500}]


@pytest.mark.asyncio
async def test_billed_spend_none_when_billing_repo_raises():
    """Finding #3 regression: a DDB throttle on the billing-account read must
    degrade to None, not propagate — this module's "never raises" contract
    covers the repo read too, not just the Metronome call."""
    with (
        patch.object(
            spend_service.billing_repo,
            "get_by_owner_id",
            new=AsyncMock(side_effect=RuntimeError("DDB throttled")),
        ),
    ):
        assert await spend_service.get_billed_month_spend("owner-1") is None


AUTH_STATUS_RESULT = {
    "ts": 1,
    "providers": [
        {"provider": "anthropic"},
        {
            "provider": "openai",
            "usage": {
                "plan": "pro",
                "windows": [
                    {"label": "5h", "usedPercent": 43, "resetAt": 1789000000000},
                    {"label": "Week", "usedPercent": 61},
                ],
            },
        },
    ],
}


def _patch_gateway(rpc_result=None, rpc_error=None, container=("c", "10.0.0.1")):
    """Patch the ecs/pool accessors spend_service uses for the OAuth branch."""
    ecs = AsyncMock()
    ecs.resolve_running_container.return_value = ({"gateway_token": "tok"}, container[1]) if container else (None, None)
    pool = AsyncMock()
    if rpc_error:
        pool.send_rpc.side_effect = rpc_error
    else:
        pool.send_rpc.return_value = rpc_result
    return (
        patch.object(spend_service, "_ecs_manager", return_value=ecs),
        patch.object(spend_service, "_gateway_pool", return_value=pool),
    )


@pytest.mark.asyncio
async def test_summary_oauth_maps_quota_windows():
    p1, p2 = _patch_gateway(rpc_result=AUTH_STATUS_RESULT)
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload["mode"] == "oauth_quota"
    assert payload["plan"] == "Pro"
    assert payload["windows"] == [
        {"label": "5-hour", "used_percent": 43, "resets_at_ms": 1789000000000},
        {"label": "Weekly", "used_percent": 61, "resets_at_ms": None},
    ]


LEGACY_AUTH_STATUS_RESULT = {
    "ts": 1,
    "providers": [
        {"provider": "anthropic"},
        {
            "provider": "openai-codex",
            "usage": {
                "plan": "plus",
                "windows": [
                    {"label": "5h", "usedPercent": 12, "resetAt": 1789000001000},
                ],
            },
        },
    ],
}


@pytest.mark.asyncio
async def test_summary_oauth_accepts_legacy_openai_codex_provider_id():
    """Finding round-3 P2: older container images (containers keep their
    provision-time image until reprovision) and the frontend's own
    llm-auth-status mapping still emit the retired "openai-codex" provider
    id for the ChatGPT-OAuth entry — not just "openai" as the current 7.1
    fork reports it."""
    p1, p2 = _patch_gateway(rpc_result=LEGACY_AUTH_STATUS_RESULT)
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload["mode"] == "oauth_quota"
    assert payload["plan"] == "Plus"
    assert payload["windows"] == [
        {"label": "5-hour", "used_percent": 12, "resets_at_ms": 1789000001000},
    ]


PARTIAL_USED_PERCENT_RESULT = {
    "ts": 1,
    "providers": [
        {
            "provider": "openai",
            "usage": {
                "plan": "prolite",
                "windows": [
                    {"label": "168h", "usedPercent": 30, "resetAt": 1789000000000},
                    {"label": "24h"},
                ],
            },
        },
    ],
}


@pytest.mark.asyncio
async def test_summary_oauth_drops_window_without_used_percent():
    """A window whose payload omits usedPercent must never be fabricated as
    0% -- it's dropped rather than rendered with a confident-but-fake bar."""
    p1, p2 = _patch_gateway(rpc_result=PARTIAL_USED_PERCENT_RESULT)
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload["mode"] == "oauth_quota"
    assert payload["plan"] == "Pro Lite"
    assert payload["windows"] == [
        {"label": "Weekly", "used_percent": 30, "resets_at_ms": 1789000000000},
    ]


ALL_MISSING_USED_PERCENT_RESULT = {
    "ts": 1,
    "providers": [
        {
            "provider": "openai",
            "usage": {
                "plan": "pro",
                "windows": [
                    {"label": "168h"},
                    {"label": "24h"},
                ],
            },
        },
    ],
}


@pytest.mark.asyncio
async def test_summary_oauth_all_windows_missing_used_percent_degrades_to_tokens_only():
    p1, p2 = _patch_gateway(rpc_result=ALL_MISSING_USED_PERCENT_RESULT)
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload == {"mode": "tokens_only"}


@pytest.mark.parametrize(
    ("raw_label", "expected"),
    [
        ("168h", "Weekly"),
        ("200h", "Weekly"),
        ("24h", "Daily"),
        ("48h", "Daily"),
        ("5h", "5-hour"),
        ("Week", "Weekly"),
        ("Day", "Daily"),
        ("something-else", "something-else"),
    ],
)
def test_normalize_window_label(raw_label, expected):
    assert spend_service._normalize_window_label(raw_label) == expected


@pytest.mark.parametrize(
    ("raw_plan", "expected"),
    [
        ("prolite", "Pro Lite"),
        ("PROLITE", "Pro Lite"),
        ("free", "Free"),
        ("plus", "Plus"),
        ("pro", "Pro"),
        ("business", "Business"),
        ("team", "Team"),
        ("enterprise", "Enterprise"),
        ("some_unknown_plan", "Some Unknown Plan"),
        (None, None),
    ],
)
def test_display_plan(raw_plan, expected):
    assert spend_service._display_plan(raw_plan) == expected


@pytest.mark.asyncio
async def test_summary_oauth_degrades_to_tokens_only_on_rpc_failure():
    p1, p2 = _patch_gateway(rpc_error=RuntimeError("container gone"))
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_oauth_degrades_when_no_container():
    p1, p2 = _patch_gateway(container=None)
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_oauth_degrades_when_authstatus_rpc_hangs():
    """Finding round-4 P2: the pool's send_rpc has no timeout of its own —
    it awaits the connection's default 30s _RPC_TIMEOUT. This display-only
    probe must bound its own wait (5s) so a container that accepts but
    never answers models.authStatus can't hold /billing/spend (and
    mobile's Promise.all'd usage screen) open for 30s."""
    p1, p2 = _patch_gateway(rpc_error=asyncio.TimeoutError())
    with p1, p2:
        payload = await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
    assert payload == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_oauth_uses_unique_req_id_per_call():
    """Finding P2: a fixed req_id keyed only on owner_id lets two
    overlapping /billing/spend requests for the same owner cross-deliver
    responses in the gateway pool's _pending_rpcs (keyed by req_id)."""
    p1, p2 = _patch_gateway(rpc_result=AUTH_STATUS_RESULT)
    with p1, p2:
        await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
        pool = spend_service._gateway_pool()
        first_req_id = pool.send_rpc.call_args.kwargs["req_id"]

        await spend_service.get_spend_summary("owner-1", "chatgpt_oauth")
        second_req_id = pool.send_rpc.call_args.kwargs["req_id"]

    assert first_req_id != second_req_id
    assert first_req_id.startswith("spend-authstatus-")


@pytest.mark.asyncio
async def test_summary_byok_and_unknown_are_tokens_only():
    assert await spend_service.get_spend_summary("owner-1", "byo_key") == {"mode": "tokens_only"}
    assert await spend_service.get_spend_summary("owner-1", None) == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_bedrock_trial_is_tokens_only():
    """resolve_provider_choice resolves trial_local owners to the
    bedrock_trial config mode (never a stored provider_choice) — this
    module has no branch for it, and it must fall through to tokens_only
    rather than get treated as billed bedrock_claude spend."""
    assert await spend_service.get_spend_summary("owner-1", "bedrock_trial") == {"mode": "tokens_only"}


@pytest.mark.asyncio
async def test_summary_bedrock_uses_billed_spend():
    billed = spend_service.BilledSpend(total_cents=1234, by_model=[], period_start="2026-07-01")
    with patch.object(spend_service, "get_billed_month_spend", new=AsyncMock(return_value=billed)):
        payload = await spend_service.get_spend_summary("owner-1", "bedrock_claude")
    assert payload == {
        "mode": "bedrock",
        "billed_month_to_date_cents": 1234,
        "by_model": [],
        "period_start": "2026-07-01",
    }


@pytest.mark.asyncio
async def test_summary_bedrock_degrades_when_metronome_unavailable():
    with patch.object(spend_service, "get_billed_month_spend", new=AsyncMock(return_value=None)):
        payload = await spend_service.get_spend_summary("owner-1", "bedrock_claude")
    assert payload == {"mode": "tokens_only"}
