"""Tests for org/member provisioning behaviors that don't fit the
heavier moto-backed suite in ``tests/test_paperclip_provisioning.py``.

Currently exercises:
* The auto-seeded Main Agent name + role (``"Friday"`` / ``"ceo"``).
* The auto-seeded Atlas agent (role="general"; "recruiter" isn't in
  Paperclip's AGENT_ROLES enum) and its independence from the Friday
  seed (defense-in-depth).
* The service-token mint contract: containers are keyed by
  ``owner_id`` and the JWT ``sub`` claim MUST equal that value (NOT
  the human creator's ``user_id``) — see ``service_token.mint`` and
  the WS Lambda authorizer.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services.paperclip_admin_client import PaperclipApiError


def _build_provision_org_doubles(monkeypatch):
    """Shared scaffolding for ``provision_org`` tests.

    Sets the env vars ``service_token.mint``/``encrypt`` need, builds
    AsyncMock-backed admin + repo, and stubs the lazily-imported
    admin-session helpers. Returns ``(admin, repo)`` so each test can
    customize behavior or add its own assertions.
    """
    # service_token.mint() reads PAPERCLIP_SERVICE_TOKEN_KEY lazily on each
    # call, so set it before invoking provision_org.
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-service-token-key")
    monkeypatch.setenv("ENCRYPTION_KEY", "0" * 44)  # Fernet expects 32 url-safe-base64 bytes

    admin = MagicMock()
    admin._http = MagicMock()  # touched by get_admin_session_cookie
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_new"})
    admin.create_company = AsyncMock(return_value={"id": "co_xyz"})
    admin.create_invite = AsyncMock(return_value={"token": "invtok"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_1"})
    admin.approve_join_request = AsyncMock()
    admin.create_agent = AsyncMock()

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.put = AsyncMock()

    # Stub admin-session helper imported lazily inside provision_org.
    import core.services.paperclip_provisioning as mod
    import core.services.paperclip_admin_session as admin_session_mod

    async def _fake_admin_cookie(_http):
        return "admin_cookie"

    monkeypatch.setattr(admin_session_mod, "get_admin_session_cookie", _fake_admin_cookie)
    monkeypatch.setattr(admin_session_mod, "invalidate_admin_session", lambda: None)
    if hasattr(mod, "get_admin_session_cookie"):
        monkeypatch.setattr(mod, "get_admin_session_cookie", _fake_admin_cookie, raising=False)

    return admin, repo


def _build_provision_member_doubles(monkeypatch):
    """Shared scaffolding for ``provision_member`` tests.

    ``provision_member`` doesn't call ``create_company``, but it DOES
    sign in as the org owner and look up the company_id. We seed a
    fake owner row directly via ``repo.get`` / ``find_org_owner``
    rather than hauling in the full moto fixture.
    """
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "test-service-token-key")
    monkeypatch.setenv("ENCRYPTION_KEY", "0" * 44)

    admin = MagicMock()
    admin._http = MagicMock()
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_owner"}, "_session_cookie": "owner-session"})
    admin.create_invite = AsyncMock(return_value={"token": "invtok"})
    admin.accept_invite = AsyncMock(return_value={"id": "req_1"})
    admin.approve_join_request = AsyncMock()

    import core.services.paperclip_provisioning as mod
    import core.services.paperclip_admin_session as admin_session_mod

    async def _fake_admin_cookie(_http):
        return "admin_cookie"

    monkeypatch.setattr(admin_session_mod, "get_admin_session_cookie", _fake_admin_cookie)
    monkeypatch.setattr(admin_session_mod, "invalidate_admin_session", lambda: None)
    if hasattr(mod, "get_admin_session_cookie"):
        monkeypatch.setattr(mod, "get_admin_session_cookie", _fake_admin_cookie, raising=False)

    return admin


@pytest.mark.asyncio
async def test_provision_org_seeds_friday_and_atlas(monkeypatch):
    """provision_org seeds BOTH Friday (role="ceo") and Atlas (role="general"; "recruiter" isn't in Paperclip's AGENT_ROLES enum) in one step.

    Atlas is the in-house agent-recruiter that Friday-as-orchestrator
    delegates "hire a new agent" work to, so it must exist alongside
    Friday at day-one provisioning. Friday must keep role='ceo' so
    config.py:463's default-agent selection still lands on Friday.
    """
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)

    # Return real dicts so the Friday id is captured for Atlas's reports_to.
    admin.create_agent = AsyncMock(side_effect=[{"id": "agent-friday"}, {"id": "agent-atlas"}])

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # Both Friday + Atlas are seeded in the same provision step.
    assert admin.create_agent.await_count == 2

    names = {call.kwargs["name"] for call in admin.create_agent.await_args_list}
    roles = {call.kwargs["role"] for call in admin.create_agent.await_args_list}
    idempotency_keys = {call.kwargs["idempotency_key"] for call in admin.create_agent.await_args_list}

    assert names == {"Friday", "Atlas"}
    # Friday must remain role='ceo' for default-agent selection;
    # Atlas uses 'general' (Paperclip's AGENT_ROLES enum doesn't include "recruiter").
    assert roles == {"ceo", "general"}
    # Idempotency keys are distinct so retries don't collide.
    assert idempotency_keys == {
        "user_owner:main-agent",
        "user_owner:atlas-agent",
    }

    # Cross-check the pairing: Friday ↔ ceo ↔ :main-agent, Atlas ↔ general ↔ :atlas-agent.
    by_name = {call.kwargs["name"]: call.kwargs for call in admin.create_agent.await_args_list}
    assert by_name["Friday"]["role"] == "ceo"
    assert by_name["Friday"]["idempotency_key"] == "user_owner:main-agent"
    assert by_name["Atlas"]["role"] == "general"
    assert by_name["Atlas"]["idempotency_key"] == "user_owner:atlas-agent"

    # Roster metadata: the roster is Friday's delegation source of truth, so
    # both seeds must carry real titles + capabilities, and Atlas must be
    # parented under Friday (chainOfCommand walks reportsTo).
    assert by_name["Friday"]["title"] == "CEO"
    assert by_name["Friday"]["capabilities"]
    assert by_name["Atlas"]["title"] == "Agent Recruiter"
    assert "not" in by_name["Atlas"]["capabilities"].lower()  # the does-NOT clause
    assert by_name["Atlas"]["reports_to"] == "agent-friday"


@pytest.mark.asyncio
async def test_provision_org_atlas_failure_does_not_block_friday_seed(monkeypatch):
    """Defense-in-depth: an Atlas seed failure must NOT undo a successful
    Friday seed.

    Both seeds are best-effort and wrapped in independent try/except
    blocks so a 5xx from Paperclip on Atlas leaves the company usable
    with Friday already created.
    """
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)

    # Friday succeeds; Atlas raises a 500 — mirrors a transient
    # Paperclip outage on the second seed call.
    admin.create_agent = AsyncMock(
        side_effect=[
            None,
            PaperclipApiError("atlas create failed", status_code=500, body="boom"),
        ]
    )

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    # No exception should propagate out — both seeds are best-effort.
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # We still attempted both seeds (the Friday try/except didn't
    # short-circuit the Atlas attempt) ...
    assert admin.create_agent.await_count == 2
    # ... and the row was still persisted (provision_org completed):
    # early adoptable credential row + final active row.
    assert repo.put.await_count == 2
    assert repo.put.await_args.args[0].status == "active"


@pytest.mark.asyncio
async def test_provision_org_friday_failure_leaves_atlas_unparented(monkeypatch):
    """When the Friday mint fails, Atlas is still seeded — with no
    ``reports_to`` (there is no Friday id to parent under)."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)

    admin.create_agent = AsyncMock(
        side_effect=[
            PaperclipApiError("friday create failed", status_code=500, body="boom"),
            {"id": "agent-atlas"},
        ]
    )

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert admin.create_agent.await_count == 2
    atlas_kwargs = admin.create_agent.await_args_list[1].kwargs
    assert atlas_kwargs["name"] == "Atlas"
    assert atlas_kwargs["reports_to"] is None


@pytest.mark.asyncio
async def test_provision_org_mints_service_token_with_owner_id(monkeypatch):
    """The service-token JWT baked into the seeded agent must have
    ``sub == owner_id`` (NOT the human creator's ``owner_user_id``).

    The WS Lambda authorizer extracts ``sub`` from the service token
    and routes the connection to that owner's OpenClaw container.
    Minting with the human creator's ``user_id`` was the regression
    that broke wakes for org-shared agents.
    """
    from core.services import service_token
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)

    mint_spy = MagicMock(return_value="svc.tok.minted")
    monkeypatch.setattr(service_token, "mint", mint_spy)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # Mint was called with owner_id, NOT the human creator's user_id.
    mint_spy.assert_called_once_with("org_xyz")
    # Belt-and-suspenders: explicit negative — never minted with the
    # human user_id on the org path.
    for call in mint_spy.call_args_list:
        assert call.args[0] != "user_owner", (
            "service_token.mint was called with the human user_id; org members "
            "share the org's container so the JWT sub MUST be the owner_id."
        )


@pytest.mark.asyncio
async def test_provision_member_mints_service_token_with_owner_id(monkeypatch):
    """Member provisioning must mint the JWT with ``sub == owner_id``,
    NOT the joining member's ``user_id`` — same routing constraint as
    ``provision_org``: org members share the org's container.
    """
    from core.repositories.paperclip_repo import PaperclipCompany
    from core.services import service_token
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)

    # Build a repo double that returns no row for the joining member
    # but a valid owner row for ``find_org_owner`` / company lookup.
    from datetime import datetime, timezone

    owner_row = PaperclipCompany(
        user_id="user_owner",
        org_id="org_xyz",
        company_id="co_xyz",
        paperclip_user_id="pc_user_owner",
        # Encryption uses the env-provided key set by
        # _build_provision_member_doubles via monkeypatch — encrypt
        # here so decrypt() in provision_member round-trips cleanly.
        paperclip_password_encrypted="",
        service_token_encrypted="",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    # Fill encrypted fields using the *real* encrypt now that ENCRYPTION_KEY is set.
    from core.encryption import encrypt

    owner_row.paperclip_password_encrypted = encrypt("owner-password-plain")
    owner_row.service_token_encrypted = encrypt("svc-token")

    repo = MagicMock()

    # First .get(user_member) -> None (joining member is new); subsequent .get(user_owner)
    # returns the seeded owner row inside find_org_owner -> repo.get(...).
    async def _get(uid: str):
        if uid == "user_owner":
            return owner_row
        return None

    repo.get = AsyncMock(side_effect=_get)
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    repo.put = AsyncMock()

    # find_org_owner queries the by-org-id GSI directly via repo._table().query.
    # Stub the whole helper instead — simpler than mocking boto3.
    import core.services.paperclip_provisioning as prov_mod

    async def _fake_find_owner(self, _org_id):
        return owner_row

    monkeypatch.setattr(prov_mod.PaperclipProvisioning, "find_org_owner", _fake_find_owner)

    mint_spy = MagicMock(return_value="svc.tok.minted")
    monkeypatch.setattr(service_token, "mint", mint_spy)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_member(
        owner_id="org_xyz",
        user_id="user_member",
        email="member@example.com",
        owner_email="owner@example.com",
    )

    mint_spy.assert_called_once_with("org_xyz")
    for call in mint_spy.call_args_list:
        assert call.args[0] != "user_member", (
            "service_token.mint was called with the joining member's user_id; "
            "org members share the org's container so the JWT sub MUST be the owner_id."
        )


@pytest.mark.asyncio
async def test_provision_org_persists_seeded_agent_ids(monkeypatch):
    """The seeded Friday/Atlas Paperclip IDs are captured onto the
    provisioner's row as the stable seeded marker (see
    paperclip_openclaw_pairing.get_seeded_agent_ids)."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    # create_agent returns the created agent dict (Friday then Atlas).
    admin.create_agent = AsyncMock(side_effect=[{"id": "friday_id"}, {"id": "atlas_id"}])

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # Two puts: the early adoptable credential row, then the final active row.
    assert repo.put.await_count == 2
    persisted = repo.put.await_args.args[0]
    assert persisted.status == "active"
    assert persisted.seeded_agent_ids == ["friday_id", "atlas_id"]


@pytest.mark.asyncio
async def test_provision_org_seeded_ids_none_when_seeds_fail(monkeypatch):
    """If both seeds fail, no ids are captured and the field stays None
    (best-effort — the company is still persisted and usable)."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(
        side_effect=[
            PaperclipApiError("friday", status_code=500, body="x"),
            PaperclipApiError("atlas", status_code=500, body="x"),
        ]
    )

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    persisted = repo.put.await_args.args[0]
    assert persisted.seeded_agent_ids is None


@pytest.mark.asyncio
async def test_provision_member_replicates_seeded_agent_ids(monkeypatch):
    """Member rows copy the owner row's seeded marker so Friday/Atlas stay
    protected even if the original provisioner's row is later purged."""
    from datetime import datetime, timezone

    from core.encryption import encrypt
    from core.repositories.paperclip_repo import PaperclipCompany
    from core.services import service_token
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)

    now = datetime.now(timezone.utc)
    owner_row = PaperclipCompany(
        user_id="user_owner",
        org_id="org_xyz",
        company_id="co_xyz",
        paperclip_user_id="pc_user_owner",
        paperclip_password_encrypted=encrypt("owner-password-plain"),
        service_token_encrypted=encrypt("svc-token"),
        status="active",
        created_at=now,
        updated_at=now,
        seeded_agent_ids=["friday_id", "atlas_id"],
    )

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    repo.put = AsyncMock()

    import core.services.paperclip_provisioning as prov_mod

    async def _fake_find_owner(self, _org_id):
        return owner_row

    monkeypatch.setattr(prov_mod.PaperclipProvisioning, "find_org_owner", _fake_find_owner)
    monkeypatch.setattr(service_token, "mint", MagicMock(return_value="svc.tok.minted"))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_member(
        owner_id="org_xyz",
        user_id="user_member",
        email="member@example.com",
        owner_email="owner@example.com",
    )

    persisted = repo.put.await_args.args[0]
    assert persisted.seeded_agent_ids == ["friday_id", "atlas_id"]


def _active_row(*, user_id, org_id):
    from datetime import datetime, timezone

    from core.repositories.paperclip_repo import PaperclipCompany

    now = datetime.now(timezone.utc)
    return PaperclipCompany(
        user_id=user_id,
        org_id=org_id,
        company_id="co_existing",
        paperclip_user_id="pc_existing",
        paperclip_password_encrypted="",
        service_token_encrypted="",
        status="active",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_provision_member_short_circuits_only_when_org_matches(monkeypatch):
    """An active row for THIS org short-circuits as idempotent success."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_active_row(user_id="user_m", org_id="org_xyz"))
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    repo.put = AsyncMock()

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    result = await svc.provision_member(
        owner_id="org_xyz", user_id="user_m", email="m@example.com", owner_email="o@example.com"
    )

    assert result.org_id == "org_xyz"
    repo.get_org_company_id.assert_not_awaited()  # short-circuited before the join flow
    repo.put.assert_not_awaited()
    admin.sign_up_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_member_does_not_short_circuit_on_other_org_row(monkeypatch):
    """An active row for a DIFFERENT org (e.g. a personal company) must NOT
    be treated as idempotent success — provision_member proceeds past the
    short-circuit (here it reaches get_org_company_id, stubbed empty, and
    raises rather than returning the unrelated row)."""
    from core.services.paperclip_provisioning import OrgNotProvisionedError, PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)
    repo = MagicMock()
    # Active row, but for the user's PERSONAL context (org_id == user_id).
    repo.get = AsyncMock(return_value=_active_row(user_id="user_m", org_id="user_m"))
    repo.get_org_company_id = AsyncMock(return_value=None)
    repo.put = AsyncMock()

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(OrgNotProvisionedError):
        await svc.provision_member(
            owner_id="org_xyz", user_id="user_m", email="m@example.com", owner_email="o@example.com"
        )
    repo.get_org_company_id.assert_awaited_once_with("org_xyz")  # proved it went past the short-circuit


@pytest.mark.asyncio
async def test_provision_member_adopts_existing_account_instead_of_resignup(monkeypatch):
    """A user who already has a Paperclip account (e.g. a personal row) must
    be ADOPTED via sign-in — NOT re-signed-up (which would 409 and corrupt
    their row). The member row is then re-pointed at this org."""
    from core.encryption import encrypt
    from core.services import service_token
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)
    # Distinct return shapes so we can tell adopt-signin from owner-signin.
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_m"}, "_session_cookie": "member-session"})

    existing_personal = _active_row(user_id="user_m", org_id="user_m")
    existing_personal.paperclip_user_id = "pc_user_m"
    existing_personal.paperclip_password_encrypted = encrypt("member-pw-plain")

    owner_row = _active_row(user_id="user_owner", org_id="org_xyz")
    owner_row.paperclip_password_encrypted = encrypt("owner-pw-plain")

    repo = MagicMock()
    repo.get = AsyncMock(return_value=existing_personal)
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    repo.put = AsyncMock()

    import core.services.paperclip_provisioning as prov_mod

    async def _fake_find_owner(self, _org_id):
        return owner_row

    monkeypatch.setattr(prov_mod.PaperclipProvisioning, "find_org_owner", _fake_find_owner)
    monkeypatch.setattr(service_token, "mint", MagicMock(return_value="svc.tok"))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com", owner_email="o@example.com")

    admin.sign_up_user.assert_not_awaited()  # adopted, not re-signed-up
    # Signed in as the member with their stored password.
    member_signins = [c for c in admin.sign_in_user.await_args_list if c.kwargs.get("email") == "m@example.com"]
    assert member_signins, "expected a member sign-in (adoption)"
    assert member_signins[0].kwargs["password"] == "member-pw-plain"
    # Row re-pointed at the org.
    put_row = repo.put.await_args.args[0]
    assert put_row.org_id == "org_xyz"
    assert put_row.paperclip_user_id == "pc_user_m"


@pytest.mark.asyncio
async def test_provision_member_adoption_failure_does_not_mark_existing_row_failed(monkeypatch):
    """A transient join failure while ADOPTING an existing row must NOT
    mark that (e.g. personal) row failed — the re-point only lands on the
    successful put, so the existing workspace stays intact and the next poll
    retries."""
    from core.encryption import encrypt
    from core.services.paperclip_admin_client import PaperclipApiError
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_m"}, "_session_cookie": "s"})
    # Fail mid-join (after adoption sign-in), before the final put.
    admin.create_invite = AsyncMock(side_effect=PaperclipApiError("invite boom", status_code=500, body="x"))

    existing_personal = _active_row(user_id="user_m", org_id="user_m")
    existing_personal.paperclip_user_id = "pc_user_m"
    existing_personal.paperclip_password_encrypted = encrypt("member-pw-plain")
    owner_row = _active_row(user_id="user_owner", org_id="org_xyz")
    owner_row.paperclip_password_encrypted = encrypt("owner-pw-plain")

    repo = MagicMock()
    repo.get = AsyncMock(return_value=existing_personal)
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    repo.put = AsyncMock()

    import core.services.paperclip_provisioning as prov_mod

    async def _fake_find_owner(self, _org_id):
        return owner_row

    monkeypatch.setattr(prov_mod.PaperclipProvisioning, "find_org_owner", _fake_find_owner)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    svc._mark_failed = AsyncMock()  # spy

    with pytest.raises(PaperclipApiError):
        await svc.provision_member(
            owner_id="org_xyz", user_id="user_m", email="m@example.com", owner_email="o@example.com"
        )

    svc._mark_failed.assert_not_awaited()  # adopted row preserved, not corrupted
    repo.put.assert_not_awaited()  # never re-pointed (join failed)


@pytest.mark.asyncio
async def test_provision_org_skips_approve_when_accept_auto_approved(monkeypatch):
    """Paperclip auto-approves human invites at accept time, so the org
    provision must NOT call the standalone approve endpoint (it 409s
    "Join request is not pending" on an already-approved request). This
    is the exact failure that broke every fresh signup before the chain
    was unified into _accept_invite_and_ensure_approved — the guard had
    landed only in the member copy of the duplicated chain.
    """
    from core.repositories.paperclip_repo import PaperclipCompany
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.accept_invite = AsyncMock(return_value={"id": "req_1", "status": "approved"})

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # The redundant approve call is skipped...
    admin.approve_join_request.assert_not_awaited()
    # ...and provisioning completes to an active row (Friday/Atlas seeded).
    assert isinstance(row, PaperclipCompany)
    assert row.status == "active"
    assert admin.create_agent.await_count == 2


@pytest.mark.asyncio
async def test_provision_org_approves_when_accept_left_pending(monkeypatch):
    """Defensive fallback: if Paperclip ever returns the join request still
    pending from accept, the admin must still approve it."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.accept_invite = AsyncMock(return_value={"id": "req_1", "status": "pending_approval"})

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    admin.approve_join_request.assert_awaited_once()
    approve_kwargs = admin.approve_join_request.await_args.kwargs
    assert approve_kwargs["company_id"] == "co_xyz"
    assert approve_kwargs["request_id"] == "req_1"


@pytest.mark.asyncio
async def test_provision_org_partial_failure_after_signup_leaves_adoptable_row(monkeypatch):
    """A brand-new owner whose provision fails AFTER sign-up must leave a
    stored row carrying the freshly created paperclip_user_id + password,
    so the next attempt ADOPTS via sign-in instead of re-signing-up (which
    would 409 against the now-existing Better Auth user and wedge every
    retry — the live 2026-06-11 failure mode: the failed row had an empty
    paperclip_user_id because provision_org persisted nothing until final
    success)."""
    from core.encryption import decrypt
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, _ = _build_provision_org_doubles(monkeypatch)
    # Fail AFTER sign-up, before completion.
    admin.create_company = AsyncMock(side_effect=PaperclipApiError("company boom", status_code=500, body="x"))

    repo = _StatefulRepo(company_id="co_xyz")

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError):
        await svc.provision_org(
            owner_id="org_xyz",
            owner_user_id="user_owner",
            owner_email="owner@example.com",
        )

    stored = await repo.get("user_owner")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.paperclip_user_id == "pc_user_1"
    assert stored.paperclip_password_encrypted, "password must be persisted for adoption"
    assert decrypt(stored.paperclip_password_encrypted)  # decrypts cleanly


@pytest.mark.asyncio
async def test_provision_member_skips_approve_when_accept_auto_approved(monkeypatch):
    """Paperclip auto-approves human invites at accept time — the accept
    response comes back ``status="approved"`` and the membership is already
    created. Calling the standalone approve endpoint on an already-approved
    request returns 409 "Join request is not pending". The member-join must
    NOT make that redundant call; it should read the accept status and skip
    approve, completing to an active row.
    """
    from core.encryption import encrypt
    from core.repositories.paperclip_repo import PaperclipCompany
    from core.services import service_token
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)
    # Accept returns the auto-approved join request.
    admin.accept_invite = AsyncMock(return_value={"id": "req_1", "status": "approved"})

    owner_row = _active_row(user_id="user_owner", org_id="org_xyz")
    owner_row.paperclip_password_encrypted = encrypt("owner-pw-plain")

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)  # brand-new member
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    repo.put = AsyncMock()

    import core.services.paperclip_provisioning as prov_mod

    async def _fake_find_owner(self, _org_id):
        return owner_row

    monkeypatch.setattr(prov_mod.PaperclipProvisioning, "find_org_owner", _fake_find_owner)
    monkeypatch.setattr(service_token, "mint", MagicMock(return_value="svc.tok"))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_member(
        owner_id="org_xyz", user_id="user_m", email="m@example.com", owner_email="o@example.com"
    )

    # The redundant approve call is skipped...
    admin.approve_join_request.assert_not_awaited()
    # ...and provisioning still completes to an active member row.
    assert isinstance(row, PaperclipCompany)
    assert row.status == "active"
    assert row.org_id == "org_xyz"
    repo.put.assert_awaited()


@pytest.mark.asyncio
async def test_provision_member_approves_when_accept_left_pending(monkeypatch):
    """Defensive fallback: if Paperclip ever returns the join request still
    ``pending_approval`` from accept (older behavior, or a non-auto-approving
    flow), the owner must still approve it so the membership goes active.
    """
    from core.encryption import encrypt
    from core.services import service_token
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)
    # Accept leaves the request pending — approve is required.
    admin.accept_invite = AsyncMock(return_value={"id": "req_1", "status": "pending_approval"})

    owner_row = _active_row(user_id="user_owner", org_id="org_xyz")
    owner_row.paperclip_password_encrypted = encrypt("owner-pw-plain")

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    repo.put = AsyncMock()

    import core.services.paperclip_provisioning as prov_mod

    async def _fake_find_owner(self, _org_id):
        return owner_row

    monkeypatch.setattr(prov_mod.PaperclipProvisioning, "find_org_owner", _fake_find_owner)
    monkeypatch.setattr(service_token, "mint", MagicMock(return_value="svc.tok"))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_member(owner_id="org_xyz", user_id="user_m", email="m@example.com", owner_email="o@example.com")

    admin.approve_join_request.assert_awaited_once()


class _StatefulRepo:
    """Minimal in-memory stand-in for paperclip_repo, keyed by user_id.

    Models the bits provision_member + _mark_failed touch: ``get`` returns
    the stored row, ``put`` stores it, ``update_status`` mutates status +
    last_error in place (preserving immutables like paperclip_user_id), and
    ``get_org_company_id`` is a fixed stub. Real enough to verify that a
    post-sign-up failure leaves an ADOPTABLE row.
    """

    def __init__(self, *, company_id: str):
        self._rows: dict[str, object] = {}
        self._company_id = company_id

    async def get(self, user_id: str):
        return self._rows.get(user_id)

    async def put(self, row):
        self._rows[row.user_id] = row

    async def update_status(self, user_id: str, *, status: str, last_error: str = "", not_if_status: str | None = None):
        row = self._rows[user_id]
        if not_if_status is not None and row.status == not_if_status:
            return False  # guarded — skip (e.g. don't downgrade an active row)
        row.status = status
        row.last_error = last_error
        return True

    async def get_org_company_id(self, _owner_id: str):
        return self._company_id


@pytest.mark.asyncio
async def test_mark_failed_does_not_downgrade_active_row(monkeypatch):
    """A concurrent provision that already completed the row to ``active`` must
    not be clobbered by a losing attempt's failure path: _mark_failed uses a
    guarded update that skips when the row is already active."""
    from datetime import datetime, timezone

    from core.repositories.paperclip_repo import PaperclipCompany
    from core.services.paperclip_provisioning import PaperclipProvisioning

    repo = _StatefulRepo(company_id="co_xyz")
    now = datetime.now(timezone.utc)
    # The winning concurrent attempt already wrote an active row.
    await repo.put(
        PaperclipCompany(
            user_id="user_m",
            org_id="org_xyz",
            company_id="co_xyz",
            paperclip_user_id="pc_user_m",
            paperclip_password_encrypted="enc",
            service_token_encrypted="enc_tok",
            status="active",
            created_at=now,
            updated_at=now,
        )
    )

    svc = PaperclipProvisioning(admin_client=MagicMock(), repo=repo, env_name="dev")
    await svc._mark_failed(user_id="user_m", owner_id="org_xyz", reason="late transient error")

    # The active row survives — NOT downgraded to failed.
    row = await repo.get("user_m")
    assert row.status == "active"
    assert row.last_error is None


@pytest.mark.asyncio
async def test_partial_failure_after_signup_leaves_adoptable_row(monkeypatch):
    """A brand-new member whose join fails AFTER sign-up (before completion)
    must leave a stored row carrying the freshly created paperclip_user_id +
    password, so the next attempt ADOPTS via sign-in instead of re-signing-up
    (which would 422 against the now-existing Better Auth user and orphan it).
    """
    from core.encryption import decrypt, encrypt
    from core.services.paperclip_admin_client import PaperclipApiError
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)
    admin.sign_up_user = AsyncMock(return_value={"user": {"id": "pc_user_member"}, "_session_cookie": "member-session"})
    # Fail mid-join AFTER the member sign-up, before the final put.
    admin.create_invite = AsyncMock(side_effect=PaperclipApiError("invite boom", status_code=500, body="x"))

    owner_row = _active_row(user_id="user_owner", org_id="org_xyz")
    owner_row.paperclip_password_encrypted = encrypt("owner-pw-plain")

    repo = _StatefulRepo(company_id="co_xyz")

    import core.services.paperclip_provisioning as prov_mod

    async def _fake_find_owner(self, _org_id):
        return owner_row

    monkeypatch.setattr(prov_mod.PaperclipProvisioning, "find_org_owner", _fake_find_owner)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    with pytest.raises(PaperclipApiError):
        await svc.provision_member(
            owner_id="org_xyz", user_id="user_m", email="m@example.com", owner_email="o@example.com"
        )

    # The stored row must be adoptable: real paperclip_user_id + a password
    # that round-trips through decrypt — NOT the empty placeholder that
    # orphaned the Better Auth user.
    stored = await repo.get("user_m")
    assert stored is not None
    assert stored.paperclip_user_id == "pc_user_member"
    assert stored.paperclip_password_encrypted, "password must be persisted for adoption"
    assert decrypt(stored.paperclip_password_encrypted)  # decrypts cleanly
