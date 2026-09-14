"""``classify`` decides who the legacy-personal purge may touch. Anyone still
paying, still inside a trial window, or running is never a purge candidate."""

from unittest.mock import MagicMock

from scripts.purge_legacy_personal_workspaces import classify

NOW = 1_800_000_000.0


def _row(org_id: str) -> MagicMock:
    return MagicMock(org_id=org_id, company_id="co", paperclip_user_id="pcu", paperclip_email="x@y.z")


def test_org_owner_ids_are_never_candidates():
    assert classify("org_1", None, None, None, now=NOW)[0] == "skip"


def test_paying_personal_owner_is_skipped():
    verdict, reason = classify(
        "user_1", {"subscription_status": "active"}, {"status": "running"}, _row("user_1"), now=NOW
    )
    assert verdict == "skip" and "entitled" in reason


def test_unexpired_trial_is_skipped():
    verdict, _ = classify(
        "user_1", {"subscription_status": "trial_local", "trial_end": NOW + 60}, {"status": "stopped"}, None, now=NOW
    )
    assert verdict == "skip"


def test_running_container_is_skipped_even_when_unpaid():
    verdict, reason = classify(
        "user_1", {"subscription_status": "trial_local", "trial_end": NOW - 60}, {"status": "running"}, None, now=NOW
    )
    assert verdict == "skip" and "running" in reason


def test_expired_personal_only_owner_is_purged():
    verdict, reason = classify(
        "user_1",
        {"subscription_status": "trial_local", "trial_end": NOW - 60},
        {"status": "stopped"},
        _row("user_1"),
        now=NOW,
    )
    assert verdict == "purge" and "personal-only" in reason


def test_dual_tenant_purges_personal_side_and_names_the_kept_org_row():
    verdict, reason = classify(
        "user_1",
        {"subscription_status": "trial_local", "trial_end": NOW - 60},
        {"status": "stopped"},
        _row("org_home"),
        now=NOW,
    )
    assert verdict == "purge" and "org_home" in reason


def test_residue_with_no_container_and_no_billing_is_purged():
    assert classify("user_1", None, None, _row("user_1"), now=NOW)[0] == "purge"
