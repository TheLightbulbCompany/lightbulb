"""Unit tests for the agent-run-timeout backfill script."""

from unittest.mock import AsyncMock

import pytest

from core.services.paperclip_adapter_config import (
    AGENT_RUN_TIMEOUT_SEC,
    AGENT_RUN_WAIT_TIMEOUT_MS,
    OPENCLAW_GATEWAY_TYPE,
)
from scripts.backfill_paperclip_agent_run_timeout import (
    backfill_one_agent,
    needs_backfill,
)


def _agent(cfg: dict, adapter_type: str = OPENCLAW_GATEWAY_TYPE) -> dict:
    return {"id": "agent_x", "name": "Lens", "adapterType": adapter_type, "adapterConfig": cfg}


def test_needs_backfill_true_for_legacy_120s():
    assert needs_backfill(_agent({"timeoutSec": 120, "waitTimeoutMs": 120000}))


def test_needs_backfill_true_when_fields_absent():
    # Pre-timeout adapterConfig with no timeout fields at all.
    assert needs_backfill(_agent({"url": "wss://ws.isol8.co?token=t", "authToken": "t"}))


def test_needs_backfill_true_when_only_wait_ms_lags():
    # Guards the two never drifting apart independently.
    assert needs_backfill(_agent({"timeoutSec": AGENT_RUN_TIMEOUT_SEC, "waitTimeoutMs": 120000}))


def test_needs_backfill_false_when_already_canonical():
    assert not needs_backfill(
        _agent(
            {
                "timeoutSec": AGENT_RUN_TIMEOUT_SEC,
                "waitTimeoutMs": AGENT_RUN_WAIT_TIMEOUT_MS,
            }
        )
    )


def test_needs_backfill_false_for_higher_than_canonical():
    # A future manual bump above the constant must not be clobbered downward.
    assert not needs_backfill(_agent({"timeoutSec": 600, "waitTimeoutMs": 600000}))


def test_needs_backfill_false_for_non_openclaw_adapter():
    assert not needs_backfill(_agent({"timeoutSec": 120}, adapter_type="slack"))


@pytest.mark.asyncio
async def test_backfill_one_agent_lifts_timeout_preserving_other_fields():
    admin_client = AsyncMock()
    agent = _agent(
        {
            "url": "wss://ws.isol8.co?token=t&clientId=paperclip-operator",
            "authToken": "tok",
            "sessionKeyStrategy": "issue",
            "sessionKey": "user_x",
            "devicePrivateKeyPem": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
            "paperclipApiUrl": "http://paperclip.internal",
            "claimedApiKeyPath": "~/.openclaw/workspaces/agent_x/paperclip-claimed-api-key.json",
            "timeoutSec": 120,
            "waitTimeoutMs": 120000,
        }
    )

    await backfill_one_agent(
        admin_client=admin_client,
        admin_cookie="cookie",
        agent=agent,
    )

    body = admin_client.patch_agent.await_args.kwargs["body"]
    new_cfg = body["adapterConfig"]
    assert body["replaceAdapterConfig"] is True
    # Timeout lifted.
    assert new_cfg["timeoutSec"] == AGENT_RUN_TIMEOUT_SEC
    assert new_cfg["waitTimeoutMs"] == AGENT_RUN_WAIT_TIMEOUT_MS
    # Every other field preserved verbatim — devicePrivateKeyPem + paperclipApiUrl
    # must survive so the deviceId / paired.json entry stay stable.
    assert new_cfg["url"] == agent["adapterConfig"]["url"]
    assert new_cfg["authToken"] == "tok"
    assert new_cfg["sessionKeyStrategy"] == "issue"
    assert new_cfg["sessionKey"] == "user_x"
    assert new_cfg["devicePrivateKeyPem"] == agent["adapterConfig"]["devicePrivateKeyPem"]
    assert new_cfg["paperclipApiUrl"] == "http://paperclip.internal"
    assert new_cfg["claimedApiKeyPath"] == agent["adapterConfig"]["claimedApiKeyPath"]
