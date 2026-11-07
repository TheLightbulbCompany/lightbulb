"""Agent-initiated email to the owner.

The general primitive behind BOTH first contact and (later) scheduled
routines. It is deliberately REPEATABLE: outbound was never inherently
one-shot — ``ses_email.send_ses_reply`` has no latch and the reply path
already calls it once per inbound message — and a one-shot endpoint would
have blocked a nightly routine on its second night.

**``purpose`` never selects policy.** It is a metric label only. Once-only
comes from ``first_contact_state``, which only the server can read or write,
so an agent cannot skip the guard by mislabelling its send — and does not
have to make a judgment call it might get wrong.

**The team address's row is created HERE, not at provisioning.** Creating it late is what
lets the slug derive from the company name the agent actually chose rather
than the auto-generated placeholder (``generate_company_name`` falls back to
the owner's email local part for a personal owner, so provision-time minting
makes them advertise it forever — and ``company_slug_repo`` has no delete
path to undo that). ``ensure_team_address`` is idempotent, so this costs one
call on a path that runs anyway. The mail goes out under the sending agent's
own name (``agent_email.from_header``).

The workspace name is read live (``workspace_name``: META ``name``, then the
Clerk org name, then the owner id), so a rename before the first send shapes the slug.
"""

import logging
from datetime import datetime, timezone

from core.auth import is_org_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, agent_mail_inbox_repo, container_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import agent_registry, ses_email
from core.services.agent_email import (
    append_agent_mail_footer,
    ensure_team_address,
    from_header,
    resolve_agent_name,
)
from core.services.agent_mail_reply import AGENT_AUTHORED_HEADERS
from core.services.agent_mail_tenant import ensure_tenant
from core.services.owner_facts import write_owner_facts
from core.services.paperclip_owner_email import lookup_owner_email

logger = logging.getLogger(__name__)


async def resolve_apex_ceo_agent_id(*, owner_id: str) -> str | None:
    """The owner's apex CEO (Friday): the rootless ``ceo`` in the registry roster (the first
    ``ceo`` if none is rootless), or ``None`` on any failure (the caller falls back to the
    onboarding row) rather than failing the send."""
    try:
        ceos = [a for a in await agent_registry.roster(owner_id) if a.get("role") == "ceo"]
    except Exception as exc:  # noqa: BLE001 -- degrade, never fail the send
        logger.warning("agent_mail_owner_send: apex CEO lookup failed for owner=%s: %s", owner_id, exc)
        return None
    return next((a["id"] for a in ceos if not a.get("reportsTo")), ceos[0]["id"] if ceos else None)


# Runaway fence. With the one-shot latch gone (it was wrong — see module
# docstring), this is what stops an agent stuck in a loop from mailing its
# owner hundreds of times an hour. Enforced durably in container_repo.
DAILY_SEND_CAP = 50

# Validated to bound metric cardinality, NOT for safety: policy never reads
# `purpose`.
VALID_PURPOSES = ("first_contact", "routine", "trial_recap", "deletion_notice")

# States that mean first contact is still OWED. `failed` is included so a bad
# mint or a failed SES send stays retryable instead of making the first
# failure terminal.
_FIRST_CONTACT_PENDING = ("not_started", "failed")


async def send_to_owner(
    *,
    owner_id: str,
    purpose: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    agent_id: str | None = None,
    tenant_name: str | None = None,
    system_notice: bool = False,
) -> str:
    """Send one owner-directed email.

    Returns "sent", "already_sent", "no_address", "disabled", "rate_limited",
    or "error". Never raises — the caller is an HTTP route whose 500 would tell
    a container nothing useful.

    ``system_notice=True`` marks a send the PLATFORM originates (e.g. the
    outbound-paused complaint notice) rather than an agent reaching out. Such
    a send skips TWO agent-facing fences:

    - the first-contact claim/settle, which it must never consume — that
      would leave the agent's real hello permanently owed; and
    - the daily send cap, which exists to fence a LOOPING AGENT. A platform
      notice ("your outbound is paused") has to reach the owner precisely
      when the agent has been misbehaving, which is exactly when the cap is
      exhausted. It stays bounded anyway: only the backend can set this flag,
      and the router's per-minute burst limiter still applies.

    Everything else is unchanged — it still mints the address and sends.
    """
    try:
        # Validated HERE, not only at the router: `purpose` is a metric
        # dimension, and the routines caller invokes this function IN-PROCESS,
        # bypassing the router's check entirely. An unvalidated value is
        # unbounded CloudWatch cardinality.
        if purpose not in VALID_PURPOSES:
            logger.warning("owner_send: rejecting unknown purpose %r for %s", purpose, owner_id)
            return "error"

        if not (settings.AGENT_EMAIL_DOMAIN or "").strip():
            return "disabled"

        repo = PaperclipRepo(table_name="paperclip-companies")
        onboarding = await repo.get_onboarding_state(owner_id) or {}
        generation = onboarding.get("onboarding_generation") or ""
        provisioned = onboarding.get("teams_state") == "active"
        fallback_ceo_agent_id = onboarding.get("friday_agent_id")
        state = onboarding.get("first_contact_state") or "not_started"

        # POLICY — inferred from state, never from the `purpose` label.
        # A platform notice is never first contact regardless of state.
        is_first_contact = not system_notice and state in _FIRST_CONTACT_PENDING
        if is_first_contact:
            if not await repo.claim_first_contact(owner_id, onboarding_generation=generation):
                put_metric(
                    "agent_email.owner_send",
                    dimensions={"purpose": purpose, "result": "already_sent"},
                )
                return "already_sent"

        async def _fail(reason: str, result: str) -> str:
            """Release the claim as ``failed`` and report ``result``.

            Every exit after a successful claim MUST go through here.
            ``sending`` is neither re-claimable nor nudged, so returning
            without settling would wedge first contact permanently.
            """
            if is_first_contact:
                await repo.settle_first_contact(
                    owner_id,
                    onboarding_generation=generation,
                    state="failed",
                    reason=reason,
                )
            put_metric("agent_email.owner_send", dimensions={"purpose": purpose, "result": result})
            return result

        # Charged only once we KNOW a send will be attempted — after the
        # once-only decision above, never before it. Charging on entry would
        # let an agent that keeps re-calling first_contact and collecting
        # `already_sent` burn the whole daily budget on no-ops, starving a
        # legitimate routine send later the same day. Hammering is still
        # bounded by the router's per-minute burst limiter, and an
        # `already_sent` return costs two DynamoDB ops and nothing else.
        #
        # Routed through _fail so a cap hit after a successful claim releases
        # it (`failed` is retryable and nudged) instead of leaving the row
        # stuck at `sending`.
        #
        # NOT charged for a platform notice: the cap fences a looping AGENT,
        # and the outbound-paused notice must reach the owner exactly when
        # that fence has already tripped. See the docstring.
        if not system_notice:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if not await container_repo.try_consume_daily_email_send(owner_id, day=day, cap=DAILY_SEND_CAP):
                return await _fail("daily_cap_exhausted", "rate_limited")

        owner_email = await lookup_owner_email(
            org_id=owner_id if is_org_owner_id(owner_id) else None,
            fallback_user_id=onboarding.get("user_id") or owner_id,
        )
        if not owner_email:
            return await _fail("no_owner_email", "error")

        if not provisioned:
            return await _fail("incomplete_provisioning", "error")

        resolved_agent_id = agent_id
        if resolved_agent_id is None:
            resolved_agent_id = await resolve_apex_ceo_agent_id(owner_id=owner_id) or fallback_ceo_agent_id
        if not resolved_agent_id:
            return await _fail("incomplete_provisioning", "error")

        address, reason = await ensure_team_address(owner_id, owner_email)
        if not address:
            result = "no_address" if reason in ("slug_allocation_failed", "mint_failed") else "error"
            return await _fail(reason, result)

        # Facts must reflect the address before the agent reads them again: a
        # stale null makes the lightbulb skill tell her she has no address, and she
        # correctly declines to send. Best-effort — the address row is the
        # source of truth, so a facts failure must not block a good send.
        try:
            await write_owner_facts(owner_id)
        except Exception:  # noqa: BLE001
            logger.warning("owner_send: facts rewrite failed for %s", owner_id)

        sent_subject = _env_subject(subject)
        agent_name = await resolve_agent_name(owner_id=owner_id, agent_id=resolved_agent_id)
        if not system_notice:
            body_text, body_html = await append_agent_mail_footer(
                owner_id=owner_id,
                agent_id=resolved_agent_id,
                address=address,
                body_text=body_text,
                body_html=body_html,
                agent_name=agent_name,
            )
        message_id = await ses_email.send_ses_reply(
            from_addr=await from_header(owner_id, resolved_agent_id, address, agent_name=agent_name),
            to=owner_email,
            subject=sent_subject,
            body_text=body_text,
            body_html=body_html,
            headers=dict(AGENT_AUTHORED_HEADERS),
            tenant_name=tenant_name,
        )
        if message_id is None:
            return await _fail("ses_send_failed", "error")
        if agent_name:  # a reply routes to this agent: never record an id that is not this workspace's
            await agent_mail_inbox_repo.put_sent(
                address=address,
                message_id=message_id,
                owner_id=owner_id,
                agent_id=resolved_agent_id,
                to=owner_email,
                subject=sent_subject,
                text=body_text,
            )

        if is_first_contact:
            await repo.settle_first_contact(owner_id, onboarding_generation=generation, state="sent")
        put_metric(
            "agent_email.owner_send",
            dimensions={
                "purpose": purpose,
                "result": "sent",
                "env": settings.ENVIRONMENT or "unknown",
            },
        )
        logger.info("owner_send: sent %s email for %s from %s", purpose, owner_id, address)
        return "sent"
    except Exception:
        logger.exception("owner_send failed for owner_id=%s", owner_id)
        put_metric("agent_email.owner_send", dimensions={"purpose": purpose, "result": "error"})
        return "error"


async def send_to_member(*, owner_id: str, user_id: str, email: str, subject: str, body_text: str) -> str:
    """One email from the CEO, on the team address, to a workspace member who is not the owner
    (the teammate welcome; later the away email). Same daily cap and transport as
    ``send_to_owner``, and the member becomes a verified sender so their reply wakes the CEO
    even when the owner has closed the address to strangers.

    Returns "sent", "disabled", "rate_limited", "no_address", "outbound_disabled" or "error".
    Never raises. Honours the address's complaint pause and sends through the owner's SES tenant.
    ``user_id`` is a log/metric subject only; ``email`` comes from the caller (the Clerk
    membership event carries it). The address is PII: it is never logged.
    """
    result = "error"
    try:
        if not (settings.AGENT_EMAIL_DOMAIN or "").strip():
            result = "disabled"
            return result
        email = (email or "").strip().lower()
        onboarding = await PaperclipRepo(table_name="paperclip-companies").get_onboarding_state(owner_id) or {}
        agent_id = await resolve_apex_ceo_agent_id(owner_id=owner_id) or onboarding.get("friday_agent_id")
        if not email or onboarding.get("teams_state") != "active" or not agent_id:
            return result
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not await container_repo.try_consume_daily_email_send(owner_id, day=day, cap=DAILY_SEND_CAP):
            result = "rate_limited"
            return result
        owner_email = await lookup_owner_email(
            org_id=owner_id if is_org_owner_id(owner_id) else None,
            fallback_user_id=onboarding.get("user_id") or owner_id,
        )
        address, reason = await ensure_team_address(owner_id, owner_email)
        if not address:
            result = "no_address" if reason in ("slug_allocation_failed", "mint_failed") else "error"
            return result
        row = await agent_email_repo.get_by_address(address) or {}
        if row.get("outbound_disabled"):
            # A recipient reported spam: the whole account's outbound is paused (agent_mail_send).
            result = "outbound_disabled"
            return result
        senders = row.get("verified_senders") or []
        if email not in senders:
            # ponytail: read-modify-write; a concurrent owner edit of the list can lose one entry.
            await agent_email_repo.update_settings(address, verified_senders=[*senders, email])
        sent_subject = _env_subject(subject)
        agent_name = await resolve_agent_name(owner_id=owner_id, agent_id=agent_id)
        message_id = await ses_email.send_ses_reply(
            from_addr=await from_header(owner_id, agent_id, address, agent_name=agent_name),
            to=email,
            subject=sent_subject,
            body_text=body_text,
            body_html=None,
            headers=dict(AGENT_AUTHORED_HEADERS),
            tenant_name=await ensure_tenant(owner_id),  # the owner's SES tenant, like every agent send
        )
        if message_id is None:
            return result
        if agent_name:
            await agent_mail_inbox_repo.put_sent(
                address=address,
                message_id=message_id,
                owner_id=owner_id,
                agent_id=agent_id,
                to=email,
                subject=sent_subject,
                text=body_text,
            )
        logger.info("member_send: sent for %s to member %s", owner_id, user_id)
        result = "sent"
        return result
    except Exception:
        logger.exception("member_send failed for owner_id=%s", owner_id)
        result = "error"
        return result
    finally:
        put_metric("agent_email.member_send", dimensions={"result": result})


def _env_subject(subject: str) -> str:
    """Prefix non-prod mail so a dev message can never be mistaken for a real
    one. Both environments send from an isol8.co address into a real inbox,
    which is exactly when that confusion happens.

    Fails toward PREFIXING. An unset ``ENVIRONMENT`` (the config default is an
    empty string) is treated as non-prod, so a misconfigured deploy sends
    obviously-marked mail rather than mail indistinguishable from production.
    Only an explicit ``prod`` suppresses the prefix.
    """
    env = (settings.ENVIRONMENT or "").strip().lower()
    if env == "prod":
        return subject
    return f"[{env or 'non-prod'}] {subject}"
