"""
OpenClaw configuration generator for per-user containers.

Generates openclaw.json with the provider block determined by the
user's signup choice (provider_choice). Each container gets a gateway
auth token so it can bind to LAN (required for Docker port mapping).
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path

from core import openclaw_version
from core.config import settings

logger = logging.getLogger(__name__)

# OpenClaw version of the extended image actually DEPLOYED to this environment,
# resolved from openclaw-version.json#dev.tag / #prod.tag via
# core.openclaw_version (CDK-injected env var in deployed containers, repo JSON
# in local dev / CI). Used as the `meta.lastTouchedVersion` value openclaw
# expects in our generated config: defeats `missing-meta-vs-last-good`
# auto-restore (src/config/io.observe-recovery.ts:425) and must match what
# openclaw itself self-writes (io.ts:879 uses `VERSION` from package.json), or
# warnIfConfigFromFuture (io.ts:885) fires. Deliberately the DEPLOYED image's
# version, NOT the build-target `tag` — `tag` can be bumped ahead of a per-env
# promotion, and a provision in that window must not stamp a future version.
DEPLOYED_UPSTREAM_VERSION = openclaw_version.deployed_upstream_version()

# Channel provider ids we provision a config block for, and whose accounts must
# survive a reprovision (read back by ecs_manager._provision_channel_accounts).
# Single source of truth — the channels block in build_openclaw_config_dict is
# built from this, and ecs_manager imports it, so the two can't drift.
CHANNEL_PROVIDER_IDS = ("telegram", "discord", "slack")


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


def _build_node_paired_entry(
    device_id: str,
    public_key_b64: str,
    declared_commands: list[str] | None = None,
) -> dict:
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
        # Pre-approved commands list — `reconcileNodePairingOnConnect`
        # in upstream's gateway/node-connect-reconcile.ts compares the
        # device's declared commands against this on every reconnect.
        # If the declared list has anything not here, the gateway opens
        # a pending pairing request AND zeros out effectiveCommands —
        # which makes `exec host=node` fail with "node doesn't support
        # system.run". Mirror whatever the desktop just declared.
        "commands": declared_commands or [],
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


async def ensure_node_paired_entry(
    *,
    owner_id: str,
    public_key_pem: str,
    client_id: str,
    display_name: str,
    commands: list[str],
    workspace,  # core.services.desktop_pair.WorkspaceLike
) -> bool:
    """Idempotently register a node device in the owner's ``paired.json``.

    Pre-2026-05 the backend held device private keys on EFS and derived the
    pubkey before writing this entry. After the two-node architecture
    cutover the keypair lives on the user's Mac (macOS Keychain) — the
    desktop sends the pubkey directly to ``POST /desktop/pair`` and the
    backend just writes it into paired.json.

    On disk paired.json is a FLAT ``Record<deviceId, PairedDevice>`` —
    upstream's ``writeJson(pairedPath, state.pairedByDeviceId)`` only
    persists the inner record (``openclaw/src/infra/device-pairing.ts:181``).
    The ``pairedByDeviceId`` wrapper is in-memory only and MUST NOT be
    written.

    Concurrency: serialization is delegated to the workspace's
    ``mutate_paired_json`` primitive. The real adapter
    (``EfsWorkspaceAdapter``) holds an exclusive ``fcntl.lockf`` on
    ``<owner>/devices/paired.json.lock`` for the full read-modify-write
    window — so the in-container node's first-connect self-registration,
    the ``/desktop/pair`` flow, and admin-side writers all serialize on
    the same lock and never clobber each other.

    Returns True if the entry was new/changed (write occurred), False if it
    was already present with the same pubkey (idempotent no-op).
    """
    # Import here to avoid a circular import between core.containers.config
    # and core.services.desktop_pair (which already pulls from
    # core.containers.workspace).
    from core.services.desktop_pair import build_paired_entry

    entry = build_paired_entry(
        pubkey_pem=public_key_pem,
        client_id=client_id,
        display_name=display_name,
        commands=commands,
    )

    wrote = {"value": False}

    def _mutate(paired: dict) -> bool:
        existing = paired.get(entry["deviceId"])
        # The stored ``publicKey`` is the base64url-no-pad raw 32 bytes
        # (see ``build_paired_entry`` — must match upstream's
        # ``publicKeyRawBase64UrlFromPem`` wire format). Compare against
        # the freshly-built entry's pubkey, NOT the raw PEM input, or
        # idempotency always fails and we clobber the existing tokens.
        if existing and existing.get("publicKey") == entry["publicKey"]:
            return False  # Idempotent — preserve existing tokens / metadata.
        paired[entry["deviceId"]] = entry
        wrote["value"] = True
        return True

    await workspace.mutate_paired_json(_mutate)
    if wrote["value"]:
        logger.info(
            "Registered node device_id %s in paired.json for owner %s",
            entry["deviceId"][:16],
            owner_id,
        )
    return wrote["value"]


def build_device_paired_json(
    node_device_id: str,
    node_public_key_b64: str,
    *,
    operator_entry: dict | None = None,
    extra_operator_entries: list[dict] | None = None,
) -> str:
    """Build the combined `devices/paired.json` content for a per-user container.

    The paired.json file is the container's trust store — it maps each
    pre-approved device public key to a role + scope list, and the gateway
    rejects any connect request whose signed device identity isn't in this
    file (see OpenClaw 4.5 `handshake-auth-helpers.ts:244-281`).

    Entries written:
    - **node** — the in-container agent's loopback identity (role: "node",
      no scopes — the node role has its own RPC surface, see
      `method-scopes.ts:NODE_ROLE_METHODS`).
    - **operator** — our Python backend's remote identity, used to make
      scoped RPCs like `sessions.list` and `chat.send` against the gateway
      from outside the container. Required since OpenClaw 4.5; without it
      the backend's self-declared scopes are silently cleared.
    - **extra operators** — one per Paperclip agent bound to this container, so
      each agent's own device clears the remote-operator pairing wall on its
      first relayed connect (it is otherwise rejected NOT_PAIRED). Built the same
      way as the backend operator entry; the writer enumerates them at provision
      so a reprovision (which overwrites paired.json) re-includes them.

    Operator entries are passed in pre-built (from
    `core.crypto.operator_device.build_paired_operator_entry`) so this module
    doesn't need to import the crypto helpers. Pass `None`/empty for pre-4.5
    behavior (node-only paired.json).
    """
    entries = {
        node_device_id: _build_node_paired_entry(node_device_id, node_public_key_b64),
    }
    if operator_entry is not None:
        entries[operator_entry["deviceId"]] = operator_entry
    for entry in extra_operator_entries or []:
        entries[entry["deviceId"]] = entry
    return json.dumps(entries, indent=2)


def _build_exec_policy() -> dict:
    """Backend-controlled exec approval policy.

    Returns the fragment that lives under ``tools`` (so callers can
    spread it into a ``tools`` dict). Kept as its own helper so the
    initial ``write_openclaw_config`` write and the ``PATCH /debug/
    provision`` deep-merge path produce identical policy — one source
    of truth.

    mode="full": exec runs without an approval gate. The per-user
    container is an isolated single-tenant sandbox AND its agents run
    autonomously via the Paperclip openclaw_gateway adapter (issue
    assignment, routines, manual wake) with NO operator connected to
    answer an approval. The previous ``security="allowlist" / ask="on-miss"``
    policy made every non-allowlisted exec in an autonomous run hang on
    an approval that never arrives, until the 120s gateway timeout —
    Paperclip then re-dispatched, looping forever (every heartbeat_run
    ``timed_out`` with ``blocked_tool_call activeTool=exec``). This
    mirrors Paperclip's own design: every local adapter defaults to
    skipping permission prompts for unattended agents
    (``dangerouslySkipPermissions``/``yolo``), "required for headless
    runs where interactive approval is impossible". ``full`` is the
    openclaw-gateway equivalent.

    ``mode`` is mutually exclusive with ``security``/``ask`` (the config
    schema rejects combining them). Valid ``mode`` values: deny |
    allowlist | ask | auto | full. ``auto`` (allowlist + LLM
    auto-review) would keep a safety layer without a human, but it
    requires a ``tools.exec.reviewer`` model to be wired, which the
    deployed image does not have — so it falls back to hanging.

    Without any policy OpenClaw defaults to security="deny", which blocks
    every exec call silently (openclaw/src/agents/exec-defaults.ts:98).
    """
    return {
        "exec": {
            "mode": "full",
        },
    }


def build_provider_block(
    *,
    provider_choice: str,
    user_id: str,
    byo_provider: str | None,
    aip_arns: dict[str, str] | None = None,
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
        # ChatGPT OAuth resolves through OpenClaw's bundled `openai` provider
        # plugin, whose model catalog (gpt-5.5 / gpt-5.4-mini / …) is baked
        # into its manifest as a STATIC catalog — so `openai/gpt-5.5` resolves
        # with no explicit provider entry. We deliberately emit NO
        # `models.providers.openai` block: an explicit override (baseUrl +
        # legacy responses/completions api) would SHADOW the built-in OAuth
        # provider path. The OAuth credentials are read from the EFS-staged
        # credentials/oauth.json (see workspace.pre_stage_auth_profile_store).
        #
        # NB: `openai-codex/*` is a RETIRED ref at the deployed image — the
        # `openai` plugin subsumes it. Emitting it produces "Unknown model:
        # openai-codex/…" at first chat (nothing migrates it at runtime; only
        # `openclaw doctor --fix` would). The canonical ref is `openai/*`.
        return (
            {},
            {"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.5"]},
            {},
        )
    if provider_choice == "byo_key":
        if byo_provider == "openai":
            # gpt-5.4 primary (already the standard tier), gpt-5.4-mini
            # fallback. gpt-5.5 is the premium tier, reserved for the CEO
            # agent via a per-agent agents.list[*].model. Fallbacks are
            # error-only, so the cheaper mini never raises cost on failover.
            return ({}, {"primary": "openai/gpt-5.4", "fallbacks": ["openai/gpt-5.4-mini"]}, {})
        if byo_provider == "anthropic":
            # Sonnet 4.6 primary, Haiku 4.5 fallback — the cheaper standard
            # tier so the default agent never pays top-tier Opus rates on
            # every turn. Opus 4.6 is reserved for the CEO agent via a
            # per-agent agents.list[*].model. Kept symmetric with the Bedrock
            # path to avoid divergent test/runtime behavior.
            return (
                {},
                {
                    "primary": "anthropic/claude-sonnet-4-6",
                    "fallbacks": ["anthropic/claude-haiku-4-5"],
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
        # cost blocks are the AWS-verified `us.` Regional (CRIS) rates in
        # USD/MTok, with the 1-hour cacheWrite tier (cacheRetention is pinned
        # to "long", so every write bills at the 1h rate; upstream's cost block
        # carries a single cacheWrite rate, so it must be the 1h value). Kept
        # in lockstep with bedrock_pricing._RATES — see
        # test_config_cost_blocks_match_bedrock_pricing_rates. Task D7 will
        # source these from the AWS rate card automatically.
        #
        # FAIL-CLOSED: once the per-owner IAM role is scoped to AIP ARNs only,
        # emitting the system us.anthropic.* ids produces a config the role
        # cannot invoke -> every chat AccessDeniedException. Require the owner's
        # AIP ARNs here; raise loud at provision rather than silently at first
        # chat. ensure_owner_aips (bedrock_aip.py) is the upstream producer.
        if not aip_arns or not all(t in aip_arns for t in ("opus", "sonnet", "haiku")):
            raise ValueError(
                "bedrock_claude requires aip_arns with opus/sonnet/haiku keys "
                "(per-owner AIP ARNs). Run ensure_owner_aips before write_user_configs."
            )
        opus_id = aip_arns["opus"]
        sonnet_id = aip_arns["sonnet"]
        haiku_id = aip_arns["haiku"]
        bedrock_models = [
            {
                "id": opus_id,
                "name": "Claude Opus 4.6",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 5.5, "output": 27.5, "cacheRead": 0.55, "cacheWrite": 11.0},
                "contextWindow": 1_000_000,
                "maxTokens": 8192,
            },
            {
                "id": sonnet_id,
                "name": "Claude Sonnet 4.6",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 3.3, "output": 16.5, "cacheRead": 0.33, "cacheWrite": 6.6},
                "contextWindow": 1_000_000,
                "maxTokens": 8192,
            },
            {
                # Cheap aux-task tier — compaction / heartbeat / memory-flush.
                # The date-stamped inference-profile id is the only valid Haiku
                # 4.5 profile; the bare us.anthropic.claude-haiku-4-5 throws
                # ValidationException on invoke.
                "id": haiku_id,
                "name": "Claude Haiku 4.5",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 1.1, "output": 5.5, "cacheRead": 0.11, "cacheWrite": 2.2},
                "contextWindow": 200_000,
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
                # Sonnet 4.6 primary, Haiku 4.5 fallback. The default is the
                # cheaper standard tier so a runaway agent loop never pays
                # top-tier rates on every turn. Opus 4.6 is reserved for the
                # CEO agent (Friday) via a per-agent agents.list[*].model
                # (see premium_model_ref / _build_agents_list).
                #
                # Why not Opus 4.7 anywhere: AWS ships Opus 4.7 with
                # `applied = 0` TPM on most accounts pending capacity
                # rollout (Service Quotas L-5DB28B7B), so 4.7 invokes
                # throttle on the very first request.
                #
                # Fallbacks are error-only, not cost routing — Haiku 4.5 is
                # the cheapest priced tier, so a failover quietly REDUCES
                # spend rather than increasing it.
                #
                # Users can still override per-agent via agents.list[*].model.
                "primary": f"amazon-bedrock/{sonnet_id}",
                "fallbacks": [f"amazon-bedrock/{haiku_id}"],
            },
            {
                "amazon-bedrock": {
                    "config": {"discovery": {"enabled": False}},
                },
            },
        )
    raise ValueError(f"Unknown provider_choice: {provider_choice!r}")


def premium_model_ref(
    provider_choice: str, byo_provider: str | None, *, aip_arns: dict[str, str] | None = None
) -> str | None:
    """The premium model ref reserved for the CEO agent (Friday), per provider
    path — or ``None`` when there is no premium tier to reserve.

    The default agent runs on the cheaper standard tier (see
    ``build_provider_block``); only the CEO is pinned to the top tier via a
    per-agent ``agents.list[*].model``. ``chatgpt_oauth`` returns ``None``
    because it's a flat-rate plan — there's no cheaper tier to demote the
    default to, so the CEO inherits the same model as everyone else.

    Returns the same ref style as ``build_provider_block`` (``<provider>/<id>``).
    """
    if provider_choice == "bedrock_claude":
        if not aip_arns or "opus" not in aip_arns:
            raise ValueError("bedrock_claude premium_model_ref requires aip_arns['opus']")
        return f"amazon-bedrock/{aip_arns['opus']}"
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            # Anthropic-DIRECT id — no Bedrock "-v1" suffix / date-stamp.
            return "anthropic/claude-opus-4-6"
        if byo_provider == "openai":
            return "openai/gpt-5.5"
    return None


def cheap_model_ref(
    provider_choice: str, byo_provider: str | None, *, aip_arns: dict[str, str] | None = None
) -> str | None:
    """The cheapest model ref per provider path, routed to pure-overhead aux
    operations — or ``None`` when there is no cheaper tier to route to.

    History compaction (summarizing very large transcripts), memory-flush, and
    OpenClaw's internal maintenance heartbeat are overhead that doesn't need the
    premium default model. Routing them to the cheapest tier cuts spend —
    compaction especially, since it processes the largest contexts.

    Unlike ``premium_model_ref``, the flat-rate ``chatgpt_oauth`` path DOES get
    a value: the bundled ``openai`` provider catalog carries a mini tier
    (``gpt-5.4-mini``), and pushing aux work onto it buys rate-limit headroom on
    the flat-rate plan.

    Returns the same ref style as ``build_provider_block`` (``<provider>/<id>``).
    """
    if provider_choice == "bedrock_claude":
        if not aip_arns or "haiku" not in aip_arns:
            raise ValueError("bedrock_claude cheap_model_ref requires aip_arns['haiku']")
        return f"amazon-bedrock/{aip_arns['haiku']}"
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            return "anthropic/claude-haiku-4-5"
        if byo_provider == "openai":
            return "openai/gpt-5.4-mini"
    if provider_choice == "chatgpt_oauth":
        return "openai/gpt-5.4-mini"
    return None


def _build_agents_list(paperclip_agents: list[dict], ceo_model: str | None = None) -> list[dict]:
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

    ``ceo_model`` (when provided) pins the CEO agent to the premium model
    via a per-agent ``model`` override; every other agent omits ``model``
    and inherits the cheaper ``agents.defaults.model``. ``None`` leaves the
    CEO inheriting the default too (flat-rate provider paths).
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
            # The CEO (Friday) runs on the premium tier; everyone else
            # inherits the cheaper agents.defaults.model. ``ceo_model`` is
            # None on flat-rate provider paths (chatgpt_oauth), where there
            # is no premium tier to reserve.
            if ceo_model is not None:
                entry["model"] = ceo_model
        entries.append(entry)
    return entries


def workflow_plugin_entries() -> dict:
    """Bundled OpenClaw workflow extensions enabled on every container,
    independent of the LLM provider.

    Single source of truth so BOTH the provision-time builder
    (``build_openclaw_config_dict``) and the post-onboarding provider
    hot-swap path (``config_patcher.replace_provider_block``, which replaces
    ``plugins.entries`` wholesale) include them. Without this shared seam a
    Bedrock ↔ ChatGPT switch would drop ``lobster`` / ``llm-task`` until the
    next full reprovision.

    Returned fresh each call so callers may merge/mutate without corrupting
    shared module state. ``llm-task`` is left unconfigured so it inherits the
    agent's default provider/model at call time across every provider path.
    """
    return {
        "lobster": {"enabled": True},
        "llm-task": {"enabled": True},
    }


def memory_plugin_entries() -> dict:
    """The bundled ``memory-core`` extension with nightly LIGHT-phase dreaming,
    shared by the provision-time builder and the provider hot-swap path.

    Light-phase dreaming reads recent session transcripts off disk and writes
    consolidated dated ``memory/<day>.md`` notes (flat files, under a lockfile,
    no embeddings). Today/yesterday's dated notes auto-load into context, so
    agents carry recent work forward without relying on the agent remembering
    to call ``memory_search``.

    REM is ON: it writes ``DREAMS.md`` narratives, now surfaced by the Dreams
    control panel's Diary tab (``doctor.memory.dreamDiary``). DEEP is OFF by
    default and is an opt-in per-container toggle (Dreams panel → ``config.patch``,
    never a builder flip): it auto-promotes recurring recalls into root
    ``MEMORY.md``. The 6.6 image that makes its embeddings store EFS-safe
    (DELETE journaling on NFS) is already deployed, so the toggle is usable.
    ``memory_search`` itself stays ENABLED in agents.defaults (live
    logs show no sqlite contention; recall is what lets agents remember beyond
    the always-loaded ``MEMORY.md``).

    Keys are restricted to the upstream ``memory-core`` configSchema, which is
    strict (``additionalProperties:false``) at every level: the only key under
    ``config`` is ``dreaming``. A stray key crash-loops the container at boot.

    Returned fresh each call so callers may merge/mutate without corrupting
    shared module state.
    """
    return {
        "memory-core": {
            "enabled": True,
            "config": {
                "dreaming": {
                    "enabled": True,
                    "frequency": "0 3 * * *",  # nightly 03:00 unified sweep
                    "timezone": "UTC",
                    "phases": {
                        "light": {"enabled": True},
                        "deep": {"enabled": False},
                        # REM writes daily REM blocks + a DREAMS.md narrative,
                        # surfaced by the Dreams control panel's Diary tab. Flat
                        # files (no embeddings), EFS-safe on the deployed image.
                        "rem": {"enabled": True},
                    },
                },
            },
        },
    }


def derive_enabled_harnesses(provider_choice: str, byo_provider: str | None) -> list[str]:
    """Which ACP coding harnesses the owner can spawn, DERIVED from their
    provider choice — no stored per-owner state.

    An ACP harness can only authenticate if the owner already has that
    harness's model-provider credentials, so harness availability is a pure
    function of ``provider_choice`` (+ ``byo_provider``):

    - ``bedrock_claude`` -> ``["claude"]`` (Claude Code via
      ``CLAUDE_CODE_USE_BEDROCK=1`` + the container task-role IAM).
    - ``chatgpt_oauth``  -> ``["codex"]``  (Codex via the OAuth tokens we
      pre-stage onto EFS; codex-auth-bridge reads host auth — works for
      personal AND org owners, the latter via the org connector).
    - ``byo_key`` + ``anthropic`` -> ``["claude"]`` (``ANTHROPIC_API_KEY``).
    - ``byo_key`` + ``openai``    -> ``["codex"]``  (``OPENAI_API_KEY``).

    Gemini is intentionally absent: there is no Google credential path on the
    platform today, so no owner can authenticate it. Returns ``[]`` for any
    unrecognized combination (caller emits no ACP block).
    """
    if provider_choice == "bedrock_claude":
        return ["claude"]
    if provider_choice == "chatgpt_oauth":
        return ["codex"]
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            return ["claude"]
        if byo_provider == "openai":
            return ["codex"]
    return []


def build_acp_config(harnesses: list[str]) -> tuple[dict, dict]:
    """Build the ``acp`` block + the ``plugins.entries.acpx`` entry for a
    container whose owner can spawn ``harnesses``.

    Returns ``(acp_block, acpx_plugin_entry)``. The acp block is the gateway's
    ACP control-plane config (``backend: "acpx"`` is the bundled runtime baked
    into the extended image at Dockerfile Layer 12); ``allowedAgents`` is the
    derived harness set so policy refuses any harness the owner can't auth.

    Permissions: ACP sessions are non-interactive (no TTY in the container), so
    the acpx defaults (``approve-reads`` / ``fail``) would abort any
    write/exec turn. Each container is the owner's own isolated sandbox —
    equivalent trust to the native loop's ``allowCommands`` — so we
    ``approve-all`` and ``deny`` (degrade gracefully) rather than crash a turn.
    """
    acp_block = {
        "enabled": True,
        "backend": "acpx",
        "allowedAgents": list(harnesses),
        "defaultAgent": harnesses[0],
        "runtime": {"ttlMinutes": 120},
        "stream": {"coalesceIdleMs": 300, "maxChunkChars": 1200},
    }
    acpx_plugin_entry = {
        "acpx": {
            "enabled": True,
            "config": {
                "permissionMode": "approve-all",
                "nonInteractivePermissions": "deny",
            },
        },
    }
    return acp_block, acpx_plugin_entry


def build_openclaw_config_dict(
    *,
    user_id: str,
    gateway_token: str,
    provider_choice: str,
    byo_provider: str | None = None,
    paperclip_agents: list[dict] | None = None,
    hook_token: str | None = None,
    owner_type: str = "personal",
    paperclip_api_key: str | None = None,
    paperclip_company_id: str | None = None,
    paperclip_api_url: str | None = None,
    integration_mcp_servers: dict | None = None,
    aip_arns: dict[str, str] | None = None,
    channel_accounts: dict[str, dict[str, dict]] | None = None,
    acp_enabled: bool = False,
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
        aip_arns=aip_arns,
    )
    # The default agent runs on the cheaper standard tier (above); the CEO
    # (Friday) is pinned to the premium tier via a per-agent override below.
    ceo_model = premium_model_ref(provider_choice, byo_provider, aip_arns=aip_arns)
    # Pure-overhead aux operations (compaction / memory-flush / internal
    # heartbeat) are routed to the cheapest tier — they don't need the
    # premium default, and compaction in particular processes very large
    # contexts where the cost difference is largest.
    cheap_model = cheap_model_ref(provider_choice, byo_provider, aip_arns=aip_arns)

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
        "meta": {"lastTouchedVersion": DEPLOYED_UPSTREAM_VERSION},
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
                # File-transfer commands (file.fetch, file.write,
                # dir.list, dir.fetch) and screen.snapshot are handled
                # natively by the Rust client (apps/desktop/src-tauri/
                # src/file_transfer.rs) — NOT through openclaw's bundled
                # file-transfer plugin's node-host TS code. The Rust impl
                # enforces a user-managed allowlist client-side
                # (file_access.rs); this list is just the Stage 2 gate.
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
                    "file.fetch",
                    "file.write",
                    "dir.list",
                    "dir.fetch",
                    "screen.snapshot",
                ],
            },
        },
        "models": {
            "providers": providers_config,
        },
        "agents": {
            "defaults": {
                "model": default_model,
                # Global default provider params applied to all models before
                # per-model/per-agent overrides (openclaw
                # zod-schema.agent-defaults.ts is a free-form record). The
                # Bedrock runtime reads `cacheRetention` from here; its default
                # is "short" (5m TTL), but this agent's runs are spaced further
                # apart than that, so cached prefixes expire before reuse and
                # get re-written (write-heavy cache). "long" = 1h TTL widens the
                # window so re-reads land within it, cutting input-token cost.
                # the openai provider ignores the key, so it's a global no-op
                # there — set unconditionally rather than per-provider.
                "params": {"cacheRetention": "long"},
                # contextLimits is a no-op stub (its schema doesn't change
                # behavior when empty); kept as an explicit default.
                "contextLimits": {},
                # compaction.midTurnPrecheck.enabled is ON for every provider.
                # It detects context pressure after each tool result, before
                # the next model call, and truncates/compacts in-place — so a
                # long tool-heavy turn recovers instead of hard-failing with
                # "Context overflow: prompt too large for the model" before
                # turn-end compaction can run. It guards the context window,
                # not model cost, so it is provider-agnostic and lives outside
                # the cheap_model guard below (flat-rate paths need it too).
                #
                # Route compaction + memory-flush to the cheap tier: both are
                # pure-overhead operations that don't need the premium default.
                # Compaction summarizes very large transcripts, so the per-turn
                # token cost is where the savings are largest. We set ONLY
                # `.model` on each slot — never an enabled/mode flag — so this
                # never turns on a feature that wasn't already running upstream
                # (memoryFlush has its own opt-in). On flat-rate paths with no
                # cheaper tier (`cheap_model` is None) the model overrides are
                # omitted so both inherit the default model. All three keys are
                # valid under upstream's strict AgentDefaultsSchema
                # (compaction.midTurnPrecheck.enabled, compaction.model,
                # compaction.memoryFlush.model).
                "compaction": {
                    "midTurnPrecheck": {"enabled": True},
                    **({"model": cheap_model, "memoryFlush": {"model": cheap_model}} if cheap_model else {}),
                },
                # heartbeat is NOT a no-op: upstream treats a present
                # `agents.defaults.heartbeat` as truthy and falls back to a
                # 30m interval with `target: none`. That is OpenClaw's
                # intended default — a periodic internal-maintenance turn per
                # agent (review HEARTBEAT.md, update memory) that does NOT
                # send any outbound message. We set `every` explicitly so the
                # cadence is visible in our config rather than inherited from
                # an upstream default. Event-driven waking (Paperclip issue
                # assignment/comment) is independent of this.
                #
                # `model` routes the heartbeat to the cheap tier — it's pure
                # overhead and doesn't need the premium default. `None` on
                # flat-rate paths with no cheaper tier omits the override so
                # the heartbeat inherits the default model.
                "heartbeat": {"every": "30m", **({"model": cheap_model} if cheap_model else {})},
                "workspace": "/home/node/.openclaw/workspaces",
                # Memory recall stays ON (matches main): memory-core registers
                # memory_search/memory_get, and the "Memory Recall" prompt
                # section only renders while those tools exist. The per-agent
                # embeddings index is sqlite on EFS; we keep it on EFS because
                # live container logs show no sqlite contention and recall is
                # what lets agents remember beyond the always-loaded MEMORY.md.
                # provider/model pinned to Bedrock Titan (not upstream's
                # "local" GGUF, which we don't ship and which historically
                # hung on first embed).
                "memorySearch": {
                    "enabled": True,
                    "provider": "bedrock",
                    "model": "amazon.titan-embed-text-v2:0",
                },
                # Do NOT add `agents.defaults.llm.idleTimeoutSeconds` here:
                # OpenClaw v2026.5.7's AgentDefaultsSchema is strict() and
                # rejects the unknown key, so the gateway exits with
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
            "list": _build_agents_list(paperclip_agents or [], ceo_model=ceo_model),
        },
        # Memory backend: "builtin" = flat MEMORY.md files in the agent
        # workspace. Do NOT switch to the qmd backend: qmd's
        # better-sqlite3 store on EFS deadlocks the gateway's V8 main
        # thread when the NFS client retries forever
        # (`rpc_wait_bit_killable`). Symptoms: container appears alive
        # (TCP port still listening) but every WS upgrade times out.
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
            # Explicit wildcard allow, separate from ``profile: "full"``.
            # The profile's implicit wildcard surfaces core tools and
            # NON-optional plugin tools, but the optional-plugin-tool gate
            # (OpenClaw src/plugins/tools.ts isOptionalToolAllowed) checks
            # the *explicit* tools.allow and denies optional tools whenever
            # that list resolves empty. Our optional plugin tools (lobster,
            # llm-task — both registered optional:true) therefore stay hidden
            # under profile-full alone; an explicit "*" is the token that
            # surfaces them. ``deny`` still wins over the wildcard, so the
            # canvas/nodes denials below are unaffected.
            "allow": ["*"],
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
            "entries": {
                # Provider-derived entries (e.g. amazon-bedrock with
                # discovery disabled) selected by ``provider_choice``.
                **plugin_entries,
                # Bundled workflow extensions shipped in the OpenClaw image
                # (OPENCLAW_BUNDLED_PLUGIN_DIR=extensions). Both activate
                # onStartup once enabled; their tools are optional:true and
                # are surfaced via the explicit tools.allow wildcard above.
                # Kept in sync with the provider hot-swap path via the shared
                # workflow_plugin_entries() seam.
                **workflow_plugin_entries(),
                # memory-core's dreaming subsystem (light phase only; REM + deep
                # OFF). Shared with the provider hot-swap path via the
                # memory_plugin_entries() seam so a Bedrock <-> ChatGPT switch
                # doesn't drop it.
                **memory_plugin_entries(),
            },
        },
        # Channels: ship EMPTY until the user configures one. A provider block
        # (channels.<id> = {enabled: true, accounts: {...}}) appears only when the
        # setup wizard PATCHes the first account. Why not the old enabled-trio:
        #   - An enabled channel with zero accounts still counts as a "configured
        #     channel" for OpenClaw's cron announce-delivery guard, so shipping all
        #     three forced agents to disambiguate delivery.channel on every announce
        #     cron — which they don't, so agent-authored delivery crons failed.
        #   - An enabled channel with a stale/orphaned provider-level token auth-
        #     loops on invalid_auth and burns CPU; empty-until-configured removes
        #     that substrate entirely.
        # COST: the first setup of a given channel is now a full gateway restart
        # (the plugin isn't loaded until the channel is enabled), deferred up to
        # ~300s — handled gracefully by the deferral-aware setup wizard
        # (awaitChannelReady). On reprovision, the channel_accounts re-injection
        # below recreates the enabled block + accounts for any channel the user had
        # configured. ChannelsSchema accepts an empty object (upstream
        # zod-schema.channels-config.ts: object().passthrough().optional()).
        "channels": {},
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
            # container Chromium — bundled in the extended image.
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

    # Wire the Paperclip MCP server when we have the per-agent API key in
    # hand. The MCP runs inside the user container via npx and lets Friday
    # call Paperclip's REST API (paperclipCreateIssue, paperclipListAgents,
    # paperclipGetHeartbeatContext, …) without us proxying through the
    # backend with a global admin cookie. Auth attributes per-agent —
    # actions show as Friday in Paperclip's audit log.
    #
    # The Routers/integrations.py read path keys on config["mcp"]["servers"]
    # — same shape consumed by the MCP Servers control-panel surface.
    #
    # If the key isn't available (Paperclip outage during provision, key
    # not yet minted, etc.), we simply omit the block. The next reprovision
    # self-heals via ensure_paperclip_api_key.
    if paperclip_api_key and paperclip_company_id and paperclip_api_url and paperclip_agents:
        friday = next(
            (a for a in paperclip_agents if a.get("role") == "ceo"),
            None,
        )
        if friday and friday.get("id"):
            config["mcp"] = {
                "servers": {
                    "paperclip": {
                        # Kept on `npx` so it resolves on ANY deployed image. The
                        # binary is now baked as a global bin (Dockerfile Layer 8),
                        # but switching this to the bare `paperclip-mcp-server`
                        # command (instant spawn, off the npx connect storm) must
                        # wait until the baked image tag is selected in
                        # openclaw-version.json and rolled out — otherwise a
                        # provision against an older image writes a command its
                        # OpenClaw can't resolve and the MCP loses all tools.
                        "command": "npx",
                        "args": ["-y", "@paperclipai/mcp-server"],
                        "env": {
                            "PAPERCLIP_API_URL": paperclip_api_url,
                            "PAPERCLIP_API_KEY": paperclip_api_key,
                            "PAPERCLIP_COMPANY_ID": paperclip_company_id,
                            "PAPERCLIP_AGENT_ID": friday["id"],
                        },
                    },
                },
            }

    # Merge the owner's connected integration MCP servers (Stripe, …) into the
    # same servers map. A reprovision does a full overwrite of openclaw.json, so
    # unless these are re-injected here the connection is dropped. Each value is
    # a full {command, args, env} block; integration keys win for their own key
    # while the paperclip block (and any servers map shape it created) is
    # preserved. setdefault handles the paperclip-absent case where no mcp block
    # was emitted above.
    if integration_mcp_servers:
        servers = config.setdefault("mcp", {}).setdefault("servers", {})
        servers.update(integration_mcp_servers)

    # Pin the bundled MCP runtime to "shared" scope: one connected runtime per
    # (workspace, server-config), reused across every session instead of spawned
    # per session — so a new session (a new Paperclip issue, a chat after the
    # daily session reset) reuses the already-connected MCP servers instead of
    # paying a fresh connect. Each container serves a single owner, so there is
    # no cross-session credential bleed.
    #
    # runtimeScope exists only in the forked base image; an older gateway's MCP
    # schema is strict and rejects unknown keys at boot, so emit it only once
    # THIS environment's deployed image supports it — otherwise a config write to
    # a container still on the old image bricks it on next boot. The gate flips
    # per-environment as each tag is promoted. Only meaningful when an mcp block
    # exists.
    if openclaw_version.deployed_upstream_at_least("2026.6.1"):
        mcp_block = config.get("mcp")
        if isinstance(mcp_block, dict):
            mcp_block["runtimeScope"] = "shared"

    # Re-inject channel bot accounts carried across a reprovision. A reprovision
    # does a full overwrite of openclaw.json (like the mcp.servers case above),
    # so without this every configured Telegram/Slack/Discord account, its token,
    # and its allowFrom pairings are silently dropped. Recreate the sibling route
    # bindings too, since config_patcher adds those per-PATCH and the overwrite
    # would otherwise lose them (without a binding the bot misroutes to main).
    if channel_accounts:
        bindings = config.setdefault("bindings", [])
        channels = config.setdefault("channels", {})
        for provider, accounts in channel_accounts.items():
            # Channels now ship empty-until-configured, so a carried-forward
            # account must (re)create its enabled channel block — otherwise the
            # reprovision would silently drop the user's configured channel.
            # Gate on the known provider set so a bad id can't brick boot.
            if provider not in CHANNEL_PROVIDER_IDS or not accounts:
                continue
            channel = channels.setdefault(provider, {"enabled": True, "dmPolicy": "pairing"})
            channel["enabled"] = True
            channel["accounts"] = accounts
            for account_id in accounts:
                binding = {
                    "type": "route",
                    "agentId": account_id,
                    "match": {"channel": provider, "accountId": account_id},
                }
                if binding not in bindings:
                    bindings.append(binding)

    # ACP coding-harness runtime (Jarvis-style delegation: a native agent can
    # ``sessions_spawn({runtime:"acp"})`` Claude Code / Codex / Gemini). Gated
    # behind ``acp_enabled`` (a backend feature flag the I/O caller resolves
    # from ``settings.ACP_ENABLED``) so it stays dark until the acpx-baked image
    # (Dockerfile Layer 12) is promoted and we choose to roll it out. Harnesses
    # are DERIVED from the provider choice — no stored per-owner state. Emitting
    # ``acp.enabled`` is what teaches a native agent it can spawn (upstream
    # ``isAcpRuntimeSpawnAvailable`` -> ``acpEnabled: true`` in the system
    # prompt), so a no-harness owner gets no block at all.
    if acp_enabled:
        harnesses = derive_enabled_harnesses(provider_choice, byo_provider)
        if harnesses:
            acp_block, acpx_plugin_entry = build_acp_config(harnesses)
            config["acp"] = acp_block
            config["plugins"]["entries"].update(acpx_plugin_entry)

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
    paperclip_api_key: str | None = None,
    paperclip_company_id: str | None = None,
    paperclip_api_url: str | None = None,
    integration_mcp_servers: dict | None = None,
    aip_arns: dict[str, str] | None = None,
    channel_accounts: dict[str, dict[str, dict]] | None = None,
    acp_enabled: bool | None = None,
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
    # ACP rollout is a backend-level feature flag, not per-owner state. When the
    # caller doesn't pin it explicitly, resolve from settings so the whole fleet
    # flips with one env var (dark by default).
    if acp_enabled is None:
        acp_enabled = settings.ACP_ENABLED

    config = build_openclaw_config_dict(
        user_id=user_id,
        gateway_token=gateway_token,
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        paperclip_agents=paperclip_agents,
        hook_token=hook_token,
        owner_type=owner_type,
        paperclip_api_key=paperclip_api_key,
        paperclip_company_id=paperclip_company_id,
        paperclip_api_url=paperclip_api_url,
        integration_mcp_servers=integration_mcp_servers,
        aip_arns=aip_arns,
        channel_accounts=channel_accounts,
        acp_enabled=acp_enabled,
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
