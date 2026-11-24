"""The subscriber's PostHog distinct id, used by the trial-will-end, subscription-deleted and invoice-paid handlers."""

import core.services.stripe_webhook_handlers as h


def test_distinct_id_personal_owner_falls_back_to_owner_id():
    assert h._subscriber_distinct_id({"owner_id": "user_SELF"}, {"metadata": {}}) == "user_SELF"
