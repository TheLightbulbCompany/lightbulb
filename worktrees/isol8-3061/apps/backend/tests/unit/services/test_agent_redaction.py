"""Tests for the shared agent-secret redaction helper.

Single source of truth used by BOTH the /teams BFF (_redact_agent) and the
Paperclip proxy (_redact_agent_secrets_json) — so a secret field added here is
stripped on every browser-facing surface at once.
"""

import json

from core.services.agent_redaction import AGENT_SECRET_FIELDS, strip_agent_secrets


def test_secret_field_set_is_the_expected_minimum():
    assert {"adapterConfig", "adapterType"} <= set(AGENT_SECRET_FIELDS)


def test_strip_single_agent_object():
    agent = {
        "id": "a1",
        "name": "Friday",
        "adapterType": "openclaw_gateway",
        "adapterConfig": {"devicePrivateKeyPem": "SECRET", "authToken": "TOK"},
    }
    assert strip_agent_secrets(agent) == {"id": "a1", "name": "Friday"}


def test_strip_list_wrapped_and_nested():
    payload = {
        "agents": [
            {
                "id": "a1",
                "adapterConfig": {"devicePrivateKeyPem": "K1"},
                # recursive strip also covers the nested modelProfiles shape
                "runtimeConfig": {"modelProfiles": {"cheap": {"adapterConfig": {"authToken": "NESTED"}}}},
            },
            {"id": "a2", "adapterType": "x", "adapterConfig": {"authToken": "K2"}},
        ]
    }
    out = strip_agent_secrets(payload)
    dumped = json.dumps(out)
    assert "adapterConfig" not in dumped
    assert "adapterType" not in dumped
    for secret in ("K1", "K2", "NESTED"):
        assert secret not in dumped
    # Non-secret fields preserved.
    assert out["agents"][0]["id"] == "a1"
    assert out["agents"][0]["runtimeConfig"]["modelProfiles"]["cheap"] == {}


def test_strip_passes_through_non_container_values():
    assert strip_agent_secrets("a string") == "a string"
    assert strip_agent_secrets(42) == 42
    assert strip_agent_secrets(None) is None
    assert strip_agent_secrets([]) == []


def test_strip_does_not_mutate_input():
    agent = {"id": "a1", "adapterConfig": {"authToken": "T"}}
    strip_agent_secrets(agent)
    # Original is untouched (a fresh structure is returned).
    assert agent == {"id": "a1", "adapterConfig": {"authToken": "T"}}
