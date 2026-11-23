"""Admin Account-Wipe orchestrator.

(docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md)

Two entry points:

* ``preview_wipe(owner_id)`` — READ-ONLY. Reads the owner's identifier rows
  (containers/paperclip-companies/billing-accounts/users) and, for the EFS
  workspace directory, does a filesystem existence check. Emits a
  ``WipeReport`` of ``would_delete``/``skipped`` steps. Calls NO delete
  method — safe to call as many times as an admin wants before committing.

* ``wipe_account(owner_id, *, actor_admin_id)`` — the ordered teardown (spec
  "complete teardown map", steps 1-12). Reads the SAME identifier rows up
  front so every later step already has the ARNs/ids it needs off a row an
  earlier step deletes — but that snapshot decides no DELETE (see
  ``_Identifiers``: it is stale by construction, and the gate-token step
  re-reads for exactly that reason). Then runs each teardown step in its own
  try/except. A step that raises is recorded
  ``status="failed"`` with the error and execution CONTINUES — this
  partial-failure tolerance is the load-bearing property: one
  collaborator's timeout must not block the Clerk delete (step 12, always last, so a
  killed account can never regrow on the next ``/container/status`` poll).

Refuses a malformed user id (``MalformedUserIdError``) — the target is always a
Clerk USER. Two callers: the admin wipe, and the Clerk ``user.deleted`` webhook
(``clerk_user_deleted=True``: no Clerk delete, a held phone line kept for an
admin wipe). The wipe calls neither the Paperclip server nor its Aurora
database (Paperclip exit, task 8b.7).

Step-to-spec-row mapping. The design spec's teardown map lists 12 rows;
this orchestrator emits one ``WipeStep`` per row EXCEPT rows 5 and 11,
which fold into sibling steps because no standalone collaborator exists
for them (see the inline notes at those call sites):

  spec #2  -> gone (the Better Auth user lived in Paperclip's Aurora)
  (no spec row) -> "bedrock_gate_tokens" (bedrock_gate_tokens.delete_gate_token_rows)
  (no spec row) -> "engine"           (wipe_engine_state: meeting bots, workflows, Schedules, tasks rows)
  spec #3  -> "runtime"               (hermes_provisioning.delete_workspace_runtime)
  spec #4  -> folded into "runtime" (the S3 profile prefix IS the owner's content
              on this transport; there is no EFS tree left)
  spec #5  -> folded into "runtime" (no per-owner EFS access point left either)
  spec #6-7-> folded into "runtime" (bedrock_aip.reap_owner_bedrock_resources)
  spec #8  -> folded into "runtime" (bedrock_aip.reap_owner_bedrock_resources)
  spec #9  -> "metronome"             (MetronomeClient.archive_customer)
  spec #10 -> "stripe"                (billing_service.delete_stripe_customer)
  spec #11 -> "usage_counters" / "messaging" / "paperclip_row" /
              "billing_row" / "user_row"
              (containers row already folded into
              "runtime" — see note below; "paperclip_row" also carries the
              owner's ``__pending_brief__:`` item, which lives in the SAME
              table under a prefixed key)
  spec #12 -> "clerk_user"            (clerk_admin.delete_user) -- ALWAYS LAST

KNOWN GAP — NOT YET COVERED (honest, not "complete"): this orchestrator does
NOT wipe the ``integrations`` DDB table, the ``oauth-tokens`` table, the
``api-keys`` table, or the Secrets-Manager ``integration_secret_store``
(BYOK API keys + ChatGPT OAuth token material). No collaborator exists yet
for any of them. A trial_local account has none of these rows, so this does
not block the trial-account dogfood use case, but it IS a real gap for paid
Bedrock-managed, ChatGPT-OAuth, and BYOK accounts — those credentials
survive a wipe today. Follow-up work, not yet built.

Also NOT wiped: the ``company-slugs`` table (a wiped account keeps its slug
reservation permanently — there is no delete path, so the name can never be
handed to another owner) and the ``agent-email-addresses`` table (once
``AGENT_EMAIL_DOMAIN`` is live, a wiped account leaves its address row
pointing at a deleted owner, so inbound mail to that address has nowhere
valid to route). No collaborator exists yet for either.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

import boto3
import httpx

from core.billing.bedrock_gate_tokens import delete_gate_token_rows, owner_gate_token_hashes
from core.billing.metronome_client import for_settings as metronome_for_settings
from core.billing.metronome_fast_gate import MetronomeFastGate
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.services.credentials import integration_secret_store
from core.services.hermes_provisioning import delete_workspace_runtime
from core.voice.voice_calls import wipe_owner_voice
from core.repositories import (
    agent_email_repo,
    billing_repo,
    company_slug_repo,
    container_repo,
    integrations_repo,
    messaging_repo,
    oauth_state_repo,
    openrouter_key_repo,
    slack_installs_repo,
    usage_repo,
    user_repo,
)
from core.repositories.workspace_repo import Identity, WorkspaceRepo
from core.engine import api as engine_api
from core.services import clerk_admin, company_stripe, connection_service, text_lines
from core.voice.meetings import records as meeting_records
from core.voice.phone import voice_onboarding
from core.services.billing_service import delete_stripe_customer
from core.services.oauth_service import revoke_user_oauth
from core.services.owner_secret_sweep import (
    _list_names_with_prefix,
    delete_owner_secrets,
    list_owner_secret_names,
)

logger = logging.getLogger(__name__)


async def release_text_lines(rows: list[dict]) -> tuple[int, list[str]]:
    """Release the texting line of every owner holding a ``text`` or ``text_pending`` row, through the one
    release path (``text_lines.release_line``: cancel a pending operation, release a finished one or the line).

    Best effort. Returns (owners released, what failed to release: an operation or a line)."""
    by_owner: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("kind") in ("text", "text_pending"):
            by_owner.setdefault(r.get("owner_id", ""), []).append(r)
    released, failed = 0, []
    for owner_id, owned in by_owner.items():
        try:
            await text_lines.release_line(owner_id)
            released += 1
        except Exception as exc:
            logger.warning("wipe: could not release the text line of %s: %s", owner_id, exc)
            failed += [
                f"operation {r['operation_id']}" if r.get("kind") == "text_pending" else r.get("address") or "?"
                for r in owned
            ]
    return released, failed


async def wipe_engine_state(owner_id: str) -> str:
    """Stop what keeps running for the owner, then delete the engine's rows: every meeting bot still booked or in a
    call is cancelled (the bot ids live on the room META, so this runs before the messaging step deletes it), the
    engine's workflows, Schedules and tasks partition go (`engine.api.delete_tenant`), and the owner's ws-connections
    rows. Idempotent. A bot Recall would not cancel fails the step, naming it, after everything else ran."""
    stuck: list[str] = []
    meetings = 0
    for room_id in await messaging_repo.list_owner_room_ids(owner_id):
        meta = await messaging_repo.get_room(room_id) or {}
        meeting = meta.get("meeting") if meta.get("ownerId") == owner_id else None
        if not meeting:
            continue
        try:
            await meeting_records.end_meeting(room_id, meeting)
            meetings += 1
        except Exception:  # noqa: BLE001 -- one refused bot must not keep the rest booked
            bot = str(meeting.get("recallBotId"))
            logger.warning("wipe: meeting bot not cancelled room=%s bot=%s", room_id, bot, exc_info=True)
            stuck.append(bot)
    # The room is deleted after this step, so its error is the only place a stuck bot id survives.
    stuck_note = f"Recall did not cancel bot(s) {', '.join(stuck)}: cancel them by hand" if stuck else ""
    try:
        engine = await engine_api.delete_tenant(owner_id)
        connections = await connection_service.delete_all_for_user(owner_id)
    except Exception as exc:
        if stuck_note:
            raise RuntimeError(f"{exc}; {stuck_note}") from exc
        raise
    detail = (
        f"closed {meetings} meeting room(s); terminated {engine['workflows']} workflow(s), stopped {engine['sessions']} "
        f"task session(s), deleted {engine['schedules']} schedule(s), {engine['rows']} tasks row(s) and "
        f"{connections} ws-connection row(s)"
    )
    if stuck_note:
        raise RuntimeError(f"{detail}; {stuck_note}")
    return detail


async def _delete_openrouter_key(owner_id: str) -> None:
    """Drop the key row and every secret under the owner's OpenRouter prefix
    (earlier hashes included). Only called once the key is ``disabled``."""
    prefix = f"lightbulb/{settings.ENVIRONMENT or 'dev'}/openrouter/owners/{owner_id}/"

    def _delete() -> None:
        sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        for name in _list_names_with_prefix(sm, prefix):  # legacy, until migrate_secrets_to_envelopes runs
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        for provider in integration_secret_store.envelope_providers(owner_id):
            if provider.startswith("openrouter/"):
                integration_secret_store.delete_envelope(owner_id, provider)
        get_table("openrouter-keys").delete_item(Key={"owner_id": owner_id})

    await run_in_thread(_delete)


async def _delete_fast_gate_row(owner_id: str) -> str:
    await MetronomeFastGate().delete_row(owner_id)
    return "metronome fast-gate row deleted"


async def retire_openrouter_key(owner_id: str) -> str:
    """Retire the owner's managed OpenRouter key, or delete it once the settlement worker has disabled it."""
    row = await openrouter_key_repo.get(owner_id)
    if row is None:
        return "no managed OpenRouter key"
    if row.get("state") != "disabled":
        # The same retirement entitlement_teardown uses. The settlement worker
        # drains pending generations with the key's secret, then disables it
        # upstream; deleting row + secret now would strand a LIVE upstream key.
        # Retiring is terminal for the wipe: the gate refuses a non-active key and no runtime
        # ever holds it, so nothing can spend it. The worker's own cleanup finishes it.
        await openrouter_key_repo.mark_retiring(owner_id)
        return f"OpenRouter key {row.get('state')} -> retiring; the settlement worker disables and cleans it up"
    await _delete_openrouter_key(owner_id)
    return "disabled OpenRouter key row + secrets deleted"


def text_release_note(released: int, failed: list[str]) -> str:
    note = f", released {released} text line(s)"
    if failed:
        note += f", FAILED to release: {', '.join(failed)} (rows kept; run the wipe again)"
    return note


ENGINE_PREVIEW = (
    "meeting bots still booked or in a call cancelled; Temporal workflows terminated and Schedules deleted; "
    "tasks partition and ws-connection rows"
)

# "refused" = a guard kept the resource on purpose. Neither a success nor a
# failure, and never a delete.
WipeStatus = Literal["ok", "skipped", "failed", "would_delete", "refused"]


async def resolve_account_emails(owner_id: str) -> list[str]:
    """Every email address known for the owner, Clerk first. Clerk is the source of
    truth for the type-to-confirm match: the users DDB
    row's email is written by an unreliable webhook and is frequently absent, so
    matching against it alone can make a legitimate wipe impossible to confirm.
    Falls back to the users-row email (deduped) when Clerk is unavailable
    (e.g. CLERK_SECRET_KEY unset in local/test)."""
    emails: list[str] = []
    clerk_user = await clerk_admin.get_user(owner_id)
    if clerk_user:
        for e in clerk_user.get("email_addresses") or []:
            addr = e.get("email_address")
            if addr and addr not in emails:
                emails.append(addr)
    row = await user_repo.get(owner_id)
    row_email = (row or {}).get("email")
    if row_email and row_email not in emails:
        emails.append(row_email)
    return emails


class MalformedUserIdError(Exception):
    """``owner_id`` is not a well-formed Clerk user id (``user_`` and alphanumerics): refused before any read
    or delete, since every step deletes by this id or a prefix built from it."""

    def __init__(self, owner_id: str):
        super().__init__(f"Account wipe needs a Clerk user id: {owner_id!r} is not one.")
        self.owner_id = owner_id


#: A Clerk user id, in the engine's tenant-id style (``core.engine.api._TENANT_ID``).
_USER_ID = re.compile(r"user_[A-Za-z0-9]{1,64}")


@dataclass
class WipeStep:
    system: str
    action: str
    status: WipeStatus
    detail: str = ""
    error: str | None = None


@dataclass
class WipeReport:
    owner_id: str
    steps: list[WipeStep]
    dry_run: bool

    def to_dict(self) -> dict:
        return {
            "owner_id": self.owner_id,
            "dry_run": self.dry_run,
            "steps": [
                {
                    "system": s.system,
                    "action": s.action,
                    "status": s.status,
                    "detail": s.detail,
                    "error": s.error,
                }
                for s in self.steps
            ],
        }


@dataclass
class _Identifiers:
    """The identifier rows read up front, before any delete runs — steps
    6-11 (bedrock/metronome/stripe/DDB cleanup) all need values off these
    rows, and some of those rows get deleted by earlier steps.

    WHAT THIS SNAPSHOT IS NOT: authoritative about what exists. Every read
    below is eventually consistent except ``get_pending_brief``, and all of
    them are taken before the first teardown step runs — so a row written by a
    provision (or an onboarding submission) that overlaps the wipe is missing
    from it. Use it to CARRY values a later step can no longer read (ARNs off a
    row an earlier step deletes) and to shape the preview. Never let it decide
    that an idempotent delete can be skipped: that is how a wipe reports
    success over a row it never touched. Two steps take the fresh answer
    instead — ``bedrock_gate_tokens`` (re-reads consistently, deletes the
    union) and ``paperclip_row`` (always attempts the pending-brief delete).

    The Bedrock footprint no longer reads this snapshot at all: it is reaped
    inside the ``runtime`` step off the containers row's own ALL_OLD image,
    so an AIP minted mid-wipe is caught as long as the row named it when it
    died. Still snapshot-gated, and knowingly: the ``runtime`` step itself,
    and the ``billing_row`` / ``user_row`` deletes.
    None of those rows is the sole handle on anything else, so the residue is
    findable and a re-run clears it.
    """

    user_row: dict | None
    container: dict | None
    paperclip: Identity | None
    """The human's identity row in paperclip-companies."""
    billing: dict | None
    pending_brief: dict | None
    """The owner's ``__pending_brief__:`` item, if any. Read independently of
    ``paperclip`` because it is a SEPARATE item in the same table under a
    prefixed key, and routinely outlives (or predates) the company row — the
    funnel is answered during provisioning, so an owner whose seed never landed
    has a pending brief and no company row at all."""


async def _read_identifiers(owner_id: str) -> _Identifiers:
    user_row = await user_repo.get(owner_id)
    container = await container_repo.get_by_owner_id(owner_id)
    workspace_repo = WorkspaceRepo()
    paperclip = await workspace_repo.get_identity(owner_id)
    pending_brief = await workspace_repo.get_pending_brief(owner_id)
    billing = await billing_repo.get_by_owner_id(owner_id)
    return _Identifiers(
        user_row=user_row,
        container=container,
        paperclip=paperclip,
        billing=billing,
        pending_brief=pending_brief,
    )


def _refuse_malformed_id(owner_id: str) -> None:
    if not _USER_ID.fullmatch(owner_id or ""):
        raise MalformedUserIdError(owner_id)


# ---------------------------------------------------------------------------
# Compound collaborators — wrapped so each is a single mockable async unit.
# ---------------------------------------------------------------------------


async def _archive_metronome_customer(customer_id: str) -> None:
    """MetronomeClient.archive_customer via the settings-derived client
    (mirrors metronome_gate.read_net_balance_microcents's client lifecycle)."""
    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10.0) as http:
        await metronome_for_settings(http).archive_customer(customer_id=customer_id)


# ---------------------------------------------------------------------------
# Step-runner helpers
# ---------------------------------------------------------------------------


def _runtime_detail(report: dict) -> str:
    """The runtime step's detail; "scheduled" when a parked member keeps the runtime a while."""
    if report.get("runtime") == "scheduled":
        return (
            "containers row removed; agentcore runtime + boot secret + S3 profiles "
            "teardown scheduled (after the cloud member forgets its entries, within a few minutes)"
        )
    return "agentcore runtime + boot secret + S3 profiles + containers row removed"


async def _step(steps: list[WipeStep], *, system: str, action: str, fn: Callable[[], Awaitable[str | None]]) -> None:
    """Run one teardown collaborator, in its own try/except.

    ``fn`` performs the delete and returns a short detail string (or None).
    A raise is recorded as a "failed" step with the error message and does
    NOT propagate — every other step still runs. This is the partial-failure
    tolerance the design spec calls out as the orchestrator's core property.
    """
    try:
        detail = await fn()
        steps.append(WipeStep(system=system, action=action, status="ok", detail=detail or ""))
    except Exception as e:  # noqa: BLE001 - partial-failure tolerance is the point
        logger.exception("account_wipe: step failed system=%s action=%s", system, action)
        steps.append(WipeStep(system=system, action=action, status="failed", error=str(e)))


def _skip(steps: list[WipeStep], system: str, action: str, detail: str) -> None:
    steps.append(WipeStep(system=system, action=action, status="skipped", detail=detail))


async def _wipe_company_stripe(steps: list[WipeStep], *, owner_id: str) -> None:
    """Step 8b: revoke the platform's link to the company's Stripe account.

    NEVER deletes the account — it is the owner's business account and keeps
    working as a normal Stripe account after we let go (spec §3.6).

    Reads the billing row FRESH (strongly consistent) rather than taking the
    ``_read_identifiers`` snapshot: everything above this step is slow (ECS,
    EFS, Aurora), and an OAuth callback landing in that window binds an
    account the snapshot cannot know about. Deauthorizing a stale id — or
    skipping a live one and leaving the platform attached to the owner's
    Stripe account after their account is gone — is the failure this avoids.
    """
    row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    account_id = row.get("company_stripe_account_id")
    if not account_id:
        _skip(steps, "stripe", "deauthorize_company_account", "no company_stripe_account_id on billing row")
        return

    async def _s8b(account_id: str = account_id) -> str:
        # `release_and_unbind` clears the binding whatever Stripe answers —
        # the unbind used to sit on the far side of the deauthorize, so a
        # raise left the row bound and the platform attached, which is the
        # single failure this step exists to prevent. Raising AFTER that
        # unbind is how the report records "Stripe never confirmed": _step
        # marks the step failed and the wipe carries on.
        if not await company_stripe.release_and_unbind(
            owner_id=owner_id, account_id=account_id, source=row.get("company_stripe_source")
        ):
            raise RuntimeError(f"unbound {account_id} locally, but Stripe did not confirm the revoke")
        return f"company_stripe_account_id={account_id} deauthorized (account retained by owner)"

    await _step(steps, system="stripe", action="deauthorize_company_account", fn=_s8b)


def _preview_step(
    steps: list[WipeStep],
    *,
    system: str,
    action: str,
    exists: bool,
    detail_if_exists: str,
    detail_if_absent: str,
) -> None:
    steps.append(
        WipeStep(
            system=system,
            action=action,
            status="would_delete" if exists else "skipped",
            detail=detail_if_exists if exists else detail_if_absent,
        )
    )


# ---------------------------------------------------------------------------
# preview_wipe — read-only
# ---------------------------------------------------------------------------


async def preview_wipe(owner_id: str) -> WipeReport:
    """Read-only enumeration of what a wipe would delete. Calls NO delete
    method — every read here is a GetItem/Query (or, for the EFS workspace
    directory, a filesystem existence check)."""
    _refuse_malformed_id(owner_id)

    ids = await _read_identifiers(owner_id)
    steps: list[WipeStep] = []

    gate_token_hashes = owner_gate_token_hashes(ids.container)
    _preview_step(
        steps,
        system="bedrock_gate_tokens",
        action="delete_gate_token_rows",
        exists=bool(gate_token_hashes),
        detail_if_exists=f"{len(gate_token_hashes)} gate-token row(s)",
        detail_if_absent="no bedrock-gate token hash on containers row",
    )
    steps.append(WipeStep("engine", "wipe_engine_state", "would_delete", ENGINE_PREVIEW))

    _preview_step(
        steps,
        system="runtime",
        action="delete_workspace_runtime",
        exists=ids.container is not None,
        detail_if_exists=(
            f"agentcore_runtime_arn={(ids.container or {}).get('agentcore_runtime_arn', '')} "
            "(+ boot secret, S3 profiles, containers row, "
            "and the Bedrock footprint: gate-token rows, AIPs, IAM role)"
        ),
        detail_if_absent="no containers row",
    )

    # No standalone bedrock_aips / bedrock_role preview steps: the executed wipe
    # no longer has them either. Both are reaped inside the runtime step, by
    # the shared reaper at the seam every teardown path crosses. Preview and
    # execution have to list the same systems or the preview lies.

    metronome_customer_id = (ids.billing or {}).get("metronome_customer_id")
    _preview_step(
        steps,
        system="metronome",
        action="archive_customer",
        exists=bool(metronome_customer_id),
        detail_if_exists=metronome_customer_id or "",
        detail_if_absent="no metronome_customer_id on billing row",
    )

    steps.append(WipeStep("metronome_fast_gate", "delete_row", "would_delete", "metronome fast-gate row"))

    stripe_customer_id = (ids.billing or {}).get("stripe_customer_id")
    _preview_step(
        steps,
        system="stripe",
        action="delete_stripe_customer",
        exists=bool(stripe_customer_id),
        detail_if_exists=stripe_customer_id or "",
        detail_if_absent="no stripe_customer_id on billing row",
    )

    company_stripe_account_id = (ids.billing or {}).get("company_stripe_account_id")
    _preview_step(
        steps,
        system="stripe",
        action="deauthorize_company_account",
        exists=bool(company_stripe_account_id),
        detail_if_exists=company_stripe_account_id or "",
        detail_if_absent="no company_stripe_account_id on billing row",
    )

    # usage-counters has no cheap existence check (would need a bounded
    # Query); delete_all_for_owner is idempotent on an empty set, so the
    # preview always shows would_delete rather than adding a second read.
    steps.append(
        WipeStep(
            system="usage_counters",
            action="delete_all_for_owner",
            status="would_delete",
            detail="all usage-counter rows for owner",
        )
    )
    room_ids = await messaging_repo.list_owner_room_ids(owner_id)
    steps.append(
        WipeStep(
            system="messaging",
            action="delete_owner_rooms_and_streams",
            status="would_delete",
            detail=(f"{len(room_ids)} owned room partition(s), then owner catalog and private user-stream mappings"),
        )
    )
    steps.append(
        WipeStep(
            system="person_memory",
            action="delete_person_memory",
            status="would_delete",
            detail="uploaded sessions, drafts, episodes and computer keys",
        )
    )

    paperclip_rows = [
        label
        for label, present in (
            ("paperclip identity row", ids.paperclip is not None),
            ("pending_brief row", ids.pending_brief is not None),
        )
        if present
    ]
    _preview_step(
        steps,
        system="paperclip_row",
        action="paperclip_repo.delete",
        exists=bool(paperclip_rows),
        detail_if_exists=" + ".join(paperclip_rows),
        detail_if_absent="no paperclip-companies or pending_brief row",
    )
    _preview_step(
        steps,
        system="billing_row",
        action="billing_repo.delete",
        exists=ids.billing is not None,
        detail_if_exists="billing-accounts row",
        detail_if_absent="no billing-accounts row",
    )
    _preview_step(
        steps,
        system="user_row",
        action="user_repo.delete",
        exists=ids.user_row is not None,
        detail_if_exists="users row",
        detail_if_absent="no users row",
    )

    # Clerk is external; we don't spend a live GET here just for the
    # preview (preview_wipe touches only our own DDB rows + EFS). Always
    # shown as would_delete — the same convention as usage_counters above.
    # Credential + identity leftovers. A preview that omits these tells an
    # admin the credentials survive — the impression this report exists to
    # correct. All read-only.
    secret_names = await list_owner_secret_names(owner_id)
    _preview_step(
        steps,
        system="owner_secrets",
        action="delete_owner_secrets",
        exists=bool(secret_names),
        detail_if_exists=f"{len(secret_names)} secret(s): BYOK keys, integration creds, OAuth apps",
        detail_if_absent="no owner secrets",
    )

    key_row = await openrouter_key_repo.get(owner_id)
    _preview_step(
        steps,
        system="openrouter_key",
        action="retire_and_delete",
        exists=key_row is not None,
        detail_if_exists=f"managed OpenRouter key (state={(key_row or {}).get('state')}): retire, then row + secret",
        detail_if_absent="no managed OpenRouter key",
    )

    integration_rows = await integrations_repo.list_for_owner(owner_id)
    _preview_step(
        steps,
        system="integrations",
        action="delete_all_for_owner",
        exists=bool(integration_rows),
        detail_if_exists=f"{len(integration_rows)} integration row(s)",
        detail_if_absent="no integration rows",
    )
    # Pending OAuth states live at most ten minutes: the preview names the step without scanning for them.
    steps.append(WipeStep(system="oauth_state", action="delete_all_for_owner", status="would_delete", detail=owner_id))

    steps.append(WipeStep(system="oauth_tokens", action="revoke_user_oauth", status="would_delete", detail=owner_id))

    email_rows = await agent_email_repo.query_by_owner(owner_id)
    _preview_step(
        steps,
        system="agent_email_addresses",
        action="delete_all_for_owner",
        exists=bool(email_rows),
        detail_if_exists=f"{len(email_rows)} address(es): " + ", ".join(r.get("address", "?") for r in email_rows),
        detail_if_absent="no agent email addresses",
    )

    install_rows = await slack_installs_repo.list_for_owner(owner_id)
    steps.append(
        WipeStep(
            system="slack_installs",
            action="delete_all_for_owner",
            status="would_delete",
            detail=", ".join(r["team_id"] for r in install_rows) or "none",
        )
    )

    slug_row = await company_slug_repo.get_by_owner(owner_id)
    _preview_step(
        steps,
        system="company_slug",
        action="delete_for_owner",
        exists=slug_row is not None,
        detail_if_exists=f"slug {(slug_row or {}).get('slug')}",
        detail_if_absent="no slug reservation",
    )

    steps.append(WipeStep(system="clerk_user", action="delete_user", status="would_delete", detail=owner_id))

    return WipeReport(owner_id=owner_id, steps=steps, dry_run=True)


# ---------------------------------------------------------------------------
# wipe_account — the ordered teardown
# ---------------------------------------------------------------------------


async def wipe_account(owner_id: str, *, actor_admin_id: str, clerk_user_deleted: bool = False) -> WipeReport:
    """Execute the ordered teardown (design spec steps 1-12).

    ``clerk_user_deleted``: Clerk already deleted the person (the ``user.deleted`` webhook). Then the wipe
    deletes no Clerk user and releases no phone line: a tenant holding one keeps
    its messaging rows so an admin wipe can release the line.

    ``actor_admin_id`` is not used for a delete decision here — it exists so
    callers (and any future audit hook inside this function) have the actor
    without threading it through every step. The router (Task 4) is the one
    that writes the admin-actions audit row with this id + the returned
    report.
    """
    _refuse_malformed_id(owner_id)

    ids = await _read_identifiers(owner_id)
    steps: list[WipeStep] = []

    logger.info(
        "account_wipe.wipe_account: starting for owner=%s actor=%s",
        owner_id,
        actor_admin_id,
    )

    # --- Step 2b: bedrock-gate token rows ----------------------------------
    # The gate-tokens table is keyed on the token hash with NO owner index, so
    # the containers row — ``bedrock_gate_token_hash`` plus every hash tracked
    # in ``bedrock_gate_superseded_hashes`` — is the only handle on an owner's
    # rows. Step 3 below destroys that row (inside delete_user_service), so
    # this is ordered ahead of it: a row that outlives its handle is
    # unreachable forever — unfindable, unexpirable, and outside
    # deny_gate_token's reach, at ``denied=False``. Four were stranded that way
    # in prod by wipes run before this step existed.
    #
    # THE SNAPSHOT IS NOT AUTHORITATIVE HERE, and ordering alone does not make
    # it so. ``_read_identifiers`` reads the containers row eventually
    # consistently, and reads it before any step runs — so a
    # ``mint_gate_token`` that landed just before the wipe, or concurrently with
    # those steps, can leave its ``denied=False`` hash off ``ids.container``
    # entirely. Re-read STRONGLY CONSISTENT at the moment of use and delete the
    # UNION of both: deleting a hash twice is a free no-op, missing one is
    # permanent. (Union, not replacement — a mint that superseded and pruned a
    # hash between the two reads leaves it named only by the snapshot.)
    #
    # The window this cannot close — a mint between that read and the row's
    # death — is closed by the sweep in step 3, off the DELETE's own ALL_OLD
    # image. That sweep also backstops this step failing outright.
    snapshot_gate_hashes = owner_gate_token_hashes(ids.container)

    async def _s2b() -> str:
        fresh = await container_repo.get_by_owner_id(owner_id, consistent=True)
        hashes = list(dict.fromkeys([*snapshot_gate_hashes, *owner_gate_token_hashes(fresh)]))
        if not hashes:
            # chatgpt_oauth / byo_key owners never mint one — a genuine no-op.
            return "no bedrock-gate token hash on containers row"
        deleted = await delete_gate_token_rows(hashes)
        return f"deleted {deleted} of {len(hashes)} gate-token row(s)"

    await _step(steps, system="bedrock_gate_tokens", action="delete_gate_token_rows", fn=_s2b)

    # --- Step 2c: engine state, before the runtime and the rooms go ------------
    await _step(steps, system="engine", action="wipe_engine_state", fn=lambda: wipe_engine_state(owner_id))

    # --- Step 3 (spec #3 + #4 + #5 folded): the runtime ------------------
    # ``hermes_provisioning.delete_workspace_runtime`` already does, in the right internal
    # order: StopRuntimeSession + DeleteAgentRuntime -> revoke the live gate-token hash ->
    # delete the boot secret -> delete every object under ``<owner>/hermes/`` (spec #4's
    # "content" step, which on this transport IS the S3 profile prefix -- there is no EFS
    # tree or access point left) -> delete the containers row (part of spec #11) -> reap the
    # owner's Bedrock footprint. One WipeStep stands in for spec
    # rows #3, #4, #5 and the containers-row slice of #11.
    # Runs with or without a containers row, like the org wipe: the seam's row-independent cleanups
    # (S3 profile prefix, browser state, Bedrock reap, workspace memory) are keyed by owner id alone and
    # idempotent, and a re-run after a failure late in the seam finds the row already deleted. A missing
    # row skips only what needs it (the runtime session and its row-named handles).
    async def _s3() -> str:
        try:  # end live voice calls before the ledger is archived; never abort the wipe
            await wipe_owner_voice(owner_id)
        except Exception:  # noqa: BLE001
            logger.warning("voice wipe failed owner=%s", owner_id, exc_info=True)
        # M6-R42: this is one of exactly TWO terminal callers that pass
        # ``delete_profiles=True`` -- this wipe (an admin's, or Clerk ``user.deleted``'s),
        # and ``routers/debug.py``'s ``DELETE /debug/user-data`` (the e2e
        # empty-slate reset; debug is 403 in prod).
        # Every other caller (entitlement_teardown on a lapsed card, debug's routine
        # reprovision) leaves it False and keeps the owner's memory/skills/SOUL.md/
        # auth.json. Do not re-broaden it.
        report: dict = {}
        row_at_death = await delete_workspace_runtime(owner_id, delete_profiles=True, report=report)
        if ids.container is None and row_at_death is None:
            return (
                "no containers row; owner-keyed cleanup done (S3 profiles, browser state, Bedrock footprint, "
                "workspace memory)."
            )
        detail = _runtime_detail(report)
        # FINAL SWEEP — the last moment any gate-token hash is reachable.
        # ``delete_workspace_runtime`` returns the row's own ALL_OLD image, so
        # this covers exactly what step 2b could not see: a hash minted
        # after its strongly-consistent re-read, and every hash at all if
        # that step failed. Already-deleted hashes cost a free no-op
        # DeleteItem and are reported as 0, so no bookkeeping is needed to
        # tell the two apart. A failure here fails THIS step rather than
        # passing silently — the residue is real and the wipe is retryable.
        late_hashes = owner_gate_token_hashes(row_at_death)
        swept = await delete_gate_token_rows(late_hashes) if late_hashes else 0
        if swept:
            detail += f" (+ swept {swept} gate-token row(s) still named on the row at delete time)"
        return detail

    await _step(steps, system="runtime", action="delete_workspace_runtime", fn=_s3)

    # --- Steps 5-6 (spec #6-7-8): Bedrock AIPs + owner role ----------------
    # Deliberately NOT separate steps any more. Both are reaped inside step 3's
    # ``delete_workspace_runtime`` (see bedrock_aip.reap_owner_bedrock_resources),
    # which is the seam EVERY teardown path crosses -- the entitlement
    # cancellation webhook and both /debug teardowns leaked these until it moved
    # there, and dev/staging/prod share one AWS account and therefore one
    # 1,000-role IAM quota.
    #
    # This is an upgrade, not just deduplication. These steps reaped from
    # ``ids.container``, the snapshot taken at the TOP of the wipe; the seam
    # reaps from the row's own ALL_OLD image at the instant it was deleted,
    # which is the authoritative answer to what the row named when it died.
    # Step 6 was also gated on ``bedrock_role_arn`` being present on that
    # snapshot, so a legacy row without the field silently skipped the role
    # delete and stranded it forever -- the reaper derives the role name from
    # owner_id and never gates on the field.

    # --- Step 7 (spec #9): Metronome ---------------------------------------
    metronome_customer_id = (ids.billing or {}).get("metronome_customer_id")
    if metronome_customer_id:

        async def _s7(metronome_customer_id: str = metronome_customer_id) -> str:
            await _archive_metronome_customer(metronome_customer_id)
            return metronome_customer_id

        await _step(steps, system="metronome", action="archive_customer", fn=_s7)
    else:
        _skip(steps, "metronome", "archive_customer", "no metronome_customer_id on billing row")

    await _step(
        steps,
        system="metronome_fast_gate",
        action="delete_row",
        fn=lambda: _delete_fast_gate_row(owner_id),
    )

    # --- Step 8 (spec #10): Stripe ------------------------------------------
    stripe_customer_id = (ids.billing or {}).get("stripe_customer_id")
    if stripe_customer_id:

        async def _s8(stripe_customer_id: str = stripe_customer_id) -> str:
            deleted = await delete_stripe_customer(owner_id)
            return f"stripe_customer_id={stripe_customer_id} deleted={deleted}"

        await _step(steps, system="stripe", action="delete_stripe_customer", fn=_s8)
    else:
        _skip(steps, "stripe", "delete_stripe_customer", "no stripe_customer_id on billing row")

    # --- Step 8b: company Stripe account — deauthorize only ------------------
    await _wipe_company_stripe(steps, owner_id=owner_id)

    # --- Step 9 (spec #11, remaining tables) -------------------------------
    # containers already removed inside step 3 above.

    async def _s9a() -> str:
        count = await usage_repo.delete_all_for_owner(owner_id)
        return f"deleted {count} row(s)"

    await _step(steps, system="usage_counters", action="delete_all_for_owner", fn=_s9a)

    async def _s9a2() -> str:
        room_ids = await messaging_repo.list_owner_room_ids(owner_id)
        deleted = 0
        # Keep the owner catalog until every room partition is gone. A partial
        # failure can then retry from the same complete room-id inventory.
        for room_id in room_ids:
            meta = await messaging_repo.get_room(room_id)
            if meta is not None and meta.get("ownerId") != owner_id:
                raise RuntimeError("messaging owner catalog referenced a room owned elsewhere")
            deleted += await messaging_repo.delete_room_partition(room_id)
        # The PHONE# line row, before the page goes. A Clerk deletion releases no line: it keeps a held line's
        # rows instead (below).
        if not clerk_user_deleted:
            deleted += bool(await voice_onboarding.release_number(owner_id))
        deleted += await messaging_repo.delete_owner_partition(owner_id)
        return f"deleted {len(room_ids)} room partition(s) and {deleted} messaging row(s)"

    if clerk_user_deleted and await voice_onboarding.held_number(owner_id):
        _skip(
            steps,
            "messaging",
            "delete_owner_rooms_and_streams",
            "kept: the tenant holds a phone line; release it with an admin wipe",
        )
    else:
        await _step(
            steps,
            system="messaging",
            action="delete_owner_rooms_and_streams",
            fn=_s9a2,
        )

    async def _s9a4() -> str:
        # The person's own memory data, in no workspace's keeping: uploaded sessions, drafts and
        # evaluator rows, episodes, the computer-key registry (workspace_memory.delete_person_memory).
        from core.services import workspace_memory

        await workspace_memory.delete_person_memory(owner_id)
        return "sessions, drafts, episodes and computer keys removed"

    await _step(steps, system="person_memory", action="delete_person_memory", fn=_s9a4)

    # Both of this owner's items in paperclip-companies, not just the company
    # row: the funnel answer lives in the SAME table under the prefixed key
    # ``__pending_brief__:{owner_id}``, and it deliberately outlives a
    # reprovision (see WorkspaceRepo's "LIFETIME" note). It is the user's own
    # typed words — "a social media agency for small restaurants" was one of
    # the two found stranded in prod — so a wipe that leaves it behind retains
    # content for an account we just deleted, in a row nothing will ever read
    # or clean up again (the key is the Clerk id, which is never reissued).
    # Folded into this step rather than given its own because it
    # is the same table, the same owner, and the same "delete the rows we
    # wrote" action — but it is NOT gated on the company row: the two exist
    # independently, and the prod case that surfaced this was a "SKIPPED — no
    # paperclip-companies row" wipe with a live pending_brief.
    #
    # Nor is it gated on the pending_brief snapshot. ``_read_identifiers`` runs
    # before every step above, so an onboarding submission that lands
    # during the wipe leaves ``ids.pending_brief`` None while its item sits in
    # the table — the exact residue this exists to remove. The delete is
    # idempotent, so it is simply always attempted, and the step reports what
    # DynamoDB says it actually removed rather than what the snapshot predicted.
    async def _s9b() -> str:
        repo = WorkspaceRepo()
        deleted: list[str] = []
        await repo.delete(owner_id)  # the identity row and the person's own workspace row; idempotent
        if ids.paperclip is not None:
            deleted.append("paperclip identity row deleted")
        if await repo.delete_pending_brief(owner_id):
            deleted.append("pending_brief row deleted")
        return " + ".join(deleted) or "no paperclip-companies or pending_brief row"

    await _step(steps, system="paperclip_row", action="paperclip_repo.delete", fn=_s9b)

    unpaid = [s.system for s in steps if s.system in ("metronome", "stripe") and s.status == "failed"]
    if unpaid:
        # A subscription may still be live: the billing row is the re-run's only handle on it.
        steps.append(WipeStep("billing_row", "billing_repo.delete", "failed", error=f"held back: {unpaid[0]} failed"))
    elif ids.billing is not None:

        async def _s9c() -> str:
            await billing_repo.delete(owner_id)
            return "billing-accounts row deleted"

        await _step(steps, system="billing_row", action="billing_repo.delete", fn=_s9c)
    else:
        _skip(steps, "billing_row", "billing_repo.delete", "no billing-accounts row")

    if ids.user_row is not None:

        async def _s9d() -> str:
            from core.services.os_billing import cancel_for_deleted_user

            await cancel_for_deleted_user(owner_id)  # a failure keeps the row: it holds the subscription id
            await user_repo.delete(owner_id)
            return "users row deleted"

        await _step(steps, system="user_row", action="user_repo.delete", fn=_s9d)
    else:
        _skip(steps, "user_row", "user_repo.delete", "no users row")

    # --- Credential + identity leftovers ----------------------------------
    # These were the module's documented KNOWN GAP: the owner's ChatGPT
    # tokens, BYOK LLM keys and integration credentials all survived a
    # "wipe", and their agent email addresses kept routing inbound SES mail
    # at an owner that no longer existed. Unconditional and idempotent —
    # each no-ops on an empty set.

    async def _s9f() -> str:
        names = await delete_owner_secrets(owner_id)
        return f"deleted {len(names)} owner secret(s) (BYOK keys, integration creds, OAuth apps)"

    await _step(steps, system="owner_secrets", action="delete_owner_secrets", fn=_s9f)
    await _step(steps, system="openrouter_key", action="retire_and_delete", fn=lambda: retire_openrouter_key(owner_id))

    async def _s9g() -> str:
        providers = await integrations_repo.delete_all_for_owner(owner_id)
        return f"deleted {len(providers)} integration row(s): {', '.join(providers) or 'none'}"

    await _step(steps, system="integrations", action="delete_all_for_owner", fn=_s9g)

    async def _s9g2() -> str:
        return f"deleted {await oauth_state_repo.delete_all_for_owner(owner_id)} pending OAuth state row(s)"

    await _step(steps, system="oauth_state", action="delete_all_for_owner", fn=_s9g2)

    # Deletes OUR stored tokens. It does NOT revoke the grant upstream at
    # OpenAI — there is no call to an OpenAI revocation endpoint anywhere in
    # this codebase, so the grant stays live on their side until it expires
    # or the human revokes it in their OpenAI account settings.
    async def _s9h() -> str:
        await revoke_user_oauth(user_id=owner_id)
        return "stored chatgpt oauth tokens deleted (grant NOT revoked upstream at OpenAI)"

    await _step(steps, system="oauth_tokens", action="revoke_user_oauth", fn=_s9h)

    async def _s9i() -> str:
        released, failed = await release_text_lines(await agent_email_repo.query_by_owner(owner_id))
        addresses = await agent_email_repo.delete_all_for_owner(owner_id)
        return (
            f"deleted {len(addresses)} agent email address(es): {', '.join(addresses) or 'none'}"
            + text_release_note(released, failed)
        )

    await _step(steps, system="agent_email_addresses", action="delete_all_for_owner", fn=_s9i)

    async def _s9i2() -> str:
        teams = await slack_installs_repo.delete_all_for_owner(owner_id)
        return f"deleted {len(teams)} slack install(s): {', '.join(teams) or 'none'}"

    await _step(steps, system="slack_installs", action="delete_all_for_owner", fn=_s9i2)

    async def _s9j() -> str:
        slug = await company_slug_repo.delete_for_owner(owner_id)
        return f"released slug {slug}" if slug else "no slug reservation"

    await _step(steps, system="company_slug", action="delete_for_owner", fn=_s9j)

    # --- Step 10 (spec #12): Clerk user -- ALWAYS LAST ---------------------
    # Deliberately unconditional and unguarded by any row read above:
    # leaving the Clerk user alive lets everything else regrow on the next
    # /container/status poll, so this must run regardless of what happened
    # to every step before it.
    async def _s10() -> str:
        await clerk_admin.delete_user(owner_id)
        return owner_id

    if clerk_user_deleted:
        _skip(steps, "clerk_user", "delete_user", "already deleted in Clerk")
    else:
        await _step(steps, system="clerk_user", action="delete_user", fn=_s10)

    logger.info(
        "account_wipe.wipe_account: finished for owner=%s actor=%s ok=%d skipped=%d failed=%d refused=%d",
        owner_id,
        actor_admin_id,
        sum(1 for s in steps if s.status == "ok"),
        sum(1 for s in steps if s.status == "skipped"),
        sum(1 for s in steps if s.status == "failed"),
        sum(1 for s in steps if s.status == "refused"),
    )

    return WipeReport(owner_id=owner_id, steps=steps, dry_run=False)
