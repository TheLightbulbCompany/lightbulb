"""The pure halves of the paying-owner migration: who may be migrated, and the
row transforms that carry billing, Paperclip and messaging state to the org."""

from datetime import datetime, timezone

from core.repositories.paperclip_repo import PaperclipCompany
from scripts.migrate_personal_owner_to_workspace import (
    billing_for_org,
    check_preconditions,
    paperclip_row_for_org,
    rekeyed_owner_item,
    slugify,
)

USER, ORG = "user_1", "org_9"
PAID = {
    "owner_id": USER,
    "subscription_status": "active",
    "stripe_customer_id": "cus_1",
    "provider_choice": "bedrock_claude",
}
BOX = {"owner_id": USER, "status": "running"}


def _row(org_id: str = USER) -> PaperclipCompany:
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    return PaperclipCompany(
        user_id=USER,
        org_id=org_id,
        company_id="co_1",
        paperclip_user_id="pcu_1",
        paperclip_password_encrypted="enc-pw",
        service_token_encrypted="enc-old",
        status="active",
        created_at=now,
        updated_at=now,
        paperclip_email="x@y.z",
        seeded_agent_ids=["a1", "a2"],
    )


def test_migratable_paying_owner_passes():
    assert check_preconditions(USER, PAID, BOX, _row(), []) is None


def test_refuses_org_ids_and_unentitled_owners():
    assert "user id" in check_preconditions(ORG, PAID, BOX, _row(), [])
    assert "purge" in check_preconditions(USER, {"subscription_status": "canceled"}, BOX, _row(), [])
    assert "purge" in check_preconditions(USER, None, BOX, _row(), [])


def test_refuses_missing_state():
    assert "containers" in check_preconditions(USER, PAID, None, _row(), [])
    assert "paperclip" in check_preconditions(USER, PAID, BOX, None, [])


def test_existing_membership_needs_explicit_resume():
    assert "--org" in check_preconditions(USER, PAID, BOX, _row(), [{"id": ORG}])
    assert check_preconditions(USER, PAID, BOX, _row(), [{"id": ORG}], org_override=ORG) is None
    assert "not one of" in check_preconditions(USER, PAID, BOX, _row(), [{"id": "org_other"}], org_override=ORG)


def test_row_already_pointed_at_another_org_is_refused_unless_resuming_onto_it():
    assert "already points" in check_preconditions(USER, PAID, BOX, _row("org_other"), [])
    assert check_preconditions(USER, PAID, BOX, _row(ORG), [{"id": ORG}], org_override=ORG) is None


def test_billing_row_keeps_every_entitlement_field():
    new = billing_for_org(PAID, ORG, now_iso="2026-09-14T00:00:00+00:00")
    assert new["owner_id"] == ORG and new["owner_type"] == "org" and new["migrated_from"] == USER
    for key in ("subscription_status", "stripe_customer_id", "provider_choice"):
        assert new[key] == PAID[key]
    assert PAID["owner_id"] == USER  # input untouched


def test_paperclip_row_moves_org_and_token_only():
    now = datetime(2026, 9, 14, 1, tzinfo=timezone.utc)
    new = paperclip_row_for_org(_row(), ORG, "enc-new", now=now)
    assert (new.org_id, new.service_token_encrypted, new.updated_at) == (ORG, "enc-new", now)
    for field in (
        "user_id",
        "company_id",
        "paperclip_user_id",
        "paperclip_password_encrypted",
        "paperclip_email",
        "seeded_agent_ids",
        "status",
    ):
        assert getattr(new, field) == getattr(_row(), field)


def test_owner_rows_rekey_pk_and_owner_id_only():
    item = {"pk": f"OWNER#{USER}", "sk": "ROOM#r1", "roomId": "r1", "ownerId": USER, "workspaceId": "ws-1"}
    new = rekeyed_owner_item(item, USER, ORG)
    assert new == {"pk": f"OWNER#{ORG}", "sk": "ROOM#r1", "roomId": "r1", "ownerId": ORG, "workspaceId": "ws-1"}
    other = {"pk": f"OWNER#{USER}", "sk": "USERSTREAM#user_7", "ownerId": "user_7"}
    assert rekeyed_owner_item(other, USER, ORG)["ownerId"] == "user_7"


def test_slugify():
    assert slugify("KidShield, Inc.") == "kidshield-inc"
    assert slugify("!!!") == "workspace"
    assert len(slugify("x" * 80)) == 40


def test_resume_accepts_a_billing_row_already_moved_to_the_org():
    moved = {"owner_id": ORG, "subscription_status": "active", "migrated_from": USER}
    assert check_preconditions(USER, None, BOX, _row(), [{"id": ORG}], org_override=ORG, org_billing=moved) is None
    stranger = {**moved, "migrated_from": "user_other"}
    assert "purge" in check_preconditions(
        USER, None, BOX, _row(), [{"id": ORG}], org_override=ORG, org_billing=stranger
    )


def test_resume_tolerates_a_personal_box_already_purged():
    moved = {"owner_id": ORG, "subscription_status": "active", "migrated_from": USER}
    assert check_preconditions(USER, None, None, _row(ORG), [{"id": ORG}], org_override=ORG, org_billing=moved) is None


def test_billing_names_the_chatgpt_connector_for_the_org():
    chatgpt = {"owner_id": USER, "subscription_status": "active", "provider_choice": "chatgpt_oauth"}
    assert billing_for_org(chatgpt, ORG, now_iso="t")["oauth_connector_user_id"] == USER
    already = {**chatgpt, "oauth_connector_user_id": "user_admin"}
    assert billing_for_org(already, ORG, now_iso="t")["oauth_connector_user_id"] == "user_admin"
    assert "oauth_connector_user_id" not in billing_for_org(PAID, ORG, now_iso="t")


def test_token_rebind_targets_only_rows_bound_to_the_personal_workspace():
    from scripts.migrate_personal_owner_to_workspace import should_rebind_token

    assert should_rebind_token({"state": "active", "bound_owner_id": USER}, USER, USER)
    assert should_rebind_token({"state": "active"}, USER, USER)  # unbound provider row = its own key
    assert should_rebind_token({"state": "active", "bound_owner_id": USER}, f"{USER}#harness", USER)
    assert not should_rebind_token({"state": "active"}, f"{USER}#harness", USER)
    assert not should_rebind_token({"state": "active", "bound_owner_id": "org_other"}, USER, USER)
    assert not should_rebind_token({"state": "revoked", "bound_owner_id": USER}, USER, USER)
    assert not should_rebind_token(None, USER, USER)
