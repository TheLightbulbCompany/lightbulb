"""Admin dashboard composition layer.

Read-side aggregation for /api/v1/admin/* endpoints. Covers:
- list_users (Clerk + container_repo join)
- get_overview (Clerk + Stripe DDB row + container DDB row + usage)
- get_provisioning (readiness contract + containers row + Paperclip company +
  provision-gate + SFN execution, all allowlist-narrowed)
- list_user_agents (gateway RPC, with timeout + stopped detection)
- get_agent_detail (4-way gateway parallel + S3 redaction)
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
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from core import openclaw_version
from core.containers import get_ecs_manager, get_gateway_pool
from core.repositories import admin_actions_repo, billing_repo, container_repo, usage_repo
from core.services import clerk_admin, cloudwatch_logs, cloudwatch_url, posthog_admin
from core.services.admin_redact import redact_openclaw_config
from core.services.provision_dispatch import describe_dispatch_execution
from core.services.provision_gate import Gate, probe_provision_gate
from core.services.readiness_state import compute_status

logger = logging.getLogger(__name__)


_PARALLEL_TIMEOUT_S = 2.0
_GATEWAY_RPC_TIMEOUT_S = 3.0


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
                "email": primary_email,
                "created_at": u.get("created_at"),
                "last_sign_in_at": u.get("last_sign_in_at"),
                "banned": u.get("banned", False),
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
        "container": container,
        "billing": billing,
        "usage": usage,
        "org": org_context,
    }


def _paperclip_repo():
    """Local import so ``patch("core.repositories.paperclip_repo.PaperclipRepo", ...)``
    (the pattern every existing test double in this codebase already uses)
    keeps working -- mirrors ``core.services.readiness_state``'s own
    local-import idiom (``readiness_state.py:102-110``).
    """
    from core.repositories.paperclip_repo import PaperclipRepo

    return PaperclipRepo(table_name="paperclip-companies")


_CONTAINER_PROVISION_FIELDS = (
    "status",
    "substatus",
    "last_error",
    "last_error_at",
    "retryable",
    "provision_generation",
    "provision_claim_id",
    "provision_claimed_at",
    "provision_attempt_token",
    "provision_execution_name",
    "onboarded_at",
    "bedrock_revoked",
    "stop_reason",
    "service_name",
    "created_at",
    "updated_at",
)

# ALLOWLIST — every name verified against the PaperclipCompany dataclass
# (paperclip_repo.py:157-256). ``paperclip_password_encrypted`` and
# ``service_token_encrypted`` must never cross the admin wire, even
# Fernet-encrypted, so they are deliberately absent here — narrowing through
# an allowlist (rather than blocklisting those two fields) means a future
# field added to the dataclass is excluded by default, not leaked by default.
_PAPERCLIP_PROVISION_FIELDS = (
    "status",
    "company_id",
    "org_id",
    "paperclip_user_id",
    "last_error",
    "seeded_agent_ids",
    "scheduled_purge_at",
    "onboarding_generation",
    "funnel_state",
    "funnel_submitted_at",
    "funnel_completed_at",
    "onboarding_goal_id",
    "onboarding_project_id",
    "onboarding_submitted_by",
    "bootstrap_state",
    "bootstrap_attempts",
    "bootstrap_dispatch_id",
    "bootstrap_dispatched_at",
    "bootstrap_send_attempted_at",
    "bootstrap_sent_at",
    "bootstrap_delivered_at",
    "bootstrap_last_error",
    "created_at",
    "updated_at",
)


def _iso(value: Any) -> Any:
    """``datetime`` -> ISO string; every other value passes through unchanged.

    Shared by ``_narrow_paperclip`` (the PaperclipCompany dataclass stores
    real ``datetime`` objects) and ``_narrow_execution`` (boto3's
    DescribeExecution response does the same for start/stop dates) so both
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
    out = {k: row.get(k) for k in _CONTAINER_PROVISION_FIELDS}
    out["claim_stale"] = container_repo.is_provision_claim_stale(row)
    return out


def _narrow_paperclip(company: Any) -> dict | None:
    """PaperclipCompany -> allowlisted onboarding/teams fields (drops the
    encrypted-credential pair: ``paperclip_password_encrypted`` and
    ``service_token_encrypted``)."""
    if company is None:
        return None
    return {k: _iso(getattr(company, k, None)) for k in _PAPERCLIP_PROVISION_FIELDS}


def _narrow_execution(resp: dict | None) -> dict | None:
    """SFN DescribeExecution response -> the four fields the admin tab needs
    (drops ResponseMetadata, executionArn, input/output)."""
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
        _with_timeout(_paperclip_repo().get(owner_id), "ddb_paperclip"),
        _with_timeout(probe_provision_gate(owner_id=owner_id), "provision_gate"),
    )
    paperclip_read_failed = _read_failed(paperclip_row, label="ddb_paperclip")

    sfn = None
    claim_id = (container or {}).get("provision_claim_id")
    if claim_id and (container or {}).get("provision_execution_name"):
        sfn = await _with_timeout(describe_dispatch_execution(owner_id, claim_id), "sfn_execution")
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

    ecs = get_ecs_manager()
    container, ip = await ecs.resolve_running_container(owner_id)
    if not container or not ip:
        return {"agents": [], "cursor": None, "container_status": "unreachable", "org": org_context}

    pool = get_gateway_pool()
    # uuid4 entropy in req_id: after switching to owner_id, all members of an
    # org share a single gateway connection whose pending-RPC dict is keyed by
    # req_id. A deterministic id would let concurrent admin requests overwrite
    # each other's futures (intermittent timeouts / mismatched responses).
    nonce = uuid.uuid4().hex[:8]
    # OpenClaw's agents.list schema rejects unknown keys (INVALID_REQUEST on
    # cursor/limit). Match the main-app call site (useAgents.ts →
    # useGatewayRpc("agents.list")) which passes no params and receives the
    # full list. cursor/limit remain on the service signature / router query
    # for forward compatibility but aren't forwarded upstream.
    _ = (cursor, limit)
    try:
        result = await asyncio.wait_for(
            pool.send_rpc(
                user_id=owner_id,
                req_id=f"admin-agents-list-{owner_id}-{nonce}",
                method="agents.list",
                params={},
                ip=ip,
                token=container["gateway_token"],
            ),
            timeout=_GATEWAY_RPC_TIMEOUT_S,  # read at call time so tests can monkeypatch
        )
    except asyncio.TimeoutError:
        return {
            "agents": [],
            "cursor": None,
            "container_status": "timeout",
            "error": "gateway_rpc_timeout",
            "org": org_context,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("admin_service.list_user_agents gateway error: %s", e)
        return {
            "agents": [],
            "cursor": None,
            "container_status": "error",
            "error": str(e),
            "org": org_context,
        }

    return {
        "agents": result.get("agents", []) if isinstance(result, dict) else [],
        "cursor": result.get("cursor") if isinstance(result, dict) else None,
        "container_status": "running",
        "org": org_context,
    }


async def get_agent_detail(user_id: str, agent_id: str) -> dict:
    """Per-agent detail: identity + sessions + skills + redacted config.

    Four parallel gateway RPCs with a single shared timeout. CEO S3
    redaction applied to the openclaw.json config slice before return.

    Resolves org context first so org-keyed containers are found. ``org`` in
    the response is null for personal-mode users and {id, slug, name, role}
    otherwise.
    """
    owner_id, org_context = await resolve_admin_owner_id(user_id)

    container_row = await container_repo.get_by_owner_id(owner_id)
    if not container_row or container_row.get("status") != "running":
        return {
            "error": "container_not_running",
            "container_status": (container_row or {}).get("status", "none"),
            "org": org_context,
        }

    ecs = get_ecs_manager()
    container, ip = await ecs.resolve_running_container(owner_id)
    if not container or not ip:
        return {"error": "container_unreachable", "org": org_context}

    pool = get_gateway_pool()
    token = container["gateway_token"]

    async def _rpc(method: str, params: dict, suffix: str) -> Any:
        # uuid4 entropy in req_id: same org → shared gateway connection whose
        # pending-RPC dict is keyed by req_id. Deterministic ids collide on
        # concurrent detail loads for the same agent (see list_user_agents).
        nonce = uuid.uuid4().hex[:8]
        return await pool.send_rpc(
            user_id=owner_id,
            req_id=f"admin-{suffix}-{agent_id}-{nonce}",
            method=method,
            params=params,
            ip=ip,
            token=token,
        )

    # RPC method names + param shapes are anchored to the main-app call sites
    # so admin and main-app always speak the same wire contract to OpenClaw.
    # Two gotchas that bit us in prod (OpenClaw returned
    # `{"code":"INVALID_REQUEST","message":"unknown method: agents.get"}`):
    #   1. Method names: the correct RPCs are `agent.identity.get` (NOT
    #      `agents.get`) and `skills.status` (NOT `skills.list`). See
    #      AgentOverviewTab.tsx and SkillsTab.tsx.
    #   2. Wire format: OpenClaw's schemas use camelCase (`agentId`), not
    #      snake_case. Main app always sends camelCase — we must too.
    #   3. `sessions.list` isn't filterable by agent server-side; main app
    #      fetches the full list and narrows client-side (SessionsPanel.tsx).
    #      We replicate that below. `config.get` takes no params
    #      (AgentOverviewTab.tsx).
    try:
        identity, sessions_raw, skills, config = await asyncio.wait_for(
            asyncio.gather(
                _rpc("agent.identity.get", {"agentId": agent_id}, "identity"),
                _rpc(
                    "sessions.list",
                    {
                        "includeGlobal": True,
                        "includeUnknown": True,
                        "includeDerivedTitles": True,
                        "includeLastMessage": True,
                    },
                    "sessions",
                ),
                _rpc("skills.status", {"agentId": agent_id}, "skills"),
                _rpc("config.get", {}, "config"),
            ),
            timeout=_GATEWAY_RPC_TIMEOUT_S,  # read at call time so tests can monkeypatch
        )
    except asyncio.TimeoutError:
        return {"error": "gateway_rpc_timeout", "org": org_context}
    except Exception as e:  # noqa: BLE001
        logger.warning("admin_service.get_agent_detail gateway error: %s", e)
        return {"error": str(e), "org": org_context}

    # sessions.list is not agent-filterable; narrow client-side.
    #
    # Two shape normalizations, both mirroring main-app SessionsPanel:
    # - envelope: response is either {sessions: [...]} or a raw array
    #   (Session[] | {sessions: Session[]}) depending on OpenClaw version.
    # - agent key: sessions may carry "agentId" (canonical) or "agent_id"
    #   (older payload shapes) — accept either.
    if isinstance(sessions_raw, dict):
        all_sessions = sessions_raw.get("sessions", [])
    elif isinstance(sessions_raw, list):
        all_sessions = sessions_raw
    else:
        all_sessions = []
    agent_sessions = [
        s
        for s in all_sessions
        if isinstance(s, dict) and (s.get("agentId") == agent_id or s.get("agent_id") == agent_id)
    ]

    # OpenClaw's skills.status response is either {skills: [...]} or a raw
    # array depending on version (the main-app SkillsPanel handles both
    # shapes). Normalize so the admin detail page doesn't silently show "no
    # skills" on array-returning environments.
    if isinstance(skills, dict):
        skills_list = skills.get("skills", [])
    elif isinstance(skills, list):
        skills_list = skills
    else:
        skills_list = []

    # config.get returns either a plain config dict OR the envelope
    # {raw: "<openclaw.json as string>", hash} used by the raw-config
    # editor surfaces. redact_openclaw_config only walks
    # dict/list nodes and passes strings through, so a raw-envelope
    # response would leak BYOK secrets verbatim in config_redacted.raw.
    # Parse+redact the envelope before returning.
    if isinstance(config, dict) and isinstance(config.get("raw"), str):
        try:
            config_to_redact = json.loads(config["raw"])
        except json.JSONDecodeError:
            # Malformed raw — drop rather than leak the unparseable blob.
            config_to_redact = {"error": "malformed_config_raw"}
    else:
        config_to_redact = config

    return {
        "agent": identity,
        "sessions": agent_sessions,
        "skills": skills_list,
        "config_redacted": redact_openclaw_config(config_to_redact),
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
    raise ValueError("get_actions_audit requires target_user_id or admin_user_id")


# ---------------------------------------------------------------------------
# ECR image listing (powers the admin queue-image-update dropdown)
# ---------------------------------------------------------------------------

_OPENCLAW_EXTENDED_REPO = "isol8/openclaw-extended"
_OPENCLAW_EXTENDED_REGISTRY = "877352799272"

# Current dev/prod extended-image tags, surfaced to the admin image-update
# dropdown so each row can render "(current dev)" / "(current prod)" badges.
# Resolved from the single source of truth (openclaw-version.json) via
# core.openclaw_version — the CDK-injected env vars in deployed containers, the
# repo JSON in local dev / CI. No hand-sync; bump openclaw-version.json only.
CURRENT_DEV_TAG = openclaw_version.dev_tag()
CURRENT_PROD_TAG = openclaw_version.prod_tag()


async def list_extended_image_tags(*, limit: int = 25) -> list[dict[str, Any]]:
    """Return recently-pushed tags from the openclaw-extended ECR repo.

    Sorted newest-first by imagePushedAt. Each row carries the full ECR URI
    plus the bare tag, so the frontend can drop a fully-qualified value into
    the queue-image-update form without string-concat.

    Failures degrade to an empty list so the admin UI falls back to free
    text rather than blocking on an ECR outage.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    from core.config import settings

    client = boto3.client("ecr", region_name=settings.AWS_REGION)

    # ECR's describe_images doesn't guarantee ordering across pages, so we
    # must paginate the whole tagged set before sorting — otherwise the
    # actually-newest tag can land on page 2 and we'd silently default the
    # dropdown to an older image. Bounded at MAX_PAGES so a
    # misconfigured filter can't loop forever; the openclaw-extended repo is
    # small enough that we expect <= 2 pages in practice.
    MAX_PAGES = 10
    images: list[dict[str, Any]] = []
    next_token: str | None = None
    for _ in range(MAX_PAGES):
        kwargs: dict[str, Any] = {
            "repositoryName": _OPENCLAW_EXTENDED_REPO,
            "maxResults": 100,
            "filter": {"tagStatus": "TAGGED"},
        }
        if next_token:
            kwargs["nextToken"] = next_token
        try:
            resp = await asyncio.to_thread(client.describe_images, **kwargs)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("list_extended_image_tags: ECR call failed: %s", exc)
            return []
        images.extend(resp.get("imageDetails", []) or [])
        next_token = resp.get("nextToken")
        if not next_token:
            break

    images.sort(key=lambda d: d.get("imagePushedAt") or 0, reverse=True)

    # Build the FULL newest-first row set before capping. The pin-or-newer
    # truncation below needs to find the pinned tag, and the pin can sit
    # outside the newest `limit` images once several builds land after a
    # promotion — capping first would hide it and defeat the filter.
    rows: list[dict[str, Any]] = []
    seen_tags: set[str] = set()
    for img in images:
        digest = img.get("imageDigest")
        pushed = img.get("imagePushedAt")
        pushed_iso = pushed.isoformat() if hasattr(pushed, "isoformat") else None
        for tag in img.get("imageTags", []) or []:
            if tag in seen_tags:
                continue
            seen_tags.add(tag)
            rows.append(
                {
                    "image_uri": f"{_OPENCLAW_EXTENDED_REGISTRY}.dkr.ecr.{settings.AWS_REGION}.amazonaws.com/{_OPENCLAW_EXTENDED_REPO}:{tag}",
                    "tag": tag,
                    "digest": digest,
                    "pushed_at": pushed_iso,
                }
            )

    # PIN OR NEWER. Rolling one owner BACKWARDS is not a per-owner action --
    # a rollback is a pin flip in openclaw-version.json, applied fleet-wide --
    # so older tags are only noise here, and noise on a force-roll control is
    # how an admin picks the wrong image. Rows are newest-first, so everything
    # up to and including the pinned prod tag is "pin or newer".
    #
    # If the pin is NOT in the listing (expired, deleted, or an ECR page
    # bound), keep the full list rather than truncating to nothing: losing the
    # lever entirely is worse than showing extra tags.
    pin_index = next((i for i, r in enumerate(rows) if r["tag"] == CURRENT_PROD_TAG), None)
    if pin_index is not None:
        rows = rows[: pin_index + 1]
    else:
        logger.warning(
            "list_extended_image_tags: pinned prod tag %s not found in ECR listing; returning unfiltered tags",
            CURRENT_PROD_TAG,
        )

    # The pinned row must SURVIVE the cap. It is the frontend's default
    # selection, and if `limit` is smaller than the number of builds pushed
    # since the promotion the pin would be capped out -- the picker would then
    # fall back to defaulting to the newest image, reintroducing exactly the
    # accidental-version-change this filter exists to prevent.
    capped = min(max(limit, 1), 100)
    if pin_index is not None and pin_index >= capped:
        return rows[: capped - 1] + [rows[pin_index]]
    return rows[:capped]
