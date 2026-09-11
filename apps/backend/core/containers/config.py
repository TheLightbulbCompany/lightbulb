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
import os
import secrets
import tempfile
import time
from pathlib import Path

from core import openclaw_version
from core.config import settings
from core.services.config_schema import gate_config
from core.services.efs_file_lock import (
    chown_uid_1000_best_effort,
    get_file_lock,
    openclaw_config_sidecar_lock,
)
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
# auto-restore (src/config/io.observe-recovery.ts) and must match what
# openclaw itself self-writes (io.ts uses `VERSION` from package.json), or
# warnIfConfigFromFuture (io.ts) fires. Deliberately the DEPLOYED image's
# version, NOT the build-target `tag` — `tag` can be bumped ahead of a per-env
# promotion, and a provision in that window must not stamp a future version.
DEPLOYED_UPSTREAM_VERSION = openclaw_version.deployed_upstream_version()


def _target_upstream(deployed_tag: str | None) -> str:
    """Upstream version of the image this config will actually RUN on.

    ``deployed_tag`` is the extended tag of that image (an image roll passes
    the one it is rolling TO). Falls back to this environment's pin, which is
    correct only for a fresh provision -- a live container can lag the pin by
    several tags, and during a promotion or rollback it lags it by a whole
    upstream version.

    Everything version-shaped in the rendered config derives from THIS, not
    from the pin. Reading the pin instead is what wrote 8.1-only keys and an
    8.1 ``meta.lastTouchedVersion`` onto a 7.2 image during a founder
    rollback: the 7.2 binary refused the stamp outright ("older than the
    config last written by OpenClaw 2026.8.1") before it ever reached the
    keys, and the container crash-looped.
    """
    prefix = (deployed_tag or "").split("-", 1)[0]
    return prefix or openclaw_version.deployed_upstream_version()


def _emits_81_shape(deployed_tag: str | None) -> bool:
    """True when the TARGET image is 2026.8.1+ (see :func:`_target_upstream`)."""
    return openclaw_version.version_at_least(_target_upstream(deployed_tag), "2026.8.1")


# PAC served to the browser tool's Chrome when BROWSER_EGRESS_PROXY_PORT is set,
# routing page loads through the container's Cloudflare WARP local proxy instead
# of the AWS NAT IP. `%d` is that port.
#
# A PAC rather than the simpler `--proxy-server=socks5://…` for ONE reason: the
# trailing `; DIRECT`. If warp-svc is not running — an unenrolled container, a
# failed enrollment, a knob flipped ahead of an image roll — Chrome falls back
# to today's egress instead of failing every page load. Verified end to end
# against a real WARP proxy (Chromium 2026-08-21): proxy up -> cdn-cgi/trace
# reports `warp=on` and a Cloudflare IP; proxy killed -> the same PAC returns
# the origin IP with `warp=off` in 0.6s; the same PAC WITHOUT `; DIRECT` fails
# the load outright, which is what the fallback is buying.
#
# Chrome does honour `data:` PAC URLs (verified the same day, Chrome 151: a PAC
# handed over a data: URL sent traffic to a listener under test exactly as
# --proxy-server did). Keeping the PAC inline avoids putting a file on EFS that
# a browser launch would then depend on.
#
# The loopback arm is belt-and-braces — Chromium applies implicit localhost
# bypasses anyway, and Chrome only ever LISTENS on the CDP port rather than
# dialling it — but it makes the intent legible: nothing local is ever proxied.
_BROWSER_EGRESS_PAC_JS = (
    "function FindProxyForURL(u,h){"
    'if(isPlainHostName(h)||shExpMatch(h,"127.*")||h=="::1")return "DIRECT";'
    'return "SOCKS5 127.0.0.1:%d; DIRECT";}'
)


def _browser_egress_args() -> list[str]:
    """Chrome flags routing browser egress through WARP, or [] when off."""
    port = settings.BROWSER_EGRESS_PROXY_PORT
    if not port:
        return []
    pac = base64.b64encode((_BROWSER_EGRESS_PAC_JS % port).encode()).decode()
    return [f"--proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,{pac}"]


# Channel provider ids we provision a config block for, and whose accounts must
# survive a reprovision (read back by ecs_manager._provision_channel_accounts).
# Single source of truth — the channels block in build_openclaw_config_dict is
# built from this, and ecs_manager imports it, so the two can't drift.
CHANNEL_PROVIDER_IDS = ("telegram", "discord", "slack")

# The two Bedrock config modes. Both emit the same `amazon-bedrock` provider
# block shape; they differ in the CATALOG (how many tiers the bedrock-gate will
# admit for this owner) and in POLICY (a trial container must not self-run).
#
#   bedrock_claude — the paid three-tier ladder (opus / sonnet / haiku).
#   bedrock_trial  — the no-card free tier. Exactly ONE tier (sonnet), because
#                    ensure_owner_aips mints one AIP and the gate admits only ARNs
#                    present in that owner's own token row (bedrock-gate auth.mjs,
#                    `tierForModel` -> forbiddenAip): emitting opus/haiku here
#                    would 403 on every turn that touched them. Model pinning is
#                    the GATE's job, not IAM's — build_owner_bedrock_policy grants
#                    no bedrock:Invoke* to anyone.
#
# `bedrock_trial` is a config MODE resolved from the owner's subscription_status
# (`trial_local`), never a stored provider_choice — same relationship the deleted
# `meta` branch had. It is deliberately a distinct provider_choice string rather
# than an inference from the AIP map so the trial LOCKDOWN below is an explicit
# product decision, not an accident of how many AIPs happen to exist.
BEDROCK_TRIAL_CHOICE = "bedrock_trial"
BEDROCK_PROVIDER_CHOICES = ("bedrock_claude", BEDROCK_TRIAL_CHOICE)

# In-container XDG data home for the OpenCode harness. The EFS access point
# chroots /users/{owner_id} to /home/node/.openclaw, so this maps to
# <EFS>/users/{owner_id}/opencode-data — where workspace.pre_stage_opencode_auth
# writes opencode/auth.json. Keep the two in lockstep.
OPENCODE_XDG_DATA_DIR = "/home/node/.openclaw/opencode-data"

# ---------------------------------------------------------------------------
# Runaway bounds — applied to EVERY provider path (see build_openclaw_config_dict).
#
# These bound how far a run can RUN AWAY; none of them changes what an agent can
# DO. OpenClaw's agentic loop is a literal `while (true)` with no step, tool-call
# or token cap (the strict config schema rejects maxSteps/maxToolCalls/budget
# outright), so without these a single un-aborted loop can burn thousands of
# dollars against whichever credential the owner is on.
#
# They are NOT the enforcement fence — the agent has `exec: full` and can rewrite
# its own openclaw.json. What the real fence is depends on the path:
#
# - BEDROCK, both tiers: no owner task role carries `bedrock:Invoke*` at all
#   (build_owner_bedrock_policy is deny-only), so the shell path AccessDenies and
#   the bedrock-gate is the only door. What the gate does behind that door is NOT
#   the same on both tiers:
#     * TRIAL — a true valve. Reserve against the owner's counter, 429 before a
#       single token, settle Bedrock's reported usage when the stream ends.
#     * PAID  — admission is checked against a CACHED Metronome projection (the
#       metronome-fast-gate row). When that row is absent — e.g. a freshly
#       converted owner, since it is only written by the low-balance webhook or a
#       lazy refresh — `paidReserve` admits unconditionally and debits nothing
#       (ledger.mjs: "no row → allow (backstopped)"). So paid spend is NOT
#       per-call fenced; the backstop is the meter Lambda reconciling from
#       Bedrock's own invocation logs, plus its deny-flag + desiredCount=0 kill.
#   Note settle happens AFTER the stream, not before it — the pre-token check is
#   the reservation, not the settlement.
#
# - BYO-key and ChatGPT-OAuth: no gate at all, the owner's own credential is used
#   directly. For those owners these bounds are the ONLY thing between a stuck
#   loop and their bill.
#
# Cheap insurance everywhere; load-bearing off the trial path.
# ---------------------------------------------------------------------------

# agents.defaults.timeoutSeconds — the big one. Upstream defaults to 172800
# (48 HOURS) and Isol8 has never set it. 30 minutes sits far above any real turn
# (the Paperclip adapter already runs with a 600s cap in production) while turning
# a ~$2,600 runaway into a ~$27 one.
AGENT_RUN_TIMEOUT_SECONDS = 1800

# logging.redactPatterns. THE single source — provision-time writes these, and
# `config_patcher.ensure_redact_patterns` carries them onto ALREADY-provisioned
# containers during an image roll. Both read this tuple, so an owner healed by
# the roll and one provisioned fresh cannot end up with different rules.
#
# Upstream PREPENDS these to its own DEFAULT_REDACT_PATTERNS rather than
# replacing them, so adding here only ever widens coverage.
REDACT_PATTERNS: tuple[str, ...] = (
    # Paperclip agent API keys: `pcp_` + hex. Bounded length so a short
    # lookalike in documentation prose is not mangled. Upstream's vendor
    # prefixes cover sk-/ghp_/github_pat_/gho_/ghu_ but not this one, which is
    # how a live key reached an owner's transcript on 2026-08-14 — it arrived
    # as prose ("token: pcp_…"), so the KEY=value pattern missed it too.
    r"(pcp_[A-Za-z0-9]{16,})",
    # Stripe shapes upstream misses: rk_test_ sits in upstream's prefilter
    # with no pattern behind it (rk_live_/sk_live_/sk_test_ are covered);
    # whsec_ is a webhook signing secret our own connect flow tells owners to
    # paste; ac_ is a Stripe OAuth authorization code. Prefix patterns cannot
    # catch a bare opaque OAuth token — that needs redaction at the sinks —
    # but they stop every prefixed Stripe credential from reaching a
    # transcript as prose.
    r"(rk_test_[A-Za-z0-9]{8,})",
    r"(whsec_[A-Za-z0-9]{16,})",
    r"(ac_[A-Za-z0-9]{16,})",
)

# tools.toolSearch.mode. THE single source — provision-time (below) and the
# provider swap's re-pin (core.services.billing_service) must write the same
# value, or a swapped owner silently runs a different tool-search mode than a
# freshly provisioned one. That is not hypothetical: the swap used to delete
# the key outright, dropping owners back onto the full tool catalog. Sharing
# one constant makes the drift impossible rather than merely tested for.
#
# See the toolSearch block below for why this is "tools" and not "code".
TOOL_SEARCH_MODE = "tools"

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

# agents.defaults.maxConcurrent — the global semaphore every run entrance passes.
# We used to leave this at the upstream default, which through 7.2 was a fixed
# DEFAULT_AGENT_MAX_CONCURRENT = 4. 2026.8.1 replaced the constant with
# min(16, max(8, availableParallelism)) (agent-limits.ts) — and the max(8, ...)
# is a FLOOR, so a 1 vCPU box cannot resolve below 8. Worse, os.availableParallelism()
# is libuv's uv_available_parallelism: it honours cpuset but NOT the CFS cpu quota
# Fargate limits us with, so a 1 vCPU / 4 GB task reads the HOST core count and
# lands on the ceiling of 16. That is 2-4x what prod ran for months, on the box
# that already crash-looped an 11-agent owner at boot (24 SQLite DBs restoring
# concurrently starved the single vCPU; "database is locked" is what SQLite
# reports when a starved process exhausts its busy timeout).
#
# So pin it. 4 is not a new number — it is the value 7.2 ran in prod. The value
# is also memoized per process and materialized into the effective config by
# applyAgentDefaults, so it cannot be corrected at runtime.
AGENT_MAX_CONCURRENT = 4

# agents.defaults.subagents.maxConcurrent — upstream default 8. The subagent lane
# does NOT count against the main agents.defaults.maxConcurrent semaphore, so it
# needs its own bound.
#
# This MOVES WITH AGENT_MAX_CONCURRENT and must never be left above it. The two
# numbers bound the same scarce resource — the 1 vCPU Fargate task — through two
# independent doors: the global run semaphore, and a subagent lane that is
# explicitly exempt from it. Pinning the semaphore to 4 while leaving the lane at
# upstream's 8 does not cap concurrency at 4, it caps it at 12, on the box that
# already crash-looped an 11-agent owner at boot. So a change to either constant
# is a change to both.
#
# It used to be trial-only, on the argument that a paid owner's spend is bounded
# by their own credits. That was the wrong axis: this is a CPU bound, not a
# spend bound, and the CPU is the same 1 vCPU on every plan.
AGENT_SUBAGENT_MAX_CONCURRENT = AGENT_MAX_CONCURRENT
# The trial box gets a tighter lane still: no human is in the loop, and a
# fan-out is the fastest way to burn the $3.00/day cap.
AGENT_SUBAGENT_MAX_CONCURRENT_TRIAL = 2

# agents.defaults.contextTokens — BEDROCK ONLY. Our Bedrock catalog declares a
# 1,000,000-token context window, so one call may take up to ~$6.74 of input.
# Bounding the effective budget caps that and triggers compaction/contextPruning
# earlier (their ratios are measured against this value, not the model's
# advertised window). Deliberately not set on the other paths: the ChatGPT/Codex
# path serves ~272k and must keep inheriting its own budget.
#
# Do NOT round this down "to be safe". 200_000 is exactly 8.1's XL tool-result
# tier boundary (tool-result-limits.ts: `tokens >= 200_000` → 64_000 chars,
# `>= 100_000` → 32_000, else 16_000), and the comparison is `>=`, so sitting ON
# the boundary buys the most generous live tool-result cap. 199_999 would halve
# it. It is also upstream's own DEFAULT_CONTEXT_TOKENS, i.e. the value the tier
# was drawn around.
BEDROCK_CONTEXT_TOKENS = 200_000

# session.maintenance.maxDiskBytes / highWaterBytes — the one number in this
# file that is arithmetic against a physical disk rather than a taste call.
#
# WHAT CHANGED UNDER US AT 8.1. `src/config/sessions/session-history-eviction.ts`
# is new at 2026.8.1 (absent at v2026.7.2-isol8.4). Through 7.2 this budget was
# measured against LOGICAL bytes — projected store entries + transcript +
# trajectory (session-accessor.sqlite-maintenance.ts:150). At 8.1 it is measured
# against PHYSICAL usage: the SQLite main file + its WAL + everything in the
# agent's sessions/ dir (disk-budget.ts `measureSessionPhysicalDiskUsage`). SQLite
# main files do not shrink on delete, so the same string now buys far less real
# history. Crossing maxDiskBytes prunes retained zstd archives with fs.promises.rm
# and then evicts historical sessions oldest-first until usage falls to
# highWaterBytes. We do not set `preserveRecent`, so on THIS path there is no
# recency floor and no synthetic-only protection: a human's old /chat thread is a
# candidate like any other. That is the opposite of what the block below used to
# claim, and it is silent — the owner just finds their history gone.
#
# THE BUDGET IS PER AGENT, not per container. paths.ts resolves the store to
# <state>/agents/<agentId>/sessions and the DB to
# <state>/agents/<agentId>/agent/openclaw-agent.sqlite, and the sweep measures one
# agent's directory. So the container-wide cost is N_agents x this number.
#
# WHY NOT UPSTREAM'S 10 GiB (store-maintenance.ts DEFAULT_SESSION_MAX_DISK_BYTES).
# Upstream sizes for a host with a real disk. Ours is a Fargate task, and these
# databases are NOT on EFS: container-stack.ts nests two config-less ECS volumes
# (`openclaw-state`, `openclaw-agents`) inside the EFS mount, so state/ and
# agents/ are task-local ext4 drawing from the task's ephemeral storage. That
# storage is the Fargate DEFAULT — `ephemeralStorage` is declared nowhere, and
# ecs_manager's register_task_definition kwargs whitelist would drop it if it
# were — so the whole fleet has 20 GiB, shared. Ten agents x 10 GiB is 5x the
# disk.
#
# Fixed per-agent budget: ordinary large identity rosters fit on the existing
# 20 GiB task without turning identity count into a commercial entitlement.
# ponytail: very large simultaneously active rosters still need measured task
# storage work; keep the ECS reliability size-up until production boot metrics
# show Standard rosters above ten no longer contend.
SESSION_MAX_DISK_MIB = 256
SESSION_HIGH_WATER_MIB = 204

# ---------------------------------------------------------------------------
# Tool deny lists — SINGLE SOURCE. node_proxy.py REPLACES tools.deny wholesale
# when a desktop node pairs/unpairs, so it derives both variants from these;
# a hand-written list anywhere else silently re-enables an entry on the next
# pair event.
#
# - canvas: no product surface (Control UI disabled). Belt on top of the
#   braces — workflow_plugin_entries pins the canvas PLUGIN off, which is what
#   also removes its always-loaded skill from the prompt; this entry keeps the
#   tool denied even if the plugin is ever re-enabled by hand.
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
# - dashboard: 8.1's interactive-widget tool renders as an iframe served over
#   gateway HTTP — a surface we have no door for (we only relay WS) — so an
#   agent could burn owner spend building widgets nobody can ever see.
# ---------------------------------------------------------------------------
TOOL_DENY_BASE = ("canvas", "cron", "tts", "music_generate", "video_generate", "dashboard")
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
    Upstream's ``listEffectivePairedDeviceRoles`` (src/infra/device-pairing.ts)
    fails closed for tokenless paired records — even with ``role: "node"``
    and ``roles: ["node"]`` set, the WS handshake's role-upgrade gate
    (server/ws-connection/message-handler.ts) rejects the
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
    persists the inner record (``openclaw/src/infra/device-pairing.ts``).
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
    file (see OpenClaw 4.5 `handshake-auth-helpers.ts`).

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
    every exec call silently (openclaw/src/agents/exec-defaults.ts).
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
    deployed_tag: str | None = None,
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
    # Same shape gate as build_openclaw_config_dict: 2026.8.1 moved the
    # effective-context bound from agents.defaults onto the per-model entries,
    # so which side carries it depends on the image this environment runs.
    _emit_81_shape = _emits_81_shape(deployed_tag)
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
        # gpt-5.6-sol, not gpt-5.5: at 2026.8.1 the openai manifest marks
        # gpt-5.5 `status: deprecated` (replacedBy gpt-5.6-sol -- identical
        # cost, context window and reasoning class), and upstream's own
        # fresh-setup default moved to gpt-5.6-sol (#122178). Deliberately NOT
        # gated on _emit_81_shape like modelPolicy below: gpt-5.6-sol is
        # already in the 7.2 manifest (verified at fork tag v2026.7.2-isol8.8),
        # so a 7.2 rollback resolves it fine. Deprecated refs
        # still RESOLVE (only picker visibility changes, and a configured
        # ref stays visible), so this was latent rather than live -- but the
        # account-scoped discovery enabled by model_policy_block() drops
        # manifest rows the account no longer lists, and a primary that
        # vanishes from the live list fails every run.
        #
        # The fallback must be a DIFFERENT model: upstream's fallback runner
        # dedupes candidates by provider/model key, so a self-fallback leaves
        # zero failover candidates and a single provider 5xx kills the run
        # (FailoverError, next=none). gpt-5.4 is the closest tier in the
        # Codex-routable OAuth allowlist; fallbacks are error-only, and the
        # plan is flat-rate, so it engages only when the primary itself errors.
        return (
            {},
            {"primary": "openai/gpt-5.6-sol", "fallbacks": ["openai/gpt-5.4"]},
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
                    # Same swap as the OpenAI path below (gpt-5.5 -> gpt-5.6-sol),
                    # finally carried across: 8.1 dropped claude-sonnet-4-6 from
                    # extensions/anthropic's catalog. A ref the static catalog no
                    # longer resolves depends entirely on the plugin's live
                    # discovery refresh against the owner's key — and before that
                    # lands (cold container, first run, key or network failure)
                    # model-setup throws model_not_found and the primary silently
                    # degrades to the haiku fallback.
                    "primary": "anthropic/claude-sonnet-5",
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
                    # EFFECTIVE context budget. 2026.8.1 removed
                    # agents.defaults.contextTokens; the per-model key is where the
                    # bound lives now. Load-bearing for COST, not just fit: pruning
                    # ratios and compaction measure against this, and only the
                    # Bedrock catalog declares a 1M window (one full call is up to
                    # ~$6.74 of input).
                    **({"contextTokens": BEDROCK_CONTEXT_TOKENS} if _emit_81_shape else {}),
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
                **({"contextTokens": BEDROCK_CONTEXT_TOKENS} if _emit_81_shape else {}),
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
                    # Equal to this model's window, so a no-op today -- set
                    # explicitly anyway so the effective budget does not depend on
                    # how upstream resolves contextWindow vs contextTokens, and so
                    # every Bedrock entry carries the bound uniformly.
                    **({"contextTokens": BEDROCK_CONTEXT_TOKENS} if _emit_81_shape else {}),
                    "maxTokens": 8192,
                }
            )
        # Fallbacks are error-only, not cost routing. With a haiku tier the
        # failover REDUCES spend (cheapest priced tier). With no cheaper tier —
        # the trial's single-model catalog — there is NO fallback at all, and the
        # key is omitted rather than pointed back at the primary.
        #
        # A self-fallback was emitted here for a while. It could never fire:
        # createModelCandidateCollector (model-fallback-candidates.ts) dedupes on
        # `provider/model`, and the primary is added first as the "requested"
        # candidate, so the identical fallback is dropped and the chain is one
        # entry long. What it DID do is make hasConfiguredModelFallbacks() report
        # true, which flips `fallbackConfigured` on every run: past the overload
        # profile-rotation cap the runner then stops rotating and throws a
        # FailoverError to reach a model lane that has nothing in it. Omitting the
        # key keeps that flag honest.
        fallback_id = haiku_id
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
                **({"fallbacks": [f"amazon-bedrock/{fallback_id}"]} if fallback_id else {}),
            },
            {
                "amazon-bedrock": {
                    "config": {"discovery": {"enabled": False}},
                },
            },
        )
    raise ValueError(f"Unknown provider_choice: {provider_choice!r}")


def model_policy_block(
    provider_choice: str,
    default_model: dict,
    *,
    deployed_tag: str | None = None,
) -> dict:
    """``agents.defaults.modelPolicy`` for the owner's provider path, or ``{}``.

    WHY: the model picker resolves against the STATIC plugin manifest unless
    full discovery is escalated, and the manifest is why a ChatGPT-OAuth owner
    was offered a model their account cannot serve (bodyless 400 on select).
    Upstream #115953/#116857 escalates to the ACCOUNT-scoped catalog when
    ``agents.defaults.modelPolicy.allow`` carries a provider wildcard
    (modelCatalogBrowseRequiresFullDiscovery, model-catalog-browse.ts). It has
    to be this key: the legacy ``agents.defaults.models`` map escalates only
    the ``configured`` view, never the ``default`` view the picker sends.

    Emitted only for providers with a live or key-scoped discovery source
    (chatgpt_oauth, byo_key). NEVER for Bedrock: we ship an explicit static,
    pricing-matched catalog with plugin discovery disabled, so there is
    nothing to discover -- and ``allow`` is a RESTRICTIVE allowlist (allowAny
    flips false the moment it has an entry), so on Bedrock it would only add
    a way to lock an owner out of their own catalog.

    The wildcard is derived from the primary's provider segment rather than
    hardcoded per path so it cannot drift from ``build_provider_block``.

    8.1-only: ``modelPolicy`` does not exist in the 7.2 schema and
    ``agents.defaults`` is ``.strict()`` there, so emitting it onto a 7.2
    image fails validation and the gateway refuses to start.

    SINGLE source of truth for this block. The provider-switch hot path
    (``config_patcher.replace_provider_block``) must be handed its result,
    because the allowlist is restrictive: a ChatGPT -> Bedrock switch that
    left ``["openai/*"]`` behind would reject every amazon-bedrock model.
    """
    if provider_choice not in ("chatgpt_oauth", "byo_key"):
        return {}
    if not _emits_81_shape(deployed_tag):
        return {}
    provider = str(default_model.get("primary", "")).split("/", 1)[0]
    if not provider:
        return {}
    return {"modelPolicy": {"allow": [f"{provider}/*"]}}


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
            # claude-opus-4-6 was dropped from the 8.1 catalog. This one matters
            # more than the primary above: the CEO pin has NO per-agent fallback,
            # so a ref the catalog cannot resolve does not degrade — it fails
            # every Friday run until discovery refreshes.
            return "anthropic/claude-opus-5"
        if byo_provider == "openai":
            # Same swap as the OAuth primary: gpt-5.5 is deprecated at 8.1 and
            # the BYO path now runs key-scoped discovery too (model_policy_block),
            # so a CEO pinned to a ref the key's live list omits would fail
            # every Friday run. gpt-5.6-sol is the drop-in replacement.
            return "openai/gpt-5.6-sol"
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
    heartbeat inherit the primary (gpt-5.6-sol), which the OAuth token can actually
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


def _default_agent_id(paperclip_agents: list[dict]) -> str:
    """The agent that owns ambient work: the CEO, else the first Paperclip
    agent, else the legacy ``main`` entry an unprovisioned container gets.

    One answer for both places that need it — the legacy ``default: true``
    marker on the roster entry and ``agents.defaults.systemAgent.agentId``
    — so they can never name different agents.
    """
    if not paperclip_agents:
        return "main"
    return next((a["id"] for a in paperclip_agents if a.get("role") == "ceo"), paperclip_agents[0]["id"])


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

    The default agent is emitted FIRST. On 2026.8.1 the loader strips the
    legacy marker at load and ``agents.list`` then reports the first roster
    entry as ``defaultId`` (src/gateway/agent-list.ts — the explicit-ownership
    fallback). Every "Ask Friday" exit in the product resolves through that
    id, so a roster that happened to list Tally first sent the owner's CEO
    prompts to Tally. Order is the only durable signal that resolver reads.

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

    default_id = _default_agent_id(paperclip_agents)

    entries: list[dict] = []
    # Stable sort: the default first, everyone else in Paperclip order.
    for agent in sorted(paperclip_agents, key=lambda a: a["id"] != default_id):
        agent_id = agent["id"]
        entry: dict = {
            "id": agent_id,
            "reasoningDefault": "stream",
            "workspace": f"/home/node/.openclaw/workspace-{agent_id}",
        }
        # Pass through the friendly display name when Paperclip has one.
        # OpenClaw's agent-runtime schema accepts an optional ``name`` on
        # agents.list[] entries (zod-schema.agent-runtime.ts); the
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
        if agent_id == default_id:
            entry["default"] = True
            # The CEO (Friday) runs on the premium tier; everyone else
            # inherits the cheaper agents.defaults.model. ``ceo_model`` is
            # None on flat-rate provider paths (chatgpt_oauth), where there
            # is no premium tier to reserve.
            if ceo_model is not None:
                entry["model"] = ceo_model
        entries.append(entry)
    return entries


def _lightbulb_agent_ids(paperclip_agents: list[dict]) -> list[str]:
    """Canonical account/binding IDs from the same roster as agents.list/entries."""
    return sorted({entry["id"] for entry in _build_agents_list(paperclip_agents)})


def _lightbulb_bindings(agent_ids: list[str]) -> list[dict]:
    return [
        {
            "agentId": agent_id,
            "match": {
                "channel": "lightbulb",
                "accountId": agent_id,
                "peer": {"kind": "channel", "id": "*"},
            },
        }
        for agent_id in agent_ids
    ]


def _build_agents_roster(
    paperclip_agents: list[dict],
    ceo_model: str | None,
    *,
    emit_81_shape: bool,
) -> dict:
    """The roster under the key the TARGET image actually reads.

    2026.8.1 renamed ``agents.list`` (array) to ``agents.entries`` (map keyed
    by agent id, ``id`` stripped from the value) and made ``AgentsSchema``
    strict over ``{ownership, defaults, entries}`` — see
    ``core/services/agent_roster.py`` for the upstream evidence. Writing
    ``list`` to an 8.1 container therefore addresses a key the gateway no
    longer reads, and every config regenerate empties the owner's roster until
    the container is restarted (boot ``doctor --fix`` re-migrates the file).

    The ``default: true`` CEO marker is retired but still explicitly accepted
    at 8.1 ("one legacy default=true marker" — zod-schema.agents.ts), and it
    is what boot doctor consumes to materialize per-surface ownership
    (``talk.agentId``, ``agents.defaults.systemAgent.agentId``, channel
    bindings). Emitting ``agents.ownership: "explicit"`` instead would satisfy
    the schema but leave every ambient surface unowned, so the marker stays.
    """
    roster = _build_agents_list(paperclip_agents, ceo_model=ceo_model)
    if not emit_81_shape:
        return {"list": roster}
    return {"entries": {e["id"]: {k: v for k, v in e.items() if k != "id"} for e in roster}}


def workflow_plugin_entries(llm_task_model: str | None = None) -> dict:
    """Bundled, provider-independent OpenClaw extension entries pinned on every
    container — enabled ones, plus the one we pin OFF.

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
      that don't need the premium default. OpenClaw 9.3 requires configured
      plugin model overrides to be explicitly allowed and named, so the same
      exact model is also the host-level ``llm.allowedCompletionModels``
      boundary.
      ``None`` leaves the entry enable-only so it inherits the agent default.
      The plugin's configSchema is strict (``additionalProperties: false``);
      only schema-listed keys may be added here or the container crash-loops.
    - ``aws-exec-profile`` — baked in-repo plugin whose ``resolve_exec_env``
      hook injects ``AWS_PROFILE=lightbulb-connector`` into the agent's exec'd
      commands only when the owner's connector AWS profile is materialized.
      Inert (returns ``{}``) until that profile exists, so it is safe to enable
      fleet-wide regardless of provider.
    - ``paperclip-exec-creds`` — baked in-repo plugin whose ``resolve_exec_env``
      hook injects this AGENT's ``PAPERCLIP_*`` credentials into its exec'd
      commands only, read per-agent from
      ``workspace-<agentId>/paperclip-claimed-api-key.json``. Inert (returns
      ``{}``) for any agent with no key file, so it is safe fleet-wide. Exists
      so the seeded persona never has to tell the agent to read its own token:
      it used to, and the model printed the token into its visible reasoning,
      which persists to the transcript DB and replicates to S3.

      No longer forward-only: ``update_service`` now carries this entry onto
      already-provisioned owners during an image roll (``ensure_plugin_entries``,
      additive-only) alongside ``ensure_redact_patterns``, so the fleet
      converges without reprovision. The original constraint still holds in
      one direction — a playbook that says credentials are in the environment
      requires the plugin to actually be enabled — which is why the roll path
      applies the plugin entry and never rewrites AGENTS.md in isolation.
    - ``tokenjuice`` — deterministic exec/bash tool-result compaction
      (baked, Dockerfile Layer 13b). Strict-empty configSchema: enable-only,
      never add a ``config`` key or the container crash-loops at boot.
    - ``sage-openclaw`` — Gen Sage ADR tool-call guard (baked, Dockerfile
      Layer 13c). Also strict-empty configSchema — behavior config is the
      image-baked ``/home/node/.sage/config.json``, NOT this entry.
    - ``canvas`` — the ONE entry pinned OFF. It ships ``enabledByDefault: true``
      with ``activation.onStartup`` and ``skills: ["./skills"]``, and that skill
      carries no ``requires``, so it loads into every agent's prompt and offers
      to "present hosted widget documents on a connected macOS panel". We have
      no such panel, and ``canvas`` is in ``TOOL_DENY_BASE`` — so today the
      prompt advertises a capability whose tool is denied. Disabling the plugin
      kills the tool registration, the skill and the startup activation in one
      key; denying the tool alone leaves the other two.

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
        llm_task_entry["llm"] = {
            "allowModelOverride": True,
            "allowedCompletionModels": [llm_task_model],
        }
    return {
        "lobster": {"enabled": True},
        "llm-task": llm_task_entry,
        "aws-exec-profile": {"enabled": True},
        "paperclip-exec-creds": {"enabled": True},
        "tokenjuice": {"enabled": True},
        "sage-openclaw": {"enabled": True},
        # Bundled key-less Firecrawl fetch provider — the Readability-failure
        # fallback that tools.web.fetch.provider selects. Enable-only; adding
        # an apiKey would move it to the owner's paid tier (BYOK surface,
        # not configured here).
        "firecrawl": {"enabled": True},
        # Off, not merely tool-denied — see the docstring.
        "canvas": {"enabled": False},
    }


def _dreaming_frequency(owner_id: str | None) -> str:
    """Nightly dreaming cron, jittered across the 03:00 UTC hour by owner.

    Dreaming is a self-run per agent: it reads the day's transcripts and writes
    consolidated notes. Every paid container firing it in the same minute is a
    fleet-wide thundering herd — a simultaneous Bedrock burst and a simultaneous
    EFS/SQLite burst across every task, for a sweep that has no reason to be
    synchronised.

    The minute is derived from a SHA-256 of the owner id rather than randomised,
    so an owner always dreams in the same minute: the schedule is reproducible
    from the owner id alone when reading a container's logs, and a reprovision
    does not silently move it. ``None`` (fleet-wide callers with no owner in
    hand, e.g. the memory-wiki-only patches) keeps the historical 03:00.
    """
    if not owner_id:
        return "0 3 * * *"
    minute = int(hashlib.sha256(owner_id.encode()).hexdigest(), 16) % 60
    return f"{minute} 3 * * *"


def memory_plugin_entries(owner_id: str | None = None) -> dict:
    """The bundled memory extensions (``memory-core`` + ``memory-wiki``),
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

    ``memory-wiki`` is the owner's COMPANY BRAIN: a single vault shared by
    every agent in the container (``vault.scope: "global"`` →
    ``~/.openclaw/wiki/main``, markdown on EFS — its compiled snapshot goes to
    the shared SQLite plugin state on the ext4 mount, not EFS). It sits beside
    memory-core (which keeps recall/dreaming) and compiles durable knowledge
    into provenance-tracked pages with ``wiki_search``/``wiki_get``/
    ``wiki_apply`` tools; the plugin ships its own ``wiki-maintainer`` skill,
    so agents learn the conventions from the image, not from us. ``scope`` is
    set explicitly because the shared-vault semantic is the whole point — an
    upstream default flip to per-agent vaults would silently fragment the
    company brain. ``renderMode: "obsidian"`` writes Obsidian-flavored
    ``[[wikilink]]`` markdown; the vault stays browsable in the file viewer.

    The vault is BRIDGE-BACKED (``vaultMode: "bridge"`` + ``bridge.enabled``,
    which defaults OFF upstream). The plugin's default ``isolated`` vault only
    ever gains pages an agent files by hand, which is why the brain reads empty
    on a real org: memory-core writes to its own store and nothing crosses over.
    In bridge mode every ``wiki_*`` tool call first imports memory-core's
    public artifacts — daily notes, dream reports, ``MEMORY.md``, event logs —
    into ``sources/`` pages and recompiles the index (sync-on-read, upstream's
    own design: ``extensions/memory-wiki/src/tool.ts`` calls
    ``syncMemoryWikiImportedSources`` before each tool body; deterministic, no
    inference, no cron and no routine). The skill tells agents to consult the
    wiki before answering, so the first consult is what fills it.
    ``wiki_apply`` stays the way agents file syntheses on top of those
    sources. The remaining ``bridge.*`` flags default true, so enabling is the
    whole configuration.

    Keys are restricted to the upstream configSchemas, which are strict
    (``additionalProperties:false``) at every level — the only key under
    memory-core's ``config`` is ``dreaming``; memory-wiki takes ``vaultMode``
    and ``bridge`` at its config root, and its ``vault`` accepts only
    ``scope``/``path``/``renderMode``. A stray key crash-loops the container
    at boot.

    ``owner_id`` only jitters the dreaming minute (see ``_dreaming_frequency``);
    everything else is identical for every owner.

    Returned fresh each call so callers may merge/mutate without corrupting
    shared module state.
    """
    return {
        "memory-core": {
            "enabled": True,
            "config": {
                "dreaming": {
                    "enabled": True,
                    "frequency": _dreaming_frequency(owner_id),
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
        "memory-wiki": {
            "enabled": True,
            "config": {
                "vault": {
                    "scope": "global",  # ONE shared vault = the company brain
                    "renderMode": "obsidian",
                },
                # Bridge mode is what fills the brain: memory-core's public
                # artifacts are imported as sources/ pages. Both keys are
                # required — the plugin gates the import on vaultMode AND
                # bridge.enabled, which is off by default.
                "vaultMode": "bridge",
                "bridge": {"enabled": True},
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

    - ``bedrock_claude`` -> ``["claude"]`` (Claude Code via
      ``CLAUDE_CODE_USE_BEDROCK=1`` + the container task-role IAM).
    - ``byo_key`` + ``anthropic`` -> ``["claude"]`` (``ANTHROPIC_API_KEY``).
    - ``byo_key`` + ``openai``    -> ``["codex"]``  (``OPENAI_API_KEY``).
    - ``chatgpt_oauth`` -> ``["opencode"]``, UNCONDITIONALLY — connection
      state deliberately does NOT gate the config. The credential is a SECOND
      dedicated OAuth grant (``/oauth/chatgpt/harness``) whose auth.json lives
      on EFS; before the owner connects, a spawn starts but the first prompt
      fails with an auth-required error, which the agent relays as "connect
      the coding harness in settings". That differs from the historical
      failed-spawn bug below in the way that matters: this failure is
      user-fixable, that one had no credential path at all. Keeping the config
      connection-agnostic means connect/disconnect touch only the EFS file —
      no config patches, no DDB↔config drift. Requires the ``opencode``
      binary in the extended image (same PR as this code): until that image
      tag is promoted, spawns on stale-image containers fail command-not-found
      — promote promptly after merge.

    History: ``chatgpt_oauth`` used to return ``["codex"]`` on the premise
    that acpx's codex-auth-bridge would pick up the ChatGPT OAuth tokens
    we pre-stage onto EFS. It does not, and never did: the bridge builds an
    ISOLATED ``CODEX_HOME``, copies only ``config.toml`` out of the host one, and
    writes ``codex-home/auth.json`` in exactly one case — ``CODEX_API_KEY`` or
    ``OPENAI_API_KEY`` present in the process env. It never reads OpenClaw's own
    ``credentials/oauth.json`` (different format, different path, written by
    ``workspace.pre_stage_auth_profile_store``). A subscription-OAuth owner
    therefore had NO credential path: every spawn failed ``Authentication
    required`` while ``acp.enabled`` still advertised the harness to the agent.

    That is an upstream property of the CODEX ACP adapter, not something we can
    wire around — openclaw/openclaw#53456 is the same defect on the Claude Code
    side, where ``claude-agent-acp`` rejects Claude Max subscription OAuth.
    OpenCode is the exception that makes ``chatgpt_oauth`` workable: its
    ChatGPT login is a first-class subscription flow (the same Codex device-code
    flow ``oauth_service`` drives), and it reads a plain ``auth.json`` we can
    mint server-side. The grant MUST be a second, dedicated chain — never the
    provider row's tokens (rotating refresh tokens, two holders race into
    ``refresh_token_reused``; see project_openclaw_oauth_auth_model).

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
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            return ["claude"]
        if byo_provider == "openai":
            return ["codex"]
    if provider_choice == "chatgpt_oauth":
        return ["opencode"]
    return []


def build_acp_config(harnesses: list[str], *, emit_81_shape: bool = False) -> tuple[dict, dict]:
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
        # RETIRED AT 8.1. acp.runtime.ttlMinutes, acp.stream.coalesceIdleMs and
        # acp.stream.maxChunkChars are all on 8.1's dead-config-keys list
        # (src/config/dead-config-keys.test.ts), and 8.1's root schema is
        # STRICT: an unrecognized key does not warn, it fails validation for
        # the whole file. Doctor then restores last-known-good, so the
        # container silently keeps its PREVIOUS config and every change in the
        # regenerated one is lost — which is how the founder came up on 8.1
        # still carrying the pre-#2032 wide gateway.trustedProxies and 403ing
        # every gateway RPC. The boot looks healthy; the config is stale.
        **(
            {}
            if emit_81_shape
            else {
                "runtime": {"ttlMinutes": 120},
                "stream": {"coalesceIdleMs": 300, "maxChunkChars": 1200},
            }
        ),
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
    if "opencode" in harnesses:
        # opencode is not an acpx builtin (those are claude/codex wrappers) —
        # it registers through the open agents map, which is strictly
        # {command, args} (no env field in the schema). XDG_DATA_HOME is
        # therefore scoped to the spawned process via an env(1) wrapper: it
        # relocates OpenCode's auth store onto EFS (rotating tokens must
        # survive task replacement) WITHOUT re-pointing every other XDG
        # consumer in the container. `opencode acp` speaks ACP v1 over stdio
        # and requires credentials already on disk (no ACP auth round) —
        # pre_stage_opencode_auth writes them. Refresh serialization: acpx
        # reuses ONE wrapper process per agentCommand (verified in the fork:
        # extensions/acpx/src/runtime.ts, "agentCommand as reuse identity"),
        # so concurrent sessions_spawn calls attach to a single opencode
        # process whose in-process single-flight serializes token refreshes;
        # OpenCode has no cross-process lock on auth.json, so that reuse —
        # plus staging the token's REAL expiry so fresh processes don't
        # refresh at boot, plus OpenAI's ~1h refresh-reuse grace window —
        # is the protection. The runtime TTL bounds process churn.
        acpx_plugin_entry["acpx"]["config"]["agents"] = {
            "opencode": {
                "command": "env",
                "args": [f"XDG_DATA_HOME={OPENCODE_XDG_DATA_DIR}", "opencode", "acp"],
            },
        }
    return acp_block, acpx_plugin_entry


# Per-platform node-command allowlist shared by both roster shapes. 2026.8.1
# renamed gateway.nodes.allowCommands to gateway.nodes.commands.allow; the list
# itself is identical, so only the key moves.
_NODE_ALLOW_COMMANDS = [
    "system.run",
    "system.run.prepare",
    "system.which",
    "system.notify",
    "system.execApprovals.get",
    "system.execApprovals.set",
    "browser.proxy",
    # Node-hosted local MCP servers (desktop sidecar). Approved pairing
    # commands already enter the runtime allowlist on their own; this is the
    # same second door every other desktop-host command here rides.
    "mcp.tools.call.v1",
    "device.info",
    "device.status",
    "device.health",
    "device.permissions",
    "file.fetch",
    "file.write",
    "dir.list",
    "dir.fetch",
    "screen.snapshot",
    # The 2026.8.1 node host / bundled browser plugin declare these
    # unconditionally (see desktop_pair.DESKTOP_CLI_SHELL_COMMANDS /
    # DESKTOP_CLI_BROWSER_COMMANDS for the upstream source refs) — this is the
    # container-side mirror of that same grant, so both allowlists must move
    # together.
    "fs.listDir",
    "terminal.upload",
    "browser.proxy.upload.v1",
]


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
    provisioned_env: dict | None = None,
    aip_arns: dict[str, str] | None = None,
    channel_accounts: dict[str, dict[str, dict]] | None = None,
    channel_bindings: list[dict] | None = None,
    acp_enabled: bool = False,
    deployed_tag: str | None = None,
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

    # See the per-key comments below: this selects the config SHAPE for the image
    # this environment is actually running, not the one we are building toward.
    _emit_81_shape = _emits_81_shape(deployed_tag)
    providers_config, default_model, plugin_entries = build_provider_block(
        provider_choice=provider_choice,
        user_id=user_id,
        byo_provider=byo_provider,
        aip_arns=aip_arns,
        deployed_tag=deployed_tag,
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
    lightbulb_agent_ids = _lightbulb_agent_ids(paperclip_agents or [])

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
        # auto-restore in src/config/io.observe-recovery.ts. That
        # check treats every backend overwrite as anomalous (openclaw
        # adds its own meta after first boot; our overwrites strip it),
        # silently restoring from .bak and clobbering whatever we wrote.
        # Use the real upstream version so warnIfConfigFromFuture
        # (io.ts) parses it as semver and short-circuits cleanly.
        "meta": {"lastTouchedVersion": _target_upstream(deployed_tag)},
        # 2026.8.1 made the root schema strict and retired a dozen keys we set.
        # Emit the shape THIS environment's deployed image actually accepts:
        # writing the 8.1 shape to a 7.2 container silently drops settings it
        # still honours (the audit ledger comes back on, the hooks body cap
        # falls back to 64KB, runRetries back to 160), and writing the 7.2 shape
        # to an 8.1 container fails its strict parse at boot. The gate flips per
        # environment as each tag is promoted -- same contract as the
        # mcp.runtimeScope gate below.
        **({} if _emit_81_shape else {"audit": {"enabled": False}}),
        # The audit ledger writes one IMMEDIATE SQLite transaction per agent/tool
        # event from a dedicated worker thread into the shared state DB. On EFS
        # (rollback journal, NFS lock latency) that worker is the dominant lock
        # contender — steady lock-wait failures that drop writes and stall the
        # gateway event loop. Nothing in Isol8 consumes the ledger (no audit.*
        # RPC callers); tool/run history is covered by transcripts, CloudWatch,
        # and Paperclip run records.
        "gateway": {
            "mode": "local",
            "bind": "lan",
            "auth": auth,
            # LOOPBACK ONLY. Never widen this to a network range, and in
            # particular never re-add an RFC1918 CIDR -- the backend dials the
            # gateway from a VPC address, so any range containing 10.x sweeps
            # the BACKEND ITSELF into the proxy class.
            #
            # 2026.8.1 added ingress attribution (gateway/ingress-attribution.ts
            # + server-http-upgrades.ts, neither present at 7.2). On the WS
            # upgrade it resolves the peer: a remote INSIDE trustedProxies is
            # treated as a proxy and must forward headers naming the real
            # client; when it doesn't, the request is "unattributable-proxy"
            # and the upgrade is refused 403 -- the only 403 on that path. Our
            # backend sends no x-forwarded-* headers because it is not a proxy,
            # it is the client, so a /8 here means every RPC fails: the
            # container boots healthy and is simply unreachable.
            #
            # The /8 was correct once. ae2ca6086 (2026-03-24) set
            # gateway.auth.mode = "trusted-proxy" and authenticated by
            # asserting the user in x-forwarded-user, which made the backend a
            # genuine proxy. That model is gone -- auth.mode is "token" and the
            # pool does the 4.5 signed-device handshake (operator_rpc
            # .perform_signed_connect); nothing in the repo emits
            # x-forwarded-user any more. The entry outlived its mechanism by
            # five months and sat inert until 8.1 became the first version to
            # read it and draw a conclusion.
            "trustedProxies": ["127.0.0.1", "::1"],
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
                **(
                    {"commands": {"allow": _NODE_ALLOW_COMMANDS}}
                    if _emit_81_shape
                    else {"allowCommands": _NODE_ALLOW_COMMANDS}
                ),
            },
        },
        "models": {
            "providers": providers_config,
            **({} if _emit_81_shape else {"pricing": {"enabled": False}}),
            # Upstream defaults pricing.enabled=true: a background OpenRouter
            # + LiteLLM pricing-catalog fetch after every gateway boot. Our
            # catalogs are static with explicit cost blocks (Bedrock) or
            # flat-rate (OAuth/BYO), and billing truth lives in the
            # bedrock-gate/meter — the fetch is pure waste and an egress
            # surface. Configured cost values keep working with it off.
        },
        "agents": {
            "defaults": {
                "model": default_model,
                **model_policy_block(provider_choice, default_model, deployed_tag=deployed_tag),
                # All three left agents.defaults at 2026.8.1: runRetries has no
                # replacement, memorySearch moved to root memory.search, and
                # contextTokens moved onto the per-model Bedrock entries. A 7.2
                # container still honours them and loses real bounds without.
                **({} if _emit_81_shape else {"runRetries": {"max": AGENT_RUN_RETRIES_MAX}}),
                **({} if _emit_81_shape else {"memorySearch": memory_search_entry(provider_choice, byo_provider)}),
                # 2026.8.1 resolves the owner of ambient work — the gateway's own
                # health RPC, the system agent, the shared session store — from
                # this key. Boot doctor materializes it from the legacy
                # ``default: true`` marker and writes it back to disk, but ONLY
                # at boot: a config regenerate on a RUNNING multi-agent container
                # rewrote the file without it, the hot reload does not
                # re-materialize, and every ambient resolver fell through to
                # AgentSelectionRequiredError — the health RPC failed against a
                # healthy process and the owner sat on the provisioning stepper
                # until a task roll. Emit the explicit owner so a regenerate
                # alone is never owner-less. 7.2's strict agents.defaults schema
                # refuses the key.
                # The other FOUR keys that legacy.default-agent-roles.ts
                # materializes from the same marker are deliberately NOT emitted;
                # each was checked against its own reader on the deployed tag:
                #   * talk.agentId — talk/agent-target.ts calls
                #     resolveAmbientOwnerAgentId(cfg, cfg.talk?.agentId), so an
                #     unset value lands on systemAgent above. Already covered.
                #   * heartbeat.agentId — the materializer skips it whenever
                #     agents.defaults.heartbeat is defined, and we ALWAYS define
                #     it (below), so it is never materialized on our shape. Its
                #     only runtime read is which agent the health summary reports
                #     (gateway/health/collector.ts); enrollment is per-agent off
                #     agents.defaults.heartbeat and does not consult it.
                #   * sessionStore.agentId — the materializer skips it unless the
                #     session store is FIXED, and ours is per-agent by omission
                #     (isPerAgentSessionStoreConfig returns true for an unset
                #     session.store). resolvePersistedSessionStoreOwner returns
                #     {kind:"none"} on that shape regardless of the key.
                #   * authInheritance.agentId — read only when the shared auth
                #     store is still at "legacy-main", and in that case its
                #     fallback ("main", via resolveLegacyInheritedAuthAgentId) is
                #     the correct answer: legacy-main IS main's store. Pinning the
                #     CEO there would repoint the inherited-auth dir at an agent
                #     that does not hold it, and isInheritedAuthStoreOwner treats
                #     an explicit value as authoritative even after relocation —
                #     making the CEO permanently undeletable.
                **({"systemAgent": {"agentId": _default_agent_id(paperclip_agents or [])}} if _emit_81_shape else {}),
                # A `/model` directive from chat is best-effort PERSISTED unless
                # this is "session": resolveStickyModelSelectionScope defaults to
                # "effective" when unset, and get-reply-directives-apply.ts then
                # sets canPersistStickyModelSelection for any owner/operator.admin
                # sender — writing the picked model into agents.entries.<id>.model
                # or agents.defaults.model on disk. That is chat mutating the file
                # the backend owns wholesale, so the write survives exactly until
                # the next regenerate and then vanishes without explanation.
                # "session" keeps a picker change where the owner expects it —
                # in the session — and leaves the file to its single writer.
                **({"modelSelectionScope": "session"} if _emit_81_shape else {}),
                **(
                    {}
                    if _emit_81_shape or provider_choice not in BEDROCK_PROVIDER_CHOICES
                    else {"contextTokens": BEDROCK_CONTEXT_TOKENS}
                ),
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
                # company: Atlas can't find a sibling (e.g. a user-made "Forge")
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
                # Global run semaphore — pinned, never inherited. See
                # AGENT_MAX_CONCURRENT: 8.1 made the upstream default derive
                # from the host's CPU count, which on quota-limited Fargate
                # reads the HOST core count, not our 1 vCPU.
                "maxConcurrent": AGENT_MAX_CONCURRENT,
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
                    # Pinned on EVERY path, not just the trial: the subagent lane
                    # is exempt from the maxConcurrent semaphore above, so leaving
                    # it at upstream's 8 makes the real ceiling 4 + 8 on a 1 vCPU
                    # box. Moves in lockstep with AGENT_MAX_CONCURRENT — see the
                    # constant.
                    "maxConcurrent": (
                        AGENT_SUBAGENT_MAX_CONCURRENT_TRIAL
                        if provider_choice == BEDROCK_TRIAL_CHOICE
                        else AGENT_SUBAGENT_MAX_CONCURRENT
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
            # 2026.8.1 keys the roster by agent id under ``entries``; 7.2 wants
            # the ``list`` array. Emitting the wrong one is silent — see
            # _build_agents_roster.
            **_build_agents_roster(
                paperclip_agents or [],
                ceo_model,
                emit_81_shape=_emit_81_shape,
            ),
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
            "citations": "auto",
            # 2026.8.1 retired the qmd backend and moved memorySearch here from
            # agents.defaults; 7.2 wants the mirror image of both.
            **(
                {
                    "search": {
                        **memory_search_entry(provider_choice, byo_provider),
                        # No literal default upstream: 8.1 derives this from
                        # session.dmScope (resolveRememberAcrossConversations).
                        # Ours is "per-account-channel-peer", so it resolves
                        # false today — but that couples a recall decision to an
                        # unrelated key, and when true it ALSO force-enables
                        # session memory and appends "sessions" to the indexed
                        # sources regardless of memory.search.sources. Say it.
                        "rememberAcrossConversations": False,
                    }
                }
                if _emit_81_shape
                else {"backend": "builtin"}
            ),
        },
        "tools": {
            "profile": "full",
            # Shared company agents communicate only through canonical rooms;
            # never expose another agent's private OpenClaw sessions/tools.
            "sessions": {"visibility": "agent"},
            "agentToAgent": {"enabled": False},
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
                # Set on EVERY provider path, chatgpt_oauth included. This
                # previously carried a chatgpt_oauth exception, on the premise
                # that pinning a managed provider would suppress Codex-native
                # hosted search the owner's ChatGPT subscription already pays
                # for. That premise assumed those owners run OpenClaw's Codex
                # runtime. They do not: we never set
                # `agents.defaults.embeddedHarness.runtime`, so every agent runs
                # the default EMBEDDED runtime, which has no hosted search of
                # its own. Prod logs (2026-08-03) showed the result — every
                # chatgpt_oauth research call dying on "web_search is disabled
                # or no provider is available", with no fallback, because
                # firecrawl_search needs an API key we never set either.
                #
                # If chatgpt_oauth owners are ever moved onto the Codex runtime,
                # revisit this line — but change the runtime first, not this.
                "search": {"provider": "parallel-free"},
            },
            # Tool Search: hide the full tool catalog behind
            # tool_search/tool_describe/tool_call. ON FOR EVERY OWNER, with no
            # gating — the catalog is unbounded from this file's point of view
            # (any owner can connect an MCP integration at any time, and Meta's
            # ads server alone measured 95 tools / ~172k tokens against the live
            # endpoint), and a config written today is read for the life of the
            # container. Conditioning on what happens to be connected at write
            # time is what left owners carrying a full catalog after the fact.
            #
            # The previous gate also excluded chatgpt_oauth and trial. Neither
            # exclusion earned its complexity: the Codex runtime never reads this
            # key (resolveToolSearchConfig is called from agent-tools.ts and
            # nowhere else), so it is simply inert there; and trial owners are the
            # ones who can least afford a full catalog against a $3 cap, since
            # integrations are not trial-gated.
            #
            # MODE IS "tools", NOT "code". Both compact the catalog identically —
            # this is only about how the agent reaches a hidden tool.
            #
            # "code" routes every call through a JS bridge whose child is spawned
            # under Node permission mode with an EMPTY environment, no filesystem
            # or network grants, ESM only, and eval/new Function disabled. The
            # whole surface is console.log/warn/error plus
            # openclaw.tools.search/describe/call — no require, no timers, no
            # Buffer, no fetch, no process. An agent therefore CANNOT read a file
            # through the bridge: the plain read tool is hidden, so every read
            # becomes a search -> describe -> call('openclaw:core:read') round
            # trip written in JavaScript. Observed live on dev 2026-08-14: Friday
            # opened its first run with three consecutive
            # `ReferenceError: require is not defined` failures on exactly that
            # path, and could not recover — the model is handed the stringified
            # error with no stack and no statement of what the sandbox actually
            # provides, and there is no retry, no repair hint, and no fallback.
            #
            # The fallback the previous comment here claimed is narrower than it
            # read: resolveToolSearchConfig only downgrades code -> tools when the
            # RUNTIME lacks --permission support. It does nothing for a sandbox
            # error at call time, which is the failure we actually hit.
            #
            # "tools" is upstream's own default fallback for that unsupported
            # case, so it is a well-trodden path rather than an exotic one.
            "toolSearch": {"mode": TOOL_SEARCH_MODE},
            "media": {
                "image": {"enabled": True},
                "audio": {"enabled": False},
                "video": {"enabled": False},
            },
        },
        # cron.store pins the cron STORE KEY to its historical resolved value.
        # Cron rows in the container's state DB are keyed by the resolved
        # jobs.json path STRING (the file itself need not exist); the default
        # derives from the state dir, so relocating the state root off EFS
        # would re-key the store and every existing row would vanish from the
        # scheduler. The pin is byte-equal to today's derived default, making
        # it a no-op on the EFS layout while keeping the key stable across the
        # local-SQLite cutover. Schema-verified: `cron.store` is a string on
        # CronSchema (zod-schema.root-shape.ts, strict-safe).
        #
        # The BEDROCK TRIAL additionally kills the cron SERVICE outright, not
        # just the tool: trial containers must not self-run. This closes the
        # in-container scheduler (including memory-dreaming's own registrations)
        # as a self-run surface that the tool-level deny above doesn't reach —
        # the cron TOOL is already denied fleet-wide via tools.deny, so this is
        # belt-and-suspenders against anything that could still arm a schedule
        # (an imported/carried-forward job, future config drift). Schema-verified
        # key: `cron.enabled` is a boolean on the pinned CronSchema. Paid
        # providers keep the service on, governed by tools.deny as before.
        "cron": {
            **({} if _emit_81_shape else {"store": "/home/node/.openclaw/cron/jobs.json"}),
            **({"enabled": False} if is_bedrock_trial else {}),
        },
        # Transcript/log redaction. Upstream already runs this — mode defaults
        # to "tools" and redactTranscriptMessage is applied on the session
        # APPEND path, so it covers what is persisted to openclaw-agent.sqlite
        # (and therefore the litestream S3 replica), what chat.history replays,
        # and what later turns re-send to the model. It does NOT cover the
        # single in-flight stream to the UI.
        #
        # DEFAULT_REDACT_PATTERNS knows sk-/ghp_/github_pat_/gho_/ghu_, env
        # assignments, Authorization headers and JSON token fields — but not
        # Paperclip's `pcp_` keys, which is why a live one reached an owner's
        # transcript on 2026-08-14 (as prose, so the KEY=value pattern missed
        # it too). Custom patterns are PREPENDED to the defaults upstream, not
        # substituted, so nothing is lost by setting this.
        #
        # This is the backstop, not the fix: the fix is that the agent is no
        # longer told to read its key (paperclip-exec-creds injects it into
        # exec children). This catches the residue — an `env`/`printenv` dump,
        # a third-party script, or a model that pastes it anyway.
        "logging": {"redactPatterns": list(REDACT_PATTERNS)},
        "skills": {
            "install": {
                "nodeManager": "npm",
            },
            # Autonomous Skill Workshop capture: the agent turns durable
            # corrections ("from now on...") and proven multi-step workflows
            # into pending skill proposals, reviewed in the Skill Proposals
            # panel. Paid providers only — each capture is an extra model
            # review over the turn, plus scheduled `skill-collection-review`
            # runs with nobody watching, which a $3-cap trial budget must not
            # fund.
            #
            # ALWAYS EMIT THE BLOCK — never rely on the upstream default to
            # mean "off". 7.x defaulted `autonomous.enabled` to false, so
            # omitting it was equivalent; 2026.8.1 renamed the key to
            # `autonomous.mode` and defaults it to "auto" — the MOST
            # aggressive setting (review after every run AND self-apply).
            # Omitting the block on 8.1 therefore gave trial owners more
            # autonomy than paid owners and burned the whole $3/day cap in
            # minutes of first-run onboarding (prod, 2026-09-01: capped 8
            # minutes in, bootstrap kickoff throttled out on all 5 attempts).
            # Only `mode: "off"` disables it (workshop/config.ts,
            # experience-review.ts, collection-review.ts).
            "workshop": {
                "autonomous": (
                    {"mode": "off" if is_bedrock_trial else "propose"}
                    if _emit_81_shape
                    else {"enabled": not is_bedrock_trial}
                )
            },
        },
        "messages": {
            "groupChat": {
                "unmentionedInbound": "room_event",
                "visibleReplies": "automatic",
                "historyLimit": 50,
            }
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
            # 7.2 ONLY. Upstream's 7.2 pre-auth body cap is 64 KB, far too small
            # for real webhook traffic (Stripe emits ~120 KB invoice payloads), so
            # 7.2 containers get 1 MB.
            #
            # 8.1 RETIRED the key: the pre-auth cap is a built-in 256 KB, and the
            # only way to raise it is per-mapping (hooks.mappings.<id>.maxBodyBytes).
            # We define NO mappings, so on 8.1 the effective ceiling is 256 KB and
            # a payload above it is refused. Nothing we drive today posts to /hooks
            # (inbound agent mail is the only real traffic, and it is small), so
            # this is recorded, not worked around — a >256 KB producer needs a
            # named mapping, which is a design decision, not a constant bump.
            **({} if _emit_81_shape else {"maxBodyBytes": 1_048_576}),
            # OpenClaw v2026.5.7 (gateway/hooks.ts) requires
            # "hook:" in this list when ``defaultSessionKey`` is unset
            # (we don't set it above). Omitting "hook:" makes the gateway
            # throw at boot — every newly-provisioned container would
            # refuse to start. DO NOT REMOVE. "lightbulb-" is for our own
            # session-key conventions used by internal callers.
            #
            # CLEAN CUT — the transitional "isol8-" entry is gone, in the same
            # release that starts minting "lightbulb-email:". Normally the
            # ordering would matter: this allowlist is CONTAINER-SIDE (it only
            # changes when a container's openclaw.json is rewritten, at
            # provision / reprovision) while the minting side is BACKEND-side
            # and flips the instant the backend deploys, so a container not yet
            # reprovisioned would reject the key with a 400
            # (gateway/server/hooks-request-handler.ts). Two things make the
            # single step safe here: the FLEET REROLL ships with this change,
            # so no container keeps a stale allowlist; and there was nothing to
            # preserve — an audit of the live prod founder container found ZERO
            # `isol8-email` session keys anywhere in its agent state, the prefix
            # being minted only for inbound agent mail it had never received.
            # Keep "hook:" — that one is functional, not brand residue.
            "allowedSessionKeyPrefixes": ["lightbulb-", "hook:"],
            # Inbound agent email needs a STABLE per-thread session key so a
            # reply continues the same conversation instead of cold-starting a
            # throwaway `hook:<uuid>` every time (see agent_mail_thread). Both
            # a request-supplied key AND a mapping-TEMPLATED one are gated by
            # this single flag upstream (resolveHookSessionKey), so there is no
            # way to get per-thread sessions with it false.
            #
            # What still bounds a leaked/replayed hook token, now that it can
            # name a session rather than only create one:
            #   * allowedSessionKeyPrefixes above — it can only reach
            #     `lightbulb-*` / `hook:*` keys, and `lightbulb-email:<sha256>`
            #     (agent_mail_thread._PREFIX) is the ONLY one we mint. (The
            #     `lightbulb-` in routers/connections.py is an STS
            #     RoleSessionName, not this.)
            #   * allowedAgentIds below — it cannot aim at an arbitrary agent.
            # The delta accepted deliberately: previously such a token could
            # only start fresh sessions; it can now target an existing one and
            # read its prior context. Tightening the prefix to
            # `lightbulb-email:` would shrink that further and is the obvious
            # next step if anything else ever mints a session key.
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
                **memory_plugin_entries(user_id),
                # Parallel web-search provider plugin
                # (@openclaw/parallel-plugin, baked into the extended image).
                # Enables the keyless parallel-free provider that
                # tools.web.search.provider selects above.
                "parallel": {"enabled": True},
                # First-party Lightbulb marketplace tools (marketplace_search,
                # recommend_agents), baked root-owned at
                # /app/dist/extensions/lightbulb-marketplace. READ-ONLY: neither
                # tool mutates the roster — hiring always ends at a human
                # click (the hire card in chat, or a Paperclip hire_agent
                # approval in Teams). Enabled on EVERY provider path: an
                # agent that cannot see the catalog recommends nothing, which
                # is the behaviour this replaced. The tools are non-optional,
                # so the explicit tools.allow ["*"] above surfaces them.
                "lightbulb-marketplace": {"enabled": True},
                # First-party canonical multiplayer channel, baked and promoted
                # before this config shape lands.
                "lightbulb": {"enabled": True},
            },
        },
        # Lightbulb is a first-party account-per-agent channel. Third-party
        # channels remain empty until configured and are carried below.
        "channels": {
            "lightbulb": {
                "enabled": True,
                "accounts": {agent_id: {"enabled": True} for agent_id in lightbulb_agent_ids},
            }
        },
        "bindings": _lightbulb_bindings(lightbulb_agent_ids),
        "session": {
            "dmScope": "per-account-channel-peer",
            "groupScope": "per-group",
            # "warn" (the old value) never deletes ANYTHING — unbounded
            # session-store + archive growth, and giant session files are
            # upstream's documented pre-OOM signature on small tasks. The
            # eviction fear that motivated warn is obsolete for the ENTRY-COUNT
            # and AGE paths: those preserve durable human conversations
            # (main/group/thread) and reap only SYNTHETIC rows (cron / hook /
            # heartbeat / ACP / subagent — exactly what our autonomous fleet
            # mass-produces), zstd-archiving before any deletion. maxEntries and
            # pruneAfter are therefore generous multiples of upstream's defaults
            # (500 rows / 30d) to protect heavy owners.
            #
            # maxDiskBytes is NOT in that family and must not be reasoned about
            # as if it were — at 8.1 it is a physical-bytes budget whose sweep
            # has no synthetic-only filter and (with preserveRecent unset) no
            # recency floor. It is derived arithmetic against the task's
            # ephemeral disk; see SESSION_MAX_DISK_MIB above before touching it.
            # resetArchiveRetention defaults to keep-forever — set explicitly.
            "maintenance": {
                "mode": "enforce",
                "maxEntries": 2000,
                "pruneAfter": "90d",
                "maxDiskBytes": f"{SESSION_MAX_DISK_MIB}mb",
                "highWaterBytes": f"{SESSION_HIGH_WATER_MIB}mb",
                "resetArchiveRetention": "90d",
            },
            # Unconfigured, sessions reset daily at 04:00 gateway-local =
            # 04:00 UTC = 23:00 ET — rolling a US owner's live evening chat
            # on their next message. Idle-based reset needs no owner-timezone
            # knowledge: a session resets only after 24h of silence.
            "reset": {"mode": "idle", "idleMinutes": 1440},
        },
        **({} if _emit_81_shape else {"web": {"enabled": True}}),
        "browser": {
            # Two profiles always defined; the `user` profile is
            # statically declared but only routable when a desktop node
            # is paired (extensions/browser/src/browser-tool.ts
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
            # 2026.8.1 reduced tabCleanup to {enabled} and dropped both launch
            # timeouts from the schema. On 7.2 we still want the raised values:
            # 20s launch for a 1 vCPU box, 30s CDP-ready because the profile
            # lives on EFS (upstream's own Linux doc says raise it on slow storage).
            **(
                {"tabCleanup": {"enabled": True}}
                if _emit_81_shape
                else {
                    "tabCleanup": {"idleMinutes": 30, "maxTabsPerSession": 4},
                    "localLaunchTimeoutMs": 20_000,
                    "localCdpReadyTimeoutMs": 30_000,
                }
            ),
            # Cookies + per-origin localStorage snapshotted to ONE JSON on EFS
            # (60s timer + graceful shutdown) and restored on launch, so a
            # logged-in session survives the image rolls that replace /tmp.
            # The profile dir itself stays on /tmp on purpose — Chrome's
            # LevelDB/SQLite fsync+lock traffic over NFS stalled the event
            # loop when it lived on EFS (container-stack.ts Step B); one
            # atomic JSON write has none of that. `~` is the container's
            # $HOME, so the file lands under the EFS access point.
            #
            # The key exists only from fork patch 2026.7.2-isol8.7 (carried
            # forward onto newer bases) and `browser` is a strictObject, so
            # it is emitted only for an image that carries it: the owner's
            # RUNNING image on a regen (live containers lag the pin), this
            # environment's pin on a fresh provision, and nothing at all when
            # the image can't be read.
            **(
                {
                    "sessionState": {
                        "enabled": True,
                        "intervalSeconds": 60,
                        "path": "~/.openclaw/browser-state/state.json",
                    }
                }
                if openclaw_version.deployed_fork_at_least("2026.7.2", 7, tag=deployed_tag)
                else {}
            ),
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
            # The next three keys are upstream's DOCUMENTED Linux recipe
            # (docs/tools/browser-linux-troubleshooting.md, "Failed to start
            # Chrome CDP on port 18800" — the exact error our containers
            # throw). We had only `enabled` + `noSandbox` of it.
            #
            # headless: upstream's default is FALSE, and this key is set
            # explicitly rather than left to the implicit fallback ("local
            # managed profiles fall back to headless on Linux when both DISPLAY
            # and WAYLAND_DISPLAY are unset"). Leaving it implicit would make a
            # load-bearing property depend on the ABSENCE of an env var.
            #
            # That absence is no longer true anywhere: every container now runs
            # headful on the Xvfb display so the browser can be watched and
            # driven from the workspace panel.
            #
            # BOTH HALVES ARE REQUIRED, and neither is sufficient alone —
            # upstream's resolveManagedBrowserHeadlessMode (extensions/browser/
            # src/browser/config.ts) evaluates its linux-no-DISPLAY branch
            # BEFORE this configured value:
            #   - headless False with no DISPLAY  -> forced back to headless
            #   - DISPLAY set with headless True  -> stays headless, Xvfb idles
            # The matching DISPLAY lives on the task definition in
            # container-stack.ts.
            #
            # Note this only reaches a container when openclaw.json is
            # (re)written — a provision or a config patch. Existing containers
            # keep headless until then, which is also the rollback: revert this
            # to True and the next config write puts them back.
            "headless": False,
            # executablePath: REAL Google Chrome (Dockerfile layer 9b), not
            # the Playwright Chrome-for-Testing build symlinked at
            # /usr/bin/chromium. CfT self-labels with an "only for automated
            # testing — Download Chrome" banner on every fresh profile, and
            # the Browser live-view panel put that in front of the owner every
            # session (the profile is on /tmp, wiped per restart, so it never
            # went away). CfT itself STAYS in the image for Playwright-based
            # MCPs, which hardcode the ms-playwright cache path and never read
            # this setting. The resolver would prefer google-chrome on its own
            # (findChromeExecutableLinux probes it first); the pin keeps the
            # choice deliberate rather than an accident of list order.
            "executablePath": "/usr/bin/google-chrome",
            # extraArgs: WebGL, the unsupported-flag infobar, and the egress
            # proxy. Chrome 137 removed the automatic
            # SwiftShader fallback (blink-dev "Intent to Remove: SwiftShader
            # Fallback") and prod runs Chrome 151 on GPU-less Fargate, so
            # before this the container had NO WebGL at all —
            # `canvas.getContext("webgl")` returned null, which essentially no
            # real desktop does. Anything drawing through WebGL simply failed;
            # Arkose/FunCaptcha (LinkedIn's sign-in challenge) renders its
            # puzzle in 3D and so could never appear.
            #
            # The first WebGL attempt used `--use-angle=gl` (ANGLE over Mesa llvmpipe
            # rasteriser from Dockerfile layer 9b). That was WRONG: measured in
            # the prod founder container 2026-08-20, ANGLE-GL initialises fine
            # (chrome://gpu shows "ANGLE (Mesa/X.org, llvmpipe ... OpenGL 4.5)")
            # but WebGL context creation still fails —
            # `webglcontextcreationerror.statusMessage` is
            # "BindToCurrentSequence failed", a GPU-scheduler bind failure on
            # the software-GL command-buffer path. Adding the mesa libs was
            # necessary-but-not-sufficient; the llvmpipe path is simply broken
            # here (same failure with or without --enable-unsafe-swiftshader).
            #
            # ANGLE's self-contained SwiftShader (Vulkan/Subzero, no mesa/GLX/X
            # dependency) creates a real WebGL context reliably — verified with
            # all three of --use-angle=swiftshader, --enable-unsafe-swiftshader,
            # and both together in the same container.
            #
            # Compositing, though, must NOT go through ANGLE. We first shipped
            # --use-gl=angle --use-angle=swiftshader, which routes ALL GL —
            # including the 2D page compositing the live-view captures — through
            # ANGLE->Vulkan->SwiftShader, emulating a whole GPU pipeline in
            # software just to paint web content. Measured 2026-08-20 on a dev
            # container, that path caps the live-view at ~4fps. Swapping it
            # for --disable-gpu (Chrome's native Skia software compositor) was
            # ~8x fps (4 -> 20-30fps) at the SAME CPU, same box. --disable-gpu
            # keeps WebGL: --enable-unsafe-swiftshader still hands WebGL surfaces
            # an in-process SwiftShader context (renderer "ANGLE (Google, Vulkan
            # 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)"),
            # verified byte-identical (shader compile+link+drawArrays+readPixels)
            # under both configs, so the Arkose/FunCaptcha 3D case is preserved.
            # CONFIG-ONLY — no image roll (mesa libs stay baked, unused for WebGL).
            #
            # Do NOT re-add --use-angle=swiftshader (slow compositor) or
            # --use-gl=angle (broken llvmpipe path — BindToCurrentSequence).
            # Verify after delivery with chrome://gpu in the Browser panel, or
            # canvas.getContext("webgl") != null over CDP on port 18800.
            "extraArgs": [
                "--disable-gpu",
                "--enable-unsafe-swiftshader",
                # Suppresses Chrome's yellow "You are using an unsupported
                # command-line flag: --no-sandbox. Stability and security will
                # suffer." bubble, which the owner sees in the Browser panel
                # every session. We do not choose --no-sandbox: `noSandbox` is
                # set above because the gateway runs as root and Chromium
                # refuses to start as root without it, so the bar is
                # unavoidable collateral of a decision made for us, and it
                # tells the owner nothing they can act on.
                #
                # This is the SECOND banner in this panel. The first was Chrome
                # for Testing's "only for automated testing" bar, fixed by
                # pinning executablePath to google-chrome above — different
                # cause, different fix, both had to be done.
                #
                # It must be --test-type, NOT the --disable-infobars in
                # upstream's own extraArgs doc example: that switch stopped
                # suppressing this bubble around Chrome 76 and is inert on the
                # Chrome 151 we run. Do not "simplify" to it.
                #
                # --test-type is deliberately BROADER than this one bar: it is
                # Chrome's "running under test automation" switch (it is what
                # skips the bad-flags check in bad_flags_prompt.cc, which is
                # why it works here) and it also suppresses some first-run and
                # session-restore prompts. That is wanted, not tolerated —
                # every one of those prompts is noise the owner would see in
                # the live-view panel and could not act on. Anyone auditing
                # this flag should expect more than the infobar.
                #
                # CONFIG-ONLY — no image roll. Verify after delivery by opening
                # the Browser panel and looking for the bar, not by reading
                # argv: the flag is only worth anything if the bubble is gone.
                "--test-type",
                # Cloudflare WARP browser egress, when the environment has the
                # knob on. Appends at most one flag; see _browser_egress_args.
                *_browser_egress_args(),
            ],
            # The WARP PAC makes this a proxy-routed profile, and OpenClaw
            # refuses EVERY navigation on one unless its Node-side private-
            # network check is switched off (extensions/browser navigation-
            # guard: "strict browser SSRF policy cannot be enforced while this
            # browser profile is proxy-routed" — it can't see the connect
            # target behind a proxy, so it fails closed; hostname allowlists
            # don't bypass it). Off ONLY while the proxy is on. Nothing new is
            # exposed: the agent's `exec: full` shell already reaches anything
            # the browser could, and proxied traffic egresses from Cloudflare,
            # not this VPC.
            **({"ssrfPolicy": {"dangerouslyAllowPrivateNetwork": True}} if settings.BROWSER_EGRESS_PROXY_PORT else {}),
            # NO crash-reporter extraArgs. `--disable-breakpad` +
            # `--crash-dumps-dir` were added here (#1594) on the theory that
            # Chrome's `chrome_crashpad_handler: --database is required` abort
            # was a missing-flag problem. Tested directly in the prod container
            # 2026-08-04 with OpenClaw's exact argv: BOTH flags present,
            # byte-identical failure. Reverted.
            #
            # The real cause was never Chrome's argv — XDG_CONFIG_HOME
            # (<owner>/.config on EFS) was root-owned 755 while the EFS access
            # point squashes the container's root to uid 1000, so Chrome could
            # not create its crash dir at all. See
            # Workspace.ensure_xdg_dirs. Do not re-add these flags to "fix"
            # a launch failure; they do nothing for it.
            # Timeouts: upstream defaults are 15s launch / 8s CDP-ready.
            #
            # localLaunchTimeoutMs was briefly 60s, raised on the theory that
            # the profile was on slow NFS storage. That theory was WRONG — the
            # profile is on /tmp (container-stack.ts symlinks
            # ~/.openclaw/browser -> /tmp/openclaw-browser), and 26ms was never
            # consistent with slow storage anyway.
            #
            # Worse, 60s actively HID the bug: the launcher waited patiently
            # while a shorter tool-level timeout fired first, so the failure
            # arrived as a bare `browser failed: timed out` with no CDP
            # diagnostic and no Chrome stderr. Keep the launch budget BELOW
            # the tool timeout (BROWSER_ACTION_TIMEOUT_MS = 60_000,
            # browser/constants.ts) so the launcher's own error always wins the
            # race and stays visible. 20s = default plus headroom for a
            # 1 vCPU box. Schema caps both at 120_000.
            "profiles": {
                "openclaw": {
                    "driver": "openclaw",
                    # Dropped from the strict profile object at 2026.8.1.
                    **({} if _emit_81_shape else {"color": "#FF4500"}),
                    # Required since OpenClaw v2026.5.7-0d10b38 — upstream
                    # zod-schema.ts now refines any non-`existing-session`
                    # profile to require cdpPort or cdpUrl, with the message
                    # "Profile must set cdpPort or cdpUrl". Without it the
                    # gateway exits at boot with `Gateway failed to start:
                    # Invalid config at /home/node/.openclaw/openclaw.json`
                    # and the container loops failed-to-start forever.
                    # 18800 matches upstream's documented default for the
                    # bundled-Chromium openclaw driver (docs/tools/
                    # browser-linux-troubleshooting.md, chrome.test.ts).
                    # Each container is per-user-isolated so collisions
                    # can't happen.
                    "cdpPort": 18800,
                },
                "user": {
                    "driver": "existing-session",
                    **({} if _emit_81_shape else {"color": "#0066FF"}),
                    # Required since OpenClaw bumped its config schema —
                    # `browser.profiles.*.color` is now a required string.
                    # Surfaces in the OpenClaw UI as the profile chip.
                    # No cdpPort/cdpUrl needed — `existing-session` is
                    # the one driver upstream's schema exempts from the
                    # require-cdp check (zod-schema.ts).
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

    # Merge env vars from resources provisioned on the owner's own identity
    # (Stripe Projects). Same reason as the mcp.servers merge above: a
    # reprovision fully overwrites openclaw.json, so without this every
    # provider token the owner provisioned disappears on the next image roll.
    if provisioned_env:
        config.setdefault("env", {}).setdefault("vars", {}).update(provisioned_env)

    # Pin the bundled MCP runtime to "shared" scope: one connected runtime per
    # (workspace, server-config), reused across every session instead of spawned
    # per session — so a new session (a new Paperclip issue, a chat after the
    # daily session reset) reuses the already-connected MCP servers instead of
    # paying a fresh connect. Each container serves a single owner, so there is
    # no cross-session credential bleed.
    #
    # runtimeScope exists only in the forked base image; an older gateway's MCP
    # schema is strict and rejects unknown keys at boot, so emit it only once the
    # image this config will actually RUN on supports it — otherwise a config
    # write to a container still on the old image bricks it on next boot. Gated on
    # _target_upstream(deployed_tag), like every other version gate in this module:
    # the environment pin is right only for a fresh provision, and a live container
    # can lag it by a whole upstream version during a promotion or a rollback.
    # Only meaningful when an mcp block exists.
    if openclaw_version.version_at_least(_target_upstream(deployed_tag), "2026.6.1"):
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
    # Bindings are carried verbatim (platform Slack app: account = team id →
    # agent); the ``agentId == accountId`` synthesis is the legacy fallback for
    # accounts whose binding was not carried.
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
        # A route binding MUST name an agent that exists in this config.
        # OpenClaw validates that at load and refuses the WHOLE file otherwise
        # ("bindings.N.agentId: Unknown agent id ..."), so one bogus entry
        # silently freezes an owner on their previous config — every later
        # config change included. Two ways in, both guarded below: upstream
        # normalizes channel-root settings into an `accounts.default` husk, and
        # the legacy `agentId == accountId` rule then invents `agentId:
        # "default"`; and a carried binding can name an agent since deleted.
        agents_block = config.get("agents") or {}
        known_agent_ids = set(agents_block.get("entries") or {}) | {
            a.get("id") for a in (agents_block.get("list") or []) if isinstance(a, dict)
        }
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
            # Progress drafts: one channel message edited live with status
            # during long autonomous turns, replaced by the final answer —
            # instead of minutes of silence. Telegram and Slack default to
            # plain block streaming upstream; Discord already defaults to
            # progress, so it needs no key. Zero extra LLM calls (edits only).
            #
            # toolProgress is forced OFF, not setdefault-ed: it posts each
            # tool's OUTPUT into the conversation, and a Read of a bundled
            # SKILL.md put the whole file — maintenance notes addressed to our
            # own engineers included — into an owner's shared company channel.
            # Existing owners carry a streaming block already, so a setdefault
            # on the parent would never reach them.
            if provider in ("telegram", "slack"):
                streaming = channel.setdefault("streaming", {"mode": "progress"})
                if isinstance(streaming, dict):
                    progress = streaming.setdefault("progress", {})
                    if isinstance(progress, dict):
                        progress["toolProgress"] = False
            # Stale bindings whose account no longer exists (e.g. deleted
            # after a crash) are dropped here — not carried, not re-written —
            # so an orphaned binding can't leak forward on every reprovision.
            carried = [
                b
                for b in (channel_bindings or [])
                if (b.get("match") or {}).get("channel") == provider
                and (b.get("match") or {}).get("accountId") in accounts
                and b.get("agentId") in known_agent_ids
            ]
            for b in carried:
                if b not in bindings:
                    bindings.append(b)
            carried_accounts = {(b.get("match") or {}).get("accountId") for b in carried}
            for account_id in accounts:
                if account_id in carried_accounts:
                    continue  # the carried binding already routes this account
                if account_id not in known_agent_ids:
                    # The legacy convention only holds for an account KEYED by
                    # an agent id. Anything else (an upstream `default` husk)
                    # would name an agent that does not exist. Leave it unbound
                    # — which is the state the container was already happy with.
                    continue
                binding = {
                    "type": "route",
                    "agentId": account_id,
                    "match": {"channel": provider, "accountId": account_id},
                }
                if binding not in bindings:
                    bindings.append(binding)

    # ACP coding-harness runtime (Forge-style delegation: a native agent can
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
            acp_block, acpx_plugin_entry = build_acp_config(harnesses, emit_81_shape=_emit_81_shape)
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
    provisioned_env: dict | None = None,
    aip_arns: dict[str, str] | None = None,
    channel_accounts: dict[str, dict[str, dict]] | None = None,
    channel_bindings: list[dict] | None = None,
    acp_enabled: bool = True,
    deployed_tag: str | None = None,
) -> None:
    """Atomically replace the user's openclaw.json with the full container config.

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
        provisioned_env=provisioned_env,
        aip_arns=aip_arns,
        channel_accounts=channel_accounts,
        channel_bindings=channel_bindings,
        acp_enabled=acp_enabled,
        deployed_tag=deployed_tag,
    )

    # Full-file write — this path does NOT go through config_patcher._locked_rmw,
    # so it carries its own schema gate (issue #941). Audit-only on purpose: the
    # input here is our own generator, whose output the chaos suite already
    # validates against the same vendored schema in CI, so a violation means the
    # schema or the generator drifted and we want the alarm, not a provisioning
    # outage on top of it.
    # Inside the thread, not on the event loop: validating a real config against
    # the ~280 KB schema is non-trivial CPU, and every other gate call site runs
    # inside `asyncio.to_thread` already (config_patcher._do_rmw).
    def _write() -> None:
        gate_config(config, owner_id=user_id, context="write_openclaw_config")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = f"{config_path}.lock"
        # This generator is also used by update paths while the owner task is
        # still running. Take the same transition-aware sidecar as hot patches:
        # legacy fcntl before boot recovery, OpenClaw-compatible O_EXCL after.
        # The process-local gate additionally serializes backend threads.
        with get_file_lock(lock_path):
            with openclaw_config_sidecar_lock(str(config_path.parent), lock_path):
                fd, temporary_path = tempfile.mkstemp(
                    dir=config_path.parent,
                    prefix=f".{config_path.name}.",
                    suffix=".tmp",
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(config, stream, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
                    chown_uid_1000_best_effort(temporary_path)
                    os.replace(temporary_path, config_path)
                finally:
                    Path(temporary_path).unlink(missing_ok=True)

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
