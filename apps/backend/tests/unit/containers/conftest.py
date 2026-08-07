"""Shared fixtures for container unit tests.

Bedrock provisioning now mints a per-owner bedrock-gate bearer token (a real
DDB PutItem + containers-row write) on every Bedrock-path provision, and a
confirmed roll expires the superseded one (real DDB + ECS reads). Stub the
DDB-touching mint/deny/expire by default so the existing provisioning tests keep
exercising provisioning logic without a live gate-tokens table — the token
module has its own moto-backed suite (tests/unit/billing/test_bedrock_gate_tokens.py).

``hash_token`` is intentionally left REAL (a pure sha256, no I/O) so
create_user_service still receives a plausible hash.

Tests that need to assert on the mint/deny calls themselves re-``monkeypatch``
these to capturing mocks in their own body (the test-body setattr runs after
this autouse fixture, so it wins).
"""

from unittest.mock import AsyncMock

import pytest

# The plaintext the stubbed mint returns. Env-var assertions (bearer token / ACP
# ANTHROPIC_AUTH_TOKEN) compare against this.
STUB_GATE_TOKEN = "gate-token-stub-xyz"


@pytest.fixture
def stub_gate_token() -> str:
    return STUB_GATE_TOKEN


@pytest.fixture(autouse=True)
def _stub_bedrock_gate_tokens(monkeypatch, stub_gate_token):
    monkeypatch.setattr(
        "core.billing.bedrock_gate_tokens.mint_gate_token",
        AsyncMock(return_value=stub_gate_token),
    )
    monkeypatch.setattr(
        "core.billing.bedrock_gate_tokens.deny_gate_token",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens",
        AsyncMock(),
    )
    # create_user_service re-asserts the minted hash onto the row it just
    # created, through container_repo.swap_gate_token_hash — another live DDB
    # write on every Bedrock provision.
    monkeypatch.setattr(
        "core.billing.bedrock_gate_tokens.persist_gate_token_hash",
        AsyncMock(),
    )
