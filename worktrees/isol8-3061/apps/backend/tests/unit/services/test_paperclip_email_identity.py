"""The stored Better Auth address survives a Clerk primary-email change.

Better Auth's ``user.email`` is written once at provision time and NEVER
updated, so the live Clerk address is not the sign-in truth. Every per-user
sign-in door must prefer ``PaperclipCompany.paperclip_email`` and fall back to
the live address only when it is empty — the wrong way round 401s, falls
through to sign-UP, and mints a duplicate Better Auth account whose sign-in
rate limit is per-IP (i.e. fleet-wide degradation).

Covered here: the two ADOPT branches (owner + member) and the owner re-signin
inside ``provision_member``. The Teams-BFF door has its own file
(``test_paperclip_user_session.py``); the proxy doors read the same field on
the same row.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.repositories.paperclip_repo import PaperclipCompany
from tests.unit.services.test_paperclip_provisioning import (
    _build_provision_member_doubles,
    _build_provision_org_doubles,
)


def _row(**kwargs) -> PaperclipCompany:
    now = datetime.now(timezone.utc)
    base = dict(
        user_id="user_owner",
        org_id="org_xyz",
        company_id="",
        paperclip_user_id="pc_user_1",
        paperclip_password_encrypted="",
        service_token_encrypted="",
        status="failed",
        created_at=now,
        updated_at=now,
    )
    base.update(kwargs)
    return PaperclipCompany(**base)


@pytest.mark.asyncio
async def test_provision_org_adopt_signs_in_with_the_stored_email(monkeypatch):
    """Owner ADOPT branch: sign in with the address Better Auth holds, not the
    live Clerk one, and carry it onto the row it rewrites."""
    from core.encryption import encrypt
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_adopt"})

    existing = _row(paperclip_password_encrypted=encrypt("owner-pw"), paperclip_email="owner@isol8.co")
    repo.get = AsyncMock(return_value=existing)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        # The human has MOVED domains; Clerk now mints the new address.
        owner_email="owner@thelightbulb.company",
    )

    admin.sign_in_user.assert_awaited_once_with(email="owner@isol8.co", password="owner-pw")
    admin.sign_up_user.assert_not_awaited()
    persisted = repo.put.await_args.args[0]
    assert persisted.paperclip_email == "owner@isol8.co"


@pytest.mark.asyncio
async def test_provision_org_adopt_falls_back_to_clerk_when_unbackfilled(monkeypatch):
    """A row written before the field existed has nothing stored, so the live
    Clerk address is still the only thing to try — and the rewrite records it."""
    from core.encryption import encrypt
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_adopt"})

    repo.get = AsyncMock(return_value=_row(paperclip_password_encrypted=encrypt("owner-pw")))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    admin.sign_in_user.assert_awaited_once_with(email="owner@example.com", password="owner-pw")
    assert repo.put.await_args.args[0].paperclip_email == "owner@example.com"


@pytest.mark.asyncio
async def test_provision_org_fresh_records_the_signup_email(monkeypatch):
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="fresh@example.com",
    )

    # Both the pre-persist credential row and the final row carry it, so a
    # failure between them still leaves the next attempt able to sign in.
    for call in repo.put.await_args_list:
        assert call.args[0].paperclip_email == "fresh@example.com"


def _member_repo(monkeypatch, *, existing, owner_row):
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=lambda uid: owner_row if uid == "user_owner" else existing)
    repo.get_org_company_id = AsyncMock(return_value="co_xyz")
    repo.try_acquire_seed_claim = AsyncMock(return_value=True)
    repo.stage_signup_credentials = AsyncMock(side_effect=lambda uid, **kw: (kw["email"], kw["password_encrypted"]))
    repo.put = AsyncMock()

    import core.services.paperclip_provisioning as prov_mod

    async def _fake_find_owner(self, _org_id):
        return owner_row

    monkeypatch.setattr(prov_mod.PaperclipProvisioning, "find_org_owner", _fake_find_owner)
    return repo


@pytest.mark.asyncio
async def test_provision_member_adopt_and_owner_resignin_use_stored_emails(monkeypatch):
    """Both sign-ins inside ``provision_member`` — the member's ADOPT and the
    owner's re-signin to mint the invite — read the stored address."""
    from core.encryption import encrypt
    from core.services import service_token
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)

    owner_row = _row(
        user_id="user_owner",
        company_id="co_xyz",
        paperclip_user_id="pc_user_owner",
        paperclip_password_encrypted=encrypt("owner-pw"),
        service_token_encrypted=encrypt("svc"),
        status="active",
        paperclip_email="owner@isol8.co",
    )
    member_existing = _row(
        user_id="user_member",
        org_id="org_other",
        paperclip_user_id="pc_user_member",
        paperclip_password_encrypted=encrypt("member-pw"),
        paperclip_email="member@isol8.co",
    )
    repo = _member_repo(monkeypatch, existing=member_existing, owner_row=owner_row)
    monkeypatch.setattr(service_token, "mint", MagicMock(return_value="svc.tok"))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_member(
        owner_id="org_xyz",
        user_id="user_member",
        email="member@thelightbulb.company",
        owner_email="owner@thelightbulb.company",
    )

    signed_in = [c.kwargs["email"] for c in admin.sign_in_user.await_args_list]
    assert signed_in == ["member@isol8.co", "owner@isol8.co"]
    admin.sign_up_user.assert_not_awaited()
    assert repo.put.await_args.args[0].paperclip_email == "member@isol8.co"


@pytest.mark.asyncio
async def test_provision_member_falls_back_to_the_live_email(monkeypatch):
    """Unbackfilled rows on both sides: the live Clerk addresses are used, and
    the member row records the one it signed up with."""
    from core.encryption import encrypt
    from core.services import service_token
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin = _build_provision_member_doubles(monkeypatch)

    owner_row = _row(
        user_id="user_owner",
        company_id="co_xyz",
        paperclip_user_id="pc_user_owner",
        paperclip_password_encrypted=encrypt("owner-pw"),
        service_token_encrypted=encrypt("svc"),
        status="active",
    )
    repo = _member_repo(monkeypatch, existing=None, owner_row=owner_row)
    monkeypatch.setattr(service_token, "mint", MagicMock(return_value="svc.tok"))

    svc = PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev")
    await svc.provision_member(
        owner_id="org_xyz",
        user_id="user_member",
        email="member@example.com",
        owner_email="owner@example.com",
    )

    admin.sign_up_user.assert_awaited_once()
    assert admin.sign_in_user.await_args.kwargs["email"] == "owner@example.com"
    for call in repo.put.await_args_list:
        assert call.args[0].paperclip_email == "member@example.com"
