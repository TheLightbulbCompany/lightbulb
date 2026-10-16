"""Server-owned Paperclip adapter configuration.

Every agent uses Paperclip's deployed ``hermes_gateway`` adapter, pointed at the
hook-token-authenticated backend facade rather than a private runtime address. No
client controls the adapter type or its config.

The device-paired ``openclaw_gateway`` shape this module used to also build is
gone with the OpenClaw gateway: there is no ws relay to dial, no per-agent
operator device to mint, and no ``paired.json`` to pre-register it in.
"""

from __future__ import annotations

from typing import Final

from core.config import settings

# Paperclip's deployed native adapter. It keeps the Paperclip run open while it
# polls our authenticated backend facade for the real Hermes terminal result.
HERMES_GATEWAY_TYPE: Final[str] = "hermes_gateway"

# A Paperclip run's wall clock. Coding turns routinely run 15-25 min; a 900 s cap marked them
# timed_out mid-turn and a continuation run then overlapped the still-running turn. 1 h stays
# far inside the AgentCore session's 8 h max lifetime.
PAPERCLIP_RUN_TIMEOUT_SEC: Final[int] = 3600

# Both are the adapter's own CLAMP CEILINGS (execute.ts clamps eventReconnectMs to
# 250..30_000 and pollIntervalMs to 250..10_000), chosen deliberately at the top of each
# range because every one of those requests is an `InvokeAgentRuntime` into the owner's
# microVM. The adapter's defaults -- 2 s reconnect, 1 s poll -- would cost ~3,600 invokes on a
# full one-hour run. At the ceilings it is ~360. The SSE `/events` route is deliberately
# unimplemented (the adapter falls back to polling and logs one line per reconnect), which
# is the other reason eventReconnectMs is pinned as high as it will go.
PAPERCLIP_EVENT_RECONNECT_MS: Final[int] = 30_000
PAPERCLIP_POLL_INTERVAL_MS: Final[int] = 10_000


def build_hermes_gateway_adapter_config(owner_id: str, *, hook_token: str) -> dict:
    """Native Hermes adapter config through the owner-authenticated backend facade.

    An EMPTY ``hook_token`` raises rather than minting a config: the adapter would send
    ``Authorization: Bearer `` on every run and 401 at the facade, and Paperclip reports that
    as ``hermes_gateway_auth_failed`` on each individual run rather than as a broken agent.
    That is the M4-R33 shape -- a seed racing ahead of ``prepare_hermes_workspace`` -- and it
    must fail at WIRING time, where one loud error names the owner, not silently per run.

    ``apiKey`` is a schema secret field on Paperclip's side, so a literal value is persisted
    as a managed company secret and resolved back at runtime. That means the token is a
    SNAPSHOT: rotating an owner's ``hook_token`` requires re-syncing every agent's adapter
    config (``paperclip_agent_lifecycle.sync_agent_adapter_config``), exactly as the previous
    ``http`` adapter's baked header did.
    """
    if not hook_token:
        raise AdapterConfigError(f"hermes_gateway adapter for owner={owner_id} needs a hook_token")
    config = {
        "apiBaseUrl": (f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/internal/paperclip/hermes/{owner_id}"),
        "apiKey": hook_token,
        "sessionKeyStrategy": "issue",
        "timeoutSec": PAPERCLIP_RUN_TIMEOUT_SEC,
        "eventReconnectMs": PAPERCLIP_EVENT_RECONNECT_MS,
        "pollIntervalMs": PAPERCLIP_POLL_INTERVAL_MS,
    }
    if settings.PAPERCLIP_INTERNAL_URL:
        config["paperclipApiUrl"] = settings.PAPERCLIP_INTERNAL_URL
    return config


#: Paperclip runs a coding agent starts at once (``runtimeConfig.heartbeat.maxConcurrentRuns``, Paperclip's
#: default 20). Each task gets its own worktree on the workspace's one 2 vCPU microVM, and a coding turn
#: that builds or tests is what saturates it (owner ruling 2026-10-16); a third task queues in Paperclip.
CODING_MAX_CONCURRENT_RUNS: Final[int] = 2


def coding_runtime_config(role: str | None, runtime_config: dict | None) -> dict | None:
    """``runtime_config`` with the engineer run cap, or None when it already has it (or ``role`` is no
    engineer). Always the WHOLE runtimeConfig: Paperclip's PATCH replaces it wholesale."""
    runtime_config = dict(runtime_config or {})
    heartbeat = dict(runtime_config.get("heartbeat") or {})
    if role != "engineer" or heartbeat.get("maxConcurrentRuns") == CODING_MAX_CONCURRENT_RUNS:
        return None
    return {**runtime_config, "heartbeat": {**heartbeat, "maxConcurrentRuns": CODING_MAX_CONCURRENT_RUNS}}


class AdapterConfigError(Exception):
    """Raised when adapter-config inputs fail validation."""
