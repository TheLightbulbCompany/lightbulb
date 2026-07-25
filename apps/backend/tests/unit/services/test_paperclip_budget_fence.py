"""Paperclip budget fence: the provider-aware predicate and the
reconcile-to-Paperclip apply rule.

The provider matrix is the load-bearing half. Fencing a ChatGPT-OAuth or
BYO-key owner on Isol8 budget is the worst failure this module can produce —
their inference is on their own account/key, so our credit state says nothing
about whether they may run. Those two rows are asserted from both directions
(zero Metronome balance, and a stray ``trial_end`` in the past).
"""

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.repositories.paperclip_repo import PaperclipCompany
from core.services import paperclip_budget_fence as fence
from core.services import trial_cap

OWNER = "org_fence"
USER = "user_fence"
COMPANY = "cmp-1234"


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def _account(**over) -> dict:
    base = {"owner_id": OWNER, "subscription_status": "active"}
    base.update(over)
    return base


def _trial(**over) -> dict:
    return _account(subscription_status="trial_local", **over)


async def _reason(account, *, spend=0, metronome_blocked=False):
    """Run ``fence_reason`` with the two external reads stubbed."""
    with (
        patch(
            "core.repositories.billing_repo.get_trial_daily_spend",
            AsyncMock(return_value=spend),
        ),
        patch(
            "core.services.metronome_gate.is_owner_blocked",
            AsyncMock(return_value=metronome_blocked),
        ),
    ):
        return await fence.fence_reason(OWNER, account)


@pytest.mark.asyncio
async def test_trial_over_daily_cap_is_fenced():
    assert await _reason(_trial(), spend=trial_cap.CAP_MICROCENTS) == trial_cap.DAILY_CAP


@pytest.mark.asyncio
async def test_trial_under_daily_cap_is_open():
    assert await _reason(_trial(), spend=trial_cap.CAP_MICROCENTS - 1) is None


@pytest.mark.asyncio
async def test_trial_expired_is_fenced():
    account = _trial(trial_end=int(time.time()) - 60)
    assert await _reason(account, spend=0) == trial_cap.TRIAL_EXPIRED


@pytest.mark.asyncio
async def test_paid_bedrock_out_of_credits_is_fenced():
    account = _account(provider_choice="bedrock_claude")
    assert await _reason(account, metronome_blocked=True) == fence.CREDITS_EXHAUSTED


@pytest.mark.asyncio
async def test_paid_bedrock_funded_is_open():
    account = _account(provider_choice="bedrock_claude")
    assert await _reason(account, metronome_blocked=False) is None


@pytest.mark.asyncio
async def test_chatgpt_oauth_owner_is_never_fenced():
    """OAuth inference is on the owner's own ChatGPT account. A Metronome
    balance of 0 is NORMAL for them (no credit grant), and a stray
    ``trial_end`` in the past is meaningless off ``trial_local``."""
    account = _account(provider_choice="chatgpt_oauth", trial_end=int(time.time()) - 86400)
    assert await _reason(account, spend=10**9, metronome_blocked=True) is None


@pytest.mark.asyncio
async def test_byok_owner_is_never_fenced():
    account = _account(provider_choice="byo_api_key", trial_end=int(time.time()) - 86400)
    assert await _reason(account, spend=10**9, metronome_blocked=True) is None


@pytest.mark.asyncio
async def test_trial_local_row_with_bedrock_provider_choice_uses_trial_predicate():
    """``/billing/trial-checkout`` writes ``provider_choice`` seconds-to-minutes
    before the Stripe webhook flips ``subscription_status``. In that window the
    row is still ``trial_local`` with ``provider_choice="bedrock_claude"`` — it
    must be judged by the trial cap, NOT by the Metronome credits gate (a
    trial owner has no credit grant, so a 0 balance would wrongly fence)."""
    account = _trial(provider_choice="bedrock_claude")
    assert await _reason(account, spend=0, metronome_blocked=True) is None


@pytest.mark.asyncio
async def test_inactive_subscription_is_fenced():
    assert await _reason(_account(subscription_status="canceled")) == fence.SUBSCRIPTION_INACTIVE


@pytest.mark.asyncio
async def test_missing_billing_row_is_open():
    """Deliberate divergence from gate_chat: this fence writes in the
    background with no card to render, so absence of data must not pause a
    whole company."""
    with patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=None)):
        assert await fence.fence_reason(OWNER) is None


@pytest.mark.asyncio
async def test_predicate_reuses_caller_supplied_account():
    get_row = AsyncMock(return_value=_account())
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", get_row),
        patch("core.services.metronome_gate.is_owner_blocked", AsyncMock(return_value=False)),
    ):
        await fence.fence_reason(OWNER, _account(provider_choice="byo_api_key"))
    get_row.assert_not_awaited()


# ---------------------------------------------------------------------------
# Reconcile / apply rule
# ---------------------------------------------------------------------------


def _row(**over) -> PaperclipCompany:
    now = datetime.now(timezone.utc)
    kwargs = {
        "user_id": USER,
        "org_id": OWNER,
        "company_id": COMPANY,
        "paperclip_user_id": "pc_user",
        "paperclip_password_encrypted": "enc",
        "service_token_encrypted": "enc",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    kwargs.update(over)
    return PaperclipCompany(**kwargs)


def _repo(row):
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=row)
    repo.set_budget_fence_state = AsyncMock()
    return repo


async def _run_reconcile(*, row, reason):
    """Reconcile with the predicate and the Paperclip PATCH stubbed.

    Returns ``(repo, patch_company_mock)``.
    """
    repo = _repo(row)
    admin = MagicMock()
    admin._http = MagicMock()
    admin.patch_company = AsyncMock(return_value={})
    with (
        patch.object(fence, "fence_reason", AsyncMock(return_value=reason)),
        patch("core.services.paperclip_admin_client.shared_admin_client", MagicMock(return_value=admin)),
        patch(
            "core.services.paperclip_admin_session.get_admin_session_cookie",
            AsyncMock(return_value="cookie=abc"),
        ),
    ):
        await fence.reconcile(OWNER, repo=repo)
    return repo, admin.patch_company


@pytest.mark.asyncio
async def test_open_to_fenced_pauses_the_company_and_stamps():
    repo, patch_company = await _run_reconcile(row=_row(), reason=trial_cap.DAILY_CAP)
    patch_company.assert_awaited_once()
    assert patch_company.await_args.kwargs["company_id"] == COMPANY
    assert patch_company.await_args.kwargs["body"] == {"status": "paused"}
    repo.set_budget_fence_state.assert_awaited_once_with(USER, state="fenced", reason=trial_cap.DAILY_CAP)


@pytest.mark.asyncio
async def test_fenced_to_open_resumes_the_company_and_stamps():
    repo, patch_company = await _run_reconcile(row=_row(budget_fence_state="fenced"), reason=None)
    assert patch_company.await_args.kwargs["body"] == {"status": "active"}
    repo.set_budget_fence_state.assert_awaited_once_with(USER, state="open", reason=None)


@pytest.mark.asyncio
async def test_no_change_makes_no_paperclip_call():
    repo, patch_company = await _run_reconcile(row=_row(budget_fence_state="fenced"), reason="trial_daily_cap")
    patch_company.assert_not_awaited()
    repo.set_budget_fence_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_never_resumes_a_pause_it_did_not_stamp():
    """A company Paperclip's OWN budget subsystem (or an operator) paused
    carries no Isol8 stamp. ``GET /api/companies/{id}`` does not project
    ``pauseReason``, so the stamp is the only discriminator — an unstamped
    row must produce no PATCH at all."""
    repo, patch_company = await _run_reconcile(row=_row(budget_fence_state=None), reason=None)
    patch_company.assert_not_awaited()
    repo.set_budget_fence_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_company_row_is_a_noop():
    repo, patch_company = await _run_reconcile(row=None, reason=trial_cap.DAILY_CAP)
    patch_company.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_active_company_is_a_noop():
    repo, patch_company = await _run_reconcile(row=_row(status="provisioning"), reason=trial_cap.DAILY_CAP)
    patch_company.assert_not_awaited()


@pytest.mark.asyncio
async def test_paperclip_failure_does_not_raise_and_leaves_stamp_unwritten():
    """A failed PATCH must not stamp — otherwise the next reconcile would
    believe the fence is applied and never retry."""
    repo = _repo(_row())
    admin = MagicMock()
    admin._http = MagicMock()
    admin.patch_company = AsyncMock(side_effect=RuntimeError("paperclip down"))
    with (
        patch.object(fence, "fence_reason", AsyncMock(return_value=trial_cap.DAILY_CAP)),
        patch("core.services.paperclip_admin_client.shared_admin_client", MagicMock(return_value=admin)),
        patch(
            "core.services.paperclip_admin_session.get_admin_session_cookie",
            AsyncMock(return_value="cookie=abc"),
        ),
    ):
        await fence.reconcile(OWNER, repo=repo)  # must not raise
    repo.set_budget_fence_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_billing_read_failure_fails_open():
    repo = _repo(_row())
    admin = MagicMock()
    admin._http = MagicMock()
    admin.patch_company = AsyncMock()
    with (
        patch.object(fence, "fence_reason", AsyncMock(side_effect=RuntimeError("ddb down"))),
        patch("core.services.paperclip_admin_client.shared_admin_client", MagicMock(return_value=admin)),
    ):
        await fence.reconcile(OWNER, repo=repo)
    admin.patch_company.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_admin_session_is_retried_once():
    from core.services.paperclip_admin_client import PaperclipApiError

    repo = _repo(_row())
    admin = MagicMock()
    admin._http = MagicMock()
    admin.patch_company = AsyncMock(side_effect=[PaperclipApiError("expired", 401, {}), {}])
    invalidate = MagicMock()
    with (
        patch.object(fence, "fence_reason", AsyncMock(return_value=trial_cap.DAILY_CAP)),
        patch("core.services.paperclip_admin_client.shared_admin_client", MagicMock(return_value=admin)),
        patch(
            "core.services.paperclip_admin_session.get_admin_session_cookie",
            AsyncMock(return_value="cookie=abc"),
        ),
        patch("core.services.paperclip_admin_session.invalidate_admin_session", invalidate),
    ):
        await fence.reconcile(OWNER, repo=repo)
    assert admin.patch_company.await_count == 2
    invalidate.assert_called_once()
    repo.set_budget_fence_state.assert_awaited_once()


# ---------------------------------------------------------------------------
# Sync-on-billing-change wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_gate_ticks_the_fence():
    """The /container/status poll is the tab-side reconcile trigger; it must
    fire on BOTH branches (blocked and open), since lifting a stale fence
    matters as much as setting one."""
    from core.services import provision_gate

    calls = []
    with (
        patch.object(fence, "schedule_reconcile", lambda owner_id, **kw: calls.append(owner_id)),
        patch.object(
            provision_gate,
            "_evaluate_provision_gate_inner",
            AsyncMock(return_value=None),
        ),
    ):
        await provision_gate.evaluate_provision_gate(owner_id=OWNER)
    assert calls == [OWNER]


@pytest.mark.asyncio
async def test_fresh_metronome_balance_ticks_the_fence_in_both_directions():
    """``apply_fresh_balance`` is the one hook the low-balance webhook and the
    gate's lazy refresh share, and the only trigger that needs no browser tab
    — it is what stops a credits-exhausted owner storming overnight."""
    from core.services import metronome_gate

    calls = []
    gate = MagicMock()
    gate.set_balance = AsyncMock()
    with (
        patch.object(fence, "schedule_reconcile", lambda owner_id, **kw: calls.append(owner_id)),
        patch.object(metronome_gate, "_maybe_restore_bedrock", AsyncMock()),
        patch.object(metronome_gate, "enforce_on_low_balance", AsyncMock()),
    ):
        await metronome_gate.apply_fresh_balance(OWNER, 5_000, gate=gate)
        await metronome_gate.apply_fresh_balance(OWNER, 0, gate=gate)
    assert calls == [OWNER, OWNER]


@pytest.mark.asyncio
async def test_schedule_reconcile_is_fire_and_forget():
    import asyncio

    seen = []

    async def _fake(owner_id, account=None):
        seen.append(owner_id)

    with (
        patch("core.config.settings.PAPERCLIP_INTERNAL_URL", "http://paperclip.internal"),
        patch.object(fence, "reconcile", _fake),
    ):
        fence.schedule_reconcile(OWNER)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert seen == [OWNER]


@pytest.mark.asyncio
async def test_schedule_reconcile_is_one_per_owner_at_a_time():
    """The in-flight guard caps the ``apply_fresh_balance -> reconcile ->
    is_owner_blocked -> apply_fresh_balance`` cycle at depth 1, and collapses
    the ~3s /container/status poll into a single outstanding reconcile."""
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def _slow(owner_id, account=None):
        seen.append(owner_id)
        started.set()
        await release.wait()

    with (
        patch("core.config.settings.PAPERCLIP_INTERNAL_URL", "http://paperclip.internal"),
        patch.object(fence, "reconcile", _slow),
    ):
        fence.schedule_reconcile(OWNER)
        await started.wait()
        fence.schedule_reconcile(OWNER)  # dropped: one already in flight
        fence.schedule_reconcile(OWNER)
        assert seen == [OWNER]

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Once the first drains, a later trigger is accepted again.
        fence.schedule_reconcile(OWNER)
        await asyncio.sleep(0)
    assert seen == [OWNER, OWNER]


@pytest.mark.asyncio
async def test_schedule_reconcile_noops_without_paperclip_configured():
    import asyncio

    seen = []

    async def _fake(owner_id, account=None):
        seen.append(owner_id)

    with (
        patch("core.config.settings.PAPERCLIP_INTERNAL_URL", ""),
        patch.object(fence, "reconcile", _fake),
    ):
        fence.schedule_reconcile(OWNER)
        await asyncio.sleep(0)
    assert seen == []
