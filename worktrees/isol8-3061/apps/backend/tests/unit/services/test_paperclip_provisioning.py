"""Tests for org/member provisioning behaviors that don't fit the
heavier moto-backed suite in
``tests/unit/services/test_paperclip_provisioning_moto.py``.

Currently exercises:
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
    # Default the seed calls to a successful dict-with-id (mirrors the moto
    # suite's _make_admin_mock). Friday landing is what gates status="active",
    # so tests that don't care about the seed still complete to an active row;
    # tests exercising a seed FAILURE override create_agent with a side_effect.
    admin.create_agent = AsyncMock(return_value={"id": "seed-agent-default"})
    # Builtin-agent purge (step 7c) lists agents then attempts to delete every
    # non-seeded one. Default to an empty roster so tests that don't exercise the
    # purge see zero deletes; purge tests override list_agents. Paperclip's own
    # built-in agents are undeletable (409 built_in_agent_undeletable) — the
    # undeletable-vs-stray behavior is exercised in the dedicated purge tests.
    admin.list_agents = AsyncMock(return_value=[])
    admin.delete_agent = AsyncMock(return_value={})

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.put = AsyncMock()
    # Fresh-create fence (see PaperclipRepo.try_acquire_seed_claim): the
    # doubles play the WINNER by default so the chain proceeds; fence-losing
    # behavior is exercised end-to-end in the moto suite.
    repo.try_acquire_seed_claim = AsyncMock(return_value=True)
    repo.stage_signup_credentials = AsyncMock(side_effect=lambda uid, **kw: (kw["email"], kw["password_encrypted"]))

    # Stub admin-session helper imported lazily inside provision_org.
    import core.services.paperclip_provisioning as mod
    import core.services.paperclip_admin_session as admin_session_mod

    async def _fake_admin_cookie(_http):
        return "admin_cookie"

    monkeypatch.setattr(admin_session_mod, "get_admin_session_cookie", _fake_admin_cookie)
    monkeypatch.setattr(admin_session_mod, "invalidate_admin_session", lambda: None)
    if hasattr(mod, "get_admin_session_cookie"):
        monkeypatch.setattr(mod, "get_admin_session_cookie", _fake_admin_cookie, raising=False)

    # provision_org's starter-seed loop reads the owner's container row for its ``hook_token``
    # -- the native adapter's ``apiKey``, which build_hermes_gateway_adapter_config refuses to
    # mint without (M4-R33). ``prepare_hermes_workspace`` writes it before step 0 seeds, so a
    # row carrying one is what the subject sees in production; a real DynamoDB call is not the
    # point of these tests.
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook-seed-test"}))

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
async def test_provision_org_seeds_every_starter_on_the_native_hermes_adapter(monkeypatch):
    """There is one adapter, and its ``apiKey`` is the OWNER's hook token off the container row.

    M4-R33 is what this pins: ``prepare_hermes_workspace`` mints ``hook_token`` onto the row
    BEFORE step 0 seeds the roster, so this read returns it. Without that ordering the row read
    returns ``{}`` and every agent bakes an EMPTY bearer — which Paperclip then reports as
    ``hermes_gateway_auth_failed`` on each individual run rather than as a broken agent.

    The ``runtime_host`` hint this test pair used to exercise is gone: it existed only to pick
    between the native adapter and the device-paired ECS default before the container row said
    which host the owner was on.
    """
    from core.repositories import container_repo
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    row_spy = AsyncMock(return_value={"hook_token": "hook-1"})
    monkeypatch.setattr(container_repo, "get_by_owner_id", row_spy)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_hermes",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    adapter_types = {call.kwargs["adapter_type"] for call in admin.create_agent.call_args_list}
    assert adapter_types == {"hermes_gateway"}
    api_keys = {call.kwargs["adapter_config"]["apiKey"] for call in admin.create_agent.call_args_list}
    assert api_keys == {"hook-1"}
    # ONE read for the whole roster, not one per agent.
    row_spy.assert_awaited_once_with("org_hermes")


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
    repo.try_acquire_seed_claim = AsyncMock(return_value=True)
    repo.stage_signup_credentials = AsyncMock(side_effect=lambda uid, **kw: (kw["email"], kw["password_encrypted"]))
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
async def test_provision_org_seeded_ids_none_when_seeds_fail(monkeypatch):
    """A missing starter CEO leaves no legacy seed-protection marker."""
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
    assert persisted.starter_agent_ids == {}
    assert persisted.status != "active"


@pytest.mark.asyncio
async def test_provision_org_purge_leaves_undeletable_builtins_removes_strays(monkeypatch):
    """The purge must survive Paperclip's UNDELETABLE built-in agents.

    Paperclip's company-bootstrap seeds its OWN built-in managed agents
    ("Reflection Coach", "Summarizer"). In the deployed BFF these carry a
    built-in marker in their metadata, and ``DELETE /api/agents/{id}`` refuses
    them with 409 ``built_in_agent_undeletable`` ("Built-in agents cannot be
    deleted; pause them instead") BEFORE touching the DB — there is no override,
    so no backend call can remove them (they are created already-paused, so they
    never run). The purge attempts every non-seeded delete: it cleanly removes a
    genuine stray, and treats the built-in 409 as the EXPECTED, non-error outcome
    (logged, NOT metered as a purge error). Friday/Atlas (seeded) are never
    touched.
    """
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    starter_ids = ["ceo", "eng", "fin", "growth", "analytics", "support"]
    admin.create_agent = AsyncMock(side_effect=[{"id": agent_id} for agent_id in starter_ids])
    # Roster after seeding: Paperclip's two undeletable builtins, a genuine stray
    # ("x"), PLUS our seeds. list_agents returns a bare list (typed dict upstream).
    admin.list_agents = AsyncMock(
        return_value=[
            {"id": "f", "name": "Reflection Coach"},
            {"id": "s", "name": "Summarizer"},
            {"id": "x", "name": "Some Stray"},
            *({"id": agent_id, "name": agent_id} for agent_id in starter_ids),
        ]
    )

    # The two builtins answer the deployed BFF's built-in-undeletable 409; the
    # genuine stray "x" deletes cleanly. Body mirrors the BFF error-handler shape
    # ({"error": ..., "code": "built_in_agent_undeletable"}), surfaced as raw text.
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

    # Spy the error metric: the built-in 409 is EXPECTED and must NOT be metered.
    metric_calls: list[str] = []
    import core.services.paperclip_provisioning as mod

    monkeypatch.setattr(mod, "put_metric", lambda name, *a, **k: metric_calls.append(name))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # The roster was listed for the freshly created company.
    admin.list_agents.assert_awaited_once()
    assert admin.list_agents.await_args.kwargs["company_id"] == "co_xyz"

    # Delete was attempted for every non-starter agent.
    attempted = {c.kwargs["agent_id"] for c in admin.delete_agent.await_args_list}
    assert attempted == {"f", "s", "x"}
    assert not attempted.intersection(starter_ids)

    # The undeletable-builtin 409 is EXPECTED — it must NOT raise the purge-error
    # metric (a genuine failure still would; see the 5xx test below).
    assert "paperclip.provision.builtin_purge_error" not in metric_calls

    # Provision still lands active: the purge is best-effort.
    assert row.status == "active"


@pytest.mark.asyncio
async def test_provision_org_builtin_purge_failure_never_fails_provision(monkeypatch):
    """A delete failure during the builtin-agent purge must NOT fail the
    provision — the purge is cosmetic cleanup, best-effort per agent."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

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
    # Every delete blows up (5xx) — the purge must swallow it and still complete.
    admin.delete_agent = AsyncMock(side_effect=PaperclipApiError("delete boom", status_code=500, body="x"))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # Deletion was attempted for both built-ins, but the failure never propagated.
    assert admin.delete_agent.await_count == 2
    assert row.status == "active"
    assert repo.put.await_args.args[0].status == "active"


@pytest.mark.asyncio
async def test_provision_org_failed_friday_skips_builtin_purge(monkeypatch):
    """The purge is gated on the REQUIRED Friday seed landing. When Friday's
    seed fails the company is left untouched — a later reconcile poll (which
    re-seeds Friday) runs the purge instead. Purging a half-provisioned company
    would race the reconcile that still needs to (re)seed into it."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    # Friday raises; Atlas would land — but a CEO-less company is not purged.
    admin.create_agent = AsyncMock(
        side_effect=[
            PaperclipApiError("friday seed failed", status_code=500, body="boom"),
            {"id": "at"},
        ]
    )
    admin.list_agents = AsyncMock(return_value=[{"id": "f", "name": "Reflection Coach"}])
    admin.delete_agent = AsyncMock(return_value={})

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # No CEO landed -> non-active row, and the purge was skipped entirely.
    assert row.status != "active"
    admin.list_agents.assert_not_awaited()
    admin.delete_agent.assert_not_awaited()


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
    repo.try_acquire_seed_claim = AsyncMock(return_value=True)
    repo.stage_signup_credentials = AsyncMock(side_effect=lambda uid, **kw: (kw["email"], kw["password_encrypted"]))
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
    be ADOPTED via sign-in — NOT re-signed-up (which would 422 and corrupt
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
    # ...and provisioning completes to an active row (starter roster seeded).
    assert isinstance(row, PaperclipCompany)
    assert row.status == "active"
    assert admin.create_agent.await_count == 6


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
    would 422 against the now-existing Better Auth user and wedge every
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
    repo.try_acquire_seed_claim = AsyncMock(return_value=True)
    repo.stage_signup_credentials = AsyncMock(side_effect=lambda uid, **kw: (kw["email"], kw["password_encrypted"]))
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
    repo.try_acquire_seed_claim = AsyncMock(return_value=True)
    repo.stage_signup_credentials = AsyncMock(side_effect=lambda uid, **kw: (kw["email"], kw["password_encrypted"]))
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

    async def get(self, user_id: str, *, consistent_read: bool = False):
        return self._rows.get(user_id)

    async def put(self, row, *, condition: str | None = None, condition_values: dict | None = None):
        # Fix round 1 (Important #2): mirrors the real repo's
        # (attribute_not_exists(onboarding_generation) OR onboarding_generation = :g) condition
        # closely enough for this double's callers -- provision_org's G2 mint site is the only
        # caller that ever passes one.
        if condition is not None:
            existing = self._rows.get(row.user_id)
            current = getattr(existing, "onboarding_generation", None) if existing else None
            expected = (condition_values or {}).get(":g")
            if current is not None and current != expected:
                from botocore.exceptions import ClientError

                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conditional check failed"}},
                    "PutItem",
                )
        self._rows[row.user_id] = row

    async def try_acquire_seed_claim(
        self, user_id: str, *, org_id: str, claim_id: str, stale_after_seconds: int = 300, company_id: str = ""
    ):
        # Fresh put-if-absent only — enough for the winner-path scenarios this
        # double serves; loser/steal behavior is pinned by the moto suites.
        # Mirrors the real claim's bare seeding row (no credentials), which the
        # winner's later puts replace wholesale.
        if user_id in self._rows:
            return False
        from datetime import datetime, timezone

        from core.repositories.paperclip_repo import PaperclipCompany

        now = datetime.now(timezone.utc)
        row = PaperclipCompany(
            user_id=user_id,
            org_id=org_id,
            company_id=company_id,
            paperclip_user_id="",
            paperclip_password_encrypted="",
            service_token_encrypted="",
            status="seeding",
            created_at=now,
            updated_at=now,
        )
        # Bare claim carries the fencing token, like the real seeding row.
        row.seeding_claim_id = claim_id
        self._rows[user_id] = row
        return True

    async def stage_signup_credentials(self, user_id, *, email, password_encrypted):
        row = self._rows[user_id]
        row.paperclip_email = row.paperclip_email or email
        row.paperclip_password_encrypted = row.paperclip_password_encrypted or password_encrypted
        return row.paperclip_email, row.paperclip_password_encrypted

    async def update_status(
        self,
        user_id: str,
        *,
        status: str,
        last_error: str = "",
        not_if_status: str | None = None,
        require_seeding_claim_id: str | None = None,
        owner_id: str | None = None,
        company_id: str | None = None,
    ):
        row = self._rows[user_id]
        if not_if_status is not None and row.status == not_if_status:
            return False  # guarded — skip (e.g. don't downgrade an active row)
        if require_seeding_claim_id is not None:
            existing_cid = getattr(row, "seeding_claim_id", None)
            # Skip iff the row still carries a DIFFERENT bare-claim token.
            if existing_cid is not None and existing_cid != require_seeding_claim_id:
                return False
        row.status = status
        row.last_error = last_error
        if company_id:
            row.company_id = company_id
        return True

    async def get_org_company_id(self, _owner_id: str):
        return self._company_id

    async def put_failed_marker_if_absent(self, *, user_id: str, org_id: str, reason: str, company_id: str = ""):
        # Conditional put-if-absent: only creates when the key is free (never
        # clobbers a real row). Marker carries NO credential fields.
        if user_id in self._rows:
            return False
        from datetime import datetime, timezone

        from core.repositories.paperclip_repo import PaperclipCompany

        now = datetime.now(timezone.utc)
        self._rows[user_id] = PaperclipCompany(
            user_id=user_id,
            org_id=org_id,
            company_id=company_id,
            paperclip_user_id="",  # absent at the DDB level; get() defaults to ""
            paperclip_password_encrypted="",
            service_token_encrypted="",
            status="failed",
            created_at=now,
            updated_at=now,
            last_error=reason,
        )
        return True


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


@pytest.mark.asyncio
async def test_provision_org_names_company_from_generated_name(monkeypatch):
    """The company gets a real name, not the owner's email address."""
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(return_value={"id": "starter-agent"})

    async def fake_generate(*, org_id, owner_email):
        return "Acme Robotics"

    monkeypatch.setattr(mod, "generate_company_name", fake_generate)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert admin.create_company.await_args.kwargs["name"] == "Acme Robotics"


@pytest.mark.asyncio
async def test_provision_org_survives_name_generation_failure(monkeypatch):
    """A naming failure must not fail the provision; fall back to the email."""
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(return_value={"id": "starter-agent"})

    async def boom(**kwargs):
        raise RuntimeError("clerk down")

    monkeypatch.setattr(mod, "generate_company_name", boom)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert row.status == "active"
    # Fell back to the previous behaviour rather than aborting.
    assert admin.create_company.await_args.kwargs["name"] == "owner@example.com"


@pytest.mark.asyncio
async def test_mark_failed_no_existing_row_writes_conditional_marker(monkeypatch):
    """With NO prior row, _mark_failed writes ONE durable ``failed`` marker via
    the repo's CONDITIONAL put-if-absent — NOT a full row carrying a fabricated
    ``paperclip_user_id`` (the old poison that blocked adoption and clobbered a
    real id on a stale read), and NOT an unconditional put. The marker restores
    the honest 503 on /teams (a MISSING row is an eternal 202 spinner) without
    the clobber risk, because put-if-absent can only ever create."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)  # no prior row
    repo.put = AsyncMock()
    repo.update_status = AsyncMock(return_value=True)
    repo.put_failed_marker_if_absent = AsyncMock(return_value=True)

    svc = PaperclipProvisioning(admin_client=MagicMock(), repo=repo, env_name="dev")
    await svc._mark_failed(user_id="user_orphan", owner_id="org_x", reason="signup 422 duplicate", company_id="co_x")

    # The durable marker went through the conditional put-if-absent, keyed on the
    # polling owner's own user_id, carrying org_id/company_id/reason.
    repo.put_failed_marker_if_absent.assert_awaited_once()
    kwargs = repo.put_failed_marker_if_absent.await_args.kwargs
    assert kwargs["user_id"] == "user_orphan"
    assert kwargs["org_id"] == "org_x"
    assert kwargs["company_id"] == "co_x"
    assert kwargs["reason"], "the failure cause must be recorded on the marker"
    # No fabricated id: the marker method takes no paperclip_user_id at all, and
    # no full-row put() is made (that path is what carried the empty-id poison).
    assert "paperclip_user_id" not in kwargs
    repo.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_org_required_ceo_seed_failure_not_marked_active(monkeypatch):
    """NEW-2: when the required CEO seed (Friday) does not land, provision_org
    must NOT persist ``status="active"``. An active company with no CEO is
    frozen by the ``status=="active"`` idempotency short-circuit and can never
    re-seed; it must instead record a non-active status a later poll can
    reconcile (keeping the adoptable credential row so the retry signs IN rather
    than re-signing-up)."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    # Friday's seed raises; Atlas would land — but a company with no CEO must
    # not be marked active on the strength of Atlas alone.
    admin.create_agent = AsyncMock(
        side_effect=[
            PaperclipApiError("friday seed failed", status_code=500, body="boom"),
            {"id": "agent-atlas"},
        ]
    )

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # Neither the returned row nor any persisted row may claim status="active".
    assert row.status != "active"
    for call in repo.put.await_args_list:
        assert call.args[0].status != "active", "must not mark a CEO-less company active"


@pytest.mark.asyncio
async def test_provision_org_does_NOT_allocate_a_slug_or_mint_an_address(monkeypatch):
    """Minting moved to the first owner-directed send.

    The slug becomes the subdomain of an address other people are given, and
    company_slug_repo has no delete path -- so reserving it during provisioning
    would permanently freeze the auto-generated placeholder name (for a personal
    owner, their own email local part). It must derive from the name the agent
    chooses during its first run instead.
    """
    import core.services.agent_email as agent_email_mod
    import core.services.company_identity as company_identity_mod
    import core.services.paperclip_provisioning as mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.create_agent = AsyncMock(return_value={"id": "starter-agent"})

    calls = []

    async def spy_allocate(*, owner_id, name):
        calls.append("allocate")
        return "acme-robotics"

    async def spy_mint(**kwargs):
        calls.append("mint")
        return "friday@acme-robotics.isol8.co"

    # Patch at the SOURCE modules: provision_org no longer imports either name,
    # so patching `mod` would silently assert nothing.
    monkeypatch.setattr(company_identity_mod, "allocate_company_slug", spy_allocate)
    monkeypatch.setattr(agent_email_mod, "mint_agent_email_address", spy_mint)
    assert not hasattr(mod, "allocate_company_slug")
    assert not hasattr(mod, "mint_agent_email_address")

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    row = await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert row.status == "active"
    assert calls == []


# ---------------------------------------------------------------------------
# provision_member fresh-join fence (the try_acquire_seed_claim claim). Four
# concurrent dispatches for one joining member (status poll + teams reconcile
# on the first page load after an invite accept) used to each sign up the same
# email with a different random password; the last DDB writer's password then
# never matched the Better Auth account sign-in resolves by email.
# ---------------------------------------------------------------------------


def _member_fence_doubles(monkeypatch, *, claimed: bool, winner_row=None):
    """provision_member doubles with the claim outcome pinned.

    ``repo.get`` returns None for the joining member on the first (existence)
    read and ``winner_row`` on every later strongly-consistent read — the
    loser-side poll — so a lost claim can adopt a peer's active row.
    """
    from datetime import datetime, timezone

    from core.encryption import encrypt
    from core.repositories.paperclip_repo import PaperclipCompany
    import core.services.paperclip_provisioning as prov_mod

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
    )

    async def _fake_find_owner(self, _org_id):
        return owner_row

    monkeypatch.setattr(prov_mod.PaperclipProvisioning, "find_org_owner", _fake_find_owner)
    # Loser-side wait: don't actually sleep.
    monkeypatch.setattr(prov_mod, "_SEED_WAIT_INTERVAL_S", 0.0)
    monkeypatch.setattr(prov_mod, "_SEED_WAIT_ATTEMPTS", 2)

    repo = MagicMock()
    member_reads = {"n": 0}

    async def _get(uid: str, *, consistent_read: bool = False):
        if uid == "user_owner":
            return owner_row
        member_reads["n"] += 1
        return None if member_reads["n"] == 1 else winner_row

    repo.get = AsyncMock(side_effect=_get)
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    repo.put = AsyncMock()
    repo.try_acquire_seed_claim = AsyncMock(return_value=claimed)
    repo.stage_signup_credentials = AsyncMock(side_effect=lambda uid, **kw: (kw["email"], kw["password_encrypted"]))
    return admin, repo


async def test_provision_member_takes_seed_claim_before_signup(monkeypatch):
    from core.services import service_token
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _member_fence_doubles(monkeypatch, claimed=True)
    monkeypatch.setattr(service_token, "mint", MagicMock(return_value="svc.tok"))
    order = MagicMock()
    order.attach_mock(repo.try_acquire_seed_claim, "claim")
    order.attach_mock(admin.sign_up_user, "signup")

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_member(
        owner_id="org_xyz", user_id="user_member", email="member@example.com", owner_email="owner@example.com"
    )

    assert [c[0] for c in order.mock_calls][:2] == ["claim", "signup"], "claim must land before the sign-up"
    repo.try_acquire_seed_claim.assert_awaited_once()
    args, kwargs = repo.try_acquire_seed_claim.await_args
    assert args == ("user_member",) and kwargs["org_id"] == "org_xyz" and kwargs["claim_id"]


async def test_provision_member_lost_claim_adopts_winner_row_without_signup(monkeypatch):
    from datetime import datetime, timezone

    from core.repositories.paperclip_repo import PaperclipCompany
    from core.services.paperclip_provisioning import PaperclipProvisioning

    now = datetime.now(timezone.utc)
    winner = PaperclipCompany(
        user_id="user_member",
        org_id="org_xyz",
        company_id="co_xyz",
        paperclip_user_id="pc_winner",
        paperclip_password_encrypted="enc",
        service_token_encrypted="enc",
        status="active",
        created_at=now,
        updated_at=now,
    )
    admin, repo = _member_fence_doubles(monkeypatch, claimed=False, winner_row=winner)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    result = await svc.provision_member(
        owner_id="org_xyz", user_id="user_member", email="member@example.com", owner_email="owner@example.com"
    )

    assert result.paperclip_user_id == "pc_winner"
    # The loser must not mint a second Better Auth account or overwrite the
    # winner's credential — that overwrite is the INVALID_EMAIL_OR_PASSWORD bug.
    admin.sign_up_user.assert_not_awaited()
    admin.sign_in_user.assert_not_awaited()
    repo.put.assert_not_awaited()


async def test_provision_member_lost_claim_timeout_raises_409_without_signup(monkeypatch):
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _member_fence_doubles(monkeypatch, claimed=False, winner_row=None)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError) as ei:
        await svc.provision_member(
            owner_id="org_xyz", user_id="user_member", email="member@example.com", owner_email="owner@example.com"
        )

    # 409 is what _ensure_paperclip_for_member already treats as "join in
    # flight, no-op" — the next poll adopts the winner's row.
    assert ei.value.status_code == 409
    admin.sign_up_user.assert_not_awaited()
    repo.put.assert_not_awaited()


async def test_provision_member_signup_failure_right_after_claim_downgrades_claim(monkeypatch):
    """Sign-up fails milliseconds after the claim landed. _mark_failed must
    still SEE the bare seeding row (strongly consistent read) and downgrade it
    to failed — with an eventually-consistent miss, put_failed_marker_if_absent
    fails its condition and the row stays ``seeding`` until the 300 s stale
    window, wedging the member on 409s meanwhile."""
    from datetime import datetime, timezone

    from core.encryption import encrypt
    from core.repositories.paperclip_repo import PaperclipCompany
    import core.services.paperclip_provisioning as prov_mod
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)
    admin.sign_up_user = AsyncMock(side_effect=PaperclipApiError("dup email", status_code=422, body="unprocessable"))
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
    )

    async def _fake_find_owner(self, _org_id):
        return owner_row

    monkeypatch.setattr(prov_mod.PaperclipProvisioning, "find_org_owner", _fake_find_owner)

    class _LaggyRepo(_StatefulRepo):
        """Eventually-consistent reads never see the member's fresh row; only
        consistent reads do — replica lag right after the claim's put."""

        async def get(self, user_id: str, *, consistent_read: bool = False):
            if user_id == "user_member" and not consistent_read:
                return None
            return self._rows.get(user_id)

    repo = _LaggyRepo(company_id="co_xyz")
    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    with pytest.raises(PaperclipApiError):
        await svc.provision_member(
            owner_id="org_xyz", user_id="user_member", email="member@example.com", owner_email="owner@example.com"
        )

    row = await repo.get("user_member", consistent_read=True)
    assert row is not None and row.status == "failed", "claim must be downgraded, not left seeding"
    assert row.company_id == "co_xyz"


async def test_sign_up_with_staged_credentials_needs_no_claim(monkeypatch):
    """Staging signup credentials is unfenced (#15 — no more leases): it
    reaches signup without any seed claim."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")

    account, email, password = await svc._sign_up_with_staged_credentials(
        user_id="user_owner", email="owner@example.com", password="plain-pw"
    )

    assert account == {"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_new"}
    repo.stage_signup_credentials.assert_awaited_once()
