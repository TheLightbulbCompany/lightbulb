"""Tests for org/member provisioning behaviors that don't fit the
heavier moto-backed suite in ``tests/test_paperclip_provisioning.py``.

Currently exercises:
* The auto-seeded Main Agent name + role (``"Friday"`` / ``"ceo"``).
* The auto-seeded Atlas (recruiter) agent and its independence from
  the Friday seed (defense-in-depth).
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
    """provision_org seeds BOTH Friday (ceo) and Atlas (recruiter) in one step.

    Atlas is the in-house agent-recruiter that Friday-as-orchestrator
    delegates "hire a new agent" work to, so it must exist alongside
    Friday at day-one provisioning. Friday must keep role='ceo' so
    config.py:463's default-agent selection still lands on Friday.
    """
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)

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
    # Atlas must be 'recruiter' (NOT 'ceo') so it doesn't steal default.
    assert roles == {"ceo", "recruiter"}
    # Idempotency keys are distinct so retries don't collide.
    assert idempotency_keys == {
        "user_owner:main-agent",
        "user_owner:atlas-agent",
    }

    # Cross-check the pairing: Friday ↔ ceo ↔ :main-agent, Atlas ↔ recruiter ↔ :atlas-agent.
    by_name = {call.kwargs["name"]: call.kwargs for call in admin.create_agent.await_args_list}
    assert by_name["Friday"]["role"] == "ceo"
    assert by_name["Friday"]["idempotency_key"] == "user_owner:main-agent"
    assert by_name["Atlas"]["role"] == "recruiter"
    assert by_name["Atlas"]["idempotency_key"] == "user_owner:atlas-agent"


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
    # ... and the row was still persisted (provision_org completed).
    repo.put.assert_awaited_once()


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
