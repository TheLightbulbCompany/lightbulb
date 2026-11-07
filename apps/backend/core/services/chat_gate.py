"""Pre-chat hard-stop for credit-drawing (openrouter_managed) owners and the free-tier
``trial_local`` daily cost cap."""

from __future__ import annotations

# The user-facing text for each way an owner can run out of Isol8 budget.
# gate_chat's pre-send refusal is the sole reader (the gateway's own mid-run
# reclassification is gone). The web card renders from `code` alone (see
# ChatErrorCard); these strings are what non-web surfaces — room notes,
# logs, the wake refusal frame — show. The iPhone app shows
# them as-is, so they state the fact and never ask for a purchase (App Store
# 3.1.1); the web's own cards and bars carry the upgrade button.
BUDGET_BLOCK_MESSAGES = {
    "trial_expired": "Reading-only — agents start again when the plan is on.",
    "trial_daily_cap": "You've used today's free allowance. It resets tomorrow.",
    "included_daily_cap": "You've used today's included usage. It resets at 00:00 UTC.",
    "out_of_credits": "This workspace is out of credits.",
    "subscription_inactive": "This workspace's plan is not active.",
}


async def budget_block_reason(owner_id: str, account: dict | None) -> str | None:
    """Why this owner's agents cannot run on our budget right now: a
    ``BUDGET_BLOCK_MESSAGES`` key, or None.

    ``gate_chat``'s Layers 2-3 as a plain answer (no subscription layer: a
    canceled owner has its own surfaces). A capped owner is answered by the
    trial/cap predicate ALONE and never reaches the credit check, exactly as
    ``gate_chat`` returns before Layer 3 for them. Read by the failed-run notice
    and ``/billing/account``'s ``block_reason``."""
    from core.services import trial_cap
    from core.services.metronome_gate import is_owner_blocked

    if trial_cap.is_managed_capped(account):
        return await trial_cap.reason_for_account(owner_id, account)
    if account and account.get("provider_choice") == "openrouter_managed" and await is_owner_blocked(owner_id):
        return "out_of_credits"
    return None


async def gate_chat(*, user_id: str, owner_id: str, session_key: str) -> dict:
    """Pre-chat hard-stop for credit-drawing (openrouter_managed) owners and the
    free-tier ``trial_local`` daily cost cap.

    Layers:

    1. **Subscription status.** The billing row's ``subscription_status``
       must be ``active``, ``trialing`` or ``trial_local``. Cards 1 + 2
       used to bypass the gate entirely because their LLM cost lives on
       the user's own provider account — but the *container* itself is on
       us, so a canceled subscription should still block chat. Without
       this, an org/personal user whose ``subscription.deleted`` webhook
       landed could keep talking through the gateway indefinitely on
       cards 1 + 2.
    2. **Free-tier $3/day cost cap.** A ``trial_local`` owner's model
       spend is pure Isol8 cost (no BYOK/OAuth/credit backstop). This is
       a **read-only** check of the daily-spend counter the Bedrock
       invocation-log meter Lambda maintains — the backend never writes
       to it and places no reserve, because the meter counts every
       invocation at the AWS layer and enforces the real stop (revoke the
       owner's inference profile + ``desiredCount=0``). Blocking here is
       purely UX: a clean 402 card instead of a burnt turn. A sub-check
       first blocks with ``code="trial_expired"`` when ``trial_end`` has
       passed. Both are gated on ``subscription_status == "trial_local"``
       — the single trial predicate — so a paid owner mid-upgrade-window
       with a stale ``trial_end`` is never told their trial expired, and
       never capped.
    3. **Credit balance.** When ``provider_choice`` is
       ``openrouter_managed``, the owner must not be blocked by the
       Metronome fast-gate read (webhook-fed flag + lazy getNetBalance
       refresh — see ``core/services/metronome_gate.py``). Cards 1
       + 2 don't draw from prepaid credits, so they're not gated
       on balance.

    All layers key on ``owner_id`` (org_id for org context, user_id
    for personal). ``user_id`` is the chatting member, retained on
    the signature for logging / future per-user telemetry.
    ``session_key`` is retained for logging / future per-turn telemetry.

    Returns:
        ``{"blocked": False}`` when chat may proceed;
        ``{"blocked": True, "code": <code>, "message": ...}``
        when the chat must NOT be forwarded to OpenClaw.

    A raised exception (the cap read is a network call) is intentionally
    NOT caught here — it propagates to the caller, which treats a gate
    error the same as any other chat-send failure (notifies the client,
    never forwards to OpenClaw). Swallowing it into ``{"blocked": False}``
    would be the unsafe direction for a cost cap.
    """
    from core.observability.product_analytics import capture_once_per_owner_per_day
    from core.repositories import billing_repo
    from core.services import trial_cap
    from core.services.metronome_gate import is_owner_blocked
    from core.services.provision_gate import is_subscription_active

    # --- Layer 1: subscription status ----------------------------
    # Anything that isn't currently allowed by ``is_subscription_active``
    # — including a *missing* billing row — gets blocked. Lumping "no
    # row" with "legacy row" would let unfunded users chat through
    # cards 1+2 (whose LLM is on the user but whose container is on us).
    account = await billing_repo.get_by_owner_id(owner_id)
    if not is_subscription_active(account):
        return {
            "blocked": True,
            "code": "subscription_inactive",
            "message": BUDGET_BLOCK_MESSAGES["subscription_inactive"],
        }

    # --- Layer 2: free-tier $3/day cost cap ----------------------
    # Keyed on subscription_status == "trial_local" — the single trial
    # predicate. The container row is NOT consulted: the counter is
    # written by the meter Lambda (which keys on the owner's inference
    # profile, not on any container-row stamp), and the moment billing
    # flips to paid the owner is off the cap by definition — their
    # spend lands on Metronome, not here.
    #
    # Guarded on the status we already have in hand from Layer 1 so a
    # paid owner's chat never pays for the extra billing read
    # ``reason_for_account`` does. That helper self-gates on the same
    # predicate, so this is belt-and-braces, not a second contract.
    if account is not None and trial_cap.is_managed_capped(account):
        # ONE predicate, shared with the provision gate, the container-start
        # chokepoint, /container/recover and the Teams run guards, so no door
        # can disagree about whether a trial owner has runway. This branch
        # used to inline its own trial_end comparison — a third copy of that
        # arithmetic, and exactly the drift the shared helper exists to
        # prevent. ``account`` is already in hand from Layer 1, so
        # reason_for_account spends no extra DDB read.
        reason = await trial_cap.reason_for_account(owner_id, account)
        if reason == trial_cap.TRIAL_EXPIRED:
            return {
                "blocked": True,
                "code": "trial_expired",
                "message": BUDGET_BLOCK_MESSAGES["trial_expired"],
            }
        if reason == trial_cap.DAILY_CAP:
            # Trial funnel telemetry: first block of the UTC day per
            # owner (dedup below) — every later turn re-hits this same
            # branch until midnight or upgrade, so without the dedup a
            # single owner would spam one event per chat send.
            capture_once_per_owner_per_day(owner_id=owner_id, event="trial_cap_reached", distinct_id=user_id)
            return {
                "blocked": True,
                "code": "trial_daily_cap",
                "message": BUDGET_BLOCK_MESSAGES["trial_daily_cap"],
            }
        if reason == trial_cap.INCLUDED_CAP:
            return {
                "blocked": True,
                "code": "included_daily_cap",
                "message": BUDGET_BLOCK_MESSAGES["included_daily_cap"],
            }
        # Under the cap and inside the window: ADMIT. A trial owner's
        # spend is governed by the $3/day meter cap, never by credits, so
        # Layer 3 has no jurisdiction over them and this must not fall
        # through to it.
        #
        # Falling through was survivable only while the credits layer
        # failed OPEN on a missing ledger. It is about to fail CLOSED, and
        # a trial owner can very easily carry provider_choice ==
        # "bedrock_claude": /billing/trial-checkout writes it
        # SYNCHRONOUSLY before redirecting to Stripe, so anyone who opens
        # Checkout and closes the tab keeps it while staying trial_local.
        # Trial owners are deliberately never given a Metronome ledger
        # (ensure_metronome_customer short-circuits them), so Layer 3
        # would find no ledger and block them out of a fully-funded free
        # tier — permanently, since the midnight cap reset does not clear
        # a credits verdict. `provision_gate` already returns here rather
        # than falling through; this makes the chat gate agree.
        return {"blocked": False}

    # --- Layer 3: card-3 credit balance --------------------------
    # provider_choice lives on the billing_accounts row (per owner);
    # credits also pool at the owner level so org members all draw
    # from one balance funded by the
    # admin. Keying on user_id meant org members saw balance=0 and
    # got blocked at chat with code=out_of_credits.
    if account is None or account.get("provider_choice") != "openrouter_managed":
        return {"blocked": False}

    if await is_owner_blocked(owner_id):
        return {
            "blocked": True,
            "code": "out_of_credits",
            "message": BUDGET_BLOCK_MESSAGES["out_of_credits"],
        }
    return {"blocked": False}
