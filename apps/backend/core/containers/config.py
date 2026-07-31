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
from core.services.paperclip_meta import (
    PAPERCLIP_AGENT_ROLES,
    PaperclipMeta,
    write_to_agent_entry,
)

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

# The two Bedrock config modes. Both emit the same `amazon-bedrock` provider
# block shape; they differ in the CATALOG (how many tiers the owner's IAM lets
# them invoke) and in POLICY (a trial container must not self-run).
#
#   bedrock_claude — the paid three-tier ladder (opus / sonnet / haiku).
#   bedrock_trial  — the no-card free tier. Exactly ONE tier (sonnet), because
#                    ensure_owner_aips mints one AIP and build_owner_bedrock_policy
#                    pins the owner's IAM role to it: emitting opus/haiku here
#                    would AccessDenied on every turn that touched them.
#
# `bedrock_trial` is a config MODE resolved from the owner's subscription_status
# (`trial_local`), never a stored provider_choice — same relationship the deleted
# `meta` branch had. It is deliberately a distinct provider_choice string rather
# than an inference from the AIP map so the trial LOCKDOWN below is an explicit
# product decision, not an accident of how many AIPs happen to exist.
BEDROCK_TRIAL_CHOICE = "bedrock_trial"
BEDROCK_PROVIDER_CHOICES = ("bedrock_claude", BEDROCK_TRIAL_CHOICE)

# ---------------------------------------------------------------------------
# Runaway bounds — applied to EVERY provider path (see build_openclaw_config_dict).
#
# These bound how far a run can RUN AWAY; none of them changes what an agent can
# DO. OpenClaw's agentic loop is a literal `while (true)` with no step, tool-call
# or token cap (the strict 7.1 schema rejects maxSteps/maxToolCalls/budget
# outright), so without these a single un-aborted loop can burn thousands of
# dollars against whichever credential the owner is on.
#
# They are NOT the enforcement fence — the agent has `exec: full` and can rewrite
# its own openclaw.json. The fence is the IAM revoke + desiredCount=0 kill switch.
# These are free insurance that shrinks the per-incident blast radius.
# ---------------------------------------------------------------------------

# agents.defaults.timeoutSeconds — the big one. Upstream defaults to 172800
# (48 HOURS) and Isol8 has never set it. 30 minutes sits far above any real turn
# (the Paperclip adapter already runs with a 600s cap in production) while turning
# a ~$2,600 runaway into a ~$27 one.
AGENT_RUN_TIMEOUT_SECONDS = 1800

# agents.defaults.runRetries.max — upstream default is 160, and each retry
# re-arms the deadline above (so 160 × 30m is not a bound at all).
AGENT_RUN_RETRIES_MAX = 8

# agents.defaults.compaction.timeoutSeconds — upstream defaults to 180, tuned
# for interactive sessions that compact while still small. Long-lived Paperclip
# heartbeat sessions only hit the compaction threshold once the transcript is
# already huge, and on the ChatGPT/Codex path the summarize call must run on
# the primary model (no cheap-summarizer override — see cheap_model_ref), so
# 180s is routinely not enough: the timeout kills the RUN, and a timed-out
# compaction leaves the session unchanged, so every subsequent run hits the
# same wall. 600s stays under both the adapter's 15-minute stall timeout and
# AGENT_RUN_TIMEOUT_SECONDS. A slow compaction that succeeds beats a dead run.
COMPACTION_TIMEOUT_SECONDS = 600

# agents.defaults.subagents.maxConcurrent — upstream default 8. The subagent lane
# does NOT count against the main agents.defaults.maxConcurrent semaphore, so it
# needs its own bound. (maxConcurrent itself is deliberately left at its upstream
# default of 4 — it is already a real global semaphore every entrance passes.)
#
# TRIAL ONLY, unlike the other three bounds. Those (timeoutSeconds, runRetries,
# loopDetection) are sane everywhere: no legitimate run wants 48 hours or 160
# retries. This one is different — it throttles *capability*, and for a paid
# owner the spend is bounded by their own credits or their own API key, so
# there is no reason to make them delegate more slowly than upstream allows.
AGENT_SUBAGENT_MAX_CONCURRENT_TRIAL = 2

# agents.defaults.contextTokens — BEDROCK ONLY. Our Bedrock catalog declares a
# 1,000,000-token context window, so one call may take up to ~$6.74 of input.
# Bounding the effective budget caps that and triggers compaction/contextPruning
# earlier (their ratios are measured against this value, not the model's
# advertised window). Deliberately not set on the other paths: the ChatGPT/Codex
# path serves ~272k and must keep inheriting its own budget.
BEDROCK_CONTEXT_TOKENS = 200_000

# ---------------------------------------------------------------------------
# Tool deny lists — SINGLE SOURCE. node_proxy.py REPLACES tools.deny wholesale
# when a desktop node pairs/unpairs, so it derives both variants from these;
# a hand-written list anywhere else silently re-enables an entry on the next
# pair event.
#
# - canvas: no product surface (Control UI disabled).
# - cron: Paperclip routines are the single scheduling authority.
# - tts: the key-less Microsoft Edge provider registers the tts tool with NO
#   config at all (tools.media.audio gates *understanding*, not TTS), so
#   reply text would egress to a public no-SLA Microsoft endpoint and none of
#   our surfaces can play the audio anyway.
# - music_generate / video_generate: generation tools that auto-register off
#   the owner's own credentials (e.g. sora video on BYO-openai). Owner-billed,
#   but no product surface renders the output and autonomous runs should not
#   discover a surprise-spend tool. image_generate deliberately stays: chat
#   renders images and the spend is equally the owner's.
# ---------------------------------------------------------------------------
TOOL_DENY_BASE = ("canvas", "cron", "tts", "music_generate", "video_generate")
# The provision-time variant: node tools stay denied until node_proxy flips
# them on for a paired desktop.
TOOL_DENY_WITH_NODES = TOOL_DENY_BASE + ("nodes",)


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
        #
        # The fallback must be a DIFFERENT model: upstream's fallback runner
        # dedupes candidates by provider/model key, so a self-fallback leaves
        # zero failover candidates and a single provider 5xx kills the run
        # (FailoverError, next=none). gpt-5.4 is the closest tier in the
        # Codex-routable OAuth allowlist; fallbacks are error-only, and the
        # plan is flat-rate, so it engages only when gpt-5.5 itself errors.
        return (
            {},
            {"primary": "openai/gpt-5.5", "fallbacks": ["openai/gpt-5.4"]},
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
    if provider_choice in BEDROCK_PROVIDER_CHOICES:
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
        #
        # The catalog is the intersection of "models we price" and "AIPs this
        # owner's IAM role can actually invoke": every tier PRESENT in aip_arns
        # is emitted, every absent tier is omitted. sonnet is the one hard
        # requirement — it is agents.defaults.model.primary on both bedrock
        # modes, so an owner without it could not invoke anything at all.
        #
        # A trial owner has exactly one AIP (sonnet). Emitting the three-tier
        # ladder for them would hand every turn a model IAM denies; the old
        # all-three-or-raise gate made that a hard provision failure instead.
        if not aip_arns or "sonnet" not in aip_arns:
            raise ValueError(
                f"{provider_choice} requires aip_arns with at least a 'sonnet' key "
                "(per-owner AIP ARNs). Run ensure_owner_aips before write_user_configs."
            )
        is_trial = provider_choice == BEDROCK_TRIAL_CHOICE
        sonnet_id = aip_arns["sonnet"]
        # A trial owner is pinned to ONE model regardless of what the map carries
        # (defense in depth against a stale/over-minted AIP map on a trial row).
        opus_id = None if is_trial else aip_arns.get("opus")
        haiku_id = None if is_trial else aip_arns.get("haiku")

        bedrock_models = []
        if opus_id:
            bedrock_models.append(
                {
                    "id": opus_id,
                    "name": "Claude Opus 4.6",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {"input": 5.5, "output": 27.5, "cacheRead": 0.55, "cacheWrite": 11.0},
                    "contextWindow": 1_000_000,
                    "maxTokens": 8192,
                }
            )
        bedrock_models.append(
            {
                "id": sonnet_id,
                "name": "Claude Sonnet 4.6",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {"input": 3.3, "output": 16.5, "cacheRead": 0.33, "cacheWrite": 6.6},
                "contextWindow": 1_000_000,
                "maxTokens": 8192,
            }
        )
        if haiku_id:
            bedrock_models.append(
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
                }
            )
        # Fallbacks are error-only, not cost routing. With a haiku tier the
        # failover REDUCES spend (cheapest priced tier). With no cheaper tier —
        # the trial's single-model catalog — the fallback is the primary itself:
        # there is nothing else the owner's IAM role can invoke, and pointing at
        # a model it can't would turn any transient error into a hard failure.
        fallback_id = haiku_id or sonnet_id
        # The bedrock-gate is the STANDARD Bedrock path: baseUrl points at the
        # gate's Function URL (discovered at runtime from SSM — see
        # core/billing/bedrock_gate_url.py) so every inference call flows
        # through the synchronous per-request budget gate. ``auth: "aws-sdk"``
        # is unchanged — the per-owner ``AWS_BEARER_TOKEN_BEDROCK`` task env
        # overrides the scheme to httpBearerAuth inside the amazon-bedrock
        # plugin, so the model ids (AIP ARNs), cost blocks, and defaults below
        # are identical either way. An empty resolver result (bootstrap before
        # the gate stack exists, or the "direct" break-glass value) falls back
        # to the direct bedrock-runtime endpoint — note post-cutover the owner
        # role carries no bedrock:Invoke, so direct also needs the break-glass
        # IAM re-grant (see the runbook).
        from core.billing.bedrock_gate_url import gate_url

        base_url = gate_url() or f"https://bedrock-runtime.{settings.AWS_REGION}.amazonaws.com"
        return (
            {
                "amazon-bedrock": {
                    "baseUrl": base_url,
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
                "fallbacks": [f"amazon-bedrock/{fallback_id}"],
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

    ``bedrock_trial`` — and any Bedrock owner whose AIP map carries no ``opus``
    — also returns ``None``: their IAM role is pinned to the AIPs they actually
    hold, so pinning Friday to an opus profile they don't own would
    AccessDeniedException on every one of the CEO's turns. A missing map still
    raises: that's a provisioning bug, not a one-tier owner.

    Returns the same ref style as ``build_provider_block`` (``<provider>/<id>``).
    """
    if provider_choice in BEDROCK_PROVIDER_CHOICES:
        if not aip_arns:
            raise ValueError(f"{provider_choice} premium_model_ref requires aip_arns")
        if provider_choice == BEDROCK_TRIAL_CHOICE or "opus" not in aip_arns:
            return None
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

    The ``chatgpt_oauth`` path returns ``None`` — like ``premium_model_ref``.
    Its agents run on OpenClaw's Codex runtime (``chatgpt.com/backend-api/codex``),
    which uses native server-side compaction and does NOT honor an OpenClaw-side
    summarizer override: a ``compaction.model`` other than the chat model is
    ignored or rejected by the Codex backend, so the aux call fails, compaction
    never runs, context grows unbounded, and the turn dies with "Context overflow:
    prompt too large". Returning ``None`` lets compaction / memory-flush /
    heartbeat inherit the primary (gpt-5.5), which the OAuth token can actually
    invoke — so compaction runs. The general rule: only return a model the
    selected provider/auth path can actually invoke as a summarizer.

    ``bedrock_trial`` — and any Bedrock owner whose AIP map carries no ``haiku``
    — returns ``None`` for the same reason ``premium_model_ref`` does: the
    owner's IAM role can only invoke the AIPs they hold, so routing compaction /
    memory-flush to a haiku profile they don't own would make every compaction
    fail (and, unrecovered, kill the turn with a context overflow). ``None``
    lets aux work inherit the single model they CAN invoke.

    Returns the same ref style as ``build_provider_block`` (``<provider>/<id>``).
    """
    if provider_choice in BEDROCK_PROVIDER_CHOICES:
        if not aip_arns:
            raise ValueError(f"{provider_choice} cheap_model_ref requires aip_arns")
        if provider_choice == BEDROCK_TRIAL_CHOICE or "haiku" not in aip_arns:
            return None
        return f"amazon-bedrock/{aip_arns['haiku']}"
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            return "anthropic/claude-haiku-4-5"
        if byo_provider == "openai":
            # byo_key openai is the DIRECT OpenAI API (not Codex) — the owner's
            # key CAN invoke the mini tier as a summarizer, so route aux to it.
            return "openai/gpt-5.4-mini"
    return None


def heartbeat_model_ref(
    provider_choice: str, byo_provider: str | None, *, aip_arns: dict[str, str] | None = None
) -> str | None:
    """Model for the periodic maintenance heartbeat — the cheap tier for EVERY
    provider, including ``chatgpt_oauth``.

    This differs from ``cheap_model_ref`` only on the ``chatgpt_oauth`` path.
    The heartbeat is an ordinary agentTurn (the synthetic "[heartbeat poll]"
    that reads HEARTBEAT.md and replies HEARTBEAT_OK when idle), which the
    Codex OAuth token CAN serve on the mini tier — unlike compaction, which
    the Codex backend runs natively and which rejects an OpenClaw-side model
    override (see ``cheap_model_ref``). So the OAuth heartbeat routes to
    ``gpt-5.4-mini`` to keep the 30-min per-agent maintenance poll off the
    premium model, while compaction/memory-flush keep using ``cheap_model``
    (None on OAuth → inherits the chat model so Codex native compaction runs).
    """
    if provider_choice == "chatgpt_oauth":
        return "openai/gpt-5.4-mini"
    return cheap_model_ref(provider_choice, byo_provider, aip_arns=aip_arns)


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
                "workspace": "/home/node/.openclaw/workspace-main",
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
            "workspace": f"/home/node/.openclaw/workspace-{agent_id}",
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
        # Sync the Paperclip role/title/capabilities into params.paperclip so the
        # agent inspector renders the agent's real role (e.g. CEO for Friday)
        # instead of defaulting to "general". Without this the entry carries no
        # role and the frontend's read_from_agent_entry falls back to "general"
        # — and a Save from that stale form would demote the agent in Paperclip.
        # Coerce an unknown/missing role to "general" (write_to_agent_entry is
        # strict and would otherwise raise); title/capabilities/icon ride along
        # when Paperclip has them.
        agent_role = agent.get("role")
        meta = PaperclipMeta(
            role=agent_role if agent_role in PAPERCLIP_AGENT_ROLES else "general",
            title=agent.get("title"),
            capabilities=agent.get("capabilities"),
            icon=agent.get("icon"),
        )
        write_to_agent_entry(entry, meta)
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


def workflow_plugin_entries(llm_task_model: str | None = None) -> dict:
    """Bundled, provider-independent OpenClaw extensions enabled on every
    container.

    Single source of truth so BOTH the provision-time builder
    (``build_openclaw_config_dict``) and the post-onboarding provider
    hot-swap path (``config_patcher.replace_provider_block``, which replaces
    ``plugins.entries`` wholesale) include them. Without this shared seam a
    Bedrock ↔ ChatGPT switch would drop these until the next full reprovision.

    Entries:
    - ``lobster`` — typed-pipeline workflow engine.
    - ``llm-task`` — routed to the cheap tier via ``llm_task_model`` (a
      ``<provider>/<model>`` ref, split into the plugin's ``defaultProvider``
      / ``defaultModel`` fields): llm-task calls are workflow-glue JSON steps
      that don't need the premium default. ``None`` (no cheap tier on this
      path — chatgpt_oauth, bedrock_trial) leaves the entry enable-only so
      calls inherit the agent's default model. The plugin's configSchema is
      strict (``additionalProperties: false``); only schema-listed keys may
      ever be added here or the container crash-loops at boot.
    - ``aws-exec-profile`` — baked in-repo plugin whose ``resolve_exec_env``
      hook injects ``AWS_PROFILE=isol8-connector`` into the agent's exec'd
      commands only when the owner's connector AWS profile is materialized.
      Inert (returns ``{}``) until that profile exists, so it is safe to enable
      fleet-wide regardless of provider.
    - ``tokenjuice`` — deterministic exec/bash tool-result compaction
      (baked, Dockerfile Layer 13b). Strict-empty configSchema: enable-only,
      never add a ``config`` key or the container crash-loops at boot.
    - ``sage-openclaw`` — Gen Sage ADR tool-call guard (baked, Dockerfile
      Layer 13c). Also strict-empty configSchema — behavior config is the
      image-baked ``/home/node/.sage/config.json``, NOT this entry.

    Returned fresh each call so callers may merge/mutate without corrupting
    shared module state.
    """
    llm_task_entry: dict = {"enabled": True}
    if llm_task_model:
        # partition on the FIRST "/" only: Bedrock model ids are AIP ARNs
        # that themselves contain "/" (…application-inference-profile/xyz).
        provider, _, model = llm_task_model.partition("/")
        llm_task_entry["config"] = {
            "defaultProvider": provider,
            "defaultModel": model,
        }
    return {
        "lobster": {"enabled": True},
        "llm-task": llm_task_entry,
        "aws-exec-profile": {"enabled": True},
        "tokenjuice": {"enabled": True},
        "sage-openclaw": {"enabled": True},
        # Bundled key-less Firecrawl fetch provider — the Readability-failure
        # fallback that tools.web.fetch.provider selects. Enable-only; adding
        # an apiKey would move it to the owner's paid tier (BYOK surface,
        # not configured here).
        "firecrawl": {"enabled": True},
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


def memory_search_entry(provider_choice: str, byo_provider: str | None) -> dict:
    """The ``agents.defaults.memorySearch`` block, resolved per provider path.

    Memory recall (``memory_search`` / ``memory_get``) stays ENABLED for every
    owner. What varies is the embedding backend, under one rule: use semantic
    (vector) embeddings ONLY where the owner's OWN credential foots the bill;
    fall back to keyword-only (SQLite FTS, ``provider: "none"``) everywhere else
    so Isol8 never pays an embedding bill.

    - ``byo_key`` + ``openai`` -> the owner's staged ``OPENAI_API_KEY`` runs
      ``text-embedding-3-small`` (their account, ~pennies). memory-core's
      ``openai`` embedding provider reads the key from that env var, so no extra
      wiring is needed. This is the ONE path with real semantic recall.
    - Every other path -> ``provider: "none"`` (FTS keyword recall), because
      none of them can embed without Isol8 paying:
        * ``chatgpt_oauth`` — ChatGPT-subscription OAuth tokens authenticate the
          chat/Codex endpoint, NOT ``api.openai.com/v1/embeddings``; there is no
          embeddings access to use.
        * ``byo_key`` + ``anthropic`` — Anthropic ships no embeddings API.
        * ``bedrock_claude`` — Titan embeddings run on the container task role
          (our AWS account, and NOT drawn from the owner's Bedrock credits — the
          per-owner AIP only wraps Claude), so semantic recall here would cost
          Isol8. Metering fractions-of-a-cent back to the owner isn't worth
          building, so Bedrock owners get keyword recall too.

    ``provider: "none"`` still builds a working FTS index (meta row written, the
    ``memory_search`` tool un-paused) — it is NOT disabled; only the vector
    layer is skipped. Prior to this the block was pinned to Bedrock Titan for
    everyone, but per-user container roles can't invoke Titan (grant is
    AIP-only), so the index never built and ``memory_search`` returned "index
    metadata is missing" fleet-wide.

    SINGLE source of truth for the memorySearch block. The provider-switch hot
    path (``config_patcher.replace_provider_block``) deliberately does NOT
    rewrite it: both hot-path providers (``bedrock_claude``, ``chatgpt_oauth``)
    resolve here to ``provider: "none"``, and the only path whose value differs
    (``byo_key`` + ``openai``) always takes the cold re-provision path, which
    rebuilds the whole config through this helper.
    """
    if provider_choice == "byo_key" and byo_provider == "openai":
        return {"enabled": True, "provider": "openai", "model": "text-embedding-3-small"}
    return {"enabled": True, "provider": "none"}


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

    ``bedrock_trial`` returns ``[]`` — no ACP on the free tier. Two independent
    reasons: (1) the Claude Code harness invokes Bedrock model ids of its own
    choosing, which the trial owner's one-AIP IAM policy denies, so every ACP
    turn would fail anyway; (2) it is an autonomy/spend surface that belongs on
    the paid tier alongside cron and the heartbeat.
    """
    if provider_choice == BEDROCK_TRIAL_CHOICE:
        return []
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

    # The free-tier Bedrock config mode. Everything keyed off this flag is a
    # LOCKDOWN of autonomous / always-on surfaces: a trial container must only
    # ever run when a human is in the loop, because its only spend fence is the
    # AWS-side meter + IAM kill switch, and a self-run (cron, heartbeat, a
    # channel bot) burns credits with nobody watching.
    is_bedrock_trial = provider_choice == BEDROCK_TRIAL_CHOICE

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
    heartbeat_model = heartbeat_model_ref(provider_choice, byo_provider, aip_arns=aip_arns)

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
            # Upstream defaults pricing.enabled=true: a background OpenRouter
            # + LiteLLM pricing-catalog fetch after every gateway boot. Our
            # catalogs are static with explicit cost blocks (Bedrock) or
            # flat-rate (OAuth/BYO), and billing truth lives in the
            # bedrock-gate/meter — the fetch is pure waste and an egress
            # surface. Configured cost values keep working with it off.
            "pricing": {"enabled": False},
        },
        "agents": {
            "defaults": {
                "model": default_model,
                # Isol8 pre-seeds every agent workspace (IDENTITY/SOUL/USER via
                # friday_identity) — skip OpenClaw's first-run bootstrap so an
                # under-seeded workspace (Atlas-recruited agent, partial
                # marketplace deploy) never autonomously runs the BOOTSTRAP.md
                # "pick your name" identity ritual headless. Upstream's own
                # docstring names pre-configured deployments as the use case.
                "skipBootstrap": True,
                # --- runaway bounds (see the module constants) -----------------
                # The agentic loop is a literal `while (true)`: no step,
                # tool-call or token cap exists in the strict 7.1 schema, so
                # this wall-clock deadline is the ONLY thing that ends a loop
                # that stops making progress. Upstream's default is 48 HOURS.
                # Applies to every provider — a runaway is a support incident on
                # the owner's own key just as much as on our Bedrock credits.
                "timeoutSeconds": AGENT_RUN_TIMEOUT_SECONDS,
                # Each retry RE-ARMS the deadline above, so the upstream default
                # of 160 would make timeoutSeconds meaningless.
                "runRetries": {"max": AGENT_RUN_RETRIES_MAX},
                # contextTokens is a first-class agents.defaults key (NOT inside
                # `params`). It sets the EFFECTIVE context budget: both
                # contextPruning's trigger ratios and compaction measure against
                # it rather than the model's advertised window. Only the Bedrock
                # catalog above declares a 1M window (one call ≈ up to $6.74 of
                # input), so only the Bedrock modes bound it; other providers
                # omit the key and inherit OpenClaw's per-model default (the
                # ChatGPT/Codex path serves ~272k and must keep its own).
                **({"contextTokens": BEDROCK_CONTEXT_TOKENS} if provider_choice in BEDROCK_PROVIDER_CHOICES else {}),
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
                "params": {
                    "cacheRetention": "long",
                },
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
                # omitted so both inherit the default model. All four keys are
                # valid under upstream's strict AgentDefaultsSchema
                # (compaction.midTurnPrecheck.enabled, compaction.timeoutSeconds,
                # compaction.model, compaction.memoryFlush.model).
                "compaction": {
                    # Pinned explicitly: upstream's default flipped to
                    # "safeguard" for NEW configs only, so an unset mode is
                    # version-dependent drift across image bumps.
                    "mode": "safeguard",
                    "midTurnPrecheck": {"enabled": True},
                    "timeoutSeconds": COMPACTION_TIMEOUT_SECONDS,
                    **({"model": cheap_model, "memoryFlush": {"model": cheap_model}} if cheap_model else {}),
                },
                # contextPruning trims STALE TOOL RESULTS out of the live
                # transcript as it fills. This is the dominant cause of context
                # overflow in practice: long, tool-heavy sessions accumulate
                # hundreds of tool outputs that compaction never touches (it only
                # summarizes conversation history, not tool results). It is
                # OPT-IN upstream — disabled unless mode is explicitly
                # "cache-ttl" — so a missing block means tool results grow
                # unbounded until a turn dies with "input exceeds the context
                # window". The remaining knobs inherit OpenClaw's defaults:
                # soft-trim the oldest results once the transcript passes ~30% of
                # the budget, hard-clear past ~50%, always keeping the most recent
                # turns intact. Crucially the trigger ratios are measured against
                # the EFFECTIVE context budget (contextTokens — ~272k on the
                # ChatGPT/Codex path), not the model's advertised raw window, so
                # it fires correctly even when a model reports a far larger window
                # than it actually serves. Provider-agnostic (guards the context
                # window, not cost), so set unconditionally like midTurnPrecheck.
                #
                # `ttl` moves in lockstep with params.cacheRetention above:
                # retention "long" buys 1h-TTL cache writes, while the pruning
                # manual-mode ttl default is 5m — leaving it unset prunes tool
                # results (mutating the prompt and invalidating the cache) up
                # to 55 minutes before the retention we paid for expires. The
                # 1h smart default doesn't fire on our paths (it requires the
                # bundled Anthropic plugin resolving an Anthropic auth profile,
                # and backs off when mode is set explicitly). Change the two
                # together or not at all.
                "contextPruning": {"mode": "cache-ttl", "ttl": "1h"},
                # The heartbeat key is ALWAYS written, because an absent key
                # does not mean off: upstream runs a 30m heartbeat per agent
                # when `agents.defaults.heartbeat` is missing (interval default
                # in src/auto-reply/heartbeat.ts; docs/gateway/heartbeat.md —
                # `every: "0m"` is the documented disable). The heartbeat is a
                # periodic internal-maintenance turn per agent (review
                # HEARTBEAT.md, update memory) that does NOT send any outbound
                # message. Event-driven waking (Paperclip issue
                # assignment/comment) is independent of this.
                #
                # The BEDROCK TRIAL pins `every: "0m"`: the heartbeat is a
                # recurring self-run per agent, and a trial container must not
                # burn credits with no human in the loop. "0m" also drops
                # HEARTBEAT.md from bootstrap context, so trial agents never
                # see heartbeat-only instructions.
                #
                # On paid paths, `model` routes the heartbeat to the cheap
                # tier — it's pure overhead and doesn't need the premium
                # default. The heartbeat is an ordinary turn (unlike
                # compaction), so it uses `heartbeat_model` (cheap tier for
                # EVERY provider, including chatgpt_oauth → gpt-5.4-mini)
                # rather than `cheap_model` (which stays None on OAuth for the
                # Codex native-compaction constraint). `None` only when a
                # provider has no cheaper tier, in which case the heartbeat
                # inherits the default model.
                #
                # `isolatedSession` + `lightContext` are the heartbeat cost
                # collapse: without them each 30m heartbeat replays the full
                # conversation context (~100K tokens on a busy session, 48×
                # /day/owner); isolated sessions rebuild only the workspace
                # bootstrap, and lightContext narrows that to HEARTBEAT.md —
                # upstream documents ~2-5K tokens per poll. The heartbeat is
                # a maintenance turn (read HEARTBEAT.md, reply HEARTBEAT_OK),
                # not a conversation, so continuity loss is the point, not a
                # cost. Both keys schema-verified on the deployed fork tag.
                "heartbeat": (
                    {"every": "0m"}
                    if is_bedrock_trial
                    else {
                        "every": "30m",
                        "isolatedSession": True,
                        "lightContext": True,
                        **({"model": heartbeat_model} if heartbeat_model else {}),
                    }
                ),
                # utilityModel routes titles/summaries and other one-shot
                # utility completions to the cheap tier. Unset does NOT mean
                # "cheap by default": the upstream resolution chain
                # (simple-completion-runtime.ts) is utilityModel?.trim() ||
                # effective PRIMARY — so an unset (or empty-string) value runs
                # utility calls on the premium default. Reuses the heartbeat
                # cheap-tier resolution (haiku AIP on paid Bedrock, mini on
                # OAuth/BYO-openai, haiku on BYO-anthropic). Omitted on trial:
                # the single-AIP IAM policy means the primary IS the only
                # invocable model, and "" would fall through to it anyway.
                **({"utilityModel": heartbeat_model} if heartbeat_model else {}),
                # Default-agent workspace, upstream convention: bare
                # ``~/.openclaw/workspace`` (per-agent dirs are the sibling
                # ``workspace-<agentId>`` set in agents.list). NEVER point any
                # workspace at ``~/.openclaw/workspaces`` — upstream doctor
                # treats that exact dir as retired Workspaces-plugin state and
                # deletes it recursively.
                "workspace": "/home/node/.openclaw/workspace",
                # Memory recall stays ON for every owner (memory-core registers
                # memory_search/memory_get, and the "Memory Recall" prompt
                # section only renders while those tools exist). The embedding
                # backend is resolved per provider path — semantic where the
                # owner's own credential pays, keyword-only (FTS) otherwise so
                # Isol8 never pays for embeddings. See memory_search_entry().
                "memorySearch": memory_search_entry(provider_choice, byo_provider),
                # Do NOT add `agents.defaults.llm.idleTimeoutSeconds` here:
                # OpenClaw v2026.5.7's AgentDefaultsSchema is strict() and
                # rejects the unknown key, so the gateway exits with
                # `Unrecognized key: "llm"`. Default idle timeout is fine.
                # verboseDefault="full" keeps tool result/partialResult in
                # agent events so the frontend can show tool input + output.
                # OpenClaw defaults to "off" which strips those fields before
                # they reach our WebSocket subscriber.
                "verboseDefault": "full",
                # Agent-roster visibility. The `agents_list` tool resolves each
                # agent's visible roster from `subagents.allowAgents`; when unset
                # an agent sees ONLY itself (verified against the deployed tag:
                # agents-list-tool.ts + subagent-target-policy.ts — undefined →
                # [requesterAgentId]). That blinds every agent to the rest of the
                # company: Atlas can't find a sibling (e.g. a user-made "Jarvis")
                # to build it out, and Friday can't enumerate its team to
                # orchestrate. `["*"]` widens each agent to the full configured
                # roster (agents.list[]). This governs the agents tool ONLY;
                # cross-agent *session* viewing (tools.sessions.visibility /
                # tools.agentToAgent) is a separate gate we deliberately leave at
                # its default. Not subject to the sandbox visibility clamp.
                #
                # `maxConcurrent` is a runaway bound, not a capability limit: the
                # subagent lane does NOT count against agents.defaults.maxConcurrent
                # (the global semaphore every other entrance passes through), so a
                # fan-out loop can run 8 parallel agents — 8 parallel spends — by
                # default. Two at a time still lets an agent delegate; it just
                # can't multiply itself out of the cap.
                "subagents": {
                    "allowAgents": ["*"],
                    # Children inherit the CALLER's model when unset — and the
                    # CEO (Friday) is pinned to the premium tier, so every
                    # Friday fan-out would be N× premium-priced parallel spend.
                    # Pin spawns to the standard tier (the defaults primary);
                    # an explicit sessions_spawn model override still wins when
                    # a child genuinely needs the premium tier.
                    "model": default_model["primary"],
                    # Spawn-level wall clock. Upstream default is 0 = NO
                    # timeout (verified in the deployed schema): the child's
                    # own agent-loop deadline re-arms per retry, so a wedged
                    # child can hold a lane and bill for hours before the 2h
                    # stale valve merely stops counting it. Align with the
                    # run fence above.
                    "runTimeoutSeconds": AGENT_RUN_TIMEOUT_SECONDS,
                    **(
                        {"maxConcurrent": AGENT_SUBAGENT_MAX_CONCURRENT_TRIAL}
                        if provider_choice == BEDROCK_TRIAL_CHOICE
                        else {}
                    ),
                },
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
            # Default denies — see the TOOL_DENY_* module constants for the
            # per-entry rationale. node_proxy.py toggles "nodes" to enabled
            # dynamically when a desktop node pairs (and back on disconnect)
            # by rewriting this list from the same constants.
            "deny": list(TOOL_DENY_WITH_NODES),
            # Upstream's own tool-loop circuit breaker — genericRepeat /
            # knownPollNoProgress / pingPong detectors that trip when an agent
            # calls the same tool with the same args over and over. It defaults
            # to DISABLED. Enabling it costs nothing and is the one bound that
            # stops a stuck loop on its own merits rather than on the clock; the
            # thresholds are left at their upstream defaults.
            "loopDetection": {"enabled": True},
            **_build_exec_policy(),
            "web": {
                # `provider: "firecrawl"` is the FALLBACK-only enrichment path:
                # it fires solely when Readability extraction fails (bot-walled
                # pages), hitting Firecrawl's hosted key-less starter tier at
                # $0 — far cheaper than escalating to in-container Chromium
                # (RAM on a 2 GB task + many more tokens). Normal fetches never
                # leave the container.
                "fetch": {"enabled": True, "provider": "firecrawl"},
                # Route native web_search through Parallel's free, keyless Search
                # MCP (search.parallel.ai/mcp). OpenClaw stopped auto-selecting
                # a keyless provider, so agents that call native web_search
                # throw "disabled or no provider" unless one is set;
                # parallel-free needs no API key and is reliable from Fargate.
                #
                # EXCEPT chatgpt_oauth: a pinned managed provider SUPPRESSES
                # Codex-native hosted search (they're mutually exclusive), so
                # pinning here would downgrade search the owner's ChatGPT
                # subscription already includes. Omitting the key lets the
                # Codex runtime engage its own hosted search automatically.
                **({} if provider_choice == "chatgpt_oauth" else {"search": {"provider": "parallel-free"}}),
            },
            # Tool Search: hide the full tool catalog behind
            # tool_search/tool_describe/tool_call. Worth the extra bridge
            # round-trips ONLY when merged integration MCP servers inflate
            # the per-turn schema payload (Stripe MCP alone is dozens of
            # tools); a bare catalog would pay latency for no byte savings,
            # so plain owners stay direct-exposure. Embedded runtime only —
            # the Codex runtime ignores this key (native tool search) — and
            # never on trial (minimal surface). "code" mode falls back to
            # plain tools mode if the Node child can't spawn; policy
            # (allow/deny) still applies to bridged calls, fail-closed.
            **(
                {"toolSearch": {"mode": "code"}}
                if (integration_mcp_servers and provider_choice != "chatgpt_oauth" and not is_bedrock_trial)
                else {}
            ),
            "media": {
                "image": {"enabled": True},
                "audio": {"enabled": False},
                "video": {"enabled": False},
            },
        },
        # The BEDROCK TRIAL additionally kills the cron SERVICE outright, not
        # just the tool: trial containers must not self-run. This closes the
        # in-container scheduler (including memory-dreaming's own registrations)
        # as a self-run surface that the tool-level deny above doesn't reach —
        # the cron TOOL is already denied fleet-wide via tools.deny, so this is
        # belt-and-suspenders against anything that could still arm a schedule
        # (an imported/carried-forward job, future config drift). Schema-verified
        # key: `cron.enabled` is a boolean on the pinned 7.1 CronSchema. Paid
        # providers emit no cron key at all — the service stays on, governed by
        # tools.deny as before.
        **({"cron": {"enabled": False}} if is_bedrock_trial else {}),
        "skills": {
            "install": {
                "nodeManager": "npm",
            },
            # Autonomous Skill Workshop capture: the agent turns durable
            # corrections ("from now on...") and proven multi-step workflows
            # into pending skill proposals, reviewed in the Skill Proposals
            # panel. Never self-applies from the autonomous path; the
            # in-chat path may apply under the upstream-default
            # approvalPolicy "auto". Paid providers only — each capture is
            # an extra model review over the turn, which a $3-cap trial
            # budget must not fund. Schema-verified key on the pinned 7.1
            # fork: skills.workshop.autonomous.enabled (zod-schema.ts).
            **({} if is_bedrock_trial else {"workshop": {"autonomous": {"enabled": True}}}),
        },
        "hooks": {
            "internal": {
                "entries": {
                    "command-logger": {"enabled": True},
                    "session-memory": {"enabled": True},
                    # Bundled hook emitting a visible "compacting context…"
                    # notice during long turns — pure UX for chat/Teams, zero
                    # model cost.
                    "compaction-notifier": {"enabled": True},
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
            # Inbound agent email needs a STABLE per-thread session key so a
            # reply continues the same conversation instead of cold-starting a
            # throwaway `hook:<uuid>` every time (see agent_mail_thread). Both
            # a request-supplied key AND a mapping-TEMPLATED one are gated by
            # this single flag upstream (resolveHookSessionKey), so there is no
            # way to get per-thread sessions with it false.
            #
            # What still bounds a leaked/replayed hook token, now that it can
            # name a session rather than only create one:
            #   * allowedSessionKeyPrefixes below — it can only reach
            #     `isol8-*` / `hook:*` keys, and `isol8-email:<sha256>` is the
            #     only isol8- OpenClaw session key we mint (the `isol8-` in
            #     routers/connections.py is an STS RoleSessionName, not this).
            #   * allowedAgentIds below — it cannot aim at an arbitrary agent.
            # The delta accepted deliberately: previously such a token could
            # only start fresh sessions; it can now target an existing one and
            # read its prior context. Tightening the prefix to
            # `isol8-email:` would shrink that further and is the obvious next
            # step if anything else ever mints an isol8- session key.
            "allowRequestSessionKey": True,
            # Bound which agents a hook call may target: the seeded roster at
            # write time ("main" for the legacy fallback shape). Nothing in
            # the backend drives /hooks today, so this is pure hardening of
            # an internet-adjacent ingress — a leaked/replayed hook token
            # cannot aim at an arbitrary agent id. Reprovision rewrites the
            # list with the then-current roster.
            "allowedAgentIds": [a["id"] for a in (paperclip_agents or [])] or ["main"],
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
                # workflow_plugin_entries() seam. llm-task rides the cheap
                # tier where one exists (None on chatgpt_oauth/trial —
                # enable-only, inherits the agent default).
                **workflow_plugin_entries(llm_task_model=cheap_model),
                # memory-core's dreaming subsystem (light phase only; REM + deep
                # OFF). Shared with the provider hot-swap path via the
                # memory_plugin_entries() seam so a Bedrock <-> ChatGPT switch
                # doesn't drop it.
                **memory_plugin_entries(),
                # Parallel web-search provider plugin
                # (@openclaw/parallel-plugin, baked into the extended image).
                # Enables the keyless parallel-free provider that
                # tools.web.search.provider selects above.
                "parallel": {"enabled": True},
                # First-party Isol8 marketplace tools (marketplace_search,
                # recommend_agents), baked root-owned at
                # /app/dist/extensions/isol8-marketplace. READ-ONLY: neither
                # tool mutates the roster — hiring always ends at a human
                # click (the hire card in chat, or a Paperclip hire_agent
                # approval in Teams). Enabled on EVERY provider path: an
                # agent that cannot see the catalog recommends nothing, which
                # is the behaviour this replaced. The tools are non-optional,
                # so the explicit tools.allow ["*"] above surfaces them.
                "isol8-marketplace": {"enabled": True},
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
            # "warn" (the old value) never deletes ANYTHING — unbounded
            # session-store + archive growth on EFS, and giant session files
            # are upstream's documented pre-OOM signature on small tasks. The
            # eviction fear that motivated warn is obsolete: enforce preserves
            # durable human conversations (main/group/thread), reaps only
            # SYNTHETIC rows (cron/hook/heartbeat/ACP/subagent — exactly what
            # our autonomous fleet mass-produces), and zstd-archives before
            # any deletion. Bounds are generous multiples of upstream's
            # defaults (500 rows / 30d) to protect heavy owners.
            # resetArchiveRetention defaults to keep-forever — set explicitly.
            "maintenance": {
                "mode": "enforce",
                "maxEntries": 2000,
                "pruneAfter": "90d",
                "maxDiskBytes": "500mb",
                "resetArchiveRetention": "90d",
            },
            # Unconfigured, sessions reset daily at 04:00 gateway-local =
            # 04:00 UTC = 23:00 ET — rolling a US owner's live evening chat
            # on their next message. Idle-based reset needs no owner-timezone
            # knowledge: a session resets only after 24h of silence.
            "reset": {"mode": "idle", "idleMinutes": 1440},
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
            # Compact role snapshots by default — the agent can still request
            # a full AI snapshot per-call. Direct token reduction on every
            # autonomous browser turn (matters against the trial cap and on
            # gated Bedrock margins).
            "snapshotDefaults": {"mode": "efficient"},
            # Upstream tab defaults (8 tabs / 120m idle) are desktop-sized;
            # on a 1 vCPU / 4 GB Fargate task live Chromium tabs are still
            # a resource constraint, so close idle ones aggressively.
            "tabCleanup": {"idleMinutes": 30, "maxTabsPerSession": 4},
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
        # Chat-command surfaces. isCommandFlagEnabled (OpenClaw
        # config/commands.flags.ts) enables a privileged command only when its
        # flag is `True`; `restart` is the exception (on unless `False`).
        # Always-on commands (help/status/reset/model/…) are not gated here —
        # the chat menu hides the irrelevant ones client-side (see
        # lib/slashCommands.ts HIDDEN_COMMANDS).
        #
        # Curation: the control panel owns MCP/plugins/config writes, so those
        # must not be mutable from chat; host-shell (/bash) and gateway
        # /restart widen the trust boundary too far for chat.
        "commands": {
            # /debug applies runtime-only overrides to ANY config path —
            # including tools.deny (re-arming the fleet-denied cron tool) and
            # the runaway bounds — and on an always-on container "runtime
            # only" effectively persists. Owner-gated chat is not a
            # sufficient boundary for that in prod; admin config-patch levers
            # cover prod diagnostics. Kept on in dev/staging for debugging.
            "debug": settings.ENVIRONMENT != "prod",
            "restart": False,  # /restart — gateway restart is operator-only, never from chat
        },
    }

    # Paperclip is no longer wired as an MCP server. The MCP env is
    # container-wide + shared-frozen, so every agent that called it acted as
    # Friday — wrong attribution. Each agent now authenticates to Paperclip
    # with its OWN key via the bundled `paperclip` skill (curl), loading its
    # identity from a per-agent file in its workspace
    # (``friday_identity.pre_stage_paperclip_identities`` +
    # ``paperclip_adapter_config.claimed_api_key_path``). See the seed prompt
    # in ``friday_identity._AGENTS_TEMPLATE``.

    # Merge the owner's connected integration MCP servers (Stripe, …) into the
    # mcp.servers map. A reprovision does a full overwrite of openclaw.json, so
    # unless these are re-injected here the connection is dropped. Each value is
    # a full {command, args, env} block.
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
    #
    # Skipped on the BEDROCK TRIAL: a channel bot is an inbound self-run surface
    # (any Telegram peer can wake the agent, with no human of ours in the loop),
    # so a trial reprovision must not resurrect saved accounts. This closes the
    # carried-forward path, so an owner who upgraded-then-reverted — or whose row
    # still carries stale accounts — can't have a channel silently re-armed
    # underneath them.
    if channel_accounts and not is_bedrock_trial:
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
            # Progress drafts: one channel message edited live with tool/status
            # lines during long autonomous turns, replaced by the final answer —
            # instead of minutes of silence. Telegram and Slack default to
            # plain block streaming upstream; Discord already defaults to
            # progress, so it needs no key. Zero extra LLM calls (edits only).
            # (streaming.progress.commandText is NOT in the deployed schema —
            # do not add it before the next image bump.)
            if provider in ("telegram", "slack"):
                channel.setdefault("streaming", {"mode": "progress"})
            for account_id in accounts:
                binding = {
                    "type": "route",
                    "agentId": account_id,
                    "match": {"channel": provider, "accountId": account_id},
                }
                if binding not in bindings:
                    bindings.append(binding)

    # ACP coding-harness runtime (Jarvis-style delegation: a native agent can
    # ``sessions_spawn({runtime:"acp"})`` Claude Code / Codex / Gemini). On
    # fleet-wide; ``acp_enabled=False`` is still passed by the marketplace
    # deploy path when a listing does not declare ``requires_acp``. Harnesses
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
    integration_mcp_servers: dict | None = None,
    aip_arns: dict[str, str] | None = None,
    channel_accounts: dict[str, dict[str, dict]] | None = None,
    acp_enabled: bool = True,
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
            ``"bedrock_claude"``, ``"bedrock_trial"``. ``bedrock_trial`` is the
            free-tier config MODE (one AIP tier + the self-run lockdown),
            resolved from the owner's ``trial_local`` subscription_status — it
            is never a stored provider_choice.
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
        integration_mcp_servers=integration_mcp_servers,
        aip_arns=aip_arns,
        channel_accounts=channel_accounts,
        acp_enabled=acp_enabled,
    )

    def _write() -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2))

    await asyncio.to_thread(_write)


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
