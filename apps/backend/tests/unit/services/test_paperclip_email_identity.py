"""The stored Better Auth address survives a Clerk primary-email change.

Better Auth's ``user.email`` is written once at sign-up and NEVER updated, so
the live Clerk address is not the sign-in truth. Every sign-in door must
prefer the identity row's ``paperclip_email`` and fall back to the live
address only when it is empty — the wrong way round 401s, falls through to
sign-UP, and mints a duplicate Better Auth account whose sign-in rate limit
is per-IP (i.e. fleet-wide degradation).

Covered here: the ADOPT branches of ``provision_org`` and ``provision_member``
and the fresh sign-up record, against a moto-backed repo. The Teams-BFF door
has its own file (``test_paperclip_user_session.py``).
"""

from unittest.mock import AsyncMock

from core.repositories.paperclip_repo import PaperclipIdentity
from core.services.paperclip_provisioning import PaperclipProvisioning
from tests.unit.services import test_paperclip_provisioning_moto as _moto
from tests.unit.services.test_paperclip_provisioning_moto import _make_admin_mock, _seed_owner

# The moto suite's fixtures: its real-table ``repo`` and the autouse stubs
# (admin session cookie, the container row's hook token).
repo = _moto.repo
_seed_container_row = _moto._seed_container_row
_stub_admin_session = _moto._stub_admin_session


async def _put_identity(repo, user_id, *, paperclip_user_id, password, email):
    from core.encryption import encrypt

    await repo.put_identity(
        PaperclipIdentity(
            user_id=user_id,
            paperclip_user_id=paperclip_user_id,
            paperclip_password_encrypted=encrypt(password),
            paperclip_email=email,
        )
    )


async def test_provision_org_adopt_signs_in_with_the_stored_email(repo):
    """Provisioner ADOPT branch: sign in with the address Better Auth holds,
    not the live Clerk one — and leave that stored address alone."""
    await _put_identity(repo, "user_owner", paperclip_user_id="pc_user_1", password="owner-pw", email="owner@isol8.co")
    admin = _make_admin_mock()
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_adopt"})

    ws = await PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev").provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        # The human has MOVED domains; Clerk now mints the new address.
        owner_email="owner@thelightbulb.company",
    )

    assert ws.status == "active"
    admin.sign_in_user.assert_awaited_once_with(email="owner@isol8.co", password="owner-pw")
    admin.sign_up_user.assert_not_awaited()
    assert (await repo.get_identity("user_owner")).paperclip_email == "owner@isol8.co"


async def test_provision_org_adopt_falls_back_to_clerk_when_unbackfilled(repo):
    """An identity written before the field existed has nothing stored, so the
    live Clerk address is the only thing to try."""
    await _put_identity(repo, "user_owner", paperclip_user_id="pc_user_1", password="owner-pw", email="")
    admin = _make_admin_mock()
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_adopt"})

    await PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev").provision_org(
        owner_id="org_xyz", owner_user_id="user_owner", owner_email="owner@example.com"
    )

    admin.sign_in_user.assert_awaited_once_with(email="owner@example.com", password="owner-pw")
    admin.sign_up_user.assert_not_awaited()


async def test_provision_org_fresh_records_the_signup_email(repo):
    admin = _make_admin_mock()

    await PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev").provision_org(
        owner_id="org_xyz", owner_user_id="user_owner", owner_email="fresh@example.com"
    )

    assert admin.sign_up_user.await_args.kwargs["email"] == "fresh@example.com"
    assert (await repo.get_identity("user_owner")).paperclip_email == "fresh@example.com"


async def test_provision_member_adopt_uses_the_stored_email(repo):
    """The member's ADOPT sign-in reads the stored address. It is the ONLY
    sign-in: the bootstrap admin mints the invite, so no owner re-signin."""
    await _seed_owner(repo, org_id="org_xyz", company_id="co_xyz")
    await _put_identity(
        repo, "user_member", paperclip_user_id="pc_user_member", password="member-pw", email="member@isol8.co"
    )
    admin = _make_admin_mock()

    ws = await PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev").provision_member(
        owner_id="org_xyz", user_id="user_member", email="member@thelightbulb.company"
    )

    assert "user_member" in ws.member_user_ids
    assert [c.kwargs["email"] for c in admin.sign_in_user.await_args_list] == ["member@isol8.co"]
    assert admin.sign_in_user.await_args.kwargs["password"] == "member-pw"
    admin.sign_up_user.assert_not_awaited()
    assert (await repo.get_identity("user_member")).paperclip_email == "member@isol8.co"


async def test_provision_member_fresh_signs_up_with_the_live_email(repo):
    """No identity yet: the live Clerk address is used for sign-up and recorded."""
    await _seed_owner(repo, org_id="org_xyz", company_id="co_xyz")
    admin = _make_admin_mock()

    await PaperclipProvisioning(admin_client=admin, repo=repo, env_name="dev").provision_member(
        owner_id="org_xyz", user_id="user_member", email="member@example.com"
    )

    admin.sign_up_user.assert_awaited_once()
    assert admin.sign_up_user.await_args.kwargs["email"] == "member@example.com"
    admin.sign_in_user.assert_not_awaited()
    assert (await repo.get_identity("user_member")).paperclip_email == "member@example.com"
