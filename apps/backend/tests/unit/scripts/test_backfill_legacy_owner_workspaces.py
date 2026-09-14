"""``plan_workspace`` decides the naming + skip rules with no I/O — pure and
fully testable. ``slugify`` is the one bit of string-munging it delegates to."""

from scripts.backfill_legacy_owner_workspaces import plan_workspace, slugify

NOW_MEMBERSHIP = [{"id": "org_existing", "slug": "existing", "name": "Existing", "role": "org:admin"}]


def test_skip_when_already_a_member():
    verdict, name, slug, warn_paid = plan_workspace("user_1", NOW_MEMBERSHIP, "Acme Co", "Ada", "ada@example.com")
    assert verdict == "skip: already in org_existing"
    assert name == "" and slug == "" and warn_paid is False


def test_company_name_wins_over_every_other_source():
    verdict, name, slug, _ = plan_workspace("user_1", [], "Acme Co", "Ada", "ada@example.com")
    assert verdict == "create"
    assert name == "Acme Co"
    assert slug == "acme-co"


def test_first_name_fallback_when_no_company_name():
    verdict, name, slug, _ = plan_workspace("user_1", [], None, "Ada", "ada@example.com")
    assert verdict == "create"
    assert name == "Ada's workspace"
    assert slug == "ada-s-workspace"


def test_email_local_part_fallback_when_no_company_or_first_name():
    verdict, name, slug, _ = plan_workspace("user_1", [], None, None, "ada.lovelace@example.com")
    assert verdict == "create"
    assert name == "ada.lovelace's workspace"


def test_owner_id_is_the_last_resort():
    verdict, name, slug, _ = plan_workspace("user_abc123", [], None, None, None)
    assert verdict == "create"
    assert name == "user_abc123"
    assert slug == "user-abc123"


def test_paid_owner_still_creates_but_flags_warn_paid():
    verdict, name, slug, warn_paid = plan_workspace(
        "user_1", [], None, "Ada", None, billing={"subscription_status": "active"}
    )
    assert verdict == "create"
    assert warn_paid is True


def test_live_trial_flags_warn_paid_but_an_expired_one_does_not():
    live = {"subscription_status": "trial_local", "trial_end": 1_000}
    expired = {"subscription_status": "trial_local", "trial_end": 10}
    assert plan_workspace("user_1", [], None, "Ada", None, billing=live, now=500)[3] is True
    assert plan_workspace("user_1", [], None, "Ada", None, billing=expired, now=500)[3] is False
    assert (
        plan_workspace("user_1", [], None, "Ada", None, billing={"subscription_status": "trial_local"}, now=500)[3]
        is False
    )


def test_no_billing_row_does_not_flag_warn_paid():
    _, _, _, warn_paid = plan_workspace("user_1", [], None, "Ada", None, billing=None)
    assert warn_paid is False


def test_canceled_billing_does_not_flag_warn_paid():
    _, _, _, warn_paid = plan_workspace("user_1", [], None, "Ada", None, billing={"subscription_status": "canceled"})
    assert warn_paid is False


def test_slugify_lowercases_and_dashes_punctuation():
    assert slugify("Ada's Workspace!!") == "ada-s-workspace"


def test_slugify_trims_leading_and_trailing_dashes():
    assert slugify("--Weird Name--") == "weird-name"


def test_slugify_caps_at_40_chars():
    long_name = "a" * 60
    slug = slugify(long_name)
    assert len(slug) == 40
    assert slug == "a" * 40


def test_slugify_empty_falls_back_to_workspace():
    assert slugify("!!!") == "workspace"
