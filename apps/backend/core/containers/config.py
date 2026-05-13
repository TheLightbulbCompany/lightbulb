"""
OpenClaw configuration generator for per-user containers.

Generates openclaw.json with the provider block determined by the
user's signup choice (provider_choice). Each container gets a gateway
auth token so it can bind to LAN (required for Docker port mapping).
"""

import asyncio
import base64
import fcntl
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

from core.config import settings

logger = logging.getLogger(__name__)

# Mirror of openclaw-version.json#tag at the repo root. Used as the
# `meta.lastTouchedVersion` value openclaw expects in our generated
# config (defeats `missing-meta-vs-last-good` auto-restore in
# src/config/io.observe-recovery.ts:425). Must match what openclaw
# itself self-writes (io.ts:879 uses `VERSION` from package.json).
# A drift test in tests/unit/containers/test_config.py asserts this
# stays in sync with openclaw-version.json on every CI run.
OPENCLAW_UPSTREAM_VERSION = "2026.5.7"


# ``fcntl.lockf`` serializes ACROSS processes but not across threads within a
# single Python process. ``ensure_node_paired_entry`` runs via
# ``asyncio.to_thread``, so two concurrent first-connects for different
# members of the same org land in different threads of the SAME process and
# can each acquire the fcntl lock simultaneously — the RMW then races and
# the last ``os.rename`` drops the other member's newly added device entry.
#
# Guard each owner's paired.json RMW with a dedicated ``threading.Lock``.
# One lock per owner keeps concurrency across different orgs; the guard
# dict itself is protected by a tiny master lock.
_PAIRED_THREAD_LOCKS: dict[str, threading.Lock] = {}
_PAIRED_THREAD_LOCKS_GUARD = threading.Lock()


def _paired_thread_lock(owner_id: str) -> threading.Lock:
    with _PAIRED_THREAD_LOCKS_GUARD:
        lock = _PAIRED_THREAD_LOCKS.get(owner_id)
        if lock is None:
            lock = threading.Lock()
            _PAIRED_THREAD_LOCKS[owner_id] = lock
        return lock


# =============================================================================
# Node device identity helpers
# =============================================================================


def _base64url_encode(data: bytes) -> str:
    """RFC 7515 base64url encoding (no padding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_node_device_identity() -> dict:
    """Generate a new Ed25519 keypair for node device identity.

    Returns dict with: device_id, public_key_b64, private_key_pem.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    raw_pub = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    device_id = hashlib.sha256(raw_pub).hexdigest()
    public_key_b64 = _base64url_encode(raw_pub)
    private_key_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("ascii")

    return {
        "device_id": device_id,
        "public_key_b64": public_key_b64,
        "private_key_pem": private_key_pem,
    }


def load_node_device_identity(private_key_pem: str) -> dict:
    """Reconstruct device identity from a stored PEM private key."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_private_key,
    )

    private_key = load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    raw_pub = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    device_id = hashlib.sha256(raw_pub).hexdigest()
    public_key_b64 = _base64url_encode(raw_pub)

    return {
        "device_id": device_id,
        "public_key_b64": public_key_b64,
        "private_key_pem": private_key_pem,
    }


def _build_node_paired_entry(device_id: str, public_key_b64: str) -> dict:
    """One `devices/paired.json` entry for the in-container node role.

    The ``tokens.node`` sub-entry is load-bearing in OpenClaw >= 2026.5.7.
    Upstream's ``listEffectivePairedDeviceRoles`` (src/infra/device-pairing.ts:246)
    fails closed for tokenless paired records — even with ``role: "node"``
    and ``roles: ["node"]`` set, the WS handshake's role-upgrade gate
    (server/ws-connection/message-handler.ts:1188-1206) rejects the
    connect with ``PAIRING_REQUIRED reason=role-upgrade`` because
    ``listActiveTokenRoles(undefined) === undefined`` resolves the
    effective role set to ``[]``. Pre-2026.5.x records without
    ``tokens`` worked under the legacy ``role``/``roles`` fields; that
    backstop is gone now.

    The token value itself is just a per-device random the gateway uses
    to disambiguate role rotations — it isn't presented on the wire by
    the desktop or backend, so a fresh ``secrets.token_urlsafe`` works.
    """
    now_ms = int(time.time() * 1000)
    return {
        "deviceId": device_id,
        "publicKey": public_key_b64,
        "role": "node",
        "roles": ["node"],
        "scopes": [],
        "approvedScopes": [],
        "tokens": {
            "node": {
                "token": secrets.token_urlsafe(32),
                "role": "node",
                "scopes": [],
                "createdAtMs": now_ms,
            },
        },
        "createdAtMs": now_ms,
        "approvedAtMs": now_ms,
    }


def ensure_node_paired_entry(
    efs_mount_path: str,
    owner_id: str,
    device_id: str,
    public_key_b64: str,
) -> bool:
    """Idempotently append a ``node`` entry to ``<owner>/devices/paired.json``.

    OpenClaw's connect handler rejects role="node" handshakes whose
    ``device.id`` isn't in the container's trust store (the paired.json file
    under ``devices/``). Per-member desktop nodes each have a distinct
    Ed25519 key, so each distinct member's ``device.id`` must be registered
    here before its first handshake will succeed.

    This function is safe to call on every ``_load_node_key`` — it's a no-op
    if the ``device_id`` is already present. It's also safe to run
    concurrently for different members.

    **Locking (two layers):**

    - ``fcntl.lockf`` on a SIBLING file (``paired.json.lock``) serializes
      ACROSS processes. Not on ``paired.json`` itself because we rewrite
      via tempfile + ``rename``: an fcntl lock is bound to the inode, and
      rename replaces it — the lock on the old inode is orphaned while a
      concurrent writer opens the new inode and acquires an independent
      lock. The sibling lock file's inode never changes.

    - A per-owner ``threading.Lock`` serializes WITHIN the process. fcntl
      locks are process-scoped, not thread-scoped, so two asyncio-to-thread
      workers for different members of the same org could both pass
      ``fcntl.lockf`` simultaneously and race the RMW. Dropping this lock
      caused intermittent lost entries under concurrent onboarding.

    Both locks are acquired in the same order (thread → fcntl) so there's
    no deadlock potential — the process-scope lock is always taken first.

    Atomicity: the rewrite uses temp-file + rename to avoid leaving the
    file half-written if the process dies mid-write. The tempfile is
    chowned to uid/gid 1000 when running as root so the in-container
    OpenClaw agent (running as ``node``) can read it.

    Returns True if a new entry was added, False if it was already present.
    """
    paired_path = os.path.join(efs_mount_path, owner_id, "devices", "paired.json")
    paired_dir = os.path.dirname(paired_path)
    lock_path = paired_path + ".lock"
    os.makedirs(paired_dir, exist_ok=True)

    # Ensure the lock file exists (atomic create-or-open; no truncation).
    # This file is never written to — it's just an inode we can lock.
    lock_create_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    if os.getuid() == 0:
        try:
            os.chown(lock_path, 1000, 1000)
        except OSError:
            pass
    os.close(lock_create_fd)

    # Thread-local first (process-scoped fcntl can't see sibling threads),
    # then fcntl on the sibling lock file (for cross-process).
    with _paired_thread_lock(owner_id):
        lock_fd = None
        try:
            lock_fd = open(lock_path, "r+", encoding="utf-8")
            fcntl.lockf(lock_fd, fcntl.LOCK_EX)

            # Read the current paired.json (creating an empty dict if missing —
            # normally ensure_device_identities writes it at provision time
            # with the operator entry, but recovery paths may not have it).
            try:
                with open(paired_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except FileNotFoundError:
                content = ""
            entries = json.loads(content) if content.strip() else {}
            if not isinstance(entries, dict):
                logger.warning(
                    "paired.json for owner %s was not a dict (got %s); resetting",
                    owner_id,
                    type(entries).__name__,
                )
                entries = {}

            if device_id in entries:
                existing = entries[device_id]
                # OpenClaw 2026.5.7+ requires a `tokens.<role>` entry for
                # role-upgrade gate (see _build_node_paired_entry docstring).
                # Existing pre-2026.5.7 records written without a tokens
                # block fail closed; patch them in-place on next connect.
                if (
                    isinstance(existing, dict)
                    and isinstance(existing.get("tokens"), dict)
                    and isinstance(existing["tokens"].get("node"), dict)
                    and existing["tokens"]["node"].get("token")
                ):
                    return False  # Already has tokens.node — true no-op.
                if not isinstance(existing, dict):
                    # Garbage entry — replace wholesale.
                    entries[device_id] = _build_node_paired_entry(device_id, public_key_b64)
                else:
                    # Preserve existing fields, just patch in the token.
                    existing.setdefault("tokens", {})["node"] = {
                        "token": secrets.token_urlsafe(32),
                        "role": "node",
                        "scopes": [],
                        "createdAtMs": int(time.time() * 1000),
                    }
                    logger.info(
                        "Upgraded tokenless paired entry %s with tokens.node for owner %s",
                        device_id[:16],
                        owner_id,
                    )
            else:
                entries[device_id] = _build_node_paired_entry(device_id, public_key_b64)

            fd, tmp_path = tempfile.mkstemp(dir=paired_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(entries, f, indent=2)
                if os.getuid() == 0:
                    try:
                        os.chown(tmp_path, 1000, 1000)
                    except OSError:
                        pass
                os.rename(tmp_path, paired_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            logger.info(
                "Registered node device_id %s in paired.json for owner %s",
                device_id[:16],
                owner_id,
            )
            return True
        finally:
            if lock_fd:
                try:
                    fcntl.lockf(lock_fd, fcntl.LOCK_UN)
                finally:
                    lock_fd.close()


def build_device_paired_json(
    node_device_id: str,
    node_public_key_b64: str,
    *,
    operator_entry: dict | None = None,
) -> str:
    """Build the combined `devices/paired.json` content for a per-user container.

    The paired.json file is the container's trust store — it maps each
    pre-approved device public key to a role + scope list, and the gateway
    rejects any connect request whose signed device identity isn't in this
    file (see OpenClaw 4.5 `handshake-auth-helpers.ts:244-281`).

    We write TWO entries per container by default:
    - **node** — the in-container agent's loopback identity (role: "node",
      no scopes — the node role has its own RPC surface, see
      `method-scopes.ts:NODE_ROLE_METHODS`).
    - **operator** — our Python backend's remote identity, used to make
      scoped RPCs like `sessions.list` and `chat.send` against the gateway
      from outside the container. Required since OpenClaw 4.5; without it
      the backend's self-declared scopes are silently cleared.

    The operator entry is passed in pre-built (from
    `core.crypto.operator_device.build_paired_operator_entry`) so this
    module doesn't need to import the crypto helpers. Pass `None` for
    pre-4.5 behavior (node-only paired.json).
    """
    entries = {
        node_device_id: _build_node_paired_entry(node_device_id, node_public_key_b64),
    }
    if operator_entry is not None:
        entries[operator_entry["deviceId"]] = operator_entry
    return json.dumps(entries, indent=2)


def _build_exec_policy() -> dict:
    """Backend-controlled exec approval policy.

    Returns the fragment that lives under ``tools`` (so callers can
    spread it into a ``tools`` dict). Kept as its own helper so the
    initial ``write_openclaw_config`` write and the ``PATCH /debug/
    provision`` deep-merge path produce identical policy — one source
    of truth.

    * security="allowlist": unknown commands are gated by an approval.
    * ask="on-miss": emit exec.approval.requested on allowlist misses
      so the in-chat approval card (#305) can decide.

    Without this OpenClaw defaults to security="deny", which blocks
    every exec call silently (openclaw/src/agents/exec-defaults.ts:98).
    """
    return {
        "exec": {
            "security": "allowlist",
            "ask": "on-miss",
        },
    }


def build_provider_block(
    *,
    provider_choice: str,
    user_id: str,
    byo_provider: str | None,
) -> tuple[dict, dict, dict]:
    """Build (models.providers, agents.defaults.model, plugins.entries) for the
    user's signup choice.

    Public so the post-onboarding change-provider hot path (billing_service
    .change_provider via config_patcher.replace_provider_block) can reuse
    the same shape that ``write_openclaw_config`` writes at provision time.
    Single source of truth for the provider-block contents — no per-call
    site can drift from it.

    Per spec §4.2 (flat-fee pivot, 2026-04). API keys are NEVER embedded in
    openclaw.json — they're injected via ECS task definition secrets at task
    start. The Bedrock branch ships a static catalog matched to
    ``bedrock_pricing.py``; discovery is explicitly disabled so we never
    surface a model we haven't priced.

    Returns:
        ``(providers_config, default_model, plugin_entries)``.
    """
    if provider_choice == "chatgpt_oauth":
        # OpenClaw's openai-codex provider has no JSON config knob for the
        # auth.json directory — it reads the CODEX_HOME env var (default
        # ${HOME}/.codex). The per-user CODEX_HOME pointing at the EFS-staged
        # auth.json is set in ecs_manager._provider_environment_for_user().
        # We deliberately omit any provider entry so the bundled provider
        # plugin's defaults apply; writing an empty {} would still trip the
        # base-schema validator that requires `baseUrl` + `models`.
        return (
            {},
            {"primary": "openai-codex/gpt-5.5", "fallbacks": ["openai-codex/gpt-5.5"]},
            {},
        )
    if provider_choice == "byo_key":
        if byo_provider == "openai":
            return ({}, {"primary": "openai/gpt-5.4", "fallbacks": ["openai/gpt-5.4"]}, {})
        if byo_provider == "anthropic":
            # Opus 4.6 primary, Sonnet 4.6 fallback. Opus 4.7 is omitted
            # while AWS holds the cross-region TPM quota at applied=0
            # (Service Quotas L-5DB28B7B); add it back here once the
            # quota lifts. For byo_key the user pays Anthropic directly
            # so the Bedrock quota doesn't bind, but we keep the catalog
            # symmetric across paths to avoid divergent test/runtime
            # behavior.
            return (
                {},
                {
                    "primary": "anthropic/claude-opus-4-6-v1",
                    "fallbacks": ["anthropic/claude-sonnet-4-6"],
                },
                {},
            )
        raise ValueError(f"byo_provider must be 'openai' or 'anthropic' for byo_key, got {byo_provider!r}")
    if provider_choice == "bedrock_claude":
        # Static catalog — exactly the models we price in
        # core/billing/bedrock_pricing.py. Discovery is OFF: the per-user
        # container's task role only has bedrock:InvokeModel, and surfacing
        # any model we haven't priced would either fail credit deduction
        # (UnknownModelError) or skip billing entirely. The pricing table
        # is the source of truth; this catalog mirrors it 1:1.
        #
        # IDs use the `us.` inference-profile prefix because every Claude
        # 4.x on Bedrock is INFERENCE_PROFILE-only — bare foundation-model
        # IDs (e.g. "anthropic.claude-opus-4-7") return ValidationException
        # "Invocation … with on-demand throughput isn't supported." The `us.`
        # form (vs `global.`) matches the canonical example in upstream's
        # docs/providers/bedrock.md and keeps inference within US regions.
        # bedrock_pricing.get_rate strips the prefix before lookup so the
        # rate table stays keyed on the bare model id.
        bedrock_models = [
            {
                "id": "us.anthropic.claude-opus-4-6-v1",
                "name": "Claude Opus 4.6",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 15.0, "output": 75.0, "cacheRead": 1.5, "cacheWrite": 18.75},
                "contextWindow": 1_000_000,
                "maxTokens": 8192,
            },
            {
                "id": "us.anthropic.claude-sonnet-4-6",
                "name": "Claude Sonnet 4.6",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 3.0, "output": 15.0, "cacheRead": 0.3, "cacheWrite": 3.75},
                "contextWindow": 1_000_000,
                "maxTokens": 8192,
            },
        ]
        return (
            {
                "amazon-bedrock": {
                    "baseUrl": f"https://bedrock-runtime.{settings.AWS_REGION}.amazonaws.com",
                    "api": "bedrock-converse-stream",
                    "auth": "aws-sdk",
                    "models": bedrock_models,
                },
            },
            {
                # Opus 4.6 primary, Sonnet 4.6 fallback.
                #
                # Why 4.6 instead of 4.7: AWS ships Opus 4.7 with
                # `applied = 0` TPM on most accounts pending capacity
                # rollout (Service Quotas L-5DB28B7B), so 4.7 invokes
                # throttle on the very first request. Until AWS lifts
                # the applied limit, 4.6 is the only Opus we can
                # actually invoke. Pricing is identical between 4.6
                # and 4.7 on Bedrock list price, so swapping is
                # cost-neutral. Add 4.7 back to the fallbacks (or
                # promote to primary) once the quota lands.
                #
                # Sonnet 4.6 stays as the capacity/throttling escape
                # hatch (requires a Marketplace agreement first; see
                # Bedrock Model Access console). Sonnet is ~5× cheaper
                # than Opus so a failover quietly REDUCES spend rather
                # than increasing it — safe direction for surprise
                # bills.
                #
                # Users can override per-agent via agents.list[*].model.
                "primary": "amazon-bedrock/us.anthropic.claude-opus-4-6-v1",
                "fallbacks": ["amazon-bedrock/us.anthropic.claude-sonnet-4-6"],
            },
            {
                "amazon-bedrock": {
                    "config": {"discovery": {"enabled": False}},
                },
            },
        )
    raise ValueError(f"Unknown provider_choice: {provider_choice!r}")


def _build_agents_list(paperclip_agents: list[dict]) -> list[dict]:
    """Build the openclaw.json agents.list entries from Paperclip agents.

    If paperclip_agents is empty, fall back to the legacy hardcoded 'main'
    agent so a freshly-provisioned container still works (e.g. when
    Paperclip provisioning failed silently and the user opens /chat
    before the next reprovision).

    Otherwise emit one entry per Paperclip agent. The agent with
    role='ceo' is marked default; if no agent has that role, the first
    one in the list wins. Each agent's workspace is keyed by its
    Paperclip UUID so OpenClaw's session-key routing
    (agent:<uuid>:<rest>) lands in the right directory on EFS.
    """
    if not paperclip_agents:
        return [
            {
                "id": "main",
                "default": True,
                "reasoningDefault": "stream",
                "workspace": "/home/node/.openclaw/workspaces/main",
            },
        ]

    default_index = next(
        (i for i, a in enumerate(paperclip_agents) if a.get("role") == "ceo"),
        0,
    )

    entries: list[dict] = []
    for i, agent in enumerate(paperclip_agents):
        agent_id = agent["id"]
        entry: dict = {
            "id": agent_id,
            "reasoningDefault": "stream",
            "workspace": f"/home/node/.openclaw/workspaces/{agent_id}",
        }
        # Pass through the friendly display name when Paperclip has one.
        # OpenClaw's agent-runtime schema accepts an optional ``name`` on
        # agents.list[] entries (zod-schema.agent-runtime.ts:816); the
        # chat surface and the frontend sidebar both render this when
        # present. Without it, OpenClaw falls back to the agentId (UUID),
        # which surfaces as a long string in the chat sidebar instead of
        # the agent's role-given name (e.g. Friday for the seeded ceo).
        friendly_name = agent.get("name")
        if friendly_name:
            entry["name"] = friendly_name
        if i == default_index:
            entry["default"] = True
        entries.append(entry)
    return entries


def build_openclaw_config_dict(
    *,
    user_id: str,
    gateway_token: str,
    provider_choice: str,
    byo_provider: str | None = None,
    paperclip_agents: list[dict] | None = None,
    hook_token: str | None = None,
    owner_type: str = "personal",
) -> dict:
    """Build the openclaw.json config dict for a per-user container.

    Pure (no I/O) so it can be unit-tested without filesystem mocks.

    The provider block (``models.providers`` + ``agents.defaults.model`` +
    ``plugins.entries``) is selected by ``provider_choice``. Everything
    else (gateway, agents.defaults, agents.list, memory, tools, hooks,
    channels, browser, nodeHost, update) is the same across signup paths
    — these are sections OpenClaw refuses to start without (e.g.
    ``gateway.mode``).

    ``hook_token`` is threaded through the signature for symmetry with
    ``gateway_token`` and to give a future rotate-flow somewhere to hand
    the new token without ECS re-registers. It is intentionally NOT
    interpolated into the JSON: the ``hooks.token`` field renders as the
    literal placeholder ``${OPENCLAW_HOOK_TOKEN}`` which OpenClaw expands
    at boot from the per-user ECS task-definition env var (set by T2).
    Plaintext tokens MUST NEVER land in this file — it sits on EFS.

    Note: ``hook_token`` is intentionally unused inside this function —
    interpolating it would defeat the env-var indirection and write
    plaintext to EFS. DO NOT "fix" this by replacing the placeholder
    string with the parameter value.
    """
    # Reprovision flows pull gateway_token from the existing container row;
    # legacy rows missing the field would silently write
    # `{"mode":"token","token":null}`, leaving the gateway unauthable.
    # Fail fast — caller must regenerate via secrets.token_urlsafe before retry.
    if not gateway_token:
        raise ValueError("gateway_token must be a non-empty string")

    providers_config, default_model, plugin_entries = build_provider_block(
        provider_choice=provider_choice,
        user_id=user_id,
        byo_provider=byo_provider,
    )

    # Token auth — shared secret between backend and container.
    # Trusted-proxy mode explicitly blocks loopback connections (OpenClaw #17761),
    # which breaks the local agent's ability to call its own gateway for node
    # discovery and other internal RPCs. Token mode works for both our backend
    # (VPC -> container) and the in-container agent (loopback -> container).
    # Network isolation (private VPC subnet) provides the transport boundary;
    # user identity is implicit since each container is per-user.
    auth = {
        "mode": "token",
        "token": gateway_token,
    }

    config = {
        # Synthetic meta defeats openclaw's "missing-meta-vs-last-good"
        # auto-restore in src/config/io.observe-recovery.ts:425. That
        # check treats every backend overwrite as anomalous (openclaw
        # adds its own meta after first boot; our overwrites strip it),
        # silently restoring from .bak and clobbering whatever we wrote.
        # Use the real upstream version so warnIfConfigFromFuture
        # (io.ts:885) parses it as semver and short-circuits cleanly.
        "meta": {"lastTouchedVersion": OPENCLAW_UPSTREAM_VERSION},
        "gateway": {
            "mode": "local",
            "bind": "lan",
            "auth": auth,
            "trustedProxies": ["10.0.0.0/8", "127.0.0.1", "::1"],
            "controlUi": {
                "enabled": False,
            },
            "http": {
                "endpoints": {
                    "chatCompletions": {"enabled": False},
                },
            },
            # Per-owner browser-routing posture. `auto` lets browser tool
            # calls auto-route to a connected node when one's paired —
            # safe for personal owners (one human, one Mac, no
            # ambiguity). `manual` forces explicit `target: "node"` +
            # `node: <id>` per call — required for orgs because
            # selectDefaultNodeFromList in extensions/browser has no
            # session-ownership awareness, so auto-routing in an org
            # with two paired Macs could send a Telegram peer's
            # request to the wrong member's Chrome.
            "nodes": {
                "browser": {
                    "mode": "auto" if owner_type == "personal" else "manual",
                },
                # Per-platform node-command allowlist (Stage 2 of OpenClaw's
                # node-invoke gate, docs/nodes/index.md:193-203). Without
                # an explicit list, macOS nodes only accept canvas.*,
                # camera.list, location.get, screen.snapshot and reject
                # everything else with `INVALID_REQUEST: node command not
                # allowed`. Mirror the commands our Rust client advertises
                # in apps/desktop/src-tauri/src/node_client.rs (Stage 1
                # gate); adding entries here without the client declaring
                # them is a no-op since Stage 1 still rejects.
                #
                # File-transfer commands (dir.list, file.fetch, file.write,
                # dir.fetch) are intentionally omitted — our Rust client
                # doesn't bundle the file-transfer plugin yet.
                "allowCommands": [
                    "system.run",
                    "system.run.prepare",
                    "system.which",
                    "system.notify",
                    "system.execApprovals.get",
                    "system.execApprovals.set",
                    "browser.proxy",
                    "device.info",
                    "device.status",
                    "device.health",
                    "device.permissions",
                ],
            },
        },
        "models": {
            "providers": providers_config,
        },
        "agents": {
            "defaults": {
                "model": default_model,
                # Stubs preserved from a v2026.4.22-era constraint where
                # AgentDefaultsSchema referenced these schemas without
                # .optional(). At v2026.5.7 each schema (Heartbeat,
                # AgentContextLimits, AgentEmbeddedHarness) ends in
                # .optional() so emitting them is no longer required.
                # Kept as explicit no-op defaults; safe to delete in a
                # follow-up cleanup PR. None of these features are
                # opted into (no heartbeat cadence, no context-limit
                # caps, no embedded-harness override).
                "embeddedHarness": {},
                "contextLimits": {},
                "heartbeat": {},
                "workspace": "/home/node/.openclaw/workspaces",
                # Memory embedding provider, kept pinned to Bedrock so any
                # future plugin that does want embeddings inherits a sane
                # default. Today's `builtin` backend doesn't use embeddings
                # — it's flat MEMORY.md files. Without an explicit provider,
                # openclaw defaults to "local" (GGUF we don't ship), which
                # historically caused hangs.
                "memorySearch": {
                    "enabled": True,
                    "provider": "bedrock",
                    "model": "amazon.titan-embed-text-v2:0",
                },
                # `agents.defaults.llm.idleTimeoutSeconds` was removed from
                # AgentDefaultsSchema between v2026.4.23 and v2026.5.7 (see
                # upstream src/config/zod-schema.agent-defaults.ts diff). We
                # used to set 300s here; OpenClaw v2026.5.7's strict() schema
                # rejects the unknown key and the gateway exits with
                # `Unrecognized key: "llm"`. Default idle timeout is fine.
                # verboseDefault="full" keeps tool result/partialResult in
                # agent events so the frontend can show tool input + output.
                # OpenClaw defaults to "off" which strips those fields before
                # they reach our WebSocket subscriber.
                "verboseDefault": "full",
            },
            # Per-agent settings. reasoningDefault is not allowed in
            # agents.defaults (see openclaw zod-schema.agent-defaults.ts), so
            # we declare the implicit "main" agent explicitly here to opt it
            # into real-time thinking streams. User-created agents inherit it
            # via AgentCreateForm passing reasoningDefault: "stream" on
            # agents.create.
            # Per-agent settings keyed by Paperclip UUID. _build_agents_list
            # falls back to a single legacy "main" entry when paperclip_agents
            # is empty/None so unprovisioned containers (and tests that don't
            # pass the kwarg) keep working.
            #
            # Absolute workspace paths so path.resolve() returns them unchanged
            # regardless of process cwd (agent exec tools run with
            # cwd=workspaceDir, which breaks relative resolution).
            "list": _build_agents_list(paperclip_agents or []),
        },
        # Memory backend: "builtin" = flat MEMORY.md files in the agent
        # workspace. The qmd backend was removed 2026-05-02 because qmd's
        # better-sqlite3 store on EFS deadlocks the gateway's V8 main
        # thread when the NFS client retries forever (`rpc_wait_bit_killable`).
        # Symptoms: container appears alive (TCP port still listening) but
        # every WS upgrade times out — 10h outage on 2026-05-01.
        # Builtin reads/writes plain markdown files: same EFS underneath,
        # but only on explicit agent recall, not via a long-lived sqlite
        # connection that's constantly hitting the disk. Future semantic
        # memory work should use a non-EFS-backed plugin (memory-lancedb
        # with a non-EFS data dir, or Aurora pgvector).
        "memory": {
            "backend": "builtin",
            "citations": "auto",
        },
        "tools": {
            "profile": "full",
            # Default-deny both canvas and nodes. node_proxy.py toggles
            # "nodes" back to enabled dynamically when a desktop node
            # pairs, and back to denied when the last one disconnects.
            # Leaving nodes always-allowed here would expose the tool
            # even to users without the desktop app.
            "deny": ["canvas", "nodes"],
            **_build_exec_policy(),
            "web": {
                "fetch": {"enabled": True},
            },
            "media": {
                "image": {"enabled": True},
                "audio": {"enabled": False},
                "video": {"enabled": False},
            },
        },
        "skills": {
            "install": {
                "nodeManager": "npm",
            },
        },
        "hooks": {
            "internal": {
                "entries": {
                    "command-logger": {"enabled": True},
                    "session-memory": {"enabled": True},
                },
            },
            # Hook ingress (OpenClaw v2026.5.7 gateway/hooks.ts). Lets
            # external systems POST to ``/hooks`` with the shared token
            # to drive agents — Stripe/GitHub webhooks, scheduled
            # routines, etc. The plaintext ``hook_token`` is NEVER written
            # here; ``${OPENCLAW_HOOK_TOKEN}`` is a literal placeholder
            # OpenClaw expands at boot from the per-user ECS task-definition
            # env var (injected by T2 in ecs_manager._build_per_user_environment).
            "enabled": True,
            "token": "${OPENCLAW_HOOK_TOKEN}",
            "path": "/hooks",
            # Upstream's pre-auth body cap is 64 KB — far too small for
            # real-world webhook traffic. Stripe routinely emits ~120 KB
            # invoice payloads; GitHub PR webhooks can exceed 500 KB on
            # large diffs. 1 MB matches the documented upper bound for
            # both providers.
            "maxBodyBytes": 1_048_576,
            # OpenClaw v2026.5.7 (gateway/hooks.ts:75-95) requires
            # "hook:" in this list when ``defaultSessionKey`` is unset
            # (we don't set it above). Omitting "hook:" makes the gateway
            # throw at boot — every newly-provisioned container would
            # refuse to start. DO NOT REMOVE. "isol8-" is for our own
            # session-key conventions used by internal callers.
            "allowedSessionKeyPrefixes": ["isol8-", "hook:"],
            # Lock the request to server-derived session keys only;
            # untrusted callers don't get to claim arbitrary session
            # ownership via a header.
            "allowRequestSessionKey": False,
        },
        "plugins": {
            "slots": {},
            "entries": plugin_entries,
        },
        # Channels: we ship every supported provider as `enabled: true`
        # so the plugin is loaded into the gateway at startup.
        # OpenClaw's reload plan treats `channels.{id}` as a hot-reload
        # prefix ONLY when the channel plugin is already running — on
        # the very first enable of a never-before-loaded channel it
        # escalates to a full gateway restart (~6 min on Fargate).
        # Shipping the plugins hot at provision time means every
        # subsequent token/account change is a fast per-channel
        # restart, not a gateway restart. Plugins with no `accounts`
        # entries sit idle safely.
        "channels": {
            # REVERTED 2026-05-12: PR #616 added a `channels.webchat` entry
            # here to flip the system-prompt builder to Mode A (rely on
            # native approval cards instead of telling Ray to print
            # `/approve <id> allow-once` in chat). It never worked.
            #
            # Upstream's config validator at src/config/validation.ts:1260
            # rejects any `channels.<id>` whose id is not in
            # `[...CHANNEL_IDS, ...registry.plugins[].channels]`. "webchat"
            # is neither bundled (src/channels/ids.ts) nor a plugin-backed
            # channel, so both startup validation AND chokidar hot-reload
            # rejected the block (`config reload skipped (invalid config):
            # channels.webchat: unknown channel id: webchat`). The runtime
            # config never picked up the capability — Ray kept emitting the
            # CLI fallback the entire time, and FRESH provisions boot-
            # looped (cofounder onboarding 2026-05-12 04:25 UTC was the
            # symptom that exposed this).
            #
            # The native-approval system prompt builder has three paths to
            # Mode A (src/agents/system-prompt.ts:187-191):
            #   (1) runtimeCapabilities.has("inlinebuttons")
            #   (2) runtimeCapabilities.has("nativeapprovals")
            #   (3) runtimeChannel in {discord,matrix,qqbot,slack,telegram}
            # Paths (1)/(2) source from `cfg.channels[X].capabilities` —
            # which still has to pass the validator's channel-id allowlist.
            # Path (3) is a hardcoded set we don't own.
            #
            # Real-fix options (none clean, all need a separate PR):
            #   B. Pass `originatingChannel: "telegram"` from chat.send so
            #      path (3) fires — side effect: upstream may attempt to
            #      route replies through telegram-config, needs testing.
            #   C. Submit an upstream PR adding "webchat" to CHANNEL_IDS +
            #      KNOWN_NATIVE_APPROVAL_PROMPT_CHANNELS, bump
            #      openclaw-version.json once it merges.
            #   D. Bump upstream to a tag where webchat is registered.
            "telegram": {
                "enabled": True,
                "dmPolicy": "pairing",
            },
            "discord": {
                "enabled": True,
                "dmPolicy": "pairing",
            },
            "slack": {
                "enabled": True,
                "dmPolicy": "pairing",
            },
        },
        "session": {
            "dmScope": "per-account-channel-peer",
        },
        "web": {
            "enabled": True,
        },
        "browser": {
            # Two profiles always defined; the `user` profile is
            # statically declared but only routable when a desktop node
            # is paired (extensions/browser/src/browser-tool.ts:243
            # filters by node.connected). Default is `openclaw` so
            # not-yet-bound sessions (channels, cron, unpaired UI) get
            # container Chromium — bundled in the extended image since
            # PR #596.
            "enabled": True,
            "defaultProfile": "openclaw",
            # Required because the gateway runs as root inside the
            # per-user ECS container (for oom-score-adj and EFS perms),
            # and Chromium refuses to start as root with the security
            # sandbox enabled. The container itself is the security
            # boundary in this architecture (per-user, single-tenant,
            # network-isolated), so the renderer-process sandbox isn't
            # protecting against anything we'd actually face. Without
            # this, OpenClaw's managed driver fails with "Running as
            # root without --no-sandbox is not supported."
            "noSandbox": True,
            "profiles": {
                "openclaw": {
                    "driver": "openclaw",
                    "color": "#FF4500",
                    # Required since OpenClaw v2026.5.7-0d10b38 — upstream
                    # zod-schema.ts:483 now refines any non-`existing-session`
                    # profile to require cdpPort or cdpUrl, with the message
                    # "Profile must set cdpPort or cdpUrl". Without it the
                    # gateway exits at boot with `Gateway failed to start:
                    # Invalid config at /home/node/.openclaw/openclaw.json`
                    # and the container loops failed-to-start forever.
                    # 18800 matches upstream's documented default for the
                    # bundled-Chromium openclaw driver (docs/tools/
                    # browser-linux-troubleshooting.md, chrome.test.ts:921).
                    # Each container is per-user-isolated so collisions
                    # can't happen.
                    "cdpPort": 18800,
                },
                "user": {
                    "driver": "existing-session",
                    # Required since OpenClaw bumped its config schema —
                    # `browser.profiles.*.color` is now a required string.
                    # Surfaces in the OpenClaw UI as the profile chip.
                    # No cdpPort/cdpUrl needed — `existing-session` is
                    # the one driver upstream's schema exempts from the
                    # require-cdp check (zod-schema.ts:483).
                    "color": "#0066FF",
                },
            },
        },
        "update": {"checkOnStart": False},
        # Enable the chat-command surfaces the owner needs to manage their
        # own container from chat. All three are off-by-default upstream
        # because they're privileged surfaces that should not be exposed
        # in shared/public deployments — but per-user Isol8 containers
        # are owner-operated, so flipping them on is the right call.
        # Skipped:
        #   - /config — too broad; the control panel + config_patcher own
        #     config writes, and we don't want chat to mutate provider /
        #     billing-relevant keys.
        #   - /bash — host-shell access from chat. Channel-linked accounts
        #     (Telegram/Discord/Slack) widen the trust boundary too far
        #     even with allowFrom narrowing; revisit if there's a clear
        #     ask with a tighter scoping story.
        "commands": {
            "mcp": True,  # /mcp — list/set/unset MCP servers (same data as the MCP Servers panel)
            "plugins": True,  # /plugins — list discovered plugins + toggle enablement
            "debug": True,  # /debug — runtime-only overrides for diagnostics (not persisted)
        },
    }

    return config


async def write_openclaw_config(
    *,
    config_path: Path,
    gateway_token: str,
    provider_choice: str,
    user_id: str,
    byo_provider: str | None = None,
    paperclip_agents: list[dict] | None = None,
    hook_token: str | None = None,
    owner_type: str = "personal",
) -> None:
    """Write the user's openclaw.json with the full container config.

    Per spec §4.2 (flat-fee pivot, 2026-04). Tier gating is removed —
    one config shape per ``provider_choice``. The OPENAI/ANTHROPIC API
    keys are NEVER written into this file; they're injected via ECS
    task definition secrets at task start.

    Args:
        config_path: Where on disk to write the JSON file. Parent dirs
            are created if missing.
        gateway_token: Shared secret for container auth. Required because
            ``gateway.auth.mode = "token"``.
        provider_choice: One of ``"chatgpt_oauth"``, ``"byo_key"``,
            ``"bedrock_claude"``.
        user_id: Owner ID; surfaced for completeness — the chatgpt_oauth
            path no longer carries it into the config (auth.json directory
            is set via the CODEX_HOME env var on the per-user ECS task).
        byo_provider: ``"openai"`` or ``"anthropic"`` — required when
            ``provider_choice == "byo_key"``.
        paperclip_agents: Optional list of Paperclip agent dicts to use
            for ``agents.list``. Omitting it preserves the legacy ``"main"``
            entry for backward compatibility with callers we haven't updated.
        hook_token: Optional per-user OpenClaw webhook auth token. Threaded
            through for signature symmetry / future rotate plumbing — NOT
            interpolated into the JSON. The rendered ``hooks.token`` is the
            literal ``${OPENCLAW_HOOK_TOKEN}`` placeholder which OpenClaw
            expands at boot from the per-user ECS task-definition env var.
            Note: intentionally unused inside this function — interpolating
            it would defeat the env-var indirection and write plaintext
            to EFS.

    Raises:
        ValueError: when ``provider_choice`` is unknown or
            ``byo_provider`` is missing/invalid for ``byo_key``.
    """
    config = build_openclaw_config_dict(
        user_id=user_id,
        gateway_token=gateway_token,
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        paperclip_agents=paperclip_agents,
        hook_token=hook_token,
        owner_type=owner_type,
    )

    def _write() -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2))

    await asyncio.to_thread(_write)


def merge_openclaw_config(
    existing_config: dict,
    updates: dict,
) -> dict:
    """Merge partial updates into an existing openclaw.json config dict.

    Performs a shallow merge at the top-level section keys (gateway, models,
    agents, tools, browser, update). Nested dicts within each section are
    deep-merged.

    Args:
        existing_config: Current openclaw.json as dict.
        updates: Partial config dict with sections to update.

    Returns:
        Merged config dict.
    """
    merged = dict(existing_config)
    for key, value in updates.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


async def read_openclaw_config_from_efs(owner_id: str) -> dict | None:
    """Read and parse openclaw.json directly from EFS.

    Works even when the container is scaled down (no gateway RPC needed).
    Returns None if the file doesn't exist yet.
    """
    import asyncio
    import json
    import os

    from core.config import settings

    config_path = os.path.join(settings.EFS_MOUNT_PATH, owner_id, "openclaw.json")
    if not os.path.exists(config_path):
        return None

    def _read():
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "Failed to parse openclaw.json for owner %s: %s",
                owner_id,
                e,
            )
            return None

    return await asyncio.to_thread(_read)
