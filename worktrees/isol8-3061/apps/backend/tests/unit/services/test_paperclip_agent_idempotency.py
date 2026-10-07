from core.services.paperclip_agent_idempotency import (
    build_agent_create_idempotency_key,
    paperclip_agent_intent,
)


def test_key_is_canonical_and_credential_free():
    intent_a = {
        "agent": paperclip_agent_intent(
            name="Forge",
            role="engineer",
            adapter_type="openclaw_gateway",
            capabilities="Ships code.",
        ),
        "source": {"listingId": "lst_1", "version": 2},
    }
    intent_b = {
        "source": {"version": 2, "listingId": "lst_1"},
        "agent": {
            "capabilities": "Ships code.",
            "role": "engineer",
            "name": "Forge",
            "adapterType": "openclaw_gateway",
        },
    }

    first = build_agent_create_idempotency_key(
        operation="marketplace-deploy",
        owner_id="org_1",
        intent=intent_a,
    )
    second = build_agent_create_idempotency_key(
        operation="marketplace-deploy",
        owner_id="org_1",
        intent=intent_b,
    )

    assert first == second
    assert first.startswith("lightbulb-agent-create:v1:marketplace-deploy:")
    assert "Forge" not in first
    assert len(first) < 160


def test_key_changes_with_business_intent_but_not_actor_credentials():
    base = paperclip_agent_intent(
        name="Gauge",
        role="researcher",
        adapter_type="openclaw_gateway",
    )
    first = build_agent_create_idempotency_key(operation="chat", owner_id="org_1", intent=base)
    same = build_agent_create_idempotency_key(operation="chat", owner_id="org_1", intent=dict(base))
    changed_role = build_agent_create_idempotency_key(
        operation="chat",
        owner_id="org_1",
        intent={**base, "role": "general"},
    )
    changed_owner = build_agent_create_idempotency_key(
        operation="chat",
        owner_id="org_2",
        intent=base,
    )

    assert first == same
    assert first != changed_role
    assert first != changed_owner
