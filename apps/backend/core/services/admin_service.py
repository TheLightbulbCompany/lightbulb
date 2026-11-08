"""Admin dashboard composition layer.

Read-side aggregation for /api/v1/admin/* endpoints. Covers:
- list_users (Clerk + container_repo join)
- get_overview (Clerk + Stripe DDB row + container DDB row + usage)
- get_provisioning (readiness contract + containers row + Paperclip company +
  provision-gate + SFN execution, all allowlist-narrowed)
- list_user_agents (the Paperclip roster + Hermes profile metadata, no runtime invoke)
- get_agent_detail (the roster + the agent's S3 profile -- see _get_agentcore_agent_detail)
- get_logs (delegates to cloudwatch_logs)
- get_cloudwatch_url (delegates to cloudwatch_url)
- get_posthog_timeline (delegates to posthog_admin)
- get_actions_audit (delegates to admin_actions_repo)

Mutation-side composition (cancel_subscription, etc.) lives in the
admin router handlers — they call existing services directly via the
@audit_admin_action decorator.

Parallel reads in get_overview wrap each upstream with a 2s timeout via
_with_timeout. Slow Stripe doesn't starve other panels.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from core.dynamodb import run_in_thread
from core.repositories import admin_actions_repo, billing_repo, container_repo, usage_repo
from core.services import (
    clerk_admin,
    cloudwatch_logs,
    cloudwatch_url,
    hermes_profiles,
    posthog_admin,
    runtime_rpc,
)
from core.services.admin_redact import redact_openclaw_config
from core.services.provision_dispatch import running_execution
from core.services.provision_gate import Gate, probe_provision_gate
from core.services.readiness_state import compute_status

logger = logging.getLogger(__name__)


_PARALLEL_TIMEOUT_S = 2.0


async def resolve_admin_owner_id(user_id: str) -> tuple[str, dict | None]:
    """Return (owner_id, org_context) for a target user viewed by an admin.

    The DDB partition key ``owner_id`` is the Clerk org_id for org-member
    resources and the Clerk user_id for personal-mode resources. The admin
    dashboard receives the target user_id from the URL, so we must ask Clerk
    which org (if any) the user belongs to before querying repos — otherwise
    every org-member user renders as "no container provisioned" (CEO
    admin-org-owner-id bug).

    This is the public helper used by both admin_service read composition and
    the admin router's mutation handlers (container / billing / config /
    agents) — org-member mutation targets ``openclaw-{org_id}-{hash}``, not
    ``openclaw-{user_id}-{hash}``. Account mutations (suspend / reactivate /
    force-signout / resend-verification) target the Clerk user directly and
    MUST NOT call this resolver.

    Per ``project_single_org_per_user`` memory, users are assumed to be in at
    most one org. If Clerk returns multiple (shouldn't happen), pick the
    first and log a warning.

    Returns:
      (org_id, org_context_dict) when the user is in an org; org_context is
        {"id", "slug", "name", "role"}.
      (user_id, None) when the user is personal-mode (no orgs) or when the
        Clerk lookup errors (fail-open: better to show personal-mode data
        than to break the dashboard).
    """
    # Bound Clerk call with the same per-panel timeout budget used by
    # _with_timeout — a slow Clerk upstream must not block overview / agents /
    # detail before their own _with_timeout-wrapped reads even start. Read the
    # constant at call time so tests can monkeypatch _PARALLEL_TIMEOUT_S.
    try:
        orgs = await asyncio.wait_for(
            clerk_admin.list_user_organizations(user_id),
            timeout=_PARALLEL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "admin_service.resolve_admin_owner_id Clerk timeout for %s after %ss; falling back to personal-mode",
            user_id,
            _PARALLEL_TIMEOUT_S,
        )
        return user_id, None
    except Exception as e:  # noqa: BLE001 — defensive, Clerk must never take the dashboard down
        logger.warning("admin_service.resolve_admin_owner_id Clerk error for %s: %s", user_id, e)
        return user_id, None

    if not orgs:
        return user_id, None

    if len(orgs) > 1:
        logger.warning(
            "admin_service: Clerk returned %d orgs for user %s — single-org-per-user expected; using first",
            len(orgs),
            user_id,
        )

    org = orgs[0]
    return org["id"], org


# Back-compat alias: ``_resolve_owner_for_admin`` is still referenced by at
# least one regression test. Keep the alias so the test in
# test_admin_org_resolution.py keeps working without churning the test
# name alongside the rename. Safe to delete in a follow-up once the test is
# migrated.
_resolve_owner_for_admin = resolve_admin_owner_id


async def _with_timeout(coro, label: str, timeout_s: float | None = None):
    """Wrap a coroutine with a timeout; return {error, source} on timeout/error.

    Used in get_overview's parallel fetches so a slow Clerk/Stripe doesn't
    take the whole overview page down. timeout_s defaults to the
    module-level _PARALLEL_TIMEOUT_S resolved at call time so tests can
    monkeypatch the constant.
    """
    effective_timeout = timeout_s if timeout_s is not None else _PARALLEL_TIMEOUT_S
    try:
        return await asyncio.wait_for(coro, timeout=effective_timeout)
    except asyncio.TimeoutError:
        return {"error": "timeout", "source": label}
    except Exception as e:  # noqa: BLE001
        logger.warning("admin_service: %s upstream failed: %s", label, e)
        return {"error": str(e), "source": label}


async def list_users(*, q: str = "", limit: int = 50, cursor: str | None = None) -> dict:
    """Paginated user list joined with container status.

    cursor is opaque — pass back to fetch next page. Internally a string
    representation of Clerk's offset (since Clerk uses int offsets, we
    stringify them so the frontend treats cursor uniformly).

    For each Clerk user we resolve the effective owner_id via
    ``resolve_admin_owner_id`` before looking up the container row — org
    members' containers are keyed by ``owner_id == org_id`` in DDB, so using
    the raw Clerk user_id would render every org member as "no container
    provisioned".

    Owner_ids are deduped before the container fetch so two org members sharing
    the same owner_id cost one DDB call, not two. Each row also carries an
    ``org`` field (``{id, slug, name, role}`` for org members, ``None`` for
    personal-mode) so the UI can badge org members inline.
    """
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    page = await clerk_admin.list_users(query=q, limit=limit, offset=offset)

    clerk_users = page.get("users", [])
    user_ids = [u["id"] for u in clerk_users]

    # Per-user owner resolution. resolve_admin_owner_id already bounds each
    # Clerk call with _PARALLEL_TIMEOUT_S and fails open to (user_id, None), so
    # a slow/down Clerk doesn't take the page down.
    resolutions = await asyncio.gather(
        *(resolve_admin_owner_id(uid) for uid in user_ids),
        return_exceptions=True,
    )
    owner_by_uid: dict[str, str] = {}
    org_by_uid: dict[str, dict | None] = {}
    for uid, res in zip(user_ids, resolutions):
        if isinstance(res, BaseException):
            # Defensive: resolve_admin_owner_id already catches internally, but
            # if gather surfaces something unexpected fall back to personal mode.
            owner_by_uid[uid] = uid
            org_by_uid[uid] = None
        else:
            owner_id, org_context = res
            owner_by_uid[uid] = owner_id
            org_by_uid[uid] = org_context

    # Dedupe owner_ids — two members of the same org share one owner_id and
    # therefore one container + billing row.
    unique_owner_ids = list({oid for oid in owner_by_uid.values()})
    # subscription_status lives on the billing_accounts row (set by
    # billing_repo.set_subscription on signup + every webhook). Fetch
    # container + billing in parallel per owner.
    containers, billings = await asyncio.gather(
        asyncio.gather(
            *(container_repo.get_by_owner_id(oid) for oid in unique_owner_ids),
            return_exceptions=True,
        ),
        asyncio.gather(
            *(billing_repo.get_by_owner_id(oid) for oid in unique_owner_ids),
            return_exceptions=True,
        ),
    )
    container_by_oid: dict[str, dict | None] = {
        oid: (c if not isinstance(c, BaseException) else None) for oid, c in zip(unique_owner_ids, containers)
    }
    billing_by_oid: dict[str, dict | None] = {
        oid: (b if not isinstance(b, BaseException) else None) for oid, b in zip(unique_owner_ids, billings)
    }

    rows = []
    for u in clerk_users:
        uid = u["id"]
        owner_id = owner_by_uid.get(uid, uid)
        container = container_by_oid.get(owner_id) or {}
        billing = billing_by_oid.get(owner_id) or {}
        emails = u.get("email_addresses", [])
        primary_email = emails[0].get("email_address") if emails else None
        rows.append(
            {
                "clerk_id": uid,
                "customer_id": uid,
                "workspace_id": owner_id,
                "email": primary_email,
                "first_name": u.get("first_name"),
                "created_at": u.get("created_at"),
                "signup_at": u.get("created_at"),
                "last_sign_in_at": u.get("last_sign_in_at"),
                "last_activity_at": container.get("updated_at") or u.get("last_sign_in_at"),
                "activation": {
                    "workspace_created_at": container.get("created_at"),
                    "workspace_ready_at": container.get("onboarded_at"),
                },
                "plan": billing.get("plan") or billing.get("plan_id") or "free",
                "trial_ends_at": billing.get("trial_ends_at"),
                "banned": u.get("banned", False),
                "account_status": "banned" if u.get("banned", False) else "active",
                "container_status": container.get("status", "none"),
                "subscription_status": billing.get("subscription_status"),
                "org": org_by_uid.get(uid),
            }
        )

    return {
        "users": rows,
        "cursor": str(page["next_offset"]) if page.get("next_offset") is not None else None,
        "stubbed": page.get("stubbed", False),
    }


async def get_overview(user_id: str) -> dict:
    """Identity + billing + container + usage in one payload.

    Parallel fetches with per-call timeout — slow Stripe doesn't starve the
    others; each panel can render even if its source errors.

    For org-member users the container / billing / usage records are keyed
    by owner_id == org_id. We resolve the effective owner via Clerk before
    issuing the DDB reads so admins see the org's resources, not empty
    personal-mode rows (admin-org-owner-id fix). The returned ``org`` field
    is null for personal-mode users and {id, slug, name, role} otherwise.
    """
    # usage_repo rows only exist as "YYYY-MM" / "lifetime" / "member:*" keys —
    # read the actual current UTC month, matching usage_service._current_period().
    now = datetime.now(timezone.utc)
    period = f"{now.year}-{now.month:02d}"

    owner_id, org_context = await resolve_admin_owner_id(user_id)

    clerk, container, billing, usage = await asyncio.gather(
        _with_timeout(clerk_admin.get_user(user_id), "clerk"),
        _with_timeout(container_repo.get_by_owner_id(owner_id), "ddb_containers"),
        _with_timeout(billing_repo.get_by_owner_id(owner_id), "ddb_billing"),
        _with_timeout(usage_repo.get_period_usage(owner_id, period), "ddb_usage"),
    )

    return {
        "identity": clerk,
        # Allowlisted: the raw row carries hook_token / gateway_token, which must never reach a browser.
        "container": container if _read_failed(container, label="ddb_containers") else _narrow_container(container),
        "billing": billing,
        "usage": usage,
        "org": org_context,
    }


def _workspace_repo():
    """Local import so ``patch("core.repositories.workspace_repo.WorkspaceRepo", ...)``
    (the pattern every existing test double in this codebase already uses)
    keeps working -- mirrors ``core.services.readiness_state``'s own
    local-import idiom (``readiness_state.py:102-110``).
    """
    from core.repositories.workspace_repo import WorkspaceRepo

    return WorkspaceRepo()


_CONTAINER_PROVISION_FIELDS = (
    "status",
    "substatus",
    "last_error",
    "last_error_at",
    "retryable",
    "provision_generation",
    "onboarded_at",
    "stop_reason",
    # The runtime's own handle, in place of the ECS service name: it is what an operator
    # pastes into `aws bedrock-agentcore get-agent-runtime` when a workspace misbehaves.
    "agentcore_runtime_arn",
    "created_at",
    "updated_at",
)

# ALLOWLIST — every name verified against the Workspace dataclass
# (the workspace row carries no credential at all; those live on identity
# rows, which this surface never reads). Narrowing through an allowlist means
# a future field is excluded by default, not leaked by default.
_PAPERCLIP_PROVISION_FIELDS = (
    "status",
    "company_id",
    "org_id",
    "provisioner_user_id",
    "member_user_ids",
    "last_error",
    "seeded_agent_ids",
    "starter_agent_ids",
    "scheduled_purge_at",
    "onboarding_generation",
    "funnel_state",
    "funnel_submitted_at",
    "funnel_completed_at",
    "onboarding_goal_id",
    "onboarding_project_id",
    "onboarding_submitted_by",
    "created_at",
    "updated_at",
)


def _iso(value: Any) -> Any:
    """``datetime`` -> ISO string; every other value passes through unchanged.

    Shared by ``_narrow_paperclip`` (the Workspace dataclass stores
    real ``datetime`` objects) and ``_narrow_execution`` (boto3's
    ListExecutions item does the same for start/stop dates) so both
    narrowers hand back a JSON-safe dict rather than leaking a bare datetime
    onto the admin wire.
    """
    return value.isoformat() if hasattr(value, "isoformat") else value


def _read_failed(value: Any, *, label: str) -> bool:
    """True iff ``value`` is the ``{"error", "source"}`` sentinel
    ``_with_timeout`` returns on a timeout/exception for THIS specific
    ``label`` -- as opposed to a genuine result (for every caller here that's
    ``dict | None``, never a two-key dict whose ``source`` happens to equal
    the label we passed).

    Same class of fix as the ``isinstance(gate, Gate)`` guard below:
    ``_with_timeout``'s failure sentinel is truthy, so a bare ``if value``
    can't tell "row exists" from "read failed". For the container read
    specifically, threading the sentinel into ``compute_status`` as if it
    were a real row would silently fabricate a plausible-looking
    ``lifecycle`` instead of surfacing that the read itself failed -- on
    exactly the diagnostic tool built for investigating provisioning
    problems.
    """
    return isinstance(value, dict) and "error" in value and value.get("source") == label


def _narrow_container(row: dict | None) -> dict | None:
    """containers row -> allowlisted provisioning fields (drops gateway_token)."""
    if not row:
        return None
    return {k: row.get(k) for k in _CONTAINER_PROVISION_FIELDS}


def _narrow_paperclip(company: Any) -> dict | None:
    """Workspace -> allowlisted onboarding/teams fields."""
    if company is None:
        return None
    fields = {k: _iso(getattr(company, k, None)) for k in _PAPERCLIP_PROVISION_FIELDS}
    if isinstance(fields["member_user_ids"], set):
        fields["member_user_ids"] = sorted(fields["member_user_ids"])
    return fields


def _narrow_execution(resp: dict | None) -> dict | None:
    """The owner's RUNNING SFN execution (ListExecutions item) -> the four
    fields the admin tab needs."""
    if not resp:
        return None
    return {
        "name": resp.get("name"),
        "status": resp.get("status"),
        "start_date": _iso(resp.get("startDate")),
        "stop_date": _iso(resp.get("stopDate")),
    }


async def get_provisioning(user_id: str) -> dict:
    """Provisioning truth for one owner: the readiness contract, the raw
    containers-row provisioning fields, the Paperclip company + onboarding
    state machines, the provision-gate decision, and the SFN execution --
    each read best-effort so a degraded store can't blank the page.

    Uses ``probe_provision_gate`` (metric-free), NOT ``evaluate_provision_gate``
    -- a diagnostic admin read must not inflate the ``provision.gate.blocked``
    spike-alarm stream.

    The containers row is read once and threaded into ``compute_status`` as
    its documented ``container=`` override (``readiness_state.py:169-176``)
    so this doesn't issue a second DDB read for the same row. That override
    contract only anticipates an already-fetched row OR an explicit ``None``
    -- never ``_with_timeout``'s failure sentinel -- so a failed/timed-out
    container read is translated to ``None`` before being threaded through,
    and the ``container`` field in the returned payload keeps the raw
    sentinel (distinguishable from a genuinely absent row, which is
    ``None``) so an operator can tell "read failed" from "no row" apart.

    The Paperclip company read and the SFN execution read get the identical
    treatment via the same ``_read_failed`` sentinel check: ``_narrow_paperclip``
    and ``_narrow_execution`` only special-case ``None`` (genuinely no row), so
    threading a failed-read sentinel through them unnarrowed would produce an
    all-``None`` fields dict that looks exactly like a present-but-empty row.
    The ``paperclip``/``sfn_execution`` fields keep the raw sentinel on a failed
    read, same convention as ``container``.
    """
    owner_id, org_context = await resolve_admin_owner_id(user_id)

    container_raw = await _with_timeout(container_repo.get_by_owner_id(owner_id), "ddb_containers")
    container_read_failed = _read_failed(container_raw, label="ddb_containers")
    container = None if container_read_failed else container_raw

    status_contract, paperclip_row, gate = await asyncio.gather(
        _with_timeout(compute_status(owner_id, container=container), "readiness_status"),
        _with_timeout(
            _workspace_repo().resolve_owner_row(owner_id, include_inactive=True),
            "ddb_paperclip",
        ),
        _with_timeout(probe_provision_gate(owner_id=owner_id), "provision_gate"),
    )
    paperclip_read_failed = _read_failed(paperclip_row, label="ddb_paperclip")

    sfn = await _with_timeout(running_execution(owner_id), "sfn_execution")
    sfn_read_failed = _read_failed(sfn, label="sfn_execution")

    return {
        "owner_id": owner_id,
        "org": org_context,
        "status_contract": status_contract,
        "container": container_raw if container_read_failed else _narrow_container(container),
        "paperclip": paperclip_row if paperclip_read_failed else _narrow_paperclip(paperclip_row),
        # isinstance guard, not a truthiness check: on timeout/error
        # _with_timeout hands back an {"error", "source"} dict rather than
        # None, and that dict is truthy — a bare `if gate else None` would
        # call .to_payload() on it and crash (AttributeError) on exactly the
        # degraded-upstream case this composer exists to survive.
        "gate": gate.to_payload()["blocked"] if isinstance(gate, Gate) else None,
        "sfn_execution": sfn if sfn_read_failed else _narrow_execution(sfn),
    }


async def list_user_agents(user_id: str, *, cursor: str | None = None, limit: int = 50) -> dict:
    """Agents list via gateway RPC with E2 timeout + E3 stopped detection.

    Resolves org context first — org-member users' container is keyed by
    org_id, not user_id. ``org`` in the response is null for personal-mode
    users and {id, slug, name, role} otherwise.

    An AgentCore owner never reaches the gateway RPC below (parity audit row 80): a
    ``container_rpc`` invoke would wake a parked microVM and floor the RPC budget at 90s just to
    render an admin page. Those owners are answered from ``runtime_rpc.agentcore_agents_list``
    instead — the Paperclip roster the backend already owns.
    """
    owner_id, org_context = await resolve_admin_owner_id(user_id)

    container_row = await container_repo.get_by_owner_id(owner_id)
    if not container_row or container_row.get("status") != "running":
        return {
            "agents": [],
            "cursor": None,
            "container_status": (container_row or {}).get("status", "none"),
            "org": org_context,
        }

    # Parity audit row 80: an invoke wakes a parked microVM and floors the RPC budget at 90s
    # (container_rpc.call). Answer from the Paperclip roster + Hermes profile metadata instead
    # -- the same source `runtime_rpc.execute_sync`'s own agents.list branch already uses for
    # the live app -- with zero runtime invokes. `cursor`/`limit` stay on the signature for
    # forward compatibility; the listing is not paginated.
    _ = (cursor, limit)
    listing = await runtime_rpc.agentcore_agents_list(owner_id)
    return {
        "agents": listing.get("agents", []),
        "cursor": None,
        "container_status": "running",
        "org": org_context,
    }


async def get_agent_detail(user_id: str, agent_id: str) -> dict:
    """Per-agent detail: identity + sessions + skills + redacted config.

    Answered by ``_get_agentcore_agent_detail`` from the roster + the agent's S3 profile
    (parity audit row 80) -- see its docstring for what that trades away against the four
    gateway RPCs it replaced.

    Resolves org context first so org-keyed workspaces are found. ``org`` in the response is
    null for personal-mode users and {id, slug, name, role} otherwise.
    """
    owner_id, org_context = await resolve_admin_owner_id(user_id)

    container_row = await container_repo.get_by_owner_id(owner_id)
    if not container_row or container_row.get("status") != "running":
        return {
            "error": "container_not_running",
            "container_status": (container_row or {}).get("status", "none"),
            "org": org_context,
        }

    return await _get_agentcore_agent_detail(owner_id, agent_id, org_context)


async def _get_agentcore_agent_detail(owner_id: str, agent_id: str, org_context: dict | None) -> dict:
    """``get_agent_detail`` for an AgentCore owner: identity + config with zero runtime
    invokes (parity audit row 80). Identity comes from ``runtime_rpc.agentcore_agents_list`` —
    the same Paperclip-roster-plus-model-metadata source the live app's sidebar reads — and
    config from the agent's own Hermes profile in S3. ``sessions``/``skills`` have no such
    backend-owned copy (they live behind a live gateway RPC only), so they come back empty
    rather than waking a parked microVM for an admin page load.
    """
    listing = await runtime_rpc.agentcore_agents_list(owner_id)
    agent = next((row for row in listing.get("agents", []) if row.get("id") == agent_id), None)
    if agent is None:
        return {"error": "agent_not_found", "org": org_context}

    config, _hash = await run_in_thread(hermes_profiles.config_snapshot, owner_id, agent_id)

    return {
        "agent": agent,
        "sessions": [],
        "skills": [],
        "config_redacted": redact_openclaw_config(config),
        "org": org_context,
    }


# Thin pass-through wrappers — admin_service is the single import point
# for the router so all admin-related logic flows through one module.


async def get_logs(
    user_id: str, *, level: str = "ERROR", hours: int = 24, limit: int = 20, cursor: str | None = None
) -> dict:
    return await cloudwatch_logs.filter_user_logs(user_id=user_id, level=level, hours=hours, limit=limit, cursor=cursor)


def get_cloudwatch_url(user_id: str, *, start: str, end: str, level: str = "ERROR") -> str:
    return cloudwatch_url.build_insights_url(user_id=user_id, start=start, end=end, level=level)


async def get_posthog_timeline(user_id: str, *, limit: int = 100) -> dict:
    return await posthog_admin.get_person_events(distinct_id=user_id, limit=limit)


async def get_actions_audit(
    *,
    target_user_id: str | None = None,
    admin_user_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    if target_user_id:
        return await admin_actions_repo.query_by_target(target_user_id, limit=limit, cursor=cursor)
    if admin_user_id:
        return await admin_actions_repo.query_by_admin(admin_user_id, limit=limit, cursor=cursor)
    # No filter: the platform-wide audit view lists recent rows newest-first.
    return await admin_actions_repo.scan_recent(limit=limit, cursor=cursor)
