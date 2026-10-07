"""Unit tests for the server-side PostHog billing bridge in
``stripe_webhook_handlers`` — the transition->event mapping and the
distinct-id resolution, exercised without any network or a real client.
"""

import core.services.stripe_webhook_handlers as h


def _run(monkeypatch, old, new, *, provider="chatgpt_oauth", clerk_user_id="user_HUMAN"):
    calls: list[dict] = []
    monkeypatch.setattr(h, "capture_product_event", lambda **kw: calls.append(kw))
    account = {"owner_id": "org_ABC", "provider_choice": provider}
    event_data = {"trial_end": 123, "metadata": {"clerk_user_id": clerk_user_id}}
    h._bridge_subscription_state(account, event_data, old, new, None)
    return calls[0] if calls else None


def test_trial_started(monkeypatch):
    assert _run(monkeypatch, None, "trialing")["event"] == "trial_started"


def test_trial_converted_is_the_trial_to_paid_moment(monkeypatch):
    call = _run(monkeypatch, "trialing", "active")
    assert call["event"] == "trial_converted"
    assert call["set_person"]["is_subscribed"] is True
    assert call["set_person"]["subscription_status"] == "active"
    assert call["properties"]["from_status"] == "trialing"
    assert call["properties"]["to_status"] == "active"


def test_reactivation_and_past_due(monkeypatch):
    assert _run(monkeypatch, "canceled", "active")["event"] == "subscription_activated"
    assert _run(monkeypatch, "trialing", "past_due")["event"] == "subscription_past_due"


def test_no_op_update_is_skipped(monkeypatch):
    # payment-method change etc. — status unchanged, nothing recorded
    assert _run(monkeypatch, "active", "active") is None


def test_distinct_id_org_owner_keys_on_subscriber_clerk_id(monkeypatch):
    # org owner_id is an org id; must key on the subscribing human so the
    # event merges onto the frontend-identified person, not a phantom org person
    assert _run(monkeypatch, "trialing", "active")["distinct_id"] == "user_HUMAN"


def test_distinct_id_personal_owner_falls_back_to_owner_id():
    assert h._subscriber_distinct_id({"owner_id": "user_SELF"}, {"metadata": {}}) == "user_SELF"


def test_bridge_never_raises_when_key_unset():
    # end-to-end through the real capture helper (write key unset in tests →
    # no-op) — must not raise on the webhook path
    h._bridge_subscription_state(
        {"owner_id": "user_X", "provider_choice": "byo_key"},
        {"trial_end": None, "metadata": {}},
        "trialing",
        "active",
        None,
    )
