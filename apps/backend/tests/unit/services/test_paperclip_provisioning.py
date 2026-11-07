"""Unit tests for ``PaperclipProvisioning`` over the workspace-row +
identity-row shape (one workspace = one Paperclip company, keyed by org id;
one Better Auth identity per human, keyed by Clerk user id).

``_build_provision_org_doubles`` / ``_build_provision_member_doubles`` are
imported by sibling suites — keep their names and return shapes.
"""

import dataclasses
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from core.repositories.paperclip_repo import PaperclipIdentity, PaperclipWorkspace
from core.services.paperclip_admin_client import PaperclipApiError


def _now():
    return datetime.now(timezone.utc)


def _ws(
    *,
    org_id="org_xyz",
    company_id="co_xyz",
    status="active",
    members=(),
    provisioner="",
    **kw,
) -> PaperclipWorkspace:
    now = _now()
    return PaperclipWorkspace(
        org_id=org_id,
        company_id=company_id,
        status=status,
        created_at=now,
        updated_at=now,
        provisioner_user_id=provisioner,
        member_user_ids=set(members),
        **kw,
    )


def _identity(user_id, *, paperclip_user_id="", password="", email="", status=None) -> PaperclipIdentity:
    from core.encryption import encrypt

    return PaperclipIdentity(
        user_id=user_id,
        paperclip_user_id=paperclip_user_id,
        paperclip_password_encrypted=encrypt(password) if password else "",
        paperclip_email=email,
        status=status,
    )


def _patch_admin_session(monkeypatch):
    import core.services.paperclip_admin_session as admin_session_mod

    async def _fake_admin_cookie(_http):
        return "admin_cookie"

    monkeypatch.setattr(admin_session_mod, "get_admin_session_cookie", _fake_admin_cookie)
    monkeypatch.setattr(admin_session_mod, "invalidate_admin_session", lambda: None)


def _build_provision_org_doubles(monkeypatch):
    """AsyncMock-backed admin + repo for ``provision_org``: a brand-new org
    (no workspace, no identity, no company on record) whose provisioner wins
    the sign-up claim. Returns ``(admin, repo)``."""
    monkeypatch.setenv("ENCRYPTION_KEY", "0" * 44)

    admin = MagicMock()
    admin._http = MagicMock()  # touched by get_admin_session_cookie
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_new"})
    admin.create_company = AsyncMock(return_value={"id": "co_xyz"})
    admin.create_invite = AsyncMock(return_value={"token": "invtok"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_1"})
    admin.approve_join_request = AsyncMock()
    # Every starter lands by default; seed-failure tests override create_agent.
    admin.create_agent = AsyncMock(return_value={"id": "seed-agent-default"})
    # Builtin purge: empty roster unless a purge test overrides it.
    admin.list_agents = AsyncMock(return_value=[])
    admin.delete_agent = AsyncMock(return_value={})
    # Starter routines: none exist yet.
    admin.list_routines = AsyncMock(return_value=[])
    admin.create_routine = AsyncMock(side_effect=lambda **kw: {"id": f"r-{kw['body']['title']}"})
    admin.create_routine_trigger = AsyncMock(return_value={})

    repo = MagicMock()
    repo.get_workspace = AsyncMock(return_value=None)
    repo.get_identity = AsyncMock(return_value=None)
    repo.org_company_ids = AsyncMock(return_value=set())
    repo.try_acquire_seed_claim = AsyncMock(return_value=True)
    repo.stage_signup_credentials = AsyncMock(side_effect=lambda uid, **kw: (kw["email"], kw["password_encrypted"]))
    repo.put_identity = AsyncMock()
    repo.put_workspace = AsyncMock()
    repo.fail_signup_claim = AsyncMock(return_value=True)
    repo.update_status = AsyncMock(return_value=True)
    repo.put_failed_marker_if_absent = AsyncMock(return_value=True)

    _patch_admin_session(monkeypatch)

    # The starter seed reads the owner's container row for its ``hook_token``
    # (the native adapter's apiKey, M4-R33).
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook-seed-test"}))

    return admin, repo


def _build_provision_member_doubles(monkeypatch):
    """Admin double for ``provision_member`` (joins via the bootstrap admin).
    Returns ``admin`` only."""
    monkeypatch.setenv("ENCRYPTION_KEY", "0" * 44)

    admin = MagicMock()
    admin._http = MagicMock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    admin.create_invite = AsyncMock(return_value={"token": "invtok"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_1"})
    admin.approve_join_request = AsyncMock()

    _patch_admin_session(monkeypatch)
    return admin


def _member_repo(ws, identity=None):
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=ws)
    repo.get_workspace = AsyncMock(return_value=ws)
    repo.get_identity = AsyncMock(return_value=identity)
    repo.try_acquire_seed_claim = AsyncMock(return_value=True)
    repo.stage_signup_credentials = AsyncMock(side_effect=lambda uid, **kw: (kw["email"], kw["password_encrypted"]))
    repo.put_identity = AsyncMock()
    repo.add_member = AsyncMock()
    repo.fail_signup_claim = AsyncMock(return_value=True)
    repo.update_status = AsyncMock(return_value=True)
    repo.put_failed_marker_if_absent = AsyncMock(return_value=True)
    return repo


class _StatefulRepo:
    """In-memory paperclip-companies: workspace rows keyed by org id, identity
    rows keyed by user id, with the conditional semantics provisioning relies
    on (seed claim put-if-absent / failed reclaim, fail_signup_claim never
    touching a confirmed identity or a stolen claim, update_status not_if_status,
    put-if-absent failed marker, the G2 onboarding_generation put condition)."""

    def __init__(self, *, legacy_company_ids: dict[str, set[str]] | None = None):
        self.workspaces: dict[str, PaperclipWorkspace] = {}
        self.identities: dict[str, PaperclipIdentity] = {}
        self.claims: dict[str, str] = {}
        self._legacy = legacy_company_ids or {}

    @staticmethod
    def _copy(ws):
        return dataclasses.replace(ws, member_user_ids=set(ws.member_user_ids)) if ws else None

    # -- workspace rows --------------------------------------------------
    async def get_workspace(self, org_id, *, consistent_read=False):
        return self._copy(self.workspaces.get(org_id))

    async def put_workspace(self, ws, *, condition=None, condition_values=None):
        if condition is not None:
            existing = self.workspaces.get(ws.org_id)
            current = existing.onboarding_generation if existing else None
            if current is not None and current != (condition_values or {}).get(":g"):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conditional check failed"}},
                    "PutItem",
                )
        self.workspaces[ws.org_id] = self._copy(ws)

    async def resolve_owner_row(self, owner_id, *, consistent_read=False, include_inactive=False):
        ws = self.workspaces.get(owner_id)
        if ws is None or (not include_inactive and ws.status != "active"):
            return None
        return self._copy(ws)

    async def org_company_ids(self, org_id):
        ids = set(self._legacy.get(org_id, set()))
        ws = self.workspaces.get(org_id)
        if ws is not None and ws.company_id:
            ids.add(ws.company_id)
        return ids

    async def add_member(self, org_id, user_id):
        self.workspaces[org_id].member_user_ids.add(user_id)

    async def remove_member(self, org_id, user_id):
        if org_id in self.workspaces:
            self.workspaces[org_id].member_user_ids.discard(user_id)

    async def update_status(self, key, *, status, last_error=None, scheduled_purge_at=None, not_if_status=None):
        ws = self.workspaces.get(key)
        if ws is None or (not_if_status is not None and ws.status == not_if_status):
            return False
        ws.status = status
        if last_error is not None:
            ws.last_error = last_error
        if scheduled_purge_at is not None:
            ws.scheduled_purge_at = scheduled_purge_at
        return True

    async def put_failed_marker_if_absent(self, *, org_id, reason):
        if org_id in self.workspaces:
            return False
        self.workspaces[org_id] = _ws(org_id=org_id, company_id="", status="failed", last_error=reason)
        return True

    async def delete(self, key):
        self.workspaces.pop(key, None)
        self.identities.pop(key, None)

    # -- identity rows ---------------------------------------------------
    async def get_identity(self, user_id, *, consistent_read=False):
        ident = self.identities.get(user_id)
        return dataclasses.replace(ident) if ident else None

    async def put_identity(self, identity):
        self.identities[identity.user_id] = dataclasses.replace(identity, status=None)
        self.claims.pop(identity.user_id, None)

    async def try_acquire_seed_claim(self, user_id, *, claim_id, stale_after_seconds=300):
        existing = self.identities.get(user_id)
        if existing is None:
            self.identities[user_id] = PaperclipIdentity(user_id=user_id, status="seeding")
        elif existing.status == "failed" and not existing.paperclip_user_id:
            existing.status = "seeding"  # reclaim, retaining staged credentials
        else:
            return False
        self.claims[user_id] = claim_id
        return True

    async def stage_signup_credentials(self, user_id, *, email, password_encrypted):
        ident = self.identities[user_id]
        assert ident.status == "seeding"
        ident.paperclip_email = ident.paperclip_email or email
        ident.paperclip_password_encrypted = ident.paperclip_password_encrypted or password_encrypted
        return ident.paperclip_email, ident.paperclip_password_encrypted

    async def fail_signup_claim(self, user_id, *, reason, claim_id):
        ident = self.identities.get(user_id)
        if ident is None or ident.paperclip_user_id:
            return False
        if claim_id is not None and self.claims.get(user_id, claim_id) != claim_id:
            return False
        ident.status = "failed"
        return True


def _svc(admin, repo):
    from core.services.paperclip_provisioning import PaperclipProvisioning

    return PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")


async def _provision_org(svc, **kw):
    return await svc.provision_org(
        owner_id=kw.get("owner_id", "org_xyz"),
        owner_user_id=kw.get("owner_user_id", "user_owner"),
        owner_email=kw.get("owner_email", "owner@example.com"),
    )


# ---------------------------------------------------------------------------
# provision_org — fresh create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_org_fresh_writes_identity_and_workspace(monkeypatch):
    admin, repo = _build_provision_org_doubles(monkeypatch)

    row = await _provision_org(_svc(admin, repo))

    identity = repo.put_identity.await_args.args[0]
    assert identity.user_id == "user_owner"
    assert identity.paperclip_user_id == "pc_user_1"
    assert identity.paperclip_email == "owner@example.com"
    assert identity.paperclip_password_encrypted

    assert row is repo.put_workspace.await_args.args[0]
    assert row.org_id == "org_xyz"
    assert row.company_id == "co_xyz"
    assert row.status == "active"
    assert row.provisioner_user_id == "user_owner"
    assert row.member_user_ids == {"user_owner"}
    # The workspace row carries no credential of any kind.
    assert not hasattr(row, "paperclip_password_encrypted")
    assert not hasattr(row, "service_token_encrypted")
    # The seed claim is on the provisioner's IDENTITY row (no org/company kwargs).
    args, kwargs = repo.try_acquire_seed_claim.await_args
    assert args == ("user_owner",) and set(kwargs) == {"claim_id"}


@pytest.mark.asyncio
async def test_provision_org_seeds_every_starter_on_the_native_hermes_adapter(monkeypatch):
    """One adapter; its ``apiKey`` is the OWNER's hook token off the container row (M4-R33)."""
    from core.repositories import container_repo

    admin, repo = _build_provision_org_doubles(monkeypatch)
    row_spy = AsyncMock(return_value={"hook_token": "hook-1"})
    monkeypatch.setattr(container_repo, "get_by_owner_id", row_spy)

    await _provision_org(_svc(admin, repo), owner_id="org_hermes")

    adapter_types = {call.kwargs["adapter_type"] for call in admin.create_agent.call_args_list}
    assert adapter_types == {"hermes_gateway"}
    api_keys = {call.kwargs["adapter_config"]["apiKey"] for call in admin.create_agent.call_args_list}
    assert api_keys == {"hook-1"}
    row_spy.assert_awaited_once_with("org_hermes")


@pytest.mark.asyncio
async def test_provision_org_only_the_starter_ceo_can_create_agents(monkeypatch):
    """Paperclip v2026.813 defaulted a role=ceo agent to canCreateAgents=true and every other
    role to false; v2026.916 defaults everyone to true. The CEO keeps its hire authority by
    opting in explicitly; the five specialists take the funnel's false default."""
    from core.repositories import container_repo
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "h"}))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(owner_id="org_x", owner_user_id="user_owner", owner_email="o@example.com")

    grants = {
        call.kwargs["role"]: call.kwargs.get("can_create_agents", False) for call in admin.create_agent.call_args_list
    }
    assert grants == {"ceo": True, "engineer": False, "cfo": False, "cmo": False, "researcher": False, "general": False}


@pytest.mark.asyncio
async def test_provision_org_seeds_specialists_without_a_timer_heartbeat(monkeypatch):
    """Paperclip timers are gone: specialists wake on assignment or demand only; the CEO gets no config."""
    from core.repositories import container_repo
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "h"}))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(owner_id="org_x", owner_user_id="user_owner", owner_email="o@example.com")

    configs = {call.kwargs["role"]: call.kwargs.get("runtime_config") for call in admin.create_agent.call_args_list}
    assert configs.pop("ceo") is None
    assert set(configs) == {"engineer", "cfo", "cmo", "researcher", "general"}
    assert all(c == {"heartbeat": {"enabled": False, "wakeOnDemand": True}} for c in configs.values())


@pytest.mark.asyncio
async def test_provision_org_a_failed_routine_seed_does_not_fail_the_workspace(monkeypatch):
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(side_effect=lambda **kw: {"id": f"id-{kw['role']}"})
    admin.list_routines = AsyncMock(side_effect=RuntimeError("boom"))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(owner_id="org_x", owner_user_id="user_owner", owner_email="o@example.com")
    assert repo.put_workspace.await_args.args[0].status == "active"


@pytest.mark.asyncio
async def test_provision_org_refuses_second_company_when_org_has_one_on_record(monkeypatch):
    """No workspace row but the by-org-id GSI already names a company (a
    pre-migration per-member row): 409 BEFORE any Paperclip side effect or
    failed marker."""
    admin, repo = _build_provision_org_doubles(monkeypatch)
    repo.org_company_ids = AsyncMock(return_value={"co_old"})

    with pytest.raises(PaperclipApiError) as ei:
        await _provision_org(_svc(admin, repo))

    assert ei.value.status_code == 409
    repo.org_company_ids.assert_awaited_once_with("org_xyz")
    repo.try_acquire_seed_claim.assert_not_awaited()
    admin.sign_up_user.assert_not_awaited()
    admin.create_company.assert_not_awaited()
    admin.create_invite.assert_not_awaited()
    admin.create_agent.assert_not_awaited()
    repo.put_identity.assert_not_awaited()
    repo.put_workspace.assert_not_awaited()
    repo.fail_signup_claim.assert_not_awaited()
    repo.update_status.assert_not_awaited()
    repo.put_failed_marker_if_absent.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_org_seeded_ids_none_when_seeds_fail(monkeypatch):
    """A missing starter CEO leaves no legacy seed-protection marker."""
    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[
            PaperclipApiError("ceo", status_code=500, body="x"),
            PaperclipApiError("eng", status_code=500, body="x"),
        ]
    )

    await _provision_org(_svc(admin, repo))

    persisted = repo.put_workspace.await_args.args[0]
    assert persisted.seeded_agent_ids is None
    assert persisted.starter_agent_ids == {}
    assert persisted.status != "active"


@pytest.mark.asyncio
async def test_provision_org_required_ceo_seed_failure_not_marked_active(monkeypatch):
    """An active company with no CEO would be frozen by the idempotency
    short-circuit; it must persist a non-active status a later poll reconciles."""
    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[PaperclipApiError("ceo seed failed", status_code=500, body="boom"), {"id": "agent-x"}]
    )

    row = await _provision_org(_svc(admin, repo))

    assert row.status != "active"
    for call in repo.put_workspace.await_args_list:
        assert call.args[0].status != "active", "must not mark a CEO-less company active"


@pytest.mark.asyncio
async def test_provision_org_purge_leaves_undeletable_builtins_removes_strays(monkeypatch):
    """Paperclip's built-ins answer 409 ``built_in_agent_undeletable`` — an
    expected no-op, not a purge error; a genuine stray is deleted; starters
    are never touched."""
    admin, repo = _build_provision_org_doubles(monkeypatch)
    starter_ids = ["ceo", "eng", "fin", "growth", "analytics", "support"]
    admin.create_agent = AsyncMock(side_effect=[{"id": agent_id} for agent_id in starter_ids])
    admin.list_agents = AsyncMock(
        return_value=[
            {"id": "f", "name": "Reflection Coach"},
            {"id": "s", "name": "Summarizer"},
            {"id": "x", "name": "Some Stray"},
            *({"id": agent_id, "name": agent_id} for agent_id in starter_ids),
        ]
    )

    def _delete(*, session_cookie, agent_id):
        if agent_id in {"f", "s"}:
            raise PaperclipApiError(
                f"DELETE /api/agents/{agent_id} -> 409",
                status_code=409,
                body=(
                    '{"error":"Built-in agents cannot be deleted; pause them instead",'
                    '"code":"built_in_agent_undeletable"}'
                ),
            )
        return {}

    admin.delete_agent = AsyncMock(side_effect=_delete)
    metric_calls: list[str] = []
    import core.services.paperclip_provisioning as mod

    monkeypatch.setattr(mod, "put_metric", lambda name, *a, **k: metric_calls.append(name))

    row = await _provision_org(_svc(admin, repo))

    admin.list_agents.assert_awaited_once()
    assert admin.list_agents.await_args.kwargs["company_id"] == "co_xyz"
    attempted = {c.kwargs["agent_id"] for c in admin.delete_agent.await_args_list}
    assert attempted == {"f", "s", "x"}
    assert "paperclip.provision.builtin_purge_error" not in metric_calls
    assert row.status == "active"


@pytest.mark.asyncio
async def test_provision_org_builtin_purge_failure_never_fails_provision(monkeypatch):
    admin, repo = _build_provision_org_doubles(monkeypatch)
    starter_ids = ["ceo", "eng", "fin", "growth", "analytics", "support"]
    admin.create_agent = AsyncMock(side_effect=[{"id": agent_id} for agent_id in starter_ids])
    admin.list_agents = AsyncMock(
        return_value=[
            {"id": "f", "name": "Reflection Coach"},
            {"id": "s", "name": "Summarizer"},
            *({"id": agent_id, "name": agent_id} for agent_id in starter_ids),
        ]
    )
    admin.delete_agent = AsyncMock(side_effect=PaperclipApiError("delete boom", status_code=500, body="x"))

    row = await _provision_org(_svc(admin, repo))

    assert admin.delete_agent.await_count == 2
    assert row.status == "active"
    assert repo.put_workspace.await_args.args[0].status == "active"


@pytest.mark.asyncio
async def test_provision_org_failed_ceo_skips_builtin_purge(monkeypatch):
    """The purge runs only once the starter contract is complete."""
    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[PaperclipApiError("ceo seed failed", status_code=500, body="boom"), {"id": "at"}]
    )
    admin.list_agents = AsyncMock(return_value=[{"id": "f", "name": "Reflection Coach"}])

    row = await _provision_org(_svc(admin, repo))

    assert row.status != "active"
    admin.list_agents.assert_not_awaited()
    admin.delete_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_org_skips_approve_when_accept_auto_approved(monkeypatch):
    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.accept_invite = AsyncMock(return_value={"id": "req_1", "status": "approved"})

    row = await _provision_org(_svc(admin, repo))

    admin.approve_join_request.assert_not_awaited()
    assert isinstance(row, PaperclipWorkspace)
    assert row.status == "active"
    assert admin.create_agent.await_count == 6
    # The ADMIN invites the provisioner as an owner; the provisioner accepts.
    invite = admin.create_invite.await_args.kwargs
    assert invite["session_cookie"] == "admin_cookie"
    assert invite["company_id"] == "co_xyz"
    assert invite["human_role"] == "owner"
    assert admin.accept_invite.await_args.kwargs["session_cookie"] == "cookie_new"


@pytest.mark.asyncio
async def test_provision_org_approves_when_accept_left_pending(monkeypatch):
    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.accept_invite = AsyncMock(return_value={"id": "req_1", "status": "pending_approval"})

    await _provision_org(_svc(admin, repo))

    admin.approve_join_request.assert_awaited_once()
    approve_kwargs = admin.approve_join_request.await_args.kwargs
    assert approve_kwargs["company_id"] == "co_xyz"
    assert approve_kwargs["request_id"] == "req_1"


@pytest.mark.asyncio
async def test_provision_org_names_company_from_generated_name(monkeypatch):
    import core.services.paperclip_provisioning as mod

    admin, repo = _build_provision_org_doubles(monkeypatch)

    async def fake_generate(*, org_id, owner_email):
        return "Acme Robotics"

    monkeypatch.setattr(mod, "generate_company_name", fake_generate)

    await _provision_org(_svc(admin, repo))

    assert admin.create_company.await_args.kwargs["name"] == "Acme Robotics"


@pytest.mark.asyncio
async def test_provision_org_survives_name_generation_failure(monkeypatch):
    import core.services.paperclip_provisioning as mod

    admin, repo = _build_provision_org_doubles(monkeypatch)

    async def boom(**kwargs):
        raise RuntimeError("clerk down")

    monkeypatch.setattr(mod, "generate_company_name", boom)

    row = await _provision_org(_svc(admin, repo))

    assert row.status == "active"
    assert admin.create_company.await_args.kwargs["name"] == "owner@example.com"


@pytest.mark.asyncio
async def test_provision_org_does_NOT_allocate_a_slug_or_mint_an_address(monkeypatch):
    """Slug/address minting happens on the first owner-directed send."""
    import core.services.agent_email as agent_email_mod
    import core.services.company_identity as company_identity_mod
    import core.services.paperclip_provisioning as mod

    admin, repo = _build_provision_org_doubles(monkeypatch)
    calls = []

    async def spy_allocate(*, owner_id, name):
        calls.append("allocate")
        return "acme-robotics"

    async def spy_mint(*args, **kwargs):
        calls.append("mint")
        return "agents@acme-robotics.example", "ok"

    monkeypatch.setattr(company_identity_mod, "allocate_company_slug", spy_allocate)
    monkeypatch.setattr(agent_email_mod, "ensure_team_address", spy_mint)
    assert not hasattr(mod, "allocate_company_slug")
    assert not hasattr(mod, "ensure_team_address")

    row = await _provision_org(_svc(admin, repo))

    assert row.status == "active"
    assert calls == []


# ---------------------------------------------------------------------------
# provision_org — idempotency, fence, onboarding generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_org_active_complete_workspace_short_circuits(monkeypatch):
    admin, repo = _build_provision_org_doubles(monkeypatch)
    from core.services.paperclip_provisioning import STARTER_ROLE_KEYS

    active = _ws(starter_agent_ids={k: f"a-{k}" for k in STARTER_ROLE_KEYS}, members={"user_owner"})
    repo.get_workspace = AsyncMock(return_value=active)

    result = await _provision_org(_svc(admin, repo))

    assert result is active
    repo.try_acquire_seed_claim.assert_not_awaited()
    repo.put_workspace.assert_not_awaited()
    admin.sign_up_user.assert_not_awaited()
    admin.create_company.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_org_lost_claim_adopts_winner_workspace(monkeypatch):
    import core.services.paperclip_provisioning as mod

    admin, repo = _build_provision_org_doubles(monkeypatch)
    monkeypatch.setattr(mod, "_SEED_WAIT_INTERVAL_S", 0.0)
    winner = _ws(members={"user_owner"}, provisioner="user_owner")
    repo.try_acquire_seed_claim = AsyncMock(return_value=False)
    repo.get_workspace = AsyncMock(side_effect=[None, winner])

    result = await _provision_org(_svc(admin, repo))

    assert result is winner
    admin.sign_up_user.assert_not_awaited()
    admin.create_company.assert_not_awaited()
    repo.put_workspace.assert_not_awaited()
    repo.put_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_org_lost_claim_peer_failed_raises_409_without_waiting(monkeypatch):
    import core.services.paperclip_provisioning as mod

    admin, repo = _build_provision_org_doubles(monkeypatch)
    monkeypatch.setattr(mod, "_SEED_WAIT_INTERVAL_S", 0.0)
    repo.try_acquire_seed_claim = AsyncMock(return_value=False)
    repo.get_workspace = AsyncMock(side_effect=[None, _ws(company_id="", status="failed")])

    with pytest.raises(PaperclipApiError) as ei:
        await _provision_org(_svc(admin, repo))

    assert ei.value.status_code == 409
    assert repo.get_workspace.await_count == 2  # broke out on the first poll
    admin.sign_up_user.assert_not_awaited()
    repo.put_workspace.assert_not_awaited()
    repo.fail_signup_claim.assert_not_awaited()  # the loser owns no claim


@pytest.mark.asyncio
async def test_provision_org_fresh_mints_onboarding_generation_conditionally(monkeypatch):
    admin, repo = _build_provision_org_doubles(monkeypatch)

    row = await _provision_org(_svc(admin, repo))

    assert row.onboarding_generation
    assert row.funnel_state == "not_started"
    kwargs = repo.put_workspace.await_args.kwargs
    assert kwargs["condition"] == "attribute_not_exists(onboarding_generation) OR onboarding_generation = :g"
    assert kwargs["condition_values"] == {":g": row.onboarding_generation}


@pytest.mark.asyncio
async def test_provision_org_adopts_existing_generation_of_a_company_less_marker(monkeypatch):
    """A pre-company failed marker carrying a generation is a fresh create
    (no company to reconcile) but keeps that generation."""
    admin, repo = _build_provision_org_doubles(monkeypatch)
    repo.get_workspace = AsyncMock(
        return_value=_ws(company_id="", status="failed", onboarding_generation="gen_old", funnel_state="submitted")
    )

    row = await _provision_org(_svc(admin, repo))

    admin.create_company.assert_awaited_once()
    assert row.onboarding_generation == "gen_old"
    assert row.funnel_state == "not_started"


@pytest.mark.asyncio
async def test_provision_org_generation_put_race_adopts_peer_generation(monkeypatch):
    """The conditional mint put loses to a peer who persisted a generation
    first: re-read (consistent), adopt the stored generation, put once more."""
    admin, _ = _build_provision_org_doubles(monkeypatch)
    repo = _StatefulRepo()
    real_put = repo.put_workspace
    raced = {"done": False}

    async def racing_put(ws, *, condition=None, condition_values=None):
        if condition is not None and not raced["done"]:
            raced["done"] = True
            repo.workspaces[ws.org_id] = _ws(status="provisioning", onboarding_generation="gen_peer")
        await real_put(ws, condition=condition, condition_values=condition_values)

    repo.put_workspace = racing_put

    row = await _provision_org(_svc(admin, repo))

    assert row.onboarding_generation == "gen_peer"
    stored = repo.workspaces["org_xyz"]
    assert stored.onboarding_generation == "gen_peer"
    assert stored.status == "active"
    assert stored.member_user_ids == {"user_owner"}


# ---------------------------------------------------------------------------
# provision_org — failure marking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_org_partial_failure_after_signup_leaves_adoptable_identity(monkeypatch):
    """A post-sign-up failure leaves a CONFIRMED identity (so the retry signs
    in instead of re-signing-up into a 422) and a ``failed`` workspace marker
    (so /teams answers 503, not an eternal 202)."""
    from core.encryption import decrypt

    admin, _ = _build_provision_org_doubles(monkeypatch)
    admin.create_company = AsyncMock(side_effect=PaperclipApiError("company boom", status_code=500, body="x"))
    repo = _StatefulRepo()

    with pytest.raises(PaperclipApiError):
        await _provision_org(_svc(admin, repo))

    identity = repo.identities["user_owner"]
    assert identity.status is None, "a confirmed identity is never downgraded"
    assert identity.paperclip_user_id == "pc_user_1"
    assert decrypt(identity.paperclip_password_encrypted)

    ws = repo.workspaces["org_xyz"]
    assert ws.status == "failed"
    assert ws.company_id == ""
    assert "company boom" in ws.last_error


@pytest.mark.asyncio
async def test_provision_org_signup_failure_downgrades_claim_and_marks_workspace(monkeypatch):
    admin, _ = _build_provision_org_doubles(monkeypatch)
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("signup down", status_code=503, body="x"))
    repo = _StatefulRepo()

    with pytest.raises(PaperclipApiError):
        await _provision_org(_svc(admin, repo))

    assert repo.identities["user_owner"].status == "failed"
    assert repo.identities["user_owner"].paperclip_user_id == ""
    assert repo.workspaces["org_xyz"].status == "failed"
    admin.create_company.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_failed_marks_existing_workspace_failed_in_place(monkeypatch):
    repo = _StatefulRepo()
    repo.workspaces["org_xyz"] = _ws(status="provisioning")

    await _svc(MagicMock(), repo)._mark_failed(user_id="user_m", org_id="org_xyz", reason="boom", seed_claim_id=None)

    assert repo.workspaces["org_xyz"].status == "failed"
    assert repo.workspaces["org_xyz"].last_error == "boom"


@pytest.mark.asyncio
async def test_mark_failed_does_not_downgrade_active_workspace_or_confirmed_identity(monkeypatch):
    repo = _StatefulRepo()
    repo.workspaces["org_xyz"] = _ws(status="active")
    repo.identities["user_m"] = PaperclipIdentity(user_id="user_m", paperclip_user_id="pc_m")

    await _svc(MagicMock(), repo)._mark_failed(
        user_id="user_m", org_id="org_xyz", reason="late transient error", seed_claim_id="claim-1"
    )

    assert repo.workspaces["org_xyz"].status == "active"
    assert repo.workspaces["org_xyz"].last_error is None
    assert repo.identities["user_m"].status is None


@pytest.mark.asyncio
async def test_mark_failed_no_workspace_writes_conditional_marker(monkeypatch):
    """No workspace row: ONE put-if-absent marker keyed by the org — never an
    unconditional whole-item put."""
    repo = MagicMock()
    repo.get_workspace = AsyncMock(return_value=None)
    repo.put_workspace = AsyncMock()
    repo.update_status = AsyncMock(return_value=True)
    repo.put_failed_marker_if_absent = AsyncMock(return_value=True)
    repo.fail_signup_claim = AsyncMock(return_value=True)

    await _svc(MagicMock(), repo)._mark_failed(
        user_id="user_orphan", org_id="org_x", reason="signup 422 duplicate", seed_claim_id="claim-1"
    )

    repo.fail_signup_claim.assert_awaited_once_with("user_orphan", reason="signup 422 duplicate", claim_id="claim-1")
    repo.put_failed_marker_if_absent.assert_awaited_once_with(org_id="org_x", reason="signup 422 duplicate")
    assert repo.get_workspace.await_args.kwargs == {"consistent_read": True}
    repo.update_status.assert_not_awaited()
    repo.put_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_failed_without_claim_or_org_writes_nothing(monkeypatch):
    repo = _member_repo(None)

    await _svc(MagicMock(), repo)._mark_failed(user_id="user_m", reason="x", seed_claim_id=None)

    repo.fail_signup_claim.assert_not_awaited()
    repo.get_workspace.assert_not_awaited()
    repo.update_status.assert_not_awaited()
    repo.put_failed_marker_if_absent.assert_not_awaited()


# ---------------------------------------------------------------------------
# provision_member
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_member_retries_the_invite_once_on_an_expired_admin_session(monkeypatch):
    import core.services.paperclip_admin_session as admin_session_mod

    admin = _build_provision_member_doubles(monkeypatch)
    invalidated = MagicMock()
    monkeypatch.setattr(admin_session_mod, "invalidate_admin_session", invalidated)
    admin.create_invite = AsyncMock(
        side_effect=[PaperclipApiError("expired", status_code=401, body=None), {"token": "invtok"}]
    )
    repo = _member_repo(_ws(members={"user_owner"}))

    await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com")

    assert admin.create_invite.await_count == 2
    invalidated.assert_called_once()
    repo.add_member.assert_awaited_once_with("org_xyz", "user_m")


@pytest.mark.asyncio
async def test_provision_member_joins_via_bootstrap_admin(monkeypatch):
    admin = _build_provision_member_doubles(monkeypatch)
    ws = _ws(members={"user_owner"})
    repo = _member_repo(ws)

    result = await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com")

    # The bootstrap admin invites (as an owner); nobody signs in as the owner.
    invite = admin.create_invite.await_args.kwargs
    assert invite == {
        "session_cookie": "admin_cookie",
        "company_id": "co_xyz",
        "email": "m@example.com",
        "human_role": "owner",
    }
    admin.sign_in_user.assert_not_awaited()
    assert admin.accept_invite.await_args.kwargs["session_cookie"] == "member-session"
    identity = repo.put_identity.await_args.args[0]
    assert identity.user_id == "user_m" and identity.paperclip_user_id == "pc_user_member"
    repo.add_member.assert_awaited_once_with("org_xyz", "user_m")
    assert result.member_user_ids == {"user_owner", "user_m"}


@pytest.mark.asyncio
async def test_provision_member_short_circuits_when_already_joined(monkeypatch):
    admin = _build_provision_member_doubles(monkeypatch)
    ws = _ws(members={"user_owner", "user_m"})
    repo = _member_repo(ws)

    result = await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com")

    assert result is ws
    repo.get_identity.assert_not_awaited()
    repo.try_acquire_seed_claim.assert_not_awaited()
    repo.add_member.assert_not_awaited()
    admin.sign_up_user.assert_not_awaited()
    admin.create_invite.assert_not_awaited()


@pytest.mark.parametrize("ws", [None, _ws(company_id="")])
@pytest.mark.asyncio
async def test_provision_member_requires_active_workspace(monkeypatch, ws):
    from core.services.paperclip_provisioning import OrgNotProvisionedError

    admin = _build_provision_member_doubles(monkeypatch)
    repo = _member_repo(ws)

    with pytest.raises(OrgNotProvisionedError):
        await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com")

    repo.resolve_owner_row.assert_awaited_once_with("org_xyz")
    repo.try_acquire_seed_claim.assert_not_awaited()
    admin.sign_up_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_member_adopts_existing_identity_instead_of_resignup(monkeypatch):
    """One identity per human: a member already signed up (e.g. in another
    workspace) is ADOPTED via sign-in with the stored password and recorded
    email — never re-signed-up — and needs no sign-up claim."""
    admin = _build_provision_member_doubles(monkeypatch)
    identity = _identity("user_m", paperclip_user_id="pc_user_m", password="member-pw-plain", email="old@example.com")
    repo = _member_repo(_ws(members={"user_owner"}), identity)

    await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com")

    admin.sign_up_user.assert_not_awaited()
    repo.try_acquire_seed_claim.assert_not_awaited()
    signin = admin.sign_in_user.await_args.kwargs
    assert signin == {"email": "old@example.com", "password": "member-pw-plain"}
    repo.put_identity.assert_not_awaited()
    repo.add_member.assert_awaited_once_with("org_xyz", "user_m")


@pytest.mark.asyncio
async def test_provision_member_adoption_failure_leaves_identity_and_membership_alone(monkeypatch):
    admin = _build_provision_member_doubles(monkeypatch)
    admin.create_invite = AsyncMock(side_effect=PaperclipApiError("invite boom", status_code=500, body="x"))
    identity = _identity("user_m", paperclip_user_id="pc_user_m", password="member-pw-plain")
    repo = _member_repo(_ws(members={"user_owner"}), identity)

    with pytest.raises(PaperclipApiError):
        await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com")

    repo.fail_signup_claim.assert_not_awaited()  # no claim was taken
    repo.update_status.assert_not_awaited()  # the workspace is never marked by a member join
    repo.put_failed_marker_if_absent.assert_not_awaited()
    repo.add_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_member_already_belongs_409_is_recorded_as_joined(monkeypatch):
    admin = _build_provision_member_doubles(monkeypatch)
    admin.accept_invite = AsyncMock(
        side_effect=PaperclipApiError("You already belong to this company", status_code=409, body="x")
    )
    repo = _member_repo(_ws(members={"user_owner"}))

    await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com")

    repo.add_member.assert_awaited_once_with("org_xyz", "user_m")
    repo.fail_signup_claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_member_skips_approve_when_accept_auto_approved(monkeypatch):
    admin = _build_provision_member_doubles(monkeypatch)
    admin.accept_invite = AsyncMock(return_value={"id": "req_1", "status": "approved"})
    repo = _member_repo(_ws(members={"user_owner"}))

    await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com")

    admin.approve_join_request.assert_not_awaited()
    repo.add_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_provision_member_approves_when_accept_left_pending(monkeypatch):
    admin = _build_provision_member_doubles(monkeypatch)
    admin.accept_invite = AsyncMock(return_value={"id": "req_1", "status": "pending_approval"})
    repo = _member_repo(_ws(members={"user_owner"}))

    await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com")

    admin.approve_join_request.assert_awaited_once()
    assert admin.approve_join_request.await_args.kwargs["session_cookie"] == "admin_cookie"


@pytest.mark.asyncio
async def test_provision_member_partial_failure_after_signup_leaves_adoptable_identity(monkeypatch):
    from core.encryption import decrypt

    admin = _build_provision_member_doubles(monkeypatch)
    admin.create_invite = AsyncMock(side_effect=PaperclipApiError("invite boom", status_code=500, body="x"))
    repo = _StatefulRepo()
    repo.workspaces["org_xyz"] = _ws(members={"user_owner"})

    with pytest.raises(PaperclipApiError):
        await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com")

    stored = repo.identities["user_m"]
    assert stored.status is None
    assert stored.paperclip_user_id == "pc_user_member"
    assert decrypt(stored.paperclip_password_encrypted)
    assert repo.workspaces["org_xyz"].member_user_ids == {"user_owner"}
    assert repo.workspaces["org_xyz"].status == "active"


# provision_member sign-up fence: concurrent dispatches for one joining member
# used to each sign up the same email with a different random password.


def _member_fence_doubles(monkeypatch, *, claimed: bool, winner_ws=None):
    import core.services.paperclip_provisioning as prov_mod

    admin = _build_provision_member_doubles(monkeypatch)
    monkeypatch.setattr(prov_mod, "_SEED_WAIT_INTERVAL_S", 0.0)
    monkeypatch.setattr(prov_mod, "_SEED_WAIT_ATTEMPTS", 2)
    repo = _member_repo(_ws(members={"user_owner"}))
    repo.try_acquire_seed_claim = AsyncMock(return_value=claimed)
    repo.get_workspace = AsyncMock(return_value=winner_ws or _ws(members={"user_owner"}))
    return admin, repo


@pytest.mark.asyncio
async def test_provision_member_takes_seed_claim_before_signup(monkeypatch):
    admin, repo = _member_fence_doubles(monkeypatch, claimed=True)
    order = MagicMock()
    order.attach_mock(repo.try_acquire_seed_claim, "claim")
    order.attach_mock(admin.sign_up_user, "signup")

    await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_member", email="member@example.com")

    assert [c[0] for c in order.mock_calls][:2] == ["claim", "signup"], "claim must land before the sign-up"
    args, kwargs = repo.try_acquire_seed_claim.await_args
    assert args == ("user_member",) and set(kwargs) == {"claim_id"} and kwargs["claim_id"]


@pytest.mark.asyncio
async def test_provision_member_lost_claim_adopts_recorded_join_without_signup(monkeypatch):
    winner = _ws(members={"user_owner", "user_member"})
    admin, repo = _member_fence_doubles(monkeypatch, claimed=False, winner_ws=winner)

    result = await _svc(admin, repo).provision_member(
        owner_id="org_xyz", user_id="user_member", email="member@example.com"
    )

    assert result is winner
    admin.sign_up_user.assert_not_awaited()
    admin.sign_in_user.assert_not_awaited()
    repo.put_identity.assert_not_awaited()
    repo.add_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_member_lost_claim_timeout_raises_409_without_signup(monkeypatch):
    admin, repo = _member_fence_doubles(monkeypatch, claimed=False)

    with pytest.raises(PaperclipApiError) as ei:
        await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_member", email="member@example.com")

    assert ei.value.status_code == 409
    assert repo.get_workspace.await_count == 2
    admin.sign_up_user.assert_not_awaited()
    repo.put_identity.assert_not_awaited()
    repo.add_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_member_signup_failure_right_after_claim_downgrades_claim(monkeypatch):
    """Sign-up fails right after the claim: the identity claim goes ``failed``
    (reclaimable) instead of staying ``seeding`` for the 300 s stale window."""
    admin = _build_provision_member_doubles(monkeypatch)
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("dup email", status_code=422, body="unprocessable"))
    repo = _StatefulRepo()
    repo.workspaces["org_xyz"] = _ws(members={"user_owner"})

    with pytest.raises(PaperclipApiError):
        await _svc(admin, repo).provision_member(owner_id="org_xyz", user_id="user_member", email="member@example.com")

    assert repo.identities["user_member"].status == "failed"
    # A member join never marks the workspace.
    assert repo.workspaces["org_xyz"].status == "active"
    assert repo.workspaces["org_xyz"].member_user_ids == {"user_owner"}


@pytest.mark.asyncio
async def test_sign_up_with_staged_credentials_needs_no_claim(monkeypatch):
    admin, repo = _build_provision_org_doubles(monkeypatch)

    account, email, password = await _svc(admin, repo)._sign_up_with_staged_credentials(
        user_id="user_owner", email="owner@example.com", password="plain-pw"
    )

    assert account == {"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_new"}
    repo.stage_signup_credentials.assert_awaited_once()
    repo.try_acquire_seed_claim.assert_not_awaited()


# ---------------------------------------------------------------------------
# archive_member — org-scoped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_member_only_touches_the_named_workspace(monkeypatch):
    """A human in orgs A and B removed from A keeps B's membership, and
    Paperclip is only ever asked about A's company. The DDB membership (the
    Teams fence) drops BEFORE the Paperclip calls."""
    _patch_admin_session(monkeypatch)
    repo = _StatefulRepo()
    repo.workspaces["org_A"] = _ws(org_id="org_A", company_id="co_A", members={"user_m", "user_a"})
    repo.workspaces["org_B"] = _ws(org_id="org_B", company_id="co_B", members={"user_m", "user_b"})
    repo.identities["user_m"] = PaperclipIdentity(user_id="user_m", paperclip_user_id="pc_m")

    admin = MagicMock()
    admin._http = MagicMock()
    seen_members_at_list: list[set] = []

    async def list_members(*, session_cookie, company_id):
        seen_members_at_list.append(set(repo.workspaces["org_A"].member_user_ids))
        return {"members": [{"id": "mem_other", "principalId": "pc_a"}, {"id": "mem_m", "principalId": "pc_m"}]}

    admin.list_members = AsyncMock(side_effect=list_members)
    admin.archive_member = AsyncMock()

    await _svc(admin, repo).archive_member(user_id="user_m", org_id="org_A")

    assert repo.workspaces["org_A"].member_user_ids == {"user_a"}
    assert repo.workspaces["org_B"].member_user_ids == {"user_m", "user_b"}
    assert seen_members_at_list == [{"user_a"}], "DDB membership must drop before the Paperclip archive"
    assert {c.kwargs["company_id"] for c in admin.list_members.await_args_list} == {"co_A"}
    admin.archive_member.assert_awaited_once_with(session_cookie="admin_cookie", company_id="co_A", member_id="mem_m")
    assert "user_m" in repo.identities  # identity outlives any one workspace


@pytest.mark.asyncio
async def test_archive_member_without_workspace_or_identity_skips_paperclip(monkeypatch):
    _patch_admin_session(monkeypatch)
    repo = _StatefulRepo()
    repo.workspaces["org_A"] = _ws(org_id="org_A", company_id="co_A", members={"user_m"})
    admin = MagicMock()
    admin.list_members = AsyncMock()
    admin.archive_member = AsyncMock()
    svc = _svc(admin, repo)

    await svc.archive_member(user_id="user_m", org_id="org_missing")
    await svc.archive_member(user_id="user_m", org_id="org_A")  # no identity row

    assert repo.workspaces["org_A"].member_user_ids == set()
    admin.list_members.assert_not_awaited()
    admin.archive_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_member_absent_from_paperclip_is_logged_not_raised(monkeypatch):
    _patch_admin_session(monkeypatch)
    repo = _StatefulRepo()
    repo.workspaces["org_A"] = _ws(org_id="org_A", company_id="co_A", members={"user_m"})
    repo.identities["user_m"] = PaperclipIdentity(user_id="user_m", paperclip_user_id="pc_m")
    admin = MagicMock()
    admin._http = MagicMock()
    admin.list_members = AsyncMock(return_value=[{"id": "mem_x", "principalId": "pc_other"}])
    admin.archive_member = AsyncMock()

    await _svc(admin, repo).archive_member(user_id="user_m", org_id="org_A")

    admin.archive_member.assert_not_awaited()
    assert repo.workspaces["org_A"].member_user_ids == set()


@pytest.mark.asyncio
async def test_archive_member_5xx_reraises_after_dropping_membership(monkeypatch):
    _patch_admin_session(monkeypatch)
    repo = _StatefulRepo()
    repo.workspaces["org_A"] = _ws(org_id="org_A", company_id="co_A", members={"user_m"})
    repo.identities["user_m"] = PaperclipIdentity(user_id="user_m", paperclip_user_id="pc_m")
    admin = MagicMock()
    admin._http = MagicMock()
    admin.list_members = AsyncMock(return_value=[{"id": "mem_m", "principalId": "pc_m"}])
    admin.archive_member = AsyncMock(side_effect=PaperclipApiError("down", status_code=503, body="x"))

    with pytest.raises(PaperclipApiError):
        await _svc(admin, repo).archive_member(user_id="user_m", org_id="org_A")

    assert repo.workspaces["org_A"].member_user_ids == set()
    assert "user_m" in repo.identities  # the retry still has what it needs
