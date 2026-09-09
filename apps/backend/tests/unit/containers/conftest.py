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

from contextlib import asynccontextmanager
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


@pytest.fixture(autouse=True)
def _owner_lifecycle_guard_stub(monkeypatch):
    """Keep ECS unit tests off EFS; the mutex has focused filesystem tests."""

    @asynccontextmanager
    async def unlocked(_owner_id):
        yield

    monkeypatch.setattr("core.services.owner_lifecycle.owner_lifecycle_guard", unlocked)


@pytest.fixture(autouse=True)
def _emit_81_config_shape(monkeypatch):
    """Emit the 2026.8.1 config shape in these tests.

    ``build_openclaw_config_dict`` picks its shape from the image the config
    will RUN on, because writing the 8.1 shape to a container still on 7.2
    fails its strict parse and writing the 7.2 shape to an 8.1 container drops
    settings it honours. While ``openclaw-version.json`` pins dev/prod to
    2026.7.2 the real default is the 7.2 shape, so these assertions would
    otherwise test the branch we are migrating away from.

    Patches the TARGET-tag derivation rather than the old
    ``deployed_upstream_at_least`` predicate, which is no longer on the path.
    That matters: after the gate moved to the target tag, this fixture kept
    patching a function nobody called, so 28 tests silently fell back to the
    7.2 shape while still claiming to cover 8.1.

    The 7.2 branch is not left uncovered: ``test_config_shape_gate.py`` drives
    both sides by passing a real ``deployed_tag``.
    """
    from core.containers import config as config_mod

    monkeypatch.setattr(config_mod, "_emits_81_shape", lambda deployed_tag: True)
    monkeypatch.setattr(config_mod, "_target_upstream", lambda deployed_tag: "2026.8.1")
