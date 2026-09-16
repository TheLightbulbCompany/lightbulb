"""
ECS Fargate service manager for per-user OpenClaw gateways.

Each subscriber gets a dedicated ECS Service (desiredCount 0/1) that
runs an OpenClaw gateway container with a per-user EFS access point
for data isolation. Task IPs are discovered via AWS Cloud Map
(``DiscoverInstances``).
"""

import asyncio
import hashlib
import logging
import math
import re
import secrets
import socket
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from ulid import ULID

from core.config import settings
from core.dynamodb import utc_now_iso
from core.observability.metrics import put_metric, timing
from core.containers.config import (
    BEDROCK_PROVIDER_CHOICES,
    BEDROCK_TRIAL_CHOICE,
    CHANNEL_PROVIDER_IDS,
    _build_node_paired_entry,
    generate_node_device_identity,
    load_node_device_identity,
    read_openclaw_config_from_efs,
    write_openclaw_config,
)
from core.containers.workspace import get_workspace
from core.repositories import billing_repo, container_repo
from core.repositories.container_repo import ProvisionSuperseded
from core.services import runtime_transport
from core.services.owner_lifecycle import serialize_owner_lifecycle

logger = logging.getLogger(__name__)

GATEWAY_PORT = 18789

# Single per-user task size for the flat-fee pivot. Per spec §3.2 / §10.
# Old per-tier sizing (free/starter 0.5/1, pro 1/2, enterprise 2/4) deleted —
# every user gets the same 1 vCPU / 4 GB box. 2 GB was upstream OpenClaw's
# documented floor and OOMed under two seeded agents + memory sidecar +
# in-container Chromium; the full vCPU stops boot/tool-exec/Chromium
# contending for half a core. Affordable because per-user services launch on
# Fargate Spot. Kept as constants so the resize/register paths pull from a
# single place. Keep in lockstep with PER_USER_TASK_RESOURCES in
# apps/infra/lib/stacks/container-stack.ts.
PER_USER_CPU = "1024"
PER_USER_MEMORY_MIB = "4096"

# Per-user containers launch on Fargate Spot (~70% cheaper than on-demand).
# Interruption is survivable by design: SINGLE_WRITER_DEPLOYMENT_CONFIG means
# the replacement task only starts after the reclaimed one fully stops (no
# EFS double-writer), EFS state persists, and readiness probes the new task's
# health through container_rpc on the next status poll. When Spot capacity is
# unavailable ECS places nothing and reports no failure, so two fallbacks
# cover it: create_service retries with on-demand if the cluster rejects the
# strategy, and _await_running_transition flips a service with zero placed
# tasks to on-demand after SPOT_PLACEMENT_FALLBACK_S.
SPOT_CAPACITY_STRATEGY = [{"capacityProvider": "FARGATE_SPOT", "weight": 1}]
ON_DEMAND_CAPACITY_STRATEGY = [{"capacityProvider": "FARGATE", "weight": 1}]
SPOT_PLACEMENT_FALLBACK_S = 300.0

# Ultra tier box. 2 vCPU / 8 GB — same constant home as PER_USER_* so the
# resize/register paths and the webhook tier logic pull sizes from one place.
ULTRA_CPU = "2048"
ULTRA_MEMORY_MIB = "8192"

# Operational boot-reliability floor, never a billing or admission limit.
AGENT_RELIABILITY_RESIZE_THRESHOLD = 10

# The AIP tiers minted for a `bedrock_trial` (no-card free tier) owner. ONE
# tier, and it must be sonnet: config.py makes sonnet the trial's
# agents.defaults.model.primary and hard-fails a Bedrock config without it.
#
# This list IS the free tier's security boundary, not a cost optimization.
# build_owner_bedrock_policy scopes the per-owner task role to exactly the AIPs
# it is handed and explicitly denies bedrock:CreateInferenceProfile, so a
# one-tier mint leaves the container with no credential path to Opus/Haiku —
# the owner cannot reach them even by rewriting their own openclaw.json (which
# they can do: agents run with `exec: full`).
BEDROCK_TRIAL_TIERS = ["sonnet"]

# Per-user services run exactly one task whose OpenClaw process writes SQLite
# state (chat history, task runs, memory) into a shared per-owner EFS dir in
# WAL mode. ECS's default rolling deploy (200% / 100%) briefly runs the old
# and new task at the same time; two OpenClaw processes on different Fargate
# hosts opening the same WAL database over NFS corrupts it, because WAL's -shm
# coordination only works between processes on a single host. Pinning
# maximumPercent=100 (never two tasks) and minimumHealthyPercent=0 (allowed to
# drop to zero between them) makes ECS stop the old task before starting the
# new one, so the EFS state is only ever touched by a single writer.
SINGLE_WRITER_DEPLOYMENT_CONFIG = {
    "deploymentCircuitBreaker": {"enable": True, "rollback": False},
    "minimumHealthyPercent": 0,
    "maximumPercent": 100,
}


def _tagged_image_tag(image: object) -> str:
    """Return a container image's tag, or ``""`` for digest/invalid refs."""
    image_ref = str(image or "")
    if "@" in image_ref:
        return ""
    _, separator, tag = image_ref.rpartition(":")
    return tag if separator and "/" not in tag else ""


def _task_private_ip(task: dict) -> str | None:
    """Extract a private IPv4 address from an ECS task ENI."""
    for attachment in task.get("attachments", []):
        if attachment.get("type") != "ElasticNetworkInterface":
            continue
        for detail in attachment.get("details", []):
            value = detail.get("value")
            if detail.get("name") == "privateIPv4Address" and isinstance(value, str) and value:
                return value
    return None


def _user_owned_mcp_servers(existing_servers: dict, integration_provider_keys: set[str]) -> dict:
    """User-added MCP servers to carry across a full openclaw.json regenerate.

    The reprovision path rebuilds openclaw.json from structured inputs and would
    otherwise drop anything added through the manual MCP panel — those servers
    live only in the on-disk file, not in a DDB store the generator reads back.
    Preserve every existing server whose key is neither a registry integration
    key (re-injected from the credential system) nor the legacy ``paperclip``
    block — i.e. the ones the user added themselves. The ``paperclip`` MCP
    server is no longer seeded (each agent authenticates with its own per-agent
    key via the bundled skill), so excluding it here PURGES any stale on-disk
    copy on the next reprovision instead of carrying it forward.
    """
    reserved = integration_provider_keys | {"paperclip"}
    return {name: block for name, block in existing_servers.items() if name not in reserved and isinstance(block, dict)}


async def _provision_mcp_servers(owner_id: str, connected_servers: dict) -> dict:
    """Servers to inject into a regenerated openclaw.json: the user's own
    on-disk MCP servers plus the connected integration servers.

    The reprovision path fully overwrites openclaw.json, so without this a
    manually-added MCP server (which lives only on disk) is lost. Read the
    current on-disk servers, keep the user-owned ones (not a registry
    integration key, not the seeded paperclip block), and merge — connected
    integrations win on the (impossible) key collision since user-owned keys
    exclude integration keys.

    Fail-safe: any read/registry hiccup falls back to ``connected_servers`` so
    provisioning never breaks (worst case the user re-adds the server).
    """
    try:
        from core.services.credentials import registry

        existing_cfg = await read_openclaw_config_from_efs(owner_id) or {}
        existing_servers = (existing_cfg.get("mcp") or {}).get("servers") or {}
        if not isinstance(existing_servers, dict):
            existing_servers = {}
        integration_keys = {
            p.wiring.mcp_server.key for p in registry.all_providers().values() if p.wiring.mcp_server is not None
        }
        user_servers = _user_owned_mcp_servers(existing_servers, integration_keys)
        return {**user_servers, **connected_servers}
    except Exception:  # noqa: BLE001 — best-effort; never fail provision over this.
        logger.exception("preserving user MCP servers failed for %s; continuing", owner_id)
        return connected_servers


# A real channel account carries a transport or a credential. OpenClaw reads
# provider-LEVEL settings as a synthetic "[default]" account and persists it to
# disk on write-back, so a config can accumulate an `accounts.default` husk
# holding nothing but policy (ours held only dmPolicy, left behind when a
# provider-level BYO bot was deleted — routers/channels.py strips the root
# credentials but never the husk). Carrying one forward re-injects a bot that
# does not exist and, before the binding guard in config.py, produced an
# `agentId: "default"` that made the whole config unloadable.
_CHANNEL_ACCOUNT_CONFIGURED_KEYS = ("mode", "botToken", "appToken", "token", "signingSecret", "webhookPath")


def _is_configured_channel_account(account: object) -> bool:
    return isinstance(account, dict) and any(k in account for k in _CHANNEL_ACCOUNT_CONFIGURED_KEYS)


async def _provision_channel_accounts(owner_id: str) -> tuple[dict[str, dict[str, dict]], list[dict]]:
    """Channel bot accounts + their route bindings, to re-inject into a
    regenerated openclaw.json.

    The reprovision path fully overwrites openclaw.json, so without this every
    configured Telegram/Slack/Discord account — bot token, allowFrom pairings
    and all — is lost, exactly like the MCP-server case that
    ``_provision_mcp_servers`` handles. Read the live on-disk accounts and
    return them keyed ``{provider: {accountId: account}}``, plus the on-disk
    route bindings that point at a channel account.

    Returns ``(accounts_by_provider, channel_bindings)``; the bindings are
    carried VERBATIM because the platform Slack app keys accounts by team id
    and routes them to an agent — re-synthesizing ``agentId == accountId``
    would rewire the workspace to a non-existent agent.

    Fail-safe: any read/parse hiccup returns ``({}, [])`` so provisioning never
    breaks (worst case the user re-runs the channel wizard).
    """
    try:
        existing_cfg = await read_openclaw_config_from_efs(owner_id) or {}
        channels = existing_cfg.get("channels") or {}
        out: dict[str, dict[str, dict]] = {}
        for provider in CHANNEL_PROVIDER_IDS:
            entry = channels.get(provider)
            if not isinstance(entry, dict):
                continue
            accounts = entry.get("accounts")
            if isinstance(accounts, dict) and accounts:
                configured = {k: v for k, v in accounts.items() if _is_configured_channel_account(v)}
                if configured:
                    out[provider] = configured
        bindings = [
            b
            for b in (existing_cfg.get("bindings") or [])
            if isinstance(b, dict) and (b.get("match") or {}).get("channel") in CHANNEL_PROVIDER_IDS
        ]

        # The installs table is the source of truth for the platform Slack
        # app: a reprovision after an EFS wipe has nothing on disk. Table
        # wins over file for the same team.
        from core.repositories import slack_installs_repo
        from core.services.slack_install_service import account_block, route_binding

        for row in await slack_installs_repo.list_for_owner(owner_id):
            if row.get("status") != "active" or not row.get("agent_id"):
                continue
            team_id = row["team_id"]
            out.setdefault("slack", {})[team_id] = account_block(team_id)
            bindings = [b for b in bindings if (b.get("match") or {}).get("accountId") != team_id]
            bindings.append(route_binding(team_id, row["agent_id"]))

        return out, bindings
    except Exception:  # noqa: BLE001 — best-effort; never fail provision over this.
        logger.exception("preserving channel accounts failed for %s; continuing", owner_id)
        return {}, []


_TASK_DEF_LOCKS: dict[str, asyncio.Lock] = {}


def _owner_task_def_lock(owner_id: str) -> asyncio.Lock:
    """One lock per owner for the task-definition read-modify-register sequence."""
    lock = _TASK_DEF_LOCKS.get(owner_id)
    if lock is None:
        lock = _TASK_DEF_LOCKS[owner_id] = asyncio.Lock()
    return lock


def slack_env_var_name(team_id: str) -> str:
    """ECS/SecretRef env var carrying one workspace's bot token. The OpenClaw
    SecretRef ``id`` must match ``^[A-Z][A-Z0-9_]{0,127}$`` — Slack team ids
    (``T0123ABC``) do; agent ids would not, which is one reason the Slack
    account is keyed by team, not agent."""
    return f"SLACK_BOT_TOKEN_{team_id.upper()}"


def slack_token_value_from(secret_arn: str) -> str:
    """Task-definition ``valueFrom`` for a Slack bot token. The secret is the
    integration store's JSON blob (``{"bot_token": ..., "bot_user_id": ...}``),
    so the entry must select the key — a bare ARN injects the whole JSON
    document and Slack answers every call with ``invalid_auth``. ECS's
    selector syntax is ``<full arn>:<json-key>:<version-stage>:<version-id>``
    with the last two left empty."""
    return f"{secret_arn}:bot_token::"


async def _secret_arn_for_name(secret_name: str) -> str | None:
    """ARN for a Secrets Manager name (task-def ``secrets:`` needs the ARN)."""
    from core.config import settings as _settings

    def _describe() -> str | None:
        sm = boto3.client("secretsmanager", region_name=_settings.AWS_REGION)
        try:
            return sm.describe_secret(SecretId=secret_name)["ARN"]
        except sm.exceptions.ResourceNotFoundException:
            return None

    return await asyncio.to_thread(_describe)


_CONFIG_LOCK_PROTOCOL_ENV = "ISOL8_CONFIG_LOCK_PROTOCOL"
_CONFIG_LOCK_PROTOCOL_VERSION = "guard-v1"


def _build_per_user_environment(
    *,
    hook_token: str,
    existing: list[dict],
) -> list[dict]:
    """Return the per-user ``environment`` list with OPENCLAW_HOOK_TOKEN injected.

    ``existing`` is the caller-supplied list of per-user env entries (e.g. an
    earlier flow may layer in CODEX_HOME or other markers). We preserve every
    entry verbatim except we replace any pre-existing ``OPENCLAW_HOOK_TOKEN``
    with the freshly generated value (matters for the backfill: the old
    task-def carried no token, but a half-complete prior backfill could have
    left a stale one).
    """
    out: list[dict] = [e for e in existing if e.get("name") != "OPENCLAW_HOOK_TOKEN"]
    out.append({"name": "OPENCLAW_HOOK_TOKEN", "value": hook_token})
    return out


def _build_bedrock_acp_harness_env(
    aip_arns: dict[str, str],
    *,
    gate_token: str | None = None,
    gate_url: str | None = None,
) -> list[dict]:
    """Env that points the ACP coding harness (Claude Code) at the owner's
    per-owner Bedrock Application Inference Profiles.

    Claude Code — spawned by acpx for ``sessions_spawn({runtime:"acp"})`` — reads
    these to route its own Bedrock calls. ``ANTHROPIC_MODEL`` /
    ``ANTHROPIC_SMALL_FAST_MODEL`` MUST be the owner's AIP ARNs, never base model
    ids: Isol8 attributes Bedrock billing by AIP (the bedrock-usage-meter Lambda
    keys on the ``bedrock-aip-owners`` table), and the per-user task role grants
    Bedrock only on ``application-inference-profile/*`` — so a base-model call is
    BOTH unbilled (the meter's log filter misses it) AND IAM-denied. Pointing the
    harness at the owner's AIP is what makes "Claude Code on Bedrock draws the
    owner's credits" true. ``AWS_REGION`` is set explicitly because, unlike
    OpenClaw's native provider (which bakes the region into its baseUrl), Claude
    Code's AWS SDK resolves the region from the environment, and the base task
    def does not set it.

    Tier mapping mirrors Claude Code's own defaults: ``sonnet`` is the main
    coding model, ``haiku`` the small/fast background model.

    The bedrock-gate is the standard path: with a ``gate_url``/``gate_token``
    the harness routes its Bedrock client through the gate with the SAME
    per-owner bearer token as the OpenClaw provider. Claude Code binds to
    ``ANTHROPIC_BEDROCK_BASE_URL`` (verified against the 2.1.156 binary — plain
    AWS-SDK endpoint vars do NOT work), skips its own SigV4 signing
    (``CLAUDE_CODE_SKIP_BEDROCK_AUTH``), and presents the token as
    ``ANTHROPIC_AUTH_TOKEN`` — so no ACP-specific IAM carve-out is needed and
    the same gate enforces its spend. An absent ``gate_url`` (bootstrap /
    break-glass) leaves the harness on direct SigV4, which post-cutover also
    requires the break-glass IAM re-grant.
    """
    env = [
        {"name": "CLAUDE_CODE_USE_BEDROCK", "value": "1"},
        {"name": "AWS_REGION", "value": settings.AWS_REGION},
        {"name": "ANTHROPIC_MODEL", "value": aip_arns["sonnet"]},
        {"name": "ANTHROPIC_SMALL_FAST_MODEL", "value": aip_arns["haiku"]},
    ]
    if gate_url and gate_token:
        env.extend(
            [
                {"name": "ANTHROPIC_BEDROCK_BASE_URL", "value": gate_url},
                {"name": "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "value": "1"},
                {"name": "ANTHROPIC_AUTH_TOKEN", "value": gate_token},
            ]
        )
    return env


class EcsManagerError(Exception):
    """Raised when ECS operations fail."""

    def __init__(self, message: str, user_id: str = ""):
        super().__init__(message)
        self.user_id = user_id


# Why ``start_user_service`` may refuse to start a container. Both are POLICY
# refusals, not failures — see ContainerStartRefused.
#
# START_REFUSED_CAP / START_REFUSED_EXPIRED are deliberately the *same strings*
# as the provision gate's ``trial_daily_cap`` / ``trial_expired`` codes, so a
# router can hand either straight to the matching gate's ``.to_payload()`` and
# the owner sees the identical 402 card whichever door they knocked on.
# (Literals, not imports of trial_cap.DAILY_CAP/TRIAL_EXPIRED: trial_cap is
# imported lazily inside the guard to avoid a module cycle. A lockstep test
# pins these to it so they cannot drift.)
START_REFUSED_CAP = "trial_daily_cap"
START_REFUSED_EXPIRED = "trial_expired"
START_REFUSED_CONVERTING = "conversion_in_flight"
START_REFUSED_REPROVISIONING = "reprovision_in_flight"

# Copy for each trial refusal. Keyed by the reason ``trial_cap.trial_block_reason``
# returns, so a new reason cannot be added without giving the owner words.
_TRIAL_REFUSAL_COPY = {
    START_REFUSED_CAP: "You've used today's free allowance — resets tomorrow.",
    START_REFUSED_EXPIRED: "Your 3-day free trial has ended.",
}


class ContainerStartRefused(Exception):
    """A guard refused to start a container. NOTHING FAILED.

    Deliberately NOT an ``EcsManagerError``: every ``except EcsManagerError`` in
    this codebase means "provisioning broke" and reacts accordingly — 503 to the
    caller, ``status="error"`` on the container row, rollback of AWS resources.
    A refusal is none of those things: the container is fine, we are declining to
    start it. Making this a subclass would silently route every refusal into
    those handlers and mark healthy rows broken.

    ``reason`` is one of the ``START_REFUSED_*`` constants above.
    """

    def __init__(self, reason: str, user_id: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.user_id = user_id


class EcsManager:
    """Manages per-user ECS Fargate services for OpenClaw gateways."""

    # The CDK base task def lives in this family; per-user clones live in
    # `<base>-user`. Splitting the families means `describe_task_definition`
    # on the base family always returns the latest CDK base — no need for
    # SSM-pinned ARNs.
    _BASE_TASK_DEF_FAMILY = "lightbulb-{env}-openclaw"
    _PER_USER_FAMILY_SUFFIX = "-user"

    def __init__(self):
        self._ecs = boto3.client("ecs", region_name=settings.AWS_REGION)
        self._efs = boto3.client("efs", region_name=settings.AWS_REGION)
        self._cluster = settings.ECS_CLUSTER_ARN
        self._base_task_def_family = self._BASE_TASK_DEF_FAMILY.format(
            env=settings.ENVIRONMENT or "dev",
        )
        self._subnets = [s.strip() for s in settings.ECS_SUBNETS.split(",") if s.strip()]
        self._security_groups = [settings.ECS_SECURITY_GROUP_ID]
        self._cloud_map_service_arn = settings.CLOUD_MAP_SERVICE_ARN

        self._servicediscovery = boto3.client("servicediscovery", region_name=settings.AWS_REGION)
        self._cloud_map_namespace_name = settings.CLOUD_MAP_NAMESPACE_NAME
        self._cloud_map_service_name = settings.CLOUD_MAP_SERVICE_NAME
        self._efs_file_system_id = settings.EFS_FILE_SYSTEM_ID

    def _service_name(self, user_id: str) -> str:
        """Generate deterministic, collision-resistant service name from user_id.

        ECS service names must be <= 255 chars. Format: openclaw-{user_id}-{hash}
        so services are identifiable in the ECS console. The hash suffix
        guarantees uniqueness even if user_id is truncated.
        """
        uid_hash = hashlib.sha256(user_id.encode()).hexdigest()[:12]
        # Sanitize user_id for ECS naming (alphanumeric, hyphens, underscores only)
        safe_uid = re.sub(r"[^a-zA-Z0-9_-]", "", user_id)[:40]
        return f"openclaw-{safe_uid}-{uid_hash}"

    # ------------------------------------------------------------------
    # Per-user EFS access points
    # ------------------------------------------------------------------

    def _create_access_point(self, user_id: str, owner_type: str = "personal") -> str:
        """Create a per-user EFS access point rooted at /users/{user_id}.

        The access point enforces POSIX UID/GID 0 (matching the
        container's root user) and creates the root directory
        with 0755 permissions if it doesn't exist.

        Args:
            user_id: Clerk user ID.

        Returns:
            The access point ID (e.g. "fsap-0abc123...").

        Raises:
            EcsManagerError: If the EFS API call fails.
        """
        try:
            resp = self._efs.create_access_point(
                FileSystemId=self._efs_file_system_id,
                PosixUser={"Uid": 1000, "Gid": 1000},
                RootDirectory={
                    "Path": f"/users/{user_id}",
                    "CreationInfo": {
                        "OwnerUid": 1000,
                        "OwnerGid": 1000,
                        "Permissions": "0755",
                    },
                },
                Tags=[
                    {"Key": "Name", "Value": f"lightbulb-{user_id}"},
                    {"Key": "owner_id", "Value": user_id},
                    {"Key": "owner_type", "Value": owner_type},
                    {"Key": "ManagedBy", "Value": "lightbulb-backend"},
                ],
            )
            access_point_id = resp["AccessPointId"]
            put_metric("container.efs.access_point", dimensions={"op": "create", "status": "ok"})
            logger.info("Created EFS access point %s for user %s", access_point_id, user_id)
            return access_point_id
        except Exception as e:
            put_metric("container.efs.access_point", dimensions={"op": "create", "status": "error"})
            raise EcsManagerError(
                f"Failed to create EFS access point for user {user_id}: {e}",
                user_id,
            )

    def _delete_access_point(self, access_point_id: str) -> None:
        """Delete an EFS access point. Idempotent (ignores not-found)."""
        try:
            self._efs.delete_access_point(AccessPointId=access_point_id)
            put_metric("container.efs.access_point", dimensions={"op": "delete", "status": "ok"})
            logger.info("Deleted EFS access point %s", access_point_id)
        except self._efs.exceptions.AccessPointNotFound:
            logger.warning("Access point %s already deleted", access_point_id)
        except Exception as e:
            put_metric("container.efs.access_point", dimensions={"op": "delete", "status": "error"})
            logger.error("Failed to delete access point %s: %s", access_point_id, e)

    # ------------------------------------------------------------------
    # Per-user task definition revisions
    # ------------------------------------------------------------------

    async def _size_for_owner(self, owner_id: str) -> tuple[str, str]:
        """The (cpu, memory) a provision/reprovision must register for ``owner_id``.

        ``ultra_pro`` owners run the large ``ULTRA_CPU`` / ``ULTRA_MEMORY_MIB``
        box;
        everyone else the flat ``PER_USER_CPU`` / ``PER_USER_MEMORY_MIB`` box —
        UNLESS their live agent count crosses the reliability floor below.

        Deriving the size from the billing plan HERE is what makes the Ultra
        size DURABLE. The upgrade webhook resizes the running container, but the
        provision/reprovision path re-registers the task def from the CDK base
        (provider switch, restart, admin reprovision) and a hardcoded standard
        size there would silently undo the webhook's resize. Callers on those
        paths route their size through this helper — as does the Track-2
        image-roll apply (``update_service.apply_update``), which resizes with no
        size of its own — so the box always tracks the owner's plan.

        **Agent count is the second input, and it is a RELIABILITY floor, not a
        billing signal or product entitlement.** Nothing else needs to watch
        ORGANIC growth, and agents arrive one at a time from several paths: the
        /chat ``agents.create`` RPC, the Teams REST endpoint, a marketplace
        single-agent hire, and Atlas (the in-house recruiter) creating them
        against Paperclip directly. An owner could therefore cross the budget
        without any of the existing gates firing and stay on the flat box.

        That is not hypothetical: prod owner with 11 agents on the flat box
        crash-looped on boot 2026-08-14. 24 SQLite databases restoring +
        replicating concurrently saturated the single vCPU (gateway got 13% of a
        core), so its state-DB writes blew OpenClaw's hardcoded 5s busy timeout
        and the gateway died with "database is locked" — repeatedly, until three
        failures tripped the ECS deployment circuit breaker.

        Reading the LIVE roster rather than hooking each create path is
        deliberate: it cannot be bypassed by a creation path nobody remembered
        to instrument, including agents Atlas creates without the backend in the
        loop. It converges on the owner's next task-def registration rather than
        bouncing the container the instant someone adds an agent.

        This helper only ever raises the plan-derived size to the reliability
        floor. Explicit downsize decisions stay owned by
        ``stripe_webhook_handlers._apply_ultra_resize``, which independently
        checks the live count and refuses an unsafe shrink.

        Fail-safe: a provision must NEVER fail because billing or Paperclip is
        momentarily unreadable. A billing read error (or an absent row) falls
        back to the standard size — worst case an Ultra owner's reprovision lands
        standard-sized and the next reprovision restores it, which beats a failed
        provision. An agent count of 0 means UNDETERMINABLE (every provisioned
        fresh workspaces start with six agents and legacy workspaces have a
        persisted roster), so it is
        treated as no signal and leaves the plan-derived size alone.
        """
        try:
            account = await billing_repo.get_by_owner_id(owner_id)
        except Exception:  # noqa: BLE001 — sizing must never fail a provision.
            logger.warning(
                "plan-size lookup failed for owner %s; defaulting to standard size",
                owner_id,
                exc_info=True,
            )
            account = None
        if (account or {}).get("plan") == "ultra_pro":
            return ULTRA_CPU, ULTRA_MEMORY_MIB

        from core.services.paperclip_openclaw_pairing import count_paperclip_agents_for_owner

        try:
            agent_count = await count_paperclip_agents_for_owner(owner_id)
        except Exception:  # noqa: BLE001 — sizing must never fail a provision.
            # The helper already swallows Paperclip/DDB errors into 0, so this
            # is the belt to that braces: an unexpected raise here (import
            # failure, a future refactor changing its contract) must degrade to
            # "no signal", never take down provisioning for every owner.
            logger.warning(
                "agent-count sizing lookup failed for owner %s; leaving the plan-derived size",
                owner_id,
                exc_info=True,
            )
            agent_count = 0
        if agent_count > 25:
            logger.warning(
                "owner %s has %d live agents during container sizing; monitor boot storage and contention",
                owner_id,
                agent_count,
            )
            put_metric(
                "container.large_agent_roster_observed",
                dimensions={"threshold": "over_25"},
            )
        if agent_count > AGENT_RELIABILITY_RESIZE_THRESHOLD:
            logger.info(
                "owner %s has %d live agents (> %d); applying the operational container reliability size floor",
                owner_id,
                agent_count,
                AGENT_RELIABILITY_RESIZE_THRESHOLD,
            )
            put_metric("container.reliability_size_upgrade", dimensions={"reason": "live_agent_roster"})
            return ULTRA_CPU, ULTRA_MEMORY_MIB
        return PER_USER_CPU, PER_USER_MEMORY_MIB

    async def current_container_size(self, owner_id: str) -> tuple[str, str] | None:
        """The (cpu, memory) of the owner's CURRENTLY REGISTERED task definition.

        Reads the live task-def off the containers row (``task_definition_arn``)
        and returns its ``(cpu, memory)`` strings — the actual size of the box
        that will run right now — or ``None`` if the owner has no provisioned
        container / no task-def ARN on the row (or the describe yields no size).

        Read-only: it describes the task def, registers nothing, mutates
        nothing — the mirror of the size the resize path WOULD change. The
        company-deploy gate uses it to refuse seeding a >10-agent Ultra company
        onto a box the upgrade-webhook resize never actually enlarged (a
        swallowed transient resize leaves ``plan=ultra_pro`` on the standard
        1 vCPU / 4 GB box, which would OOM under a big roster).
        """
        container = await container_repo.get_by_owner_id(owner_id)
        if not container or not container.get("task_definition_arn"):
            return None
        resp = await asyncio.to_thread(
            self._ecs.describe_task_definition,
            taskDefinition=container["task_definition_arn"],
        )
        td = resp.get("taskDefinition") or {}
        cpu = td.get("cpu")
        memory = td.get("memory")
        if cpu is None or memory is None:
            return None
        return str(cpu), str(memory)

    async def _deployed_image_tag(self, owner_id: str) -> str | None:
        """Tag of the image the owner's container is registered to run.

        Feeds ``openclaw_version.deployed_fork_at_least`` so config keys
        that exist only from a given fork patch level are written against the
        image that will actually read them. Returns ``None`` when the owner has
        no task definition yet (fresh provision — the env pin is what the new
        task runs), and ``""`` when a task-def exists but its image can't be
        read (unknown ⇒ the gate stays closed rather than risking a key the
        running image's strict schema rejects at boot).
        """
        try:
            container = await container_repo.get_by_owner_id(owner_id)
            if not container or not container.get("task_definition_arn"):
                return None
            return await self._task_definition_image_tag(container["task_definition_arn"])
        except Exception:
            # A lookup failure must never fail the config write itself; it
            # only withholds the fork-gated keys until the next write.
            logger.warning("Could not read deployed image for owner %s; fork-gated config keys omitted", owner_id)
            return ""

    async def _base_image_tag(self) -> str:
        """Tag of the image a REPLACEMENT task definition will run.

        Provisioning clones the CDK-managed base family
        (:meth:`_build_register_kwargs_from_base`), so on a fresh create or a
        stopped-service scale-up the base family's image — not the owner's
        previous task definition, and not the version pin (CDK may not have
        deployed it yet) — is what reads the config being written. ``""`` when
        it can't be read, which gates fork-level keys closed.
        """
        try:
            return await self._task_definition_image_tag(self._base_task_def_family)
        except Exception:
            logger.warning("Could not read base task-definition image; fork-gated config keys omitted")
            return ""

    async def _task_definition_image_tag(self, task_definition: str) -> str:
        """Image tag of a task definition's primary container; ``""`` when
        it has none (a digest-pinned ``@sha256:…`` image carries no tag)."""
        resp = await asyncio.to_thread(self._ecs.describe_task_definition, taskDefinition=task_definition)
        defs = (resp.get("taskDefinition") or {}).get("containerDefinitions") or []
        return _tagged_image_tag((defs[0] if defs else {}).get("image"))

    def _build_register_kwargs_from_base(
        self,
        access_point_id: str,
        new_image: str | None = None,
        new_cpu: str | None = None,
        new_memory: str | None = None,
        secrets_for_task: list[dict] | None = None,
        environment_for_task: list[dict] | None = None,
        hook_token: str | None = None,
        task_role_arn: str | None = None,
    ) -> dict:
        """Build register_task_definition kwargs by cloning the CDK-managed base.

        Reads the latest revision from the base family (e.g. ``lightbulb-dev-openclaw``)
        live on every call — that family contains ONLY CDK-managed base
        revisions because per-user clones register into ``<base>-user`` (see
        the family below). This means the running backend always picks up
        new CDK base deploys without a backend restart.

        Per-user state layered on top:
          - access_point_id  (per-user EFS access point, replaces the volume's)
          - new_image        (image_update flow only — None preserves base image)
          - new_cpu/memory   (tier-resize flow only — None preserves base values)
          - hook_token       (per-user OpenClaw webhook auth token; injected
                              as ``OPENCLAW_HOOK_TOKEN`` env var on the
                              primary container so the gateway can verify
                              inbound webhooks). Optional today — the
                              backfill-only callers (image-update, resize)
                              that re-register without a hook_token leave
                              the env var off and rely on a separate backfill
                              pass to land it.
        """
        desc_resp = self._ecs.describe_task_definition(
            taskDefinition=self._base_task_def_family,
        )
        base = desc_resp["taskDefinition"]

        volumes = []
        for vol in base.get("volumes", []):
            vol_copy = dict(vol)
            efs_config = vol_copy.get("efsVolumeConfiguration")
            if efs_config:
                efs_copy = dict(efs_config)
                auth_config = dict(efs_copy.get("authorizationConfig", {}))
                auth_config["accessPointId"] = access_point_id
                efs_copy["authorizationConfig"] = auth_config
                vol_copy["efsVolumeConfiguration"] = efs_copy
            volumes.append(vol_copy)

        # Deep-copy containerDefinitions because we may mutate `image`,
        # `secrets`, and `environment`. Each entry is itself a dict — we
        # shallow-copy each before mutation. Per-user `secrets:` (BYOK provider
        # key ARN) and `environment:` (chatgpt_oauth CODEX_HOME pointing at
        # the EFS-staged auth.json dir) are layered onto the primary container.
        container_defs = []
        for idx, cd in enumerate(base["containerDefinitions"]):
            cd_copy = dict(cd)
            if new_image:
                cd_copy["image"] = new_image
            # SIGTERM→SIGKILL grace. The ECS default (30s) truncates
            # OpenClaw's up-to-5-minute graceful drain on every Track-2
            # update, scale-to-zero stop, and deploy — killing in-flight
            # autonomous runs that restart-recovery then re-spends tokens
            # replaying. 120s is the Fargate maximum.
            cd_copy["stopTimeout"] = 120
            if idx == 0 and secrets_for_task:
                # Merge with any baseline `secrets` list the CDK base may have
                # set so we don't drop platform secrets while injecting per-user
                # provider keys.
                base_secrets = list(cd_copy.get("secrets") or [])
                cd_copy["secrets"] = base_secrets + list(secrets_for_task)
            if idx == 0:
                # Layer per-user env entries on top of the CDK base (which carries
                # CHOKIDAR_USEPOLLING, CLAWHUB_WORKDIR, etc.). When a hook_token
                # is provided, inject it as OPENCLAW_HOOK_TOKEN — the helper
                # also de-dupes any stale entry the caller supplied (matters
                # for the backfill story).
                base_env = [
                    entry
                    for entry in (cd_copy.get("environment") or [])
                    if entry.get("name") != _CONFIG_LOCK_PROTOCOL_ENV
                ]
                per_user_env = [
                    entry for entry in (environment_for_task or []) if entry.get("name") != _CONFIG_LOCK_PROTOCOL_ENV
                ]
                if hook_token:
                    per_user_env = _build_per_user_environment(
                        hook_token=hook_token,
                        existing=per_user_env,
                    )
                # Rollout handshake: the CDK base task definition is published
                # before ServiceStack updates the backend. The new boot recovery
                # refuses to run without this marker, which only a backend that
                # participates in the recovery guard can add while cloning.
                per_user_env.append({"name": _CONFIG_LOCK_PROTOCOL_ENV, "value": _CONFIG_LOCK_PROTOCOL_VERSION})
                cd_copy["environment"] = base_env + per_user_env
            container_defs.append(cd_copy)

        reg_kwargs = dict(
            # Per-user clones go to a SEPARATE family so future
            # describe_task_definition(base_family) always returns a CDK base
            # revision, never a per-user clone. Keeps the cross-stack story
            # simple and removes the SSM-pin dance that #299 had to add when
            # the two kinds of revisions co-mingled.
            family=f"{base['family']}{self._PER_USER_FAMILY_SUFFIX}",
            taskRoleArn=task_role_arn or base.get("taskRoleArn", ""),
            executionRoleArn=base.get("executionRoleArn", ""),
            networkMode=base.get("networkMode", "awsvpc"),
            containerDefinitions=container_defs,
            volumes=volumes,
            requiresCompatibilities=base.get("requiresCompatibilities", ["FARGATE"]),
            # Single per-user task size (flat-fee pivot). Explicit `new_cpu`/
            # `new_memory` (used by the resize flow) win; otherwise fall back to
            # the canonical PER_USER_* constants instead of whatever the CDK base
            # task def carries — a stale base value would leak into per-user
            # revisions.
            cpu=new_cpu or PER_USER_CPU,
            memory=new_memory or PER_USER_MEMORY_MIB,
        )
        if base.get("runtimePlatform"):
            reg_kwargs["runtimePlatform"] = base["runtimePlatform"]

        return reg_kwargs

    @staticmethod
    def _carry_per_user_container_state(new_defs: list[dict], prior_defs: list[dict]) -> None:
        """Preserve per-user env/secrets across a base-clone re-register.

        The CDK base carries only fleet-static env/secrets; the per-user set
        (AWS_BEARER_TOKEN_BEDROCK, ACP harness env, BYOK secret refs, ...) is
        layered at provision/restart time and exists ONLY on the owner's prior
        revision. resize/image-update clones from the base as a drift fence,
        so names the new revision already defines win unconditionally; only
        names it lacks are carried over. Stale carried names converge on the
        next full restart, which rebuilds the set from provisioning context.
        Mutates ``new_defs`` in place.
        """

        def _primary(defs: list[dict]) -> dict | None:
            for cd in defs:
                if cd.get("name") == "openclaw":
                    return cd
            return defs[0] if defs else None

        new_cd = _primary(new_defs)
        prior_cd = _primary(prior_defs)
        if not new_cd or not prior_cd:
            return
        for key in ("environment", "secrets"):
            have = {e.get("name") for e in new_cd.get(key, [])}
            carried = [e for e in prior_cd.get(key, []) if e.get("name") not in have]
            if carried:
                new_cd[key] = [*new_cd.get(key, []), *carried]

    def _register_task_definition(
        self,
        access_point_id: str,
        secrets_for_task: list[dict] | None = None,
        environment_for_task: list[dict] | None = None,
        hook_token: str | None = None,
        task_role_arn: str | None = None,
        new_cpu: str | None = None,
        new_memory: str | None = None,
    ) -> str:
        """Clone the CDK base for a new per-user container.

        Args:
            access_point_id: The per-user EFS access point ID.
            secrets_for_task: Optional ECS-task ``secrets:`` entries to layer
                onto the primary container. Each entry is a dict like
                ``{"name": "OPENAI_API_KEY", "valueFrom": "<secrets-arn>"}``.
            environment_for_task: Optional plain ``environment:`` entries
                (``{"name": "...", "value": "..."}``). Used for the
                ``chatgpt_oauth`` path which injects ``CODEX_HOME`` so
                OpenClaw reads its auth.json from EFS.
            hook_token: Optional per-user OpenClaw webhook auth token. When
                provided, injected as ``OPENCLAW_HOOK_TOKEN`` env var on the
                primary container.
            new_cpu / new_memory: Optional task size. The provision/reprovision
                callers pass the owner's plan-derived size (``_size_for_owner``)
                so an Ultra owner's box stays 2 vCPU / 8 GB across every
                re-register. ``None`` falls back to the standard PER_USER_* box.

        Returns:
            The ARN of the newly registered task definition revision.

        Raises:
            EcsManagerError: If the ECS API calls fail.
        """
        try:
            reg_kwargs = self._build_register_kwargs_from_base(
                access_point_id,
                secrets_for_task=secrets_for_task,
                environment_for_task=environment_for_task,
                hook_token=hook_token,
                task_role_arn=task_role_arn,
                new_cpu=new_cpu,
                new_memory=new_memory,
            )
            reg_resp = self._ecs.register_task_definition(**reg_kwargs)
            task_def_arn = reg_resp["taskDefinition"]["taskDefinitionArn"]
            put_metric("container.task_def.register", dimensions={"status": "ok"})
            logger.info("Registered per-user task definition %s", task_def_arn)
            return task_def_arn
        except Exception as e:
            put_metric("container.task_def.register", dimensions={"status": "error"})
            raise EcsManagerError(
                f"Failed to register per-user task definition: {e}",
                user_id="",
            )

    def _deregister_task_definition(self, task_definition_arn: str) -> None:
        """Deregister a per-user task definition revision. Idempotent."""
        try:
            self._ecs.deregister_task_definition(taskDefinition=task_definition_arn)
            logger.info("Deregistered task definition %s", task_definition_arn)
        except Exception as e:
            logger.error("Failed to deregister task definition %s: %s", task_definition_arn, e)

    @serialize_owner_lifecycle
    async def refresh_task_definition(
        self,
        owner_id: str,
        *,
        extra_secrets: list[dict] | None = None,
        drop_secrets: list[str] | None = None,
    ) -> str | None:
        """Per-owner serialized wrapper — see ``_refresh_task_definition_locked``.

        The body is describe → mutate secrets → register → update-service. Two
        concurrent callers for one owner (a second Slack workspace installed
        while the first is still rolling, or an install racing a revoke) would
        both read the same old revision and the last register would silently
        drop the other's secret. One lock per owner makes them take turns.
        """
        # ponytail: in-process lock, belt-and-suspenders over the real
        # cross-instance guard. @serialize_owner_lifecycle (owner_lifecycle_
        # guard) is an EFS-backed fcntl lock held for the whole describe ->
        # mutate -> register -> update-service body below, so at
        # desiredCount 2+ a second instance's call for the same owner
        # already blocks on THAT guard, not this lock. The fleet-roll path
        # (prepare_config_lock_protocol_task_definition ->
        # resize_user_container) goes through the same decorator. No DDB
        # fence needed on top.
        async with _owner_task_def_lock(owner_id):
            return await self._refresh_task_definition_locked(
                owner_id, extra_secrets=extra_secrets, drop_secrets=drop_secrets
            )

    async def _refresh_task_definition_locked(
        self,
        owner_id: str,
        *,
        extra_secrets: list[dict] | None = None,
        drop_secrets: list[str] | None = None,
    ) -> str | None:
        """Re-register a user's ECS task definition with the current env list,
        then roll the running task onto the new revision.

        Used by the hook-token backfill: existing per-user task-defs were
        registered before OPENCLAW_HOOK_TOKEN existed; just patching
        openclaw.json on EFS leaves the env var undefined inside the running
        container, so the gateway's ${VAR} substitution resolves to empty.
        This helper rebuilds the env list (now including the freshly-set
        hook_token from the containers row) and updates the service to
        deploy a new task-def revision.

        Unlike ``_register_task_definition`` (which clones from the CDK base
        family via ``_build_register_kwargs_from_base``), the backfill path
        must preserve every per-user mutation already on the live revision
        (BYOK provider secrets, chatgpt_oauth ``CODEX_HOME``, etc.). So we
        describe the user's CURRENT task-def, swap its env list, and
        re-register that — the base-cloning seam doesn't fit here.

        ``extra_secrets`` (``{name, valueFrom}``) are merged onto the
        openclaw container's ``secrets`` list, replacing same-named entries —
        the Slack install uses this to add ``SLACK_BOT_TOKEN_{TEAM}`` without
        touching BYOK entries. ``drop_secrets`` removes same-named entries —
        an uninstalled Slack workspace's token must leave the task-def before
        its secret is force-deleted, or ECS can never relaunch the task.

        The EFS access point and DDB row are untouched — losing agent
        state would be a backfill regression.

        Returns the new task definition ARN on success, or None if the user
        has no provisioned container or the row is missing task_definition_arn.
        Callers need the ARN so they can wait specifically for the PRIMARY
        deployment to roll onto this revision (waiting for any RUNNING task
        races against the OLD task during ``forceNewDeployment``).
        """
        container = await container_repo.get_by_owner_id(owner_id)
        if not container or not container.get("task_definition_arn"):
            return None

        # Read the current task-def to pull family/volumes/secrets/networkMode/etc.
        current = self._ecs.describe_task_definition(taskDefinition=container["task_definition_arn"])
        td = current["taskDefinition"]

        # Rebuild the openclaw container-def with a fresh environment list
        # that includes the freshly-set OPENCLAW_HOOK_TOKEN.
        existing_env = next(c for c in td["containerDefinitions"] if c["name"] == "openclaw")["environment"]
        # Drop any container-wide PAPERCLIP_* env carried by task-defs registered
        # before per-agent Paperclip identity. That shared env let every agent's
        # curl resolve the seeded Friday identity; identity is now per-agent
        # (each agent reads its own claimed-key file), so the legacy shared env
        # must not survive a refresh and re-seed Friday's credentials fleet-wide.
        existing_env = [
            e
            for e in existing_env
            if not e.get("name", "").startswith("PAPERCLIP_") and e.get("name") != _CONFIG_LOCK_PROTOCOL_ENV
        ]
        new_env = _build_per_user_environment(
            hook_token=container.get("hook_token") or "",
            existing=existing_env,
        )
        new_env.append({"name": _CONFIG_LOCK_PROTOCOL_ENV, "value": _CONFIG_LOCK_PROTOCOL_VERSION})
        for cd in td["containerDefinitions"]:
            if cd["name"] == "openclaw":
                cd["environment"] = new_env
                if extra_secrets or drop_secrets:
                    drop = set(drop_secrets or [])
                    incoming = {s["name"]: s for s in (extra_secrets or [])}
                    kept = [
                        s
                        for s in (cd.get("secrets") or [])
                        if s.get("name") not in incoming and s.get("name") not in drop
                    ]
                    cd["secrets"] = kept + list(incoming.values())

        register_kwargs = {
            "family": td["family"],
            "containerDefinitions": td["containerDefinitions"],
            "volumes": td.get("volumes", []),
            "networkMode": td["networkMode"],
            "requiresCompatibilities": td.get("requiresCompatibilities", []),
            "cpu": td.get("cpu"),
            "memory": td.get("memory"),
            "executionRoleArn": td.get("executionRoleArn"),
            "taskRoleArn": td.get("taskRoleArn"),
        }
        register_kwargs = {k: v for k, v in register_kwargs.items() if v is not None}
        new_td = self._ecs.register_task_definition(**register_kwargs)
        new_arn = new_td["taskDefinition"]["taskDefinitionArn"]

        self._ecs.update_service(
            cluster=settings.ECS_CLUSTER_ARN,
            service=container["service_name"],
            taskDefinition=new_arn,
            forceNewDeployment=True,
        )
        # Persist the new ARN so subsequent operations see it.
        await container_repo.update_fields(owner_id, {"task_definition_arn": new_arn})
        return new_arn

    # ------------------------------------------------------------------
    # Safe DB helpers
    # ------------------------------------------------------------------

    async def _update_container(
        self,
        user_id: str,
        *,
        claim_id: str | None = None,
        generation: str | None = None,
        attempt_token: str | None = None,
        **fields,
    ) -> None:
        """Update container fields via the DynamoDB container repo.

        CLAIM FENCE: with ``claim_id`` the write goes through
        ``container_repo.update_container_leased``, so it is conditioned on
        this run still holding the provision lease and raises
        ``ProvisionSuperseded`` when it does not. Without a lease-conditioned
        write, a run superseded between two checkpoints still stamped its own
        ``access_point_id`` / ``task_definition_arn`` / ``substatus`` over the
        winner's row (``update_fields`` is conditioned only on the row
        existing), leaving a row that describes a mixture of two provisions.
        ``claim_id=None`` keeps the plain unfenced ``update_fields``, exactly
        as the checkpoints treat a missing lease: the callers that reach here
        without one are ``routers/debug.py``'s direct create_user_service, the
        resize path (webhook/admin, no provision lease), and tests.
        ``start_user_service`` picks between this helper and its original bare
        ``update_status`` on the same rule, at its own call site.

        ``generation`` is the same optional extra fence the checkpoints take,
        and each call site passes exactly what the checkpoint next to it
        passes. ``_provision_user_container_inner``'s writes (and the
        ``start_user_service`` write it drives) DO pass it — the value they
        hold is the one every checkpoint in that function was just validated
        against. ``create_user_service``'s three writes do NOT: its own upsert
        re-mints ``provision_generation``, so any caller-supplied value is
        stale by construction there — which is why that method takes no
        ``generation`` argument at all.

        SINGLE-OWNER RULE (guarded): this generic provision-path setter must
        NEVER write a ``status="error"`` transition. That transition is owned
        solely by the claim-fenced ``container_repo.release_provision_to_error``
        (fired from ``provision_user_container``'s except), which lands the
        cause — ``retryable`` + ``last_error`` — atomically with the flip.
        Eight scattered pre-emptive ``status="error"`` writes used to smuggle
        through here and pre-empt that release (its ``status = provisioning``
        condition then no-op'd, so the cause never landed — the T3 Critical);
        they were removed, and this guard keeps them from creeping back. Other
        status values (e.g. ``provisioning`` on the resize path) are fine —
        only the ``error`` terminal transition is reserved. The one legitimate
        non-release ``status="error"`` writer, ``_await_running_transition``'s
        rolloutState=FAILED path, writes through ``container_repo.update_fields``
        DIRECTLY (fire-and-forget, no outer release to defer to) and so is
        intentionally not routed through this helper."""
        if fields.get("status") == "error":
            raise AssertionError(
                "status='error' must not be written through _update_container — "
                "the claim-fenced release_provision_to_error owns that transition"
            )
        if claim_id is None:
            await container_repo.update_fields(user_id, fields)
            return
        # Lazy module lookup for the LEASE primitive, same reason as
        # _provision_checkpoint: several tests wholesale-replace this module's
        # `container_repo` binding with a MagicMock to stub row I/O, while the
        # lease is stubbed at its source module.
        from core.repositories import container_repo as _lease_repo

        await _lease_repo.update_container_leased(
            user_id, claim_id, fields, generation=generation, attempt_token=attempt_token
        )

    # ------------------------------------------------------------------
    # Service lifecycle
    # ------------------------------------------------------------------

    async def create_user_service(
        self,
        user_id: str,
        gateway_token: str,
        owner_type: str = "personal",
        secrets_for_task: list[dict] | None = None,
        environment_for_task: list[dict] | None = None,
        hook_token: str | None = None,
        task_role_arn: str | None = None,
        bedrock_aip_arns: dict[str, str] | None = None,
        bedrock_gate_token_hash: str | None = None,
        claim_id: str | None = None,
        attempt_token: str | None = None,
    ) -> str:
        """Create an ECS Service for a user with per-user EFS isolation.

        1. Upserts the Container DB record (status=provisioning) so frontend can poll
        2. Creates a per-user EFS access point -> substatus=efs_created
        3. Registers a per-user task definition revision -> substatus=task_registered
        4. Creates the ECS service -> substatus=service_created

        On failure, rolls back AWS resources and re-raises WITHOUT flipping the
        row to status=error — that transition is owned solely by the outer
        claim-fenced release_provision_to_error (see the except blocks below).

        Args:
            user_id: Clerk user ID.
            gateway_token: Auth token for the OpenClaw gateway HTTP API.
            owner_type: "personal" or "org" for tagging the EFS access point.
            secrets_for_task: Optional per-user ECS ``secrets:`` entries to
                inject into the primary container (e.g. the BYOK
                OPENAI_API_KEY/ANTHROPIC_API_KEY ARN). Empty for the
                ``chatgpt_oauth`` and ``bedrock_claude`` paths.
            environment_for_task: Optional plain ``environment:`` entries.
                Used by the ``chatgpt_oauth`` path to set ``CODEX_HOME``
                so OpenClaw reads its auth.json from the EFS-staged dir.
            hook_token: Optional per-user OpenClaw webhook auth token. When
                provided, persisted onto the containers row by the upsert
                below (atomically with the rest of the row, so brand-new
                rows land with the token already set) and injected as
                ``OPENCLAW_HOOK_TOKEN`` on the primary container.
            task_role_arn: Optional per-owner ECS task-role ARN override (the
                scoped per-owner Bedrock role for the ``bedrock_claude`` path).
                Threaded into ``_register_task_definition`` AND, when present,
                persisted onto the containers row as ``bedrock_role_arn`` by the
                upsert below — see the bedrock-fields note there.
            bedrock_aip_arns: Optional per-owner AIP map ({tier: aip_arn}) for
                the ``bedrock_claude`` path. Persisted onto the containers row
                as ``bedrock_aip_arns`` by the upsert below.
            attempt_token: Optional additional checkpoint fence — see
                ``container_repo.claim_attempt`` docstring (spec §7 hazard 1).

        Returns:
            The ECS service name.

        Raises:
            EcsManagerError: If any step fails.
        """
        service_name = self._service_name(user_id)
        access_point_id = None
        task_def_arn = None

        # Step 0: Upsert container record EARLY so frontend can start polling.
        # Persist hook_token here so brand-new rows land with it set in the
        # same write that creates the row — folding it into a separate
        # set_hook_token call would no-op for first-time provisioning (the
        # row doesn't exist until this upsert).
        upsert_fields: dict = {
            "service_name": service_name,
            "gateway_token": gateway_token,
            "status": "provisioning",
            "substatus": None,
            "owner_type": owner_type,
            # The container-incarnation fence (INV-2). THE single mint point
            # for provision_generation, system-wide. Every other
            # containers-row write path (scale-from-zero, force-redeploy,
            # restart, provider hot-swap, Bedrock revoke/restore, Stripe
            # conversion) goes through container_repo.update_fields /
            # update_status / other scoped writers, none of which touch this
            # field, so it survives all of them untouched.
            "provision_generation": str(ULID()),
        }
        if hook_token:
            upsert_fields["hook_token"] = hook_token
            upsert_fields["hook_token_rotated_at"] = utc_now_iso()
            # Stamp the backfill-style "this user is in the desired end-state"
            # marker so a re-run of the backfill script's idempotency guard
            # (which reads hook_token_provisioned_at) doesn't rotate native
            # users' tokens and silently break their external integrations.
            # Native provisioning achieves the desired state atomically here
            # (token in DDB + env on task def + openclaw.json rendered with
            # the hooks block); backfill achieves it after refresh + config
            # patch. Both should stamp.
            upsert_fields["hook_token_provisioned_at"] = utc_now_iso()
        # Persist the per-owner Bedrock role + AIP map here too, for the SAME
        # reason as hook_token: the mint (ensure_owner_aips/ensure_owner_bedrock_role)
        # ran BEFORE this upsert, so its container_repo.update_fields calls
        # no-opped against a not-yet-existing row (update_fields returns None and
        # writes nothing when the row is absent). Only the reverse table got
        # written. Without folding them into this row-creating upsert, the row
        # would carry no bedrock_role_arn, and the next resize/restart re-register
        # (which reads bedrock_role_arn off the row) would silently revert the
        # container to the shared all-models task role — which no longer carries
        # any Bedrock grant — AccessDenying every subsequent chat. restore_owner_bedrock
        # would also break (it reads the AIP map off the row).
        if task_role_arn:
            upsert_fields["bedrock_role_arn"] = task_role_arn
        if bedrock_aip_arns:
            upsert_fields["bedrock_aip_arns"] = bedrock_aip_arns
        # CHECKPOINT before the row write: a run superseded during the mint must
        # not deposit its gateway_token / AIP map / role onto the row — the
        # upsert deliberately preserves the NEW owner's lease fields, so without
        # this fence the zombie's write would sail straight through.
        if claim_id is not None:
            await self._provision_checkpoint(user_id, claim_id, attempt_token=attempt_token)
        await container_repo.upsert(user_id, upsert_fields)

        # The bedrock-gate token hash is NOT folded into that upsert. It moves
        # only through container_repo.swap_gate_token_hash — one atomic writer —
        # because the whole-item put has a read-to-write window a concurrent
        # mint can slip through, orphaning a denied=False credential the kill
        # switch can then never reach. mint_gate_token already ran its own swap;
        # this re-asserts it for the case where the row did not exist yet, now
        # that the upsert above has created it. Placed HERE, before the task
        # definition carrying the plaintext is registered, so no task can ever
        # start holding a token the row does not know about. It needs no
        # checkpoint in front of it: when it writes, it carries the lease in its
        # own condition and refuses a superseded run atomically; when it does
        # not write (the row already points at this hash) there is nothing to
        # fence — the checkpoint below catches that run before any artifact.
        if bedrock_gate_token_hash:
            from core.billing.bedrock_gate_tokens import persist_gate_token_hash

            await persist_gate_token_hash(
                user_id, bedrock_gate_token_hash, claim_id=claim_id, attempt_token=attempt_token
            )

        # CHECKPOINT at the last moment before the first thing that CANNOT carry
        # a condition. Everything above here is a conditional DynamoDB write
        # that fences itself; the EFS access point and the task-definition
        # revision below are AWS calls with no such option, so this is the only
        # fence they can have. Deliberately AFTER the swap rather than before:
        # the swap's no-op branch writes nothing and so needs no guard, and
        # guarding it there would leave the branch that matters — a superseded
        # run whose hash is already current — sailing straight into the artifact
        # calls, which is exactly the window this closes.
        if claim_id is not None:
            await self._provision_checkpoint(user_id, claim_id, attempt_token=attempt_token)

        try:
            # Step 1: Create per-user EFS access point
            access_point_id = self._create_access_point(user_id, owner_type=owner_type)
            # Fenced on the lease (see _update_container): the checkpoint above
            # only covers the moment it ran — a takeover landing while the
            # access point was being created must not let this run stamp its
            # own access_point_id over the winner's row. No `generation`: the
            # upsert above just re-minted provision_generation, so a
            # caller-supplied one would be stale by construction — which is why
            # create_user_service takes no `generation` at all and its
            # checkpoints omit it too.
            await self._update_container(
                user_id,
                claim_id=claim_id,
                attempt_token=attempt_token,
                access_point_id=access_point_id,
                substatus="efs_created",
            )

            # Step 2: Register per-user task definition with that access point.
            # Inject any per-user secrets (e.g. byo_key OPENAI/ANTHROPIC ARN),
            # per-user env vars, and the per-user OPENCLAW_HOOK_TOKEN (so the
            # OpenClaw gateway can verify inbound webhook calls) before the
            # service is created so the first task pulls them. Size by the
            # owner's plan so an Ultra owner's first task def is the large
            # box, not the standard one.
            new_cpu, new_memory = await self._size_for_owner(user_id)
            task_def_arn = self._register_task_definition(
                access_point_id,
                secrets_for_task=secrets_for_task,
                environment_for_task=environment_for_task,
                hook_token=hook_token,
                task_role_arn=task_role_arn,
                new_cpu=new_cpu,
                new_memory=new_memory,
            )
            await self._update_container(
                user_id,
                claim_id=claim_id,
                attempt_token=attempt_token,
                task_definition_arn=task_def_arn,
                substatus="task_registered",
            )

            # Step 3: Create ECS service with desiredCount=0 so the container
            # does NOT start yet -- the caller writes config files (openclaw.json)
            # to EFS before calling start_user_service().
            create_kwargs = dict(
                cluster=self._cluster,
                serviceName=service_name,
                taskDefinition=task_def_arn,
                desiredCount=0,
                capacityProviderStrategy=SPOT_CAPACITY_STRATEGY,
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": self._subnets,
                        "securityGroups": self._security_groups,
                        "assignPublicIp": "DISABLED",
                    }
                },
                serviceRegistries=[{"registryArn": self._cloud_map_service_arn}],
                deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
            )
            # Enable ECS Exec for all environments so we can debug per-user
            # OpenClaw containers (skill installs, workspace state, gateway
            # health) without redeploying. Requires the task role to grant
            # ssmmessages:CreateControlChannel/CreateDataChannel/OpenControlChannel/OpenDataChannel.
            create_kwargs["enableExecuteCommand"] = True
            # CHECKPOINT before the ECS service create — the last unfenced
            # window where a superseded run could stand up a service built from
            # its own task definition/role.
            if claim_id is not None:
                await self._provision_checkpoint(user_id, claim_id, attempt_token=attempt_token)
            try:
                self._ecs.create_service(**create_kwargs)
            except ClientError as e:
                # A cluster without the FARGATE_SPOT capacity provider
                # associated (CDK deploy lagging a backend deploy) rejects the
                # strategy with InvalidParameterException. Provisioning must
                # not hard-fail on a cost optimization — retry on-demand.
                if e.response.get("Error", {}).get("Code") != "InvalidParameterException":
                    raise
                logger.warning(
                    "Spot capacity provider rejected for %s; creating on-demand: %s",
                    service_name,
                    e,
                )
                put_metric(
                    "container.spot_fallback",
                    dimensions={"trigger": "create_rejected"},
                )
                # launchType, not ON_DEMAND_CAPACITY_STRATEGY: a cluster with
                # no capacity providers associated rejects ANY strategy, while
                # launchType works unconditionally.
                del create_kwargs["capacityProviderStrategy"]
                create_kwargs["launchType"] = "FARGATE"
                self._ecs.create_service(**create_kwargs)
            await self._update_container(
                user_id,
                claim_id=claim_id,
                attempt_token=attempt_token,
                substatus="service_created",
            )
        except ProvisionSuperseded:
            raise
        except EcsManagerError:
            put_metric("container.error_state")
            # NO status="error" write here (single-owner rule): the outer
            # claim-fenced release_provision_to_error owns the provision-path
            # error transition. A pre-emptive flip here pre-empted that release
            # (its `status = provisioning` condition then no-op'd), so
            # retryable/last_error never landed — the T3 Critical. Roll back the
            # AWS resources we created and re-raise so the outer release records
            # the cause. (Dev-only routers/debug.py also calls this directly; on
            # its failure the row simply stays provisioning and the next status
            # poll re-dispatches — see report.)
            if task_def_arn:
                self._deregister_task_definition(task_def_arn)
            if access_point_id:
                self._delete_access_point(access_point_id)
            raise
        except Exception as e:
            put_metric("container.error_state")
            # No status="error" write — see the EcsManagerError branch above;
            # the outer release owns the transition. Roll back + re-raise as
            # EcsManagerError so the cause is classified by the outer release.
            if task_def_arn:
                self._deregister_task_definition(task_def_arn)
            if access_point_id:
                self._delete_access_point(access_point_id)
            logger.error(
                "Failed to create ECS service %s for user %s: %s",
                service_name,
                user_id,
                e,
            )
            raise EcsManagerError(f"Failed to create ECS service: {e}", user_id)

        logger.info("Created ECS service %s for user %s", service_name, user_id)
        return service_name

    async def _hosted_on_agentcore(self, user_id: str) -> bool:
        """An AgentCore workspace has no ECS task to start or stop: AWS wakes the microVM on
        invoke and parks it on idle. Both chokepoints answer "nothing to do", so no door — the
        status poll, chat wake, agent mail, the trial-idle sweep, an admin click — can start a
        second gateway on the workspace's access point or flip its row back to ``stopped``.
        Rollout scaffolding: deleted with the ECS path once the last workspace has moved."""
        row = await container_repo.get_by_owner_id(user_id)
        return bool(row) and runtime_transport.host_for(row) == "agentcore"

    @serialize_owner_lifecycle
    async def stop_user_service(self, user_id: str, *, reason: str = "manual") -> None:
        """Scale a user's ECS service to 0 (stopped).

        Args:
            user_id: Clerk user ID.
            reason: Stop provenance stamped onto the row as ``stop_reason``
                (``trial_idle`` also stamps ``parked_at``). The wake-suppression
                predicate treats ONLY ``trial_idle`` rows as parked; every other
                reason keeps the status poll's wake/self-heal behavior, so a
                killswitch or admin stop can never masquerade as a healthy
                parked box.

        Raises:
            EcsManagerError: If the ECS update_service call fails. A service
                that doesn't exist (or is already INACTIVE) is idempotent
                success, not an error — "stopped" is already true, and the
                callers that can hit it (a provider change or reprovision on
                a subscribed-but-never-provisioned owner, a killswitch on an
                already-deleted box) all want to proceed, mirroring
                delete_user_service's not-found handling below.
        """
        if await self._hosted_on_agentcore(user_id):
            logger.info("stop_user_service(reason=%s): workspace is on AgentCore, which parks itself; no-op", reason)
            return
        service_name = self._service_name(user_id)
        put_metric(
            "container.lifecycle.state_change",
            dimensions={"state": "stopping", "reason": reason},
        )

        try:
            with timing("container.lifecycle.latency", {"op": "stop"}):
                self._ecs.update_service(
                    cluster=self._cluster,
                    service=service_name,
                    desiredCount=0,
                )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ServiceNotFoundException", "ServiceNotActiveException"):
                logger.info(
                    "stop_user_service: service %s for user %s doesn't exist — nothing to stop",
                    service_name,
                    user_id,
                )
            else:
                logger.error(
                    "Failed to stop ECS service %s for user %s: %s",
                    service_name,
                    user_id,
                    e,
                )
                raise EcsManagerError(f"Failed to stop ECS service: {e}", user_id)
        except Exception as e:
            logger.error(
                "Failed to stop ECS service %s for user %s: %s",
                service_name,
                user_id,
                e,
            )
            raise EcsManagerError(f"Failed to stop ECS service: {e}", user_id)

        container = await container_repo.get_by_owner_id(user_id)
        if container:
            await container_repo.update_status(user_id, "stopped")
            # Separate call because stamp_stop_reason needs a conditional REMOVE
            # of parked_at that a plain field merge cannot express. Order no
            # longer matters for safety — both are scoped update_items now, so
            # neither can resurrect a stale item over a concurrent wake's lease
            # claim; it was the get→put update_status that once could.
            await container_repo.stamp_stop_reason(user_id, reason)

        logger.info("Stopped ECS service %s for user %s (reason=%s)", service_name, user_id, reason)

    async def kill_owner_bedrock(self, owner_id: str) -> None:
        """ADMIN BREAK-GLASS — NOT balance enforcement.

        Balance enforcement is revoke-only (the $50 subscription pays for the
        container; only Bedrock inference draws credits) — see
        `core/services/bedrock_enforcement.py`. This combined kill exists for
        genuine runaways (a container looping Bedrock calls hard enough that
        even the ~5-15s IAM-propagation window matters, or an incident where
        an operator wants the box DOWN), invoked by an operator, never by the
        low-balance webhook.

        Fires BOTH:
          1. IAM revoke (`revoke_owner_bedrock` from
             `core.billing.bedrock_aip`, Plan 2's primitive) — detaches/denies
             the per-owner Bedrock policy. Eventually-consistent (~5-15s), so it
             is fired first to start the propagation clock.
          2. ECS `desiredCount=0` (via `stop_user_service`) — the guaranteed
             instant stop; the running container is the only live spend source.

        The IAM revoke is best-effort: if it raises, the ECS stop MUST still
        run (the container keeps invoking Bedrock until desired=0 lands).
        """
        from core.billing.bedrock_aip import revoke_owner_bedrock
        from core.observability.metrics import put_metric

        put_metric("container.killswitch.invoke")
        try:
            await revoke_owner_bedrock(owner_id)
        except Exception:
            put_metric("container.killswitch.iam_revoke_error")
            logger.exception(
                "IAM revoke failed for owner %s during kill-switch — proceeding to ECS desired=0 (the guaranteed stop)",
                owner_id,
            )
        await self.stop_user_service(owner_id, reason="killswitch")
        logger.warning("Bedrock kill-switch completed for owner %s", owner_id)

    async def _assert_start_allowed(self, user_id: str) -> None:
        """The two guards every customer-driven container start must pass.

        This is the chokepoint. Both checks used to live ONLY on the
        ``GET /container/status`` auto-retry branch, so ``POST
        /container/provision`` — which restarts a stopped container directly —
        bypassed both. Duplicating them into a second router branch is how they
        drifted apart in the first place; they live here instead, where no
        caller can forget them.

        1. FREE-TIER DAILY CAP. A ``trial_local`` owner over the $3/day cap had
           their container scaled to zero *by* the cap. Waking it is exactly
           what the cap forbids, so the container stays down until the counter
           resets (UTC midnight) or the owner upgrades. Same predicate the
           status path uses (``trial_cap.trial_block_reason``), so the two can
           never disagree.

        2. TRIAL->PAID CONVERSION IN FLIGHT. ``reprovision_for_user`` scales the
           trial container to zero and then BLOCKS in a ~90s drain wait — the
           per-owner state is WAL-mode SQLite on EFS and a second live task
           holding it open is our documented corruption mode. A start inside
           that window IS that second task. ``conversion_guard_active`` is the
           same predicate the status path stands down on; it keys off the
           ``conversion_started_at`` TIMESTAMP rather than the ``converting``
           substatus, because ``stop_user_service`` writes
           ``update_status(owner, "stopped")`` — substatus defaults to None —
           and so clobbers the label the moment the drain begins.

        Raises:
            ContainerStartRefused: If either guard is up. Never EcsManagerError:
                nothing failed, so the row must not be marked "error".
        """
        # Lazy imports: both modules sit above core.containers in the import
        # graph (stripe_webhook_handlers reaches back into it via
        # ``from core.containers import get_ecs_manager``), so importing either
        # at module scope would close a cycle.
        from core.services import trial_cap
        from core.services.stripe_webhook_handlers import conversion_guard_active

        container = await container_repo.get_by_owner_id(user_id)
        if conversion_guard_active(container):
            put_metric("container.start.refused", dimensions={"reason": START_REFUSED_CONVERTING})
            raise ContainerStartRefused(
                START_REFUSED_CONVERTING,
                user_id,
                "A subscription upgrade is reprovisioning this container; it will come back on its own.",
            )

        # 3. PROVIDER-CHANGE REPROVISION IN FLIGHT. change_provider's cold
        #    path stops the container (stop_reason="reprovision") and then
        #    drains before dispatching the provision. A direct start inside
        #    that window (POST /container/provision's restart door) would
        #    set desiredCount=1 on the STALE task def, and the later
        #    provision claim would then see a desired/running service and
        #    take the branch that rewrites nothing — silently dropping the
        #    provider switch. Refusing here is safe: the status poll's wake
        #    path dispatches dispatch_provision (force=True internally),
        #    bypasses this guard, and applies the switch properly — so the
        #    row always converges even if the drain task died.
        if container and container.get("status") == "stopped" and container.get("stop_reason") == "reprovision":
            put_metric("container.start.refused", dimensions={"reason": START_REFUSED_REPROVISIONING})
            raise ContainerStartRefused(
                START_REFUSED_REPROVISIONING,
                user_id,
                "A provider change is reprovisioning this container; it will come back on its own.",
            )

        # 4. TASK-DEF-SHAPED CONFIG CHANGE PENDING (``needs_provision``). A
        #    provider switch made while the container was ALREADY stopped
        #    leaves no ``stop_reason`` breadcrumb — guard 3's signal exists
        #    only when change_provider itself stopped a running box. The
        #    marker (written by ``billing_repo.set_provider_choice``) is the
        #    row-state signal that the REGISTERED task definition is stale;
        #    a direct start would boot the old shape (old secrets/env/AIP
        #    wiring) and silently drop the switch. Refusing here covers
        #    every direct-start door at once — the wake verb, agent-mail
        #    delivery, POST /container/start — and each falls back to (or
        #    the user's next status poll triggers) the full executor, which
        #    re-registers the task def and clears the marker with its
        #    terminal running write. The executor's own start runs with
        #    force=True, so the full path can never deadlock on this guard.
        if container and container.get("status") == "stopped" and container.get("needs_provision"):
            put_metric("container.start.refused", dimensions={"reason": START_REFUSED_REPROVISIONING})
            raise ContainerStartRefused(
                START_REFUSED_REPROVISIONING,
                user_id,
                "A configuration change is being applied; the container will come back on its own.",
            )

        # ONE read covering both ways a free-tier owner runs out of runway.
        # Checking only the cap here would let an EXPIRED trial wake its own
        # container through this path — the gate refuses it, this would not.
        reason = await trial_cap.trial_block_reason(user_id)
        if reason:
            put_metric("container.start.refused", dimensions={"reason": reason})
            raise ContainerStartRefused(reason, user_id, _TRIAL_REFUSAL_COPY[reason])

    @serialize_owner_lifecycle
    async def start_user_service(
        self,
        user_id: str,
        *,
        force: bool = False,
        claim_id: str | None = None,
        generation: str | None = None,
        attempt_token: str | None = None,
    ) -> None:
        """Scale a user's ECS service to 1 (running).

        THE START CHOKEPOINT. Unless ``force``, every start runs
        ``_assert_start_allowed`` first — the free-tier daily cap and the
        trial->paid conversion guard.

        Fires _await_running_transition afterwards so the provisioning -> running
        transition happens eventually even if the user disconnects before the
        ECS task finishes warming up. Without this, cold-start restart rows
        get stuck at status=provisioning forever. EXCEPT when ``claim_id`` is
        given: that means the caller is ``_provision_user_container_inner``'s
        own fresh-provision path, whose only production caller
        (``scripts.provision_runner``) already awaits
        ``wait_for_running(user_id, claim_id=...)`` in the FOREGROUND right
        after ``provision_user_container`` returns — firing a second detached
        poller here would duplicate ECS API calls against the same row for no
        upside. Every OTHER caller (wake-from-stopped, the admin break-glass
        start, /debug/provision) passes no ``claim_id`` and keeps the
        detached poller exactly as before.

        Does NOT pass forceNewDeployment=True. The historical use case here was
        a stopped service (desiredCount=0) — `desiredCount=1` alone is enough
        to launch a new task. Forcing a new deployment was unintentionally
        destructive when called in racy contexts: if `stop_user_service` had
        just set `desiredCount=0` but ECS hadn't yet stopped the old task,
        a follow-up `start_user_service` with forceNewDeployment would
        terminate that still-running task and replace it with a fresh one,
        producing a ~30s outage right after a user logged in (the cascade
        traced in the post-incident review).

        Args:
            user_id: Clerk user ID.
            force: Skip the start guards. SYSTEM and OPERATOR callers only —
                never a customer-reachable path:
                  - ``provision_user_container``'s own start. It is the
                    conversion's reprovision (and the auto-retry's), dispatched
                    *after* the row is stamped — guarding it would deadlock the
                    conversion against its own marker. Its callers gate
                    themselves upstream (``evaluate_provision_gate``, which
                    already returns the same ``trial_daily_cap`` gate).
                  - the audited admin start (``POST /admin/users/{id}
                    /container/start``) — the operator break-glass, and the only
                    manual lever to un-wedge a container carrying a stranded
                    ``converting`` substatus.
                  - the dev-only ``/debug/provision`` routes (403 in prod).

        Raises:
            ContainerStartRefused: If a start guard is up (and not ``force``).
            EcsManagerError: If the ECS update_service call fails.
        """
        if await self._hosted_on_agentcore(user_id):
            logger.info("start_user_service: workspace is on AgentCore, which wakes on invoke; no-op")
            return
        if not force:
            await self._assert_start_allowed(user_id)

        service_name = self._service_name(user_id)
        put_metric("container.lifecycle.state_change", dimensions={"state": "starting"})

        try:
            with timing("container.lifecycle.latency", {"op": "start"}):
                # A wake reuses the service's existing task-def revision, which
                # pins the log group by name. If CDK has since renamed it, that
                # revision names a group that no longer exists and the task
                # cannot start at all. Refresh before scaling up, not after.
                refreshed = None
                try:
                    svc = self._ecs.describe_services(cluster=self._cluster, services=[service_name])
                    td = (svc.get("services") or [{}])[0].get("taskDefinition")
                    if td:
                        refreshed = self._refresh_stale_log_config(td)
                except Exception as e:  # noqa: BLE001 — a failed check must not block the start
                    logger.warning("log-config refresh check failed for %s: %s", service_name, e)

                self._ecs.update_service(
                    cluster=self._cluster,
                    service=service_name,
                    desiredCount=1,
                    # Re-assert the Cloud Map registry on EVERY start, not just
                    # on the provision paths. This is the call the scale-up
                    # branch actually reaches, so without it a parked service
                    # wakes still registered against whatever registry it was
                    # created with -- discover_ip then finds nothing and the
                    # container looks unreachable while running perfectly.
                    **self._service_registries(),
                    **({"taskDefinition": refreshed} if refreshed else {}),
                    deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
                )
        except Exception as e:
            logger.error(
                "Failed to start ECS service %s for user %s: %s",
                service_name,
                user_id,
                e,
            )
            raise EcsManagerError(f"Failed to start ECS service: {e}", user_id)

        container = await container_repo.get_by_owner_id(user_id)
        if container:
            if claim_id is None:
                # No lease: the original unconditional write, untouched. Every
                # caller but the provision lands here (wake-from-stopped, agent
                # mail, the admin and debug starts).
                await container_repo.update_status(user_id, "provisioning")
            else:
                # A claim_id reaches here only from
                # _provision_user_container_inner's two calls, and only when
                # the SFN runner supplied one. Unfenced, this is a blind
                # "provisioning" stamp: a superseded run arriving after the
                # winner had finished would knock the winner's status=running
                # row back to provisioning, and the status poll would
                # re-provision a healthy container. The fields are exactly
                # update_status's — it writes substatus=None alongside.
                await self._update_container(
                    user_id,
                    claim_id=claim_id,
                    generation=generation,
                    attempt_token=attempt_token,
                    status="provisioning",
                    substatus=None,
                )

        logger.info("Started ECS service %s for user %s", service_name, user_id)

        if claim_id is not None:
            # Fenced fresh-provision caller — its own foreground
            # wait_for_running owns the transition; see docstring.
            return

        # Fire the durable poller. Any code path that sets status=provisioning
        # MUST ensure a transition poller is running, otherwise a slow ECS
        # cold-start leaves the row stuck.
        asyncio.create_task(self._await_running_transition(user_id))

    @serialize_owner_lifecycle
    async def force_redeploy_user_service(self, user_id: str) -> None:
        """Replace the service's task with a fresh one on the CURRENT revision.

        No task-def re-register — this is a plain forceNewDeployment, used when
        a container-local state change can only be applied by a boot pass (the
        clear-auth sentinel). Single-writer deployment config keeps the old
        task fully stopped before the replacement starts.
        """
        # Persist the browser session (cookies + localStorage) BEFORE the roll
        # stops the old task — its /tmp Chrome profile is wiped on replacement.
        # Best-effort, never raises. Lazy import: services -> containers, so a
        # module-level import here would cycle.
        from core.services.browser_state import snapshot_browser_state

        await snapshot_browser_state(user_id)
        await asyncio.to_thread(
            self._ecs.update_service,
            cluster=self._cluster,
            service=self._service_name(user_id),
            forceNewDeployment=True,
            deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
        )

    @serialize_owner_lifecycle
    async def resize_user_container(
        self,
        user_id: str,
        new_cpu: str | None = None,
        new_memory: str | None = None,
        new_image: str | None = None,
        *,
        _register_only: bool = False,
    ) -> str:
        """Update a user's container with new CPU/memory/image and force redeploy.

        Registers a new task definition revision with the updated values,
        then updates the ECS service to use it with forceNewDeployment.

        Args:
            user_id: Owner ID (user or org).
            new_cpu: New CPU value (e.g. "1024"). None = keep current.
            new_memory: New memory value (e.g. "2048"). None = keep current.
            new_image: New container image. None = keep current.

        Returns:
            The ARN of the new task definition revision.
        """
        container = await container_repo.get_by_owner_id(user_id)
        if not container:
            raise EcsManagerError(f"No container found for user {user_id}", user_id)

        access_point_id = container.get("access_point_id")
        if not access_point_id:
            raise EcsManagerError(f"No EFS access point for user {user_id}", user_id)

        service_name = self._service_name(user_id)

        try:
            # Always read containerDefinitions/env/command/mounts from the
            # CDK-managed base, NEVER from the user's prior per-user revision.
            # Reading from a prior per-user revision propagates any drift it
            # had (e.g., missing CLAWHUB_WORKDIR). Per-user state (EFS access
            # point, image, cpu/memory) is layered here.
            # The hook_token is per-user state stored on the containers row;
            # we must thread it through so the new task def keeps OPENCLAW_HOOK_TOKEN
            # set (otherwise resize would silently strip the env var).
            #
            # Self-heal legacy rows that pre-date hook-token provisioning:
            # build_openclaw_config_dict renders the hooks block unconditionally
            # with the "${OPENCLAW_HOOK_TOKEN}" placeholder, but
            # _build_register_kwargs_from_base only injects the env var when
            # hook_token is truthy. Letting None pass through would leave the
            # new revision without OPENCLAW_HOOK_TOKEN, so the container would
            # boot with the unsubstituted placeholder string and 401 every
            # webhook. Mint+persist here so the row, the env var, and the
            # rendered config stay consistent.
            hook_token = container.get("hook_token") or secrets.token_urlsafe(32)
            if not container.get("hook_token"):
                await container_repo.update_fields(
                    user_id,
                    {
                        "hook_token": hook_token,
                        "hook_token_rotated_at": utc_now_iso(),
                        "hook_token_provisioned_at": utc_now_iso(),
                    },
                )
            # Per-owner Bedrock role: a resize that re-registers from the CDK
            # base would otherwise revert the container to the shared
            # all-models task role (same self-heal class as hook_token above).
            bedrock_task_role_arn = container.get("bedrock_role_arn")
            reg_kwargs = await asyncio.to_thread(
                self._build_register_kwargs_from_base,
                access_point_id,
                new_image=new_image,
                new_cpu=new_cpu,
                new_memory=new_memory,
                hook_token=hook_token,
                task_role_arn=bedrock_task_role_arn,
            )

            # Carry the per-user env/secrets from the owner's current revision
            # onto the fresh base clone. Without this, every resize/Track-2
            # image apply strips AWS_BEARER_TOKEN_BEDROCK, the ACP harness env,
            # and BYOK secret refs — the container reboots with no inference
            # credentials until the next full restart rebuilds them. Base names
            # win, so the drift fence above still heals structural drift.
            # Deregistered prior revisions stay describable (INACTIVE), and a
            # failure here must fail the resize loudly — proceeding would
            # register a credential-stripped revision, the very bug this fixes.
            prior_task_def_arn = container.get("task_definition_arn")
            if prior_task_def_arn:
                prior_resp = await asyncio.to_thread(
                    self._ecs.describe_task_definition,
                    taskDefinition=prior_task_def_arn,
                )
                self._carry_per_user_container_state(
                    reg_kwargs["containerDefinitions"],
                    prior_resp["taskDefinition"].get("containerDefinitions", []),
                )
                # Rows created before the per-owner Bedrock role was persisted
                # can still be running a correctly scoped role on their live
                # task definition. Preserve that role as the fallback instead
                # of silently reverting the clone to the shared base role.
                if not bedrock_task_role_arn and prior_resp["taskDefinition"].get("taskRoleArn"):
                    reg_kwargs["taskRoleArn"] = prior_resp["taskDefinition"]["taskRoleArn"]

            reg_resp = await asyncio.to_thread(self._ecs.register_task_definition, **reg_kwargs)
            new_task_def_arn = reg_resp["taskDefinition"]["taskDefinitionArn"]
            logger.info("Registered resized task definition %s for user %s", new_task_def_arn, user_id)

            # The durable config-lock rollout persists this ARN on its fenced
            # pending-update row before it changes the ECS service. Keeping the
            # registration and deployment phases separate closes the crash
            # window where a retry could only guess which revision to resume.
            if _register_only:
                return new_task_def_arn

            # Persist the browser session (cookies + localStorage) BEFORE the
            # roll stops the old task — its /tmp Chrome profile is wiped on
            # replacement. Placed after the _register_only return so it only
            # fires on an actual roll. Best-effort, never raises. Lazy import:
            # services -> containers would cycle at module load.
            from core.services.browser_state import snapshot_browser_state

            await snapshot_browser_state(user_id)

            # Update service to use new task def + force redeploy
            await asyncio.to_thread(
                self._ecs.update_service,
                cluster=self._cluster,
                service=service_name,
                taskDefinition=new_task_def_arn,
                forceNewDeployment=True,
                deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
            )

            # Decide whether to flip status=provisioning + fire the poller.
            # resize never touches desiredCount -- if the service is currently
            # scaled to zero (e.g. a free-tier container stopped by the reaper),
            # update_service just registers the new task definition for later
            # use. No tasks will start as a result of resize alone, so flipping
            # status=provisioning would leave an orphan poller spinning forever
            # (rolloutState won't go to FAILED; the deployment "completes" with
            # zero tasks). The new task def will take effect on the next
            # start_user_service call, which has its own poller firing.
            desc_resp = await asyncio.to_thread(
                self._ecs.describe_services,
                cluster=self._cluster,
                services=[service_name],
            )
            services = desc_resp.get("services", [])
            current_desired = services[0].get("desiredCount", 0) if services else 0

            if current_desired > 0:
                # Update DB record
                await self._update_container(
                    user_id,
                    task_definition_arn=new_task_def_arn,
                    status="provisioning",
                )
                logger.info(
                    "Resized running container for user %s: cpu=%s memory=%s image=%s",
                    user_id,
                    new_cpu,
                    new_memory,
                    new_image,
                )
                # Fire the durable transition poller. resize sets
                # status=provisioning via update_fields above and forces a new
                # ECS deployment; without the poller, the row stays stuck at
                # provisioning until the next backend restart's startup
                # reconciler catches it.
                asyncio.create_task(self._await_running_transition(user_id))
            else:
                # Scaled to zero: only update task_definition_arn so the new
                # def takes effect on next start. Do NOT flip status or fire
                # the poller -- there are no tasks to become healthy.
                await self._update_container(
                    user_id,
                    task_definition_arn=new_task_def_arn,
                )
                logger.info(
                    "Resized stopped container for user %s: cpu=%s memory=%s image=%s (new task def applies on next start)",
                    user_id,
                    new_cpu,
                    new_memory,
                    new_image,
                )

            return new_task_def_arn

        except EcsManagerError:
            raise
        except Exception as e:
            raise EcsManagerError(f"Failed to resize container: {e}", user_id)

    async def prepare_config_lock_protocol_task_definition(self, user_id: str) -> str:
        """Register, but do not deploy, one owner's guarded base clone."""

        base_resp = await asyncio.to_thread(
            self._ecs.describe_task_definition,
            taskDefinition=self._base_task_def_family,
        )
        base_defs = (base_resp.get("taskDefinition") or {}).get("containerDefinitions") or []
        base_openclaw = next((definition for definition in base_defs if definition.get("name") == "openclaw"), None)
        base_command = " ".join((base_openclaw or {}).get("command") or [])
        required_fragments = (
            "${ISOL8_CONFIG_LOCK_PROTOCOL:-}",
            'protocol_path = lock_path + ".isol8-protocol"',
            "|| exit 1",
        )
        if not all(fragment in base_command for fragment in required_fragments):
            raise EcsManagerError(
                "CDK base task definition does not contain fail-fast config-lock recovery",
                user_id,
            )

        current_size = await self.current_container_size(user_id)
        if current_size is None:
            raise EcsManagerError(f"No registered task definition for user {user_id}", user_id)
        return await self.resize_user_container(
            user_id,
            new_cpu=current_size[0],
            new_memory=current_size[1],
            _register_only=True,
        )

    @serialize_owner_lifecycle
    async def rollout_config_lock_protocol(
        self,
        user_id: str,
        *,
        target_task_definition_arn: str | None = None,
        timeout_s: float = 900.0,
        poll_interval_s: float = 15.0,
    ) -> str:
        """Converge one owner onto a prepared protocol task definition.

        ContainerStack updates only the CDK base task definition; existing
        per-user clones do not inherit its startup command. If no target is
        supplied, this method prepares one from the latest base while retaining
        the owner's current size and per-user state. Durable fleet callers pass
        a target already persisted on their fenced update row.

        ECS is updated without ``forceNewDeployment``. The task-definition ARN
        is the desired-state idempotency key: replaying the same target after a
        backend crash does not manufacture a second replacement deployment.

        The ECS waiter is part of the contract: fleet callers may bound their
        concurrency around this method knowing a slot remains occupied until
        the stop-first replacement is stable (or the bounded waiter fails).
        A stopped service is already stable; its new revision runs recovery on
        the next ordinary scale-up.
        """

        if timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError("timeout_s and poll_interval_s must be positive")

        required_fragments = (
            "${ISOL8_CONFIG_LOCK_PROTOCOL:-}",
            'protocol_path = lock_path + ".isol8-protocol"',
            "|| exit 1",
        )

        async def _describe(task_definition: str) -> dict:
            response = await asyncio.to_thread(
                self._ecs.describe_task_definition,
                taskDefinition=task_definition,
            )
            return response.get("taskDefinition") or {}

        def _openclaw_definition(task_definition: dict) -> dict:
            definitions = task_definition.get("containerDefinitions") or []
            return next((definition for definition in definitions if definition.get("name") == "openclaw"), {})

        if target_task_definition_arn is None:
            target_task_definition_arn = await self.prepare_config_lock_protocol_task_definition(user_id)

        target = await _describe(target_task_definition_arn)
        target_openclaw = _openclaw_definition(target)
        target_command = " ".join(target_openclaw.get("command") or [])
        target_env = {entry.get("name"): entry.get("value") for entry in target_openclaw.get("environment") or []}
        if (
            not all(fragment in target_command for fragment in required_fragments)
            or target_env.get(_CONFIG_LOCK_PROTOCOL_ENV) != _CONFIG_LOCK_PROTOCOL_VERSION
        ):
            raise EcsManagerError(
                "Target task definition does not contain the guarded config-lock protocol",
                user_id,
            )

        # A changed taskDefinition starts a deployment by itself. Omitting
        # forceNewDeployment makes replaying this exact target convergent.
        await asyncio.to_thread(
            self._ecs.update_service,
            cluster=self._cluster,
            service=self._service_name(user_id),
            taskDefinition=target_task_definition_arn,
            deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
        )

        pre_wait_response = await asyncio.to_thread(
            self._ecs.describe_services,
            cluster=self._cluster,
            services=[self._service_name(user_id)],
        )
        pre_wait_services = pre_wait_response.get("services") or []
        desired_count = pre_wait_services[0].get("desiredCount", 0) if pre_wait_services else 0
        if desired_count > 0:
            await self._update_container(
                user_id,
                task_definition_arn=target_task_definition_arn,
                status="provisioning",
            )
            asyncio.create_task(self._await_running_transition(user_id))
        else:
            await self._update_container(user_id, task_definition_arn=target_task_definition_arn)

        waiter = self._ecs.get_waiter("services_stable")
        max_attempts = max(1, math.ceil(timeout_s / poll_interval_s))
        await asyncio.to_thread(
            waiter.wait,
            cluster=self._cluster,
            services=[self._service_name(user_id)],
            WaiterConfig={"Delay": max(1, math.ceil(poll_interval_s)), "MaxAttempts": max_attempts},
        )
        service_resp = await asyncio.to_thread(
            self._ecs.describe_services,
            cluster=self._cluster,
            services=[self._service_name(user_id)],
        )
        services = service_resp.get("services") or []
        service = services[0] if services else {}
        primary = next(
            (deployment for deployment in service.get("deployments") or [] if deployment.get("status") == "PRIMARY"),
            {},
        )
        actual_task_def_arn = primary.get("taskDefinition") or service.get("taskDefinition")
        if primary.get("rolloutState") == "FAILED":
            raise EcsManagerError("Config-lock protocol rollout failed its ECS deployment", user_id)
        if actual_task_def_arn != target_task_definition_arn:
            if actual_task_def_arn:
                # A circuit-breaker rollback can satisfy services_stable on the
                # prior revision. Keep the DDB pointer honest before surfacing
                # failure so a retry reads the task definition that will run.
                await self._update_container(user_id, task_definition_arn=actual_task_def_arn)
            raise EcsManagerError(
                "Config-lock protocol rollout stabilized on a different task definition",
                user_id,
            )
        # Deliberately no direct status="running" write here: the
        # _await_running_transition poller spawned above owns the
        # provisioning -> running transition and only flips after its
        # gateway reachability check. A direct write would change the DDB
        # status out of "provisioning" and make that poller exit silently
        # before the check, recording an ECS-stable but gateway-unreachable
        # container as running.
        return target_task_definition_arn

    def _tasks_fully_stopped_blocking(self, service_name: str) -> bool:
        """Blocking: whether every task of the service has reached lastStatus
        STOPPED — the only signal equivalent to "the container has exited".

        Service-level counts are NOT that signal: ``runningCount`` drops the
        moment a task leaves RUNNING, but the task then sits in STOPPING for
        up to its 120s stopTimeout while the entrypoint (litestream) performs
        its final replica sync — and a DELETED service describes as absent
        while its last task is still stopping. Either window lets a caller
        that trusted the counts race writes that have not landed yet (the
        replica-prefix delete, the OAuth seed harvest, the single-writer
        reprovision).

        A per-user service runs at most one task and stopped-task records are
        retained ~1h, so one unpaginated page per desiredStatus is enough.
        ServiceNotFoundException means ECS has garbage-collected the service
        record itself — long after its tasks stopped — and reads as drained.
        (Residual risk: if ECS also raises it for a fresh INACTIVE service,
        a task could still be STOPPING; the harvest's freshness check is the
        backstop for that narrow window.) ``describe_tasks`` failures with
        reason MISSING are expired stopped records — not running — so only
        the returned tasks are checked.
        """
        arns: list[str] = []
        for desired in ("RUNNING", "STOPPED"):
            try:
                resp = self._ecs.list_tasks(
                    cluster=self._cluster,
                    serviceName=service_name,
                    desiredStatus=desired,
                )
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "ServiceNotFoundException":
                    return True
                raise
            arns.extend(resp.get("taskArns", []))
        if not arns:
            return True
        for i in range(0, len(arns), 100):
            desc = self._ecs.describe_tasks(cluster=self._cluster, tasks=arns[i : i + 100])
            if any(t.get("lastStatus") != "STOPPED" for t in desc.get("tasks", [])):
                return False
        return True

    async def _wait_for_service_drained(self, user_id: str, timeout_s: float = 90.0) -> bool:
        """Poll until every task of the user's service has actually STOPPED.

        Returns True once drained, or False on timeout (the caller proceeds
        anyway — a stuck poll is not worth wedging an admin reprovision).
        Verifies task ``lastStatus`` rather than service running/pending
        counts — see ``_tasks_fully_stopped_blocking`` for why the counts lie
        during the STOPPING window. Enforces the single-writer invariant on
        the reprovision path and the write-ordering the wipe flows depend on
        (replica delete + OAuth seed harvest must postdate litestream's
        final sync, which only lands when the container exits).
        """
        service_name = self._service_name(user_id)
        interval_s = 3.0
        attempts = max(1, int(timeout_s / interval_s))
        for _ in range(attempts):
            try:
                if await asyncio.to_thread(self._tasks_fully_stopped_blocking, service_name):
                    return True
            except Exception:
                logger.warning("task-termination check failed while draining %s; will retry", service_name)
            await asyncio.sleep(interval_s)
        logger.warning(
            "Service %s did not drain within %.0fs; proceeding with reprovision",
            service_name,
            timeout_s,
        )
        return False

    async def wait_until_drained(self, user_id: str, timeout_s: float = 90.0) -> bool:
        """Block until the user's service has no running/pending tasks.

        Public wrapper for cross-module callers (wipe/reprovision flows that
        must not race a still-running container's writes). Returns False on
        timeout; callers proceed anyway.
        """
        return await self._wait_for_service_drained(user_id, timeout_s=timeout_s)

    @serialize_owner_lifecycle
    async def reprovision_for_user(self, user_id: str) -> dict:
        """Full reprovision: re-register the task def from the current CDK
        base (picks up the latest pinned image) AND rewrite the EFS
        openclaw.json from the owner's current provider settings, then roll
        the service onto the new revision. Used by the admin dashboard's
        container-reprovision action.

        This replaces the old stop+start, which only scaled the existing
        service — it left the container on its stale task-def AND its stale
        EFS config, so it could neither pick up a new image nor apply a
        backend config-builder change. Routing through
        ``provision_user_container`` (via ``dispatch_provision``) is the
        only path that re-runs ``write_user_configs`` + the chatgpt OAuth
        pre-stage, so an admin reprovision now genuinely re-applies config.

        Dispatched as a background task: a full reprovision waits on the
        image pull + running transition (minutes), which would blow the
        admin request's HTTP timeout. The container row flips to
        provisioning -> running on the usual status-poll path; admins watch
        the Container card. Preserves EFS data, gateway token, and the EFS
        access point (provision_user_container reuses the existing row).

        Scale to 0 FIRST: provision_user_container only re-registers the task
        def + rewrites the EFS config on its scale-FROM-zero branch. Its
        already-running branch just force-redeploys the SAME (stale)
        task-def + config — so without dropping to 0 first, an admin
        reprovision on a live container would apply neither an image bump nor
        a config-builder change. The new task is only started once the old one
        has fully drained (single EFS SQLite writer — see the drain-wait
        below); EFS, gateway token, and access point are all preserved.
        """
        # Drop to desiredCount=0 so the dispatched provision takes the
        # scale-from-zero branch (re-register + write_user_configs), not the
        # running force-redeploy branch.
        await self.stop_user_service(user_id, reason="reprovision")

        return await self.drain_then_dispatch_provision(user_id)

    async def drain_then_dispatch_provision(self, user_id: str) -> dict:
        """Drain-wait + provision dispatch for a service that has ALREADY
        been stopped by the caller. Deliberately does NOT re-issue the stop:
        a status-poll wake can legitimately claim the exposed "stopped" row
        before this runs, flipping it to "provisioning" — a second
        stop_user_service here would clobber that back to "stopped", making
        the row claimable AGAIN and letting this method's own dispatch start a
        second executor against the poll's in-flight one. Still true now that
        ``update_status`` is a scoped update_item: it is conditioned on the row
        EXISTING, not on the lease, so it writes ``status`` straight over a
        freshly claimed provisioning row. Skipping the stop keeps the
        single-flight guarantee: if the poll's claim won, the dispatch below
        simply loses the lease and stands down.

        Block until the old task is fully stopped before provisioning the
        new one. stop_user_service only issues desiredCount=0 and returns at
        once; ECS then takes up to the task's stop-timeout to actually kill
        it. The per-owner state lives in WAL-mode SQLite on EFS, which
        corrupts if the draining old task and the new task hold it open
        together (the same reason SINGLE_WRITER_DEPLOYMENT_CONFIG pins
        max=100). Waiting here guarantees a single writer across the
        reprovision.
        """
        from core.services.provision_dispatch import dispatch_provision

        await self._wait_for_service_drained(user_id)

        # clerk_user_id is unused by the reprovision path: provision reads
        # provider_choice off the billing row by owner_id, and chatgpt OAuth
        # resolves the connector via resolve_token_owner(owner_id). Pass
        # user_id for both to satisfy the signature.
        asyncio.create_task(dispatch_provision(user_id))
        return {"status": "reprovisioning"}

    async def reapply_standard_sizing(self, user_id: str) -> dict:
        """Roll the user's task definition back to the owner's canonical
        provision size. Admin recovery action for containers wedged on a
        stale task def.

        Sizes by the owner's plan (``_size_for_owner``) rather than a flat
        hardcode: for a standard owner that IS the 1 vCPU / 4 GB box, but an
        Ultra owner must land on their 2 vCPU / 8 GB box — re-applying the
        standard size here would silently undo the upgrade webhook's resize,
        the exact revert the plan-derived provision sizing exists to prevent.
        """
        new_cpu, new_memory = await self._size_for_owner(user_id)
        await self.resize_user_container(
            user_id,
            new_cpu=new_cpu,
            new_memory=new_memory,
        )
        return {
            "status": "resized",
            "cpu": new_cpu,
            "memory": new_memory,
        }

    @serialize_owner_lifecycle
    async def delete_user_service(self, user_id: str) -> dict | None:
        """Remove a user's ECS service and per-user resources entirely.

        1. Scale to 0 and delete ECS service
        2. Deregister per-user task definition (if present)
        3. Delete per-user EFS access point (if present)
        4. Delete Container DB record

        Args:
            user_id: Clerk user ID.

        Returns:
            The containers row as it was at the instant it was deleted, or
            None if there was none. Callers tearing an account down use it to
            reach state the row is the only handle on (the bedrock-gate token
            hashes) — see ``container_repo.delete``. It is the DELETE's own
            ``ALL_OLD`` image, not the read above it, because the read is
            eventually consistent and a row written after it would otherwise
            be both undeleted and unreadable.

        Raises:
            EcsManagerError: If the ECS API calls fail.
        """
        service_name = self._service_name(user_id)

        try:
            # Scale to 0 before deleting
            self._ecs.update_service(
                cluster=self._cluster,
                service=service_name,
                desiredCount=0,
            )
            self._ecs.delete_service(
                cluster=self._cluster,
                service=service_name,
                force=True,
            )
        except ClientError as e:
            # Service-not-found is idempotent success — the e2e teardown can
            # legitimately retry against a state where the service was already
            # deleted (e.g. previous partial run). Treating "doesn't exist" as
            # an error breaks retry semantics. All other ClientErrors propagate.
            #
            # NB: we do NOT early-return here — the per-user resources below
            # (task-def revision, EFS access point, DDB container row) may
            # still exist even after the service is gone, and a partial
            # cleanup that left the access point orphaned can only be
            # recovered if subsequent retries fall through to this block.
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ServiceNotFoundException", "ServiceNotActiveException"):
                logger.info(
                    "delete_user_service: service %s for user %s already gone — continuing per-user cleanup",
                    service_name,
                    user_id,
                )
            else:
                logger.error(
                    "Failed to delete ECS service %s for user %s: %s",
                    service_name,
                    user_id,
                    e,
                )
                raise EcsManagerError(f"Failed to delete ECS service: {e}", user_id)
        except Exception as e:
            logger.error(
                "Failed to delete ECS service %s for user %s: %s",
                service_name,
                user_id,
                e,
            )
            raise EcsManagerError(f"Failed to delete ECS service: {e}", user_id)

        # Clean up per-user resources and delete container record from DB
        container = await container_repo.get_by_owner_id(user_id)
        if container:
            # Deregister per-user task definition
            if container.get("task_definition_arn"):
                self._deregister_task_definition(container["task_definition_arn"])

            # Delete per-user EFS access point
            if container.get("access_point_id"):
                self._delete_access_point(container["access_point_id"])

        # UNCONDITIONAL, and after the per-user cleanup above. The read is
        # eventually consistent, so a row created (or re-created by a racing
        # provision) since it was written can come back empty here — gating the
        # delete on it leaves that row behind, along with everything only it
        # names. DeleteItem is a no-op when there is genuinely nothing, and its
        # ALL_OLD image is the authoritative answer to "what did this row say
        # when it died", which is what the account wipe sweeps.
        deleted = await container_repo.delete(user_id)

        # THE SEAM. Every teardown in the system reaches AWS through this
        # function — entitlement cancellation, the admin account-wipe, DELETE
        # /debug/provision, DELETE /debug/user-data — and the row just deleted
        # was the only handle on the owner's Bedrock footprint (gate-token
        # hashes, AIP arns). Reaping HERE, off that row's ALL_OLD image, is
        # what makes the cleanup structural: no caller can forget it, and none
        # needs its own copy. Deliberately before the channel-links sweep
        # below, which can throw and would otherwise skip the reap entirely.
        #
        # Raises on a partial reap rather than reporting success — the ECS
        # teardown above has already happened, so a raise honestly means
        # "teardown incomplete, retry me", and every step is idempotent.
        # scale-to-zero/idle parking goes through stop_user_service, NOT here,
        # so a sleeping container never loses its Bedrock identity.
        from core.billing.bedrock_aip import reap_owner_bedrock_resources

        reaped = await reap_owner_bedrock_resources(user_id, deleted)
        logger.info("Bedrock reap for user %s: %s", user_id, reaped)

        # Sweep channel_links rows for this owner
        try:
            from core.repositories import channel_link_repo

            link_count = await channel_link_repo.sweep_by_owner(user_id)
            if link_count:
                logger.info(
                    "Swept %d channel_link rows for deleted container (owner=%s)",
                    link_count,
                    user_id,
                )
        except Exception:
            # Non-fatal — the container is already gone, orphan rows are
            # cheap to keep. Log and continue.
            logger.exception(
                "Failed to sweep channel_link rows for owner %s after container delete",
                user_id,
            )

        logger.info("Deleted ECS service %s for user %s", service_name, user_id)
        return deleted

    # ------------------------------------------------------------------
    # Discovery and health
    # ------------------------------------------------------------------

    def _service_registries(self) -> dict:
        """``serviceRegistries`` kwarg for update_service, or {} when unset.

        Empty dict rather than an empty list: passing ``serviceRegistries=[]``
        DEREGISTERS the service from Cloud Map, which would make every provision
        silently strand the container -- ``discover_ip`` would find nothing and
        the box would look unreachable while running perfectly.
        """
        if not self._cloud_map_service_arn:
            return {}
        return {"serviceRegistries": [{"registryArn": self._cloud_map_service_arn}]}

    def _refresh_stale_log_config(self, task_def_arn: str) -> str | None:
        """Re-register *task_def_arn* onto the CDK base's log configuration.

        Per-user task definitions are IMMUTABLE revisions that pin the log group
        by name, and only a full provision re-derives them from the base. A wake
        reuses whatever revision the service already has -- so renaming the log
        group in CDK leaves every parked owner pinned to a group that no longer
        exists, and their next start dies with

            ResourceInitializationError: failed to validate logger args

        which is not a degraded container but one that cannot start at all, and
        no amount of retrying fixes it.

        Refreshing ONLY logConfiguration, rather than calling
        _register_task_definition, is deliberate: that path needs per-user
        secrets, hook token, task role and sizing that a wake does not have, and
        reconstructing them here would duplicate the restart branch. Cloning the
        owner's current revision preserves all of that untouched.

        Returns the new task-def ARN, or None when nothing needed changing (the
        common case, so the extra describe is the only cost on a healthy wake).
        """
        try:
            base = self._ecs.describe_task_definition(taskDefinition=self._base_task_def_family)["taskDefinition"]
            current = self._ecs.describe_task_definition(taskDefinition=task_def_arn)["taskDefinition"]
        except Exception as e:  # noqa: BLE001 — never block a start on this check
            logger.warning("could not compare task def %s against base: %s", task_def_arn, e)
            return None

        base_log = (base.get("containerDefinitions") or [{}])[0].get("logConfiguration")
        if not base_log:
            return None
        cds = current.get("containerDefinitions") or []
        if all(cd.get("logConfiguration") == base_log for cd in cds):
            return None

        stale = {(cd.get("logConfiguration") or {}).get("options", {}).get("awslogs-group") for cd in cds}
        logger.warning(
            "task def %s pins log group(s) %s but the base now uses %s — re-registering",
            task_def_arn.rsplit("/", 1)[-1],
            sorted(g for g in stale if g),
            (base_log.get("options") or {}).get("awslogs-group"),
        )
        for cd in cds:
            cd["logConfiguration"] = base_log

        keys = (
            "family",
            "taskRoleArn",
            "executionRoleArn",
            "networkMode",
            "volumes",
            "requiresCompatibilities",
            "cpu",
            "memory",
            "runtimePlatform",
        )
        kwargs = {k: current[k] for k in keys if current.get(k)}
        kwargs["containerDefinitions"] = cds
        return self._ecs.register_task_definition(**kwargs)["taskDefinition"]["taskDefinitionArn"]

    def discover_ip(self, service_name: str) -> str | None:
        """Discover a task's private IP via AWS Cloud Map.

        Every per-user OpenClaw ECS service registers its task into one shared
        Cloud Map service (see apps/infra/lib/stacks/container-stack.ts), each
        instance carrying an ``ECS_SERVICE_NAME`` attribute. We scope the
        lookup to a single tenant by filtering on that attribute, so the IP
        reflects the *current* task and can never be stale across task
        replacements — unlike a cached address.

        ``HealthStatus="HEALTHY_OR_ELSE_ALL"``: the shared Cloud Map service has
        no HealthCheckCustomConfig, so ECS-registered instances report health
        ``UNKNOWN``. This mode returns the current task regardless of that, and
        automatically prefers healthy tasks if health-checking is ever enabled.

        Args:
            service_name: The per-user ECS service name (the ``ECS_SERVICE_NAME``
                attribute value to match).

        Returns:
            The current task's private IPv4 address, or None.
        """
        if not self._cloud_map_namespace_name or not self._cloud_map_service_name:
            logger.error(
                "Cloud Map names not configured (namespace=%r service=%r); cannot discover %s",
                self._cloud_map_namespace_name,
                self._cloud_map_service_name,
                service_name,
            )
            return None
        try:
            resp = self._servicediscovery.discover_instances(
                NamespaceName=self._cloud_map_namespace_name,
                ServiceName=self._cloud_map_service_name,
                QueryParameters={"ECS_SERVICE_NAME": service_name},
                HealthStatus="HEALTHY_OR_ELSE_ALL",
            )
        except Exception as e:
            logger.error("Cloud Map discovery failed for %s: %s", service_name, e)
            return None
        for inst in resp.get("Instances", []):
            attrs = inst.get("Attributes", {})
            if attrs.get("ECS_SERVICE_NAME") == service_name and attrs.get("AWS_INSTANCE_IPV4"):
                return attrs["AWS_INSTANCE_IPV4"]
        return None

    def is_healthy(self, ip: str) -> bool:
        """Check if a gateway at the given IP is responding.

        Opens a TCP connection to the gateway port. If the connection
        succeeds, the gateway is listening and healthy.

        Args:
            ip: The task's private IPv4 address.

        Returns:
            True if healthy, False otherwise.
        """
        try:
            with socket.create_connection((ip, GATEWAY_PORT), timeout=5):
                return True
        except Exception:
            return False

    async def resolve_running_container(self, user_id: str) -> tuple[dict | None, str | None]:
        """Look up a user's container and discover its task IP.

        Accepts containers in "provisioning" or "running" status.

        READ-ONLY on the row's status/substatus (L2 fix, spec §7): this used
        to opportunistically write status="running" the moment a
        "provisioning" row's task became reachable — a second, unfenced
        writer of the exact transition ``_await_running_transition`` also
        owns, and the dead-reckoning branch the SFN reshape needed to close
        (an in-flight fenced run's row could be raced by a GET-triggered
        write with no claim/generation check at all). SFN (via
        ``wait_for_running``, foreground-awaited by
        ``scripts.provision_runner``) is now the SOLE writer of the
        provisioning -> running transition; this method only ever READS
        current state + probes reachability. A container whose task is
        already healthy but whose row hasn't caught up yet simply reports
        ``status="provisioning"`` for one more poll tick until the runner's
        own write lands — a minor, acceptable latency, not a correctness
        gap (the runner is the same process watching the same task).

        Args:
            user_id: Clerk user ID.

        Returns:
            Tuple of (container_dict, task_ip) or (None, None) if no active container.
        """
        container = await container_repo.get_by_owner_id(user_id)
        if container and runtime_transport.host_for(container) == "agentcore":
            # No task IP to discover: AWS wakes the microVM on invoke. The sentinel is truthy
            # so no caller mistakes this workspace for a stopped box and starts its old ECS
            # service next to the microVM (two gateways on one access point corrupt the
            # litestream replica set silently — reproduced 2026-09-16).
            return container, runtime_transport.AGENTCORE_ADDRESS
        if not container or container.get("status") not in ("provisioning", "running"):
            return None, None

        # A `provisioning` row EXISTS BEFORE its ECS service does. The
        # provisioning claim's first act is the atomic conditional write that
        # creates the row as status="provisioning" (see
        # container_repo.claim_provisioning) -- `service_name` is only stamped
        # once the service has actually been created. Subscripting it here
        # raised KeyError('service_name') for the whole window in between,
        # which surfaced as `RPC agents.list failed ... error_class=KeyError`
        # every ~3s and a 500 on `GET /container/status?wake=1` -- observed on
        # prod 2026-07-29 01:20:52-01:21:09 during a fresh provision. No
        # service means nothing to discover, which is exactly the
        # "not reachable yet" case the caller already handles.
        service_name = container.get("service_name")
        if not service_name:
            return container, None

        # discover_ip is a SYNCHRONOUS boto3 Cloud Map call; resolve_running_container
        # is on the /container/status + chat send hot path. Offload so a slow
        # Cloud Map response can't block the event loop for every other request.
        # (GW-2a.)
        ip = await asyncio.to_thread(self.discover_ip, service_name)
        if not ip:
            # DDB row says provisioning/running but ECS has no running task
            # for the service. Either the task is still pulling the image
            # (provisioning) or it died and is being replaced (running →
            # zombie row). Logged so we can correlate against the
            # /lightbulb/<env>/ecs-task-events log group to see whether the
            # task was stopped, by whom, and why.
            logger.warning(
                "resolve_running_container ip=None user=%s service=%s ddb_status=%s ddb_substatus=%s last_handshake=%s",
                user_id,
                container.get("service_name"),
                container.get("status"),
                container.get("substatus"),
                container.get("last_gateway_handshake_at") or "never",
            )
            return container, None
        # Stale-row detection: DDB says running but TCP probe fails. This
        # is the wedge-class signature — kernel listener bound but gateway
        # process is in NFS deadlock. Log loudly so the next iteration of
        # this bug shows up immediately in CloudWatch Insights:
        #   filter ip_unhealthy=true and ddb_status="running"
        # is_healthy opens a blocking TCP socket (5s timeout) — offload it too so
        # a wedged container's dead listener can't stall the loop. (GW-2a.)
        if container.get("status") == "running" and not await asyncio.to_thread(self.is_healthy, ip):
            logger.warning(
                "resolve_running_container ip_unhealthy=true user=%s service=%s ip=%s last_handshake=%s",
                user_id,
                container.get("service_name"),
                ip,
                container.get("last_gateway_handshake_at") or "never",
            )

        # No write here — see the docstring. SFN's wait_for_running (or, for
        # the non-provisioning-flow callers, _await_running_transition's
        # unfenced path) is the sole writer of provisioning -> running.

        return container, ip

    async def get_service_status(self, user_id: str) -> dict | None:
        """Get the Container record for a user.

        Args:
            user_id: Clerk user ID.

        Returns:
            The container dict, or None if not found.
        """
        return await container_repo.get_by_owner_id(user_id)

    # ------------------------------------------------------------------
    # Full provisioning flow
    # ------------------------------------------------------------------

    def _service_exists(self, service_name: str, user_id: str = "") -> dict | None:
        """Check if an ECS service exists and return its description, or None
        iff it genuinely does not exist (never created, or INACTIVE after a
        prior delete).

        Raises ``EcsManagerError`` on a ``describe_services`` API failure
        (throttle, transient network, auth blip) — that is NOT the same as
        "no service". The caller (``_provision_user_container_inner``) uses a
        ``None`` return to route into the full-provision branch
        (``create_service``); silently swallowing a describe_services error
        and returning ``None`` used to risk attempting to CREATE a second
        service over one that is actually ACTIVE — an ECS "already exists"
        error, misclassified as a generic retryable failure, papering over
        the real state instead of surfacing what actually went wrong (L2 fix,
        spec §7 / ecs_manager.py:1751-1753 in the original report). Letting
        this propagate routes it through the normal
        ``container_repo.classify_provision_error`` path instead, where an
        ``EcsManagerError`` correctly defaults to retryable=True (a
        describe_services blip IS transient).
        """
        try:
            resp = self._ecs.describe_services(
                cluster=self._cluster,
                services=[service_name],
            )
        except Exception as e:
            raise EcsManagerError(f"describe_services failed for {service_name}: {e}", user_id) from e
        services = resp.get("services", [])
        if services and services[0].get("status") != "INACTIVE":
            return services[0]
        return None

    async def _build_byo_secrets_for_user(
        self,
        user_id: str,
        byo_provider: str,
    ) -> list[dict]:
        """Build the ECS task ``secrets:`` block for a BYOK provisioning.

        Resolves the per-user LLM-key secret ARN from Secrets Manager (via
        ``key_service.get_secret_arn``, keyed deterministically on
        ``{user_id, byo_provider}``) and turns it into a single ``secrets:``
        entry that the per-user task definition references.
        ``service-stack.ts`` already grants the backend
        ``secretsmanager:GetSecretValue`` for ``lightbulb/{env}/user-keys/*``, so
        the per-user ARN is reachable from the task IAM role.
        """
        if byo_provider not in {"openai", "anthropic"}:
            raise EcsManagerError(
                f"byo_provider must be 'openai' or 'anthropic' for byo_key, got {byo_provider!r}",
                user_id,
            )
        from core.services.key_service import get_secret_arn

        secret_arn = await get_secret_arn(user_id, byo_provider)
        if not secret_arn:
            raise EcsManagerError(
                f"No saved {byo_provider} key for user {user_id} - caller should "
                "block provisioning until the user adds their key",
                user_id,
            )
        env_var_name = "OPENAI_API_KEY" if byo_provider == "openai" else "ANTHROPIC_API_KEY"
        return [{"name": env_var_name, "valueFrom": secret_arn}]

    async def _build_slack_secrets_for_user(self, owner_id: str) -> list[dict]:
        """ECS ``secrets:`` entries for every ACTIVE Slack install of ``owner_id``
        — one ``SLACK_BOT_TOKEN_{TEAM}`` per workspace, so a reprovision or
        image roll re-registers them and the SecretRef in openclaw.json keeps
        resolving. Best-effort: ``[]`` on any failure; provisioning must never
        break because the Slack table hiccupped (the owner re-installs)."""
        try:
            from core.repositories import slack_installs_repo

            rows = await slack_installs_repo.list_for_owner(owner_id)
            out: list[dict] = []
            for row in rows:
                if row.get("status") != "active" or not row.get("token_secret_name"):
                    continue
                arn = await _secret_arn_for_name(row["token_secret_name"])
                if arn:
                    out.append({"name": slack_env_var_name(row["team_id"]), "valueFrom": slack_token_value_from(arn)})
            return out
        except Exception:  # noqa: BLE001 — best-effort; never fail provision over this.
            logger.exception("building slack secrets failed for %s; continuing", owner_id)
            return []

    async def _pre_stage_oauth_for_user(
        self,
        user_id: str,
        *,
        token_owner_id: str | None = None,
    ) -> None:
        """Pre-stage the container-global ``credentials/oauth.json`` for a
        chatgpt_oauth owner.

        Fetches the decrypted ChatGPT tokens persisted by ``oauth_service``
        and writes ``<EFS>/users/{user_id}/credentials/oauth.json`` (a single
        container-global file, NOT a per-agent fan-out) BEFORE the ECS service
        is created, so OpenClaw imports it into every agent's auth store on
        first boot. See workspace.pre_stage_auth_profile_store.

        ``user_id`` is the CONTAINER OWNER (org_id for orgs) — it keys the
        EFS path the container mounts. ``token_owner_id`` is the Clerk user
        whose OAuth row holds the tokens (the org's workspace owner);
        defaults to ``user_id``, which is correct for personal owners.
        Raises if no active OAuth row exists — caller should gate
        provisioning on a completed device-code flow.
        """
        from core.services.oauth_service import get_decrypted_tokens

        token_owner = token_owner_id or user_id
        # ``user_id`` is the CONTAINER OWNER — pass it as ``bound_to`` so we only
        # stage a row the connector claimed for THIS workspace. A row bound to a
        # different owner reads as missing and raises below, rather than staging
        # one ChatGPT account into a second container.
        tokens = await get_decrypted_tokens(user_id=token_owner, bound_to=user_id)
        if not tokens:
            raise EcsManagerError(
                f"No ChatGPT OAuth tokens for connector {token_owner} (owner {user_id}) - "
                "caller should complete OAuth before provisioning",
                user_id,
            )
        from core.containers.workspace import pre_stage_auth_profile_store

        # Stage the container-global oauth.json seed. We do NOT clear the
        # default agent's auth row here: the container's per-agent SQLite auth
        # store is the source of truth for the LIVE (rotating) token, and this
        # DDB-sourced seed is a stale snapshot the instant the container's first
        # refresh rotates it. The seed stays viable across wipe-and-reprovision
        # because the flows that destroy the live store's replica harvest the
        # rotated chain back into the DDB row first (oauth_service.
        # harvest_rotated_oauth_seed) — so this stage always writes the
        # freshest credential the backend has seen. The container's
        # store-row-absent import gate already consumes this seed ONLY for a
        # container with no credential yet (fresh provision) and ignores it on
        # a populated store — so re-staging on a reprovision/recover/
        # config-rewrite never clobbers a live token.
        # Force-reimport (clear_default_agent_auth_profile_row) happens ONLY on a
        # genuine fresh-token event: reconnect (routers/oauth.py /poll) and
        # provider switch (_hot_swap_provider). See
        # docs/superpowers/specs/2026-06-24-oauth-reprovision-stale-token-design.md.
        await pre_stage_auth_profile_store(user_id=user_id, oauth_tokens=tokens)

        # Also stage the OpenCode harness credential when a dedicated harness
        # grant exists. Best-effort and write-if-missing: the harness is
        # optional (absence = owner never connected it), and OpenCode owns the
        # live rotating file once written — overwriting it with the stale DDB
        # seed would kill the chain (see pre_stage_opencode_auth).
        from core.containers.workspace import pre_stage_opencode_auth
        from core.services.oauth_service import PURPOSE_HARNESS

        try:
            harness_tokens = await get_decrypted_tokens(user_id=token_owner, bound_to=user_id, purpose=PURPOSE_HARNESS)
            if harness_tokens:
                await pre_stage_opencode_auth(user_id=user_id, oauth_tokens=harness_tokens, overwrite=False)
        except Exception:
            logger.exception("opencode harness auth staging failed for %s", user_id)

    @serialize_owner_lifecycle
    async def provision_user_container(
        self,
        user_id: str,
        *,
        provider_choice: str,
        byo_provider: str | None = None,
        owner_type: str = "personal",
        claim_id: str | None = None,
        generation: str | None = None,
        attempt_token: str | None = None,
    ) -> str:
        """Fenced entry point for every container provision.

        ``claim_id`` is the provision lease's fencing token
        (container_repo.try_claim_provision). The SOLE production caller —
        ``scripts.provision_runner`` (the SFN executor's one-shot ECS task) —
        always claims before calling and passes its token here. A DIRECT call
        with no token is, by definition, a DELIBERATE unconditional takeover
        (used by direct/debug/test callers only; the three synchronous doors
        that used to call this directly — /provision, /retry, /recover — now
        route through ``core.services.provision_dispatch.dispatch_provision``
        instead, spec §7) — it takes the lease over unconditionally, which
        fences any in-flight poll provision out at that run's next checkpoint.
        So a deliberate takeover is never blocked, and two provisions can
        never interleave their writes: at most one holds the lease, and the
        loser stops before its next side effect (ProvisionSuperseded).

        ``generation`` / ``attempt_token`` are optional additional fences
        threaded to every checkpoint (see ``container_repo.update_container_leased``
        / ``claim_attempt`` docstrings) — the runner passes both; other
        callers default to None (no extra condition, unchanged behavior).

        POLLER SUPPRESSION is keyed on whether the CALLER supplied
        ``claim_id`` — not merely on whether it ends up non-None. A takeover
        (``claim_id`` argument None) mints its own claim internally but has
        NO runner awaiting ``wait_for_running`` afterward, so it must still
        get the detached poller `_provision_user_container_inner` /
        `create_user_service` / `start_user_service` would otherwise fire;
        only a caller that supplied its own ``claim_id`` (in production,
        exclusively ``scripts.provision_runner``) is trusted to own the
        foreground wait itself. Recording this BEFORE the takeover
        reassignment below is what makes the distinction possible.

        On any failure the lease is released to ``error`` (token- and
        status-conditioned, see release_provision_to_error), which the status
        poll's existing auto-retry re-claims on the next ~3s tick — a failed
        provision self-heals with no operator.
        """
        # Lazy module lookup for the LEASE primitives (only): several tests
        # wholesale-replace this module's `container_repo` binding with a
        # MagicMock to stub row I/O; the lease has its own moto-backed suite
        # and is stubbed at the source module instead.
        from core.repositories import container_repo as _lease_repo

        caller_supplied_claim = claim_id is not None
        if claim_id is None:
            claim_id = await _lease_repo.takeover_provision(user_id)
            logger.info("Provision lease takeover for owner %s (deliberate reprovision)", user_id)
        try:
            return await self._provision_user_container_inner(
                user_id,
                provider_choice=provider_choice,
                byo_provider=byo_provider,
                owner_type=owner_type,
                claim_id=claim_id,
                suppress_transition_poller=caller_supplied_claim,
                generation=generation,
                attempt_token=attempt_token,
            )
        except ProvisionSuperseded:
            # A newer run owns the row now; write nothing over its state.
            put_metric("container.provision", dimensions={"status": "superseded"})
            raise
        except Exception as exc:
            # This is the release that ACTUALLY lands on the common inner-failure
            # path: it fires before dispatch_provision's own (which then no-ops
            # on the already-flipped status), and it also covers the deliberate
            # doors (conversion / recover / admin) that never reach
            # dispatch_provision. So the failure CAUSE must be classified and
            # recorded HERE too, or a deterministic 4xx would loop as retryable.
            last_error, retryable = _lease_repo.classify_provision_error(exc)
            try:
                await _lease_repo.release_provision_to_error(
                    user_id,
                    claim_id,
                    last_error=last_error,
                    retryable=retryable,
                    generation=generation,
                    attempt_token=attempt_token,
                )
            except Exception:  # noqa: BLE001 — release is best-effort
                logger.exception("Failed to release provision lease for owner %s", user_id)
            raise

    async def has_live_service(self, user_id: str) -> bool:
        """True when the owner has an ACTIVE ECS service with desiredCount > 0.

        The guard for the status poll's stale-lease recovery: a provision that
        already reached start_user_service stops refreshing its lease when it
        RETURNS, while the container boots for up to ~12 minutes — so a stale
        lease alone does not mean the provision is dead. If ECS shows a live
        service, the right move is to keep waiting for the running transition,
        not to re-provision (which would force-restart a healthy boot)."""
        container = await container_repo.get_by_owner_id(user_id)
        service_name = (container or {}).get("service_name")
        if not service_name:
            return False
        try:
            resp = await asyncio.to_thread(
                self._ecs.describe_services,
                cluster=self._cluster,
                services=[service_name],
            )
        except Exception:  # noqa: BLE001 — liveness probe must not 500 the poll
            logger.warning("has_live_service: describe_services failed for %s", user_id, exc_info=True)
            return True  # fail SAFE: assume live, do not re-provision on a blind read
        services = [svc for svc in resp.get("services", []) if svc.get("status") == "ACTIVE"]
        if not services or services[0].get("desiredCount", 0) == 0:
            return False
        svc = services[0]
        # desiredCount alone is NOT liveness: a crash-looping deployment sits at
        # ACTIVE + desired=1 + running=0 + rolloutState=FAILED forever, and
        # treating that as live would suppress stale-lease recovery permanently
        # (the in-process transition poller that would mark it failed is exactly
        # what died). Live = a task actually running, or a rollout still making
        # progress.
        if svc.get("runningCount", 0) > 0:
            return True
        primary = next((d for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"), None)
        return bool(primary and primary.get("rolloutState") == "IN_PROGRESS")

    async def _provision_checkpoint(
        self,
        user_id: str,
        claim_id: str,
        *,
        generation: str | None = None,
        attempt_token: str | None = None,
    ) -> None:
        """Verify this run still holds the provision lease and refresh its
        stamp. Placed immediately before non-idempotent writes (service create,
        config write) so a superseded run stops BEFORE poisoning the winner's
        artifacts — this is what makes the fencing token real.

        ``generation`` / ``attempt_token``, when given, add the additional
        fences described in ``container_repo.update_container_leased``."""
        from core.repositories import container_repo as _lease_repo

        await _lease_repo.update_container_leased(user_id, claim_id, generation=generation, attempt_token=attempt_token)

    async def _provision_user_container_inner(
        self,
        user_id: str,
        *,
        provider_choice: str,
        byo_provider: str | None = None,
        owner_type: str = "personal",
        claim_id: str,
        suppress_transition_poller: bool = False,
        generation: str | None = None,
        attempt_token: str | None = None,
    ) -> str:
        """Provision or recover a user's container.

        Handles all scenarios:
        1. No service exists -> full provisioning (create service, write configs, start)
        2. Service exists with 0 desired -> write configs, scale up to 1
        3. Service exists and running -> force new deployment (restart)
        4. Container in error state -> clean up and re-provision

        Preserves EFS data (agent files) across all scenarios.

        Args:
            user_id: Clerk user ID.
            provider_choice: One of ``"chatgpt_oauth"``, ``"byo_key"``,
                ``"bedrock_claude"``, ``"bedrock_trial"``. Selects the LLM
                provider block in openclaw.json and drives any pre-task setup
                (codex auth pre-stage for chatgpt_oauth, per-user Secrets
                Manager wiring for byo_key, per-owner AIP + IAM mint for both
                Bedrock modes). ``bedrock_trial`` is a config-path MODE
                resolved from the owner's ``trial_local`` subscription_status
                (container_provision.resolve_provider_choice) — it is never a
                stored provider_choice.
            byo_provider: Required when ``provider_choice == "byo_key"``;
                one of ``"openai"`` or ``"anthropic"``.
            owner_type: "personal" or "org" (for EFS access-point tagging).

        Returns:
            The ECS service name.

        Raises:
            EcsManagerError: If any provisioning step fails.
        """
        # --- Card-specific pre-task setup (must run BEFORE ECS service create
        # so the first task starts with auth/keys already in place). ---
        secrets_for_task: list[dict] = []
        environment_for_task: list[dict] = []
        # Give the container a base URL to curl the public platform-info catalog
        # ($ISOL8_API_URL/api/v1/platform-info). The lightbulb skill reads the live
        # connectable integrations/channels from there instead of a stale baked
        # list. PUBLIC_API_BASE is the public origin of THIS API, wired by CDK per
        # environment. Public route -> no auth, no owner_id.
        environment_for_task.append({"name": "ISOL8_API_URL", "value": settings.PUBLIC_API_BASE.rstrip("/")})
        # Owner id for the BAKED marketplace plugin. Unlike Atlas's per-owner
        # seed files (which substitute {{ISOL8_BRIDGE_URL}} at stage time), a
        # baked plugin is byte-identical in every container and cannot carry
        # owner identity in its source — it must read it from the environment.
        # This is an IDENTIFIER, not a credential: the recommend route compares
        # OPENCLAW_HOOK_TOKEN timing-safely against the named owner's own
        # container row, so naming a different owner grants nothing.
        environment_for_task.append({"name": "ISOL8_OWNER_ID", "value": user_id})
        if provider_choice == "chatgpt_oauth":
            from core.services.oauth_service import resolve_token_owner

            await self._pre_stage_oauth_for_user(user_id, token_owner_id=await resolve_token_owner(owner_id=user_id))
            # OpenClaw 6.6 imports the container-global credentials/oauth.json
            # into each agent's auth store at boot. _pre_stage_oauth_for_user
            # above writes that single file to EFS. No env var override needed.
        elif provider_choice == "byo_key":
            if byo_provider is None:
                raise EcsManagerError(
                    "byo_provider is required when provider_choice == 'byo_key'",
                    user_id,
                )
            secrets_for_task = await self._build_byo_secrets_for_user(user_id, byo_provider)

        # Slack bot tokens ride the task-def like BYOK keys do, so a fresh
        # provision / reprovision of an owner with installs keeps them.
        secrets_for_task = list(secrets_for_task) + await self._build_slack_secrets_for_user(user_id)

        if provider_choice not in ("chatgpt_oauth", "byo_key", *BEDROCK_PROVIDER_CHOICES):
            raise EcsManagerError(
                f"Unknown provider_choice: {provider_choice!r}",
                user_id,
            )

        # Read the row ONCE up front. Reused for the bedrock kill-switch check
        # AND the state-machine routing below — no second round-trip. The mint
        # block's update_fields calls do not change bedrock_revoked (only
        # revoke_owner_bedrock does), so this pre-mint read is authoritative for
        # the revoke guard; the freshly-minted bedrock_role_arn is carried in
        # the in-memory `bedrock_role_arn` local (not re-read off the row), so a
        # stale `existing` here cannot drop it.
        existing = await container_repo.get_by_owner_id(user_id)

        # Every owner — regardless of provider_choice — runs under a UNIQUE
        # per-owner ECS task role (isol8-<env>-owner-bedrock-<owner_id>), minted
        # SYNCHRONOUSLY here before the service starts. The role NAME keeps the
        # historical "owner-bedrock" prefix because the CDK
        # PassRole/PutRolePolicy/permissions-boundary grants are scoped to it
        # (renaming silently breaks them). A unique per-owner IAM principal is
        # what the AWS read-only integration pins each owner's cross-account
        # trust to — the shared base task role is a cross-tenant hole. Lazy
        # import to avoid pulling bedrock_aip's boto3 clients into module load.
        from core.billing.bedrock_aip import (
            ensure_owner_aips,
            ensure_owner_bedrock_role,
        )

        bedrock_aip_arns: dict[str, str] | None = None
        bedrock_role_arn: str | None = None
        bedrock_gate_token_hash: str | None = None
        if provider_choice in BEDROCK_PROVIDER_CHOICES:
            # Bedrock owner: mint the per-owner AIPs + scoped IAM role. The
            # container's openclaw.json model ids become the AIP ARNs; if the
            # AIPs were absent at first boot the container would AccessDenied on
            # the AIP-scoped role.
            #
            # A `bedrock_trial` (no-card free tier) owner is a REAL Bedrock owner
            # — the grant is NOT skipped. What bounds them is the SCOPE of the
            # mint: one tier (sonnet), which pins their task role to one model
            # (see BEDROCK_TRIAL_TIERS). The paid call shape is left exactly as
            # it was — no `tiers` kwarg — so ensure_owner_aips keeps stamping
            # plan="paid" on the reverse rows; the trial mint stamps
            # plan="trial", which is what arms the meter Lambda's daily-cap kill
            # switch (a trial owner has no Stripe customer to meter).
            if provider_choice == BEDROCK_TRIAL_CHOICE:
                bedrock_aip_arns = await ensure_owner_aips(user_id, tiers=BEDROCK_TRIAL_TIERS)
            else:
                bedrock_aip_arns = await ensure_owner_aips(user_id)

            # KILL-SWITCH GUARD: ensure_owner_bedrock_role always re-puts the
            # scoped inline policy, so calling it for a revoked owner would
            # silently RESTORE Bedrock access — defeating the kill-switch on
            # any re-provision/restart. Mint the AIPs + role SHELL (so the task
            # role exists + carries EFS/logs/ssm), but do NOT re-grant the
            # Bedrock inline policy if the owner is revoked. Plan 4 owns
            # un-revoking (restore_owner_bedrock), not provision. This matters
            # doubly on the free tier: the revoke IS the daily-cap enforcement,
            # so a re-provision that re-granted it would hand a capped trial
            # owner an unlimited reset.
            is_revoked = bool(existing and existing.get("bedrock_revoked"))
            bedrock_role_arn = await ensure_owner_bedrock_role(user_id, bedrock_aip_arns, skip_bedrock_grant=is_revoked)

            # Mint (rotate) the per-owner bedrock-gate bearer token for EVERY
            # Bedrock owner — trial AND paid. The gate is the standard Bedrock
            # path, and the token row's deny flag is the kill switch
            # (revoke_owner_bedrock). mint_gate_token rotates, but it EXPIRES
            # NOTHING on a clock: the superseded hash is tracked on the row and
            # its token stays valid until a confirmed roll proves no task
            # definition and no live task can present it. Lazy import keeps
            # boto3 out of module load.
            from core.billing.bedrock_gate_tokens import (
                deny_gate_token,
                hash_token,
                mint_gate_token,
            )

            gate_token = await mint_gate_token(user_id, bedrock_aip_arns)
            # First-provision persistence: mint_gate_token's own conditional
            # row write no-ops against a not-yet-created row, so hand the hash
            # to create_user_service, which re-asserts it through the same
            # conditional swap once its upsert has created the row.
            bedrock_gate_token_hash = hash_token(gate_token)
            # Keep a revoked owner DENIED across the reprovision: mint wrote a
            # fresh row with denied=False, so re-assert the kill switch (mirrors
            # skip_bedrock_grant on the IAM half). Best-effort — a deny hiccup
            # must not break provisioning (IAM revoke is the primary fence).
            if is_revoked:
                try:
                    await deny_gate_token(user_id, True)
                except Exception:  # noqa: BLE001 — deny is a backstop, not the fence
                    logger.warning("deny_gate_token(True) failed for revoked owner %s", user_id)

            # Bearer env — the SAME resolver predicate build_provider_block
            # uses for the baseUrl, so token and URL always travel together:
            # the bearer token forces httpBearerAuth in the amazon-bedrock
            # plugin regardless of baseUrl (stream.runtime.ts), so injecting it
            # while baseUrl targets real Bedrock (break-glass/bootstrap direct
            # mode) would 403 direct invocation.
            from core.billing.bedrock_gate_url import gate_url as _resolve_gate_url

            resolved_gate_url = _resolve_gate_url()
            if resolved_gate_url:
                environment_for_task.append({"name": "AWS_BEARER_TOKEN_BEDROCK", "value": gate_token})
                environment_for_task.append({"name": "AWS_BEDROCK_FORCE_HTTP1", "value": "1"})

            # ACP coding-harness (Claude Code) Bedrock wiring. Skipped for
            # revoked owners — their Bedrock grant is withheld above, so
            # pointing the harness at it would only AccessDeny. Points Claude
            # Code at the owner's AIP ARNs so its inference is billed to the
            # owner (see helper docstring).
            #
            # PAID ONLY. derive_enabled_harnesses returns [] for bedrock_trial
            # (no ACP on the free tier), so a trial container gets no acp block
            # to drive — and the harness env could not be built for one anyway:
            # it needs a haiku AIP the trial owner deliberately does not have.
            if provider_choice == "bedrock_claude" and not is_revoked and bedrock_aip_arns:
                environment_for_task.extend(
                    _build_bedrock_acp_harness_env(
                        bedrock_aip_arns,
                        gate_token=gate_token,
                        gate_url=resolved_gate_url,
                    )
                )
        else:
            # Non-Bedrock owner (chatgpt_oauth / byo_key): mint/keep the
            # per-owner role SHELL — base EFS/logs/ssm policy + permissions
            # boundary — but pass skip_bedrock_grant=True so it carries NO
            # Bedrock inline policy, and DO NOT mint Bedrock AIPs (the owner
            # never uses Bedrock inference). The empty aip_arns map is unread
            # under skip_bedrock_grant=True. This gives every owner a unique
            # task-role principal without granting Bedrock to non-Bedrock owners.
            bedrock_role_arn = await ensure_owner_bedrock_role(user_id, {}, skip_bedrock_grant=True)

        # Paperclip auth is no longer a container-wide task env. Each agent
        # authenticates as ITSELF via its own per-agent identity file
        # (paperclip-claimed-api-key.json staged by
        # friday_identity.pre_stage_paperclip_identities), so there is nothing
        # to inject here.

        service_name = self._service_name(user_id)

        # Check current state (`existing` was read above, before the mint).
        # Raises EcsManagerError (never silently None) on a describe_services
        # API failure — see _service_exists docstring: a swallowed error here
        # used to silently divert into the "no service exists" full-provision
        # branch below, which could then race a create_service call against
        # an ACTUALLY-active service (L2 fix).
        svc = self._service_exists(service_name, user_id)

        # CHECKPOINT: the AIP/role mint above is done. Verify the lease is
        # still ours (and refresh it) before any ECS/EFS side effect — a run
        # superseded during the mint stops here.
        await self._provision_checkpoint(user_id, claim_id, generation=generation, attempt_token=attempt_token)

        # --- Scenario: Service exists (ACTIVE or DRAINING) ---
        if svc and svc.get("status") == "ACTIVE":
            desired = svc.get("desiredCount", 0)
            running = svc.get("runningCount", 0)

            if desired == 0:
                # Service exists but stopped -- write fresh configs and scale up
                logger.info("Service %s exists with 0 desired, scaling up", service_name)
                # Legacy rows pre-dating gateway_token would yield None here;
                # fall through to a fresh token so write_openclaw_config doesn't
                # silently emit a null auth.token (see config.py validation).
                gateway_token = (existing.get("gateway_token") if existing else None) or secrets.token_urlsafe(32)

                # Update gateway token and write configs.
                # Every _update_container call in this method carries the
                # claim: claim_id is non-optional here, so unlike
                # create_user_service there is no unfenced door — a run
                # superseded in this branch stops instead of stamping its
                # state over the winner's row. The row writes further down the
                # call chain (write_user_configs -> ensure_device_identities)
                # take no lease of their own; the checkpoint immediately
                # before each write_user_configs call is their fence.
                if existing:
                    await self._update_container(
                        user_id,
                        claim_id=claim_id,
                        generation=generation,
                        attempt_token=attempt_token,
                        status="provisioning",
                        substatus="restarting",
                    )

                # Self-heal hook_token for legacy rows that pre-date hook-token
                # provisioning. Without this, a legacy user hitting the restart
                # path passes None for hook_token through write_user_configs and
                # _register_task_definition. Because build_openclaw_config_dict
                # renders the hooks block UNCONDITIONALLY with the literal
                # "${OPENCLAW_HOOK_TOKEN}" placeholder while
                # _build_register_kwargs_from_base gates env injection on
                # `if hook_token:`, the new task-def would lack
                # OPENCLAW_HOOK_TOKEN and the container would boot with the
                # unsubstituted placeholder string as its hooks.token — every
                # webhook 401s indefinitely. Mint+persist here so the row, the
                # env var, and the rendered config stay consistent.
                existing_hook_token = (existing.get("hook_token") if existing else None) or secrets.token_urlsafe(32)
                if existing and not existing.get("hook_token"):
                    await self._update_container(
                        user_id,
                        claim_id=claim_id,
                        generation=generation,
                        attempt_token=attempt_token,
                        hook_token=existing_hook_token,
                        hook_token_rotated_at=utc_now_iso(),
                        hook_token_provisioned_at=utc_now_iso(),
                    )

                try:
                    # CHECKPOINT before the config write — the poison write in
                    # the double-provision incident. A superseded run must not
                    # overwrite the winner's openclaw.json.
                    await self._provision_checkpoint(
                        user_id, claim_id, generation=generation, attempt_token=attempt_token
                    )
                    # Reuse the existing (or freshly-minted) hook_token so the
                    # rendered openclaw.json placeholder resolves at boot
                    # against the env var we layer back onto the new task-def
                    # below.
                    await self.write_user_configs(
                        user_id,
                        gateway_token,
                        provider_choice=provider_choice,
                        byo_provider=byo_provider,
                        hook_token=existing_hook_token,
                        owner_type=owner_type,
                        aip_arns=bedrock_aip_arns,
                        # The replacement task-def registered below is cloned
                        # from the base family — gate on THAT image, not the
                        # stopped service's previous one (an image rollback
                        # would otherwise carry a key the older schema rejects).
                        deployed_tag=await self._base_image_tag(),
                    )
                except ProvisionSuperseded:
                    raise
                except Exception:
                    put_metric("container.provision", dimensions={"status": "error"})
                    put_metric("container.error_state")
                    raise

                # Re-register the task definition with the freshly-computed
                # secrets_for_task. Without this, a BYOK user whose container
                # scaled to zero would restart on the prior revision and skip
                # the per-user OPENAI/ANTHROPIC secrets injection (the path
                # that flows in via _build_byo_secrets_for_user above).
                # We always re-register so the path is uniform across
                # providers; oauth/bedrock_claude have empty secrets_for_task.
                # Use the same existing/self-healed hook_token computed above
                # so the env var on the new revision matches what
                # write_user_configs just rendered.
                access_point_id = existing.get("access_point_id") if existing else None
                if access_point_id:
                    try:
                        # Prefer the role just minted in THIS call (in-memory)
                        # over the row's value: `existing` was read before the
                        # mint, so for a re-provision its bedrock_role_arn may be
                        # stale/absent. The mint above runs for ALL providers now,
                        # so bedrock_role_arn is normally set here; the row
                        # fallback only covers a degenerate path where the mint
                        # returned nothing — it must still resolve to a per-owner
                        # role, never the shared base.
                        restart_task_role_arn = bedrock_role_arn or (
                            existing.get("bedrock_role_arn") if existing else None
                        )
                        # Size by plan on the restart path too — this is the
                        # branch a full reprovision (stop -> dispatch_provision)
                        # lands on, so without it an Ultra reprovision would
                        # silently revert the container to the standard box.
                        restart_cpu, restart_memory = await self._size_for_owner(user_id)
                        new_task_def_arn = await asyncio.to_thread(
                            self._register_task_definition,
                            access_point_id,
                            secrets_for_task=secrets_for_task,
                            environment_for_task=environment_for_task,
                            hook_token=existing_hook_token,
                            task_role_arn=restart_task_role_arn,
                            new_cpu=restart_cpu,
                            new_memory=restart_memory,
                        )
                        await asyncio.to_thread(
                            self._ecs.update_service,
                            cluster=self._cluster,
                            service=service_name,
                            desiredCount=1,
                            taskDefinition=new_task_def_arn,
                            # Re-assert the Cloud Map registry on every provision.
                            # create_service was the ONLY writer of this field, so a
                            # service created against a different registry stayed on
                            # it forever with no code path able to move it -- even
                            # though UpdateService accepts serviceRegistries for
                            # deploymentController=ECS. Re-asserting the current
                            # target makes every provision self-heal.
                            **self._service_registries(),
                            deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
                        )
                        old_task_def_arn = existing.get("task_definition_arn") if existing else None
                        # The widest unfenced window on this path: the
                        # checkpoint above is separated from this write by the
                        # plan lookup, the task-def register and the ECS
                        # update_service call. A takeover landing in there must
                        # not leave the winner's row naming THIS run's
                        # revision, which the winner's service never runs.
                        await self._update_container(
                            user_id,
                            claim_id=claim_id,
                            generation=generation,
                            attempt_token=attempt_token,
                            task_definition_arn=new_task_def_arn,
                        )
                        if old_task_def_arn and old_task_def_arn != new_task_def_arn:
                            try:
                                await asyncio.to_thread(
                                    self._deregister_task_definition,
                                    old_task_def_arn,
                                )
                            except Exception as e:
                                logger.warning(
                                    "Failed to deregister stale task def %s: %s",
                                    old_task_def_arn,
                                    e,
                                )
                        # start_user_service normally fires the running-transition
                        # poller as a detached task; we bypassed it. When this
                        # branch is reached via the SFN runner
                        # (suppress_transition_poller=True — the caller
                        # explicitly supplied claim_id and will foreground-await
                        # wait_for_running right after this returns), firing a
                        # second detached poller here would duplicate ECS API
                        # calls against the same row for no upside. A deliberate
                        # takeover (claim minted internally, no runner awaiting
                        # the transition) still needs it.
                        if not suppress_transition_poller:
                            asyncio.create_task(self._await_running_transition(user_id))
                    except ProvisionSuperseded:
                        # NOT a provision failure — bare re-raise, ahead of the
                        # handlers below. This is the ONLY newly-fenced write
                        # sitting inside a try that wraps: `except Exception`
                        # would have rebranded the fence's refusal as an
                        # EcsManagerError, so provision_user_container would
                        # classify + release instead of taking its superseded
                        # branch, and the runner would exit retry/deterministic
                        # instead of EXIT_SUPERSEDED. (The release itself is
                        # conditioned on the same claim, so it would no-op —
                        # the row is safe either way; the exception TYPE is
                        # what the runner reads.) The sibling fenced writes in
                        # this method sit in no try at all and reach that
                        # branch directly.
                        raise
                    except EcsManagerError:
                        # Metrics only — the outer claim-fenced
                        # release_provision_to_error owns the status="error"
                        # transition (single-owner rule); flipping it here
                        # pre-empted that release so retryable/last_error never
                        # landed (T3 Critical). Re-raise for the outer release.
                        put_metric("container.provision", dimensions={"status": "error"})
                        put_metric("container.error_state")
                        raise
                    except Exception as e:
                        put_metric("container.provision", dimensions={"status": "error"})
                        put_metric("container.error_state")
                        raise EcsManagerError(
                            f"Failed to restart service with refreshed task def: {e}",
                            user_id,
                        )
                else:
                    # No access point on record (shouldn't happen for an
                    # ACTIVE service but fall back to the simple start path).
                    # force: this IS a provision — see the Step 4 comment below.
                    # claim_id passed only when suppress_transition_poller
                    # (a caller-supplied claim, i.e. the SFN runner) — see
                    # start_user_service's docstring; a deliberate takeover
                    # (claim minted internally, no runner awaiting the
                    # transition) still needs its own poller.
                    try:
                        await self.start_user_service(
                            user_id,
                            force=True,
                            claim_id=claim_id if suppress_transition_poller else None,
                            generation=generation,
                            attempt_token=attempt_token,
                        )
                    except EcsManagerError:
                        # Metrics only; outer release owns status="error".
                        put_metric("container.provision", dimensions={"status": "error"})
                        put_metric("container.error_state")
                        raise

                put_metric("container.provision", dimensions={"status": "ok"})
                return service_name

            elif running > 0:
                # Service exists and running -- force new deployment
                logger.info(
                    "Service %s running (%d tasks), forcing new deployment",
                    service_name,
                    running,
                )
                if existing:
                    await self._update_container(
                        user_id,
                        claim_id=claim_id,
                        generation=generation,
                        attempt_token=attempt_token,
                        status="provisioning",
                        substatus="redeploying",
                    )

                try:
                    self._ecs.update_service(
                        cluster=self._cluster,
                        service=service_name,
                        forceNewDeployment=True,
                        # Same self-heal as the restart branch above.
                        **self._service_registries(),
                        deploymentConfiguration=SINGLE_WRITER_DEPLOYMENT_CONFIG,
                    )
                except Exception as e:
                    # Metrics only; outer release owns status="error". (This
                    # branch already flipped the row to "provisioning" above, so
                    # the release's status-condition lands the error transition
                    # here — now carrying retryable + last_error, which the old
                    # inner flip dropped.)
                    put_metric("container.provision", dimensions={"status": "error"})
                    put_metric("container.error_state")
                    raise EcsManagerError(f"Failed to force redeploy: {e}", user_id)

                put_metric("container.provision", dimensions={"status": "ok"})
                # New deployment forces a fresh task; fire the poller UNLESS
                # the SFN runner will foreground-await the transition itself
                # — see the comment on the sibling branch above.
                if not suppress_transition_poller:
                    asyncio.create_task(self._await_running_transition(user_id))
                return service_name

            else:
                # Desired > 0 but not running yet -- ECS is working on it, just wait
                logger.info(
                    "Service %s has %d desired, %d running -- ECS is starting",
                    service_name,
                    desired,
                    running,
                )
                if existing and existing.get("status") != "provisioning":
                    await self._update_container(
                        user_id,
                        claim_id=claim_id,
                        generation=generation,
                        attempt_token=attempt_token,
                        status="provisioning",
                        substatus="starting",
                    )
                    # Fire the poller UNLESS the SFN runner will
                    # foreground-await the transition itself — see the
                    # comment on the sibling branches above.
                    if not suppress_transition_poller:
                        asyncio.create_task(self._await_running_transition(user_id))
                return service_name

        # --- Scenario: No service exists -- full provisioning ---
        logger.info("No active service for user %s, full provisioning", user_id)

        # Clean up stale DB record if in error state
        if existing and existing.get("status") == "error":
            if existing.get("task_definition_arn"):
                try:
                    self._deregister_task_definition(existing["task_definition_arn"])
                except Exception:
                    pass
            # Don't delete access point -- preserves user's EFS data
            await self._update_container(
                user_id,
                claim_id=claim_id,
                generation=generation,
                attempt_token=attempt_token,
                task_definition_arn=None,
                status="provisioning",
                substatus=None,
            )

        # Step 1: Generate gateway token + hook token. The gateway token
        # authorizes our backend's RPC calls into the OpenClaw HTTP API; the
        # hook token authorizes inbound webhooks the container needs to
        # verify (channel ingress, etc.). They are independent secrets.
        # hook_token is persisted atomically with the rest of the row by
        # create_user_service's upsert below — passing it as a separate
        # set_hook_token call before the upsert would no-op for first-time
        # provisioning since the row doesn't exist yet.
        gateway_token = secrets.token_urlsafe(32)
        hook_token = secrets.token_urlsafe(32)

        # Step 2: Create ECS service (desiredCount=0). Per-user secrets (only
        # set for byo_key) and per-user env (only set for chatgpt_oauth, to
        # point CODEX_HOME at the EFS-staged auth.json dir) are layered onto
        # the per-user task def so the first task ECS launches has them.
        # The hook_token is injected as OPENCLAW_HOOK_TOKEN on the primary
        # container by create_user_service -> _register_task_definition.
        service_name = await self.create_user_service(
            user_id,
            gateway_token,
            owner_type=owner_type,
            secrets_for_task=secrets_for_task,
            environment_for_task=environment_for_task,
            hook_token=hook_token,
            task_role_arn=bedrock_role_arn,
            bedrock_aip_arns=bedrock_aip_arns,
            bedrock_gate_token_hash=bedrock_gate_token_hash,
            claim_id=claim_id,
            attempt_token=attempt_token,
        )

        # create_user_service's upsert MINTED a fresh provision_generation
        # (the single mint point — a full create IS a new incarnation).
        # A caller-supplied ``generation`` predates that mint (the SFN
        # runner reads it off the row at startup), so without refreshing it
        # here every generation-fenced checkpoint below would compare
        # stale-vs-new and raise ProvisionSuperseded against THIS SAME run
        # — the task would exit "superseded" with resources created and the
        # row wedged in "provisioning". Safe to adopt blindly: claim_id
        # (and attempt_token) remain the primary fences on every write, so
        # a genuinely superseded run still stops at its next checkpoint.
        if generation is not None:
            refreshed_row = await container_repo.get_by_owner_id(user_id)
            generation = (refreshed_row or {}).get("provision_generation")

        # Step 3: Write configs to EFS
        try:
            # CHECKPOINT before the config write — the poison write in the
            # double-provision incident: this is exactly where the losing run
            # historically overwrote the winner's openclaw.json with an AIP ARN
            # the IAM policy did not allow.
            await self._provision_checkpoint(user_id, claim_id, generation=generation, attempt_token=attempt_token)
            await self.write_user_configs(
                user_id,
                gateway_token,
                provider_choice=provider_choice,
                byo_provider=byo_provider,
                hook_token=hook_token,
                owner_type=owner_type,
                aip_arns=bedrock_aip_arns,
                deployed_tag=await self._base_image_tag(),
            )
        except ProvisionSuperseded:
            raise
        except Exception as e:
            # Metrics only; outer release owns status="error".
            put_metric("container.provision", dimensions={"status": "error"})
            put_metric("container.error_state")
            raise EcsManagerError(f"Failed to write configs for user {user_id}: {e}", user_id)

        # Step 4: Start the container.
        #
        # force: this call IS the provision, not a wake. Two reasons it must not
        # re-run the start guards:
        #   - The trial->paid conversion STAMPS the container row and then
        #     dispatches this provision. Guarding here would make the conversion
        #     refuse to start the container it just reprovisioned — a deadlock
        #     against its own marker, and the paying customer is left with
        #     nothing running.
        #   - Every caller that reaches provision_user_container has already
        #     passed evaluate_provision_gate (which returns the same
        #     trial_daily_cap gate for an over-cap trial owner) or is a
        #     deliberate system/operator reprovision.
        try:
            # claim_id passed only when suppress_transition_poller (a
            # caller-supplied claim, i.e. the SFN runner) -- see
            # start_user_service's docstring; a deliberate takeover still
            # needs its own poller since nothing else awaits the transition.
            await self.start_user_service(
                user_id,
                force=True,
                claim_id=claim_id if suppress_transition_poller else None,
                generation=generation,
                attempt_token=attempt_token,
            )
        except EcsManagerError:
            # Metrics only; outer release owns status="error".
            put_metric("container.provision", dimensions={"status": "error"})
            put_metric("container.error_state")
            raise

        put_metric("container.provision", dimensions={"status": "ok"})
        logger.info("Provisioned container %s for user %s", service_name, user_id)

        # Poller fired by start_user_service above UNLESS
        # suppress_transition_poller (SFN runner owns the wait itself via
        # wait_for_running in the foreground).

        return service_name

    async def wait_for_running(
        self,
        user_id: str,
        *,
        claim_id: str | None = None,
        generation: str | None = None,
        attempt_token: str | None = None,
    ) -> None:
        """Public foreground wait for the provisioning -> running transition.

        The one-shot provision runner (scripts.provision_runner) awaits this
        so the transition is owned by a process the state machine supervises,
        instead of a detached create_task that dies with its host process.
        Passing ``claim_id`` (always set by the runner) makes
        ``_await_running_transition`` write through the FENCED helpers
        (``update_container_leased`` / ``release_provision_to_error``)
        instead of a bare ``update_fields`` — see that method's docstring.
        Propagates ``ProvisionSuperseded`` when the fenced write loses its
        race (a straggler from a superseded attempt, or a deliberate
        takeover) — the runner's caller classifies that as EXIT_SUPERSEDED.
        """
        await self._await_running_transition(
            user_id, claim_id=claim_id, generation=generation, attempt_token=attempt_token
        )

    async def _await_running_transition(
        self,
        user_id: str,
        *,
        interval_s: float = 10.0,
        claim_id: str | None = None,
        generation: str | None = None,
        attempt_token: str | None = None,
    ) -> None:
        """Durable background poller that drives provisioning -> running.

        Exits on one of four conditions:

        1. Container is reachable (ECS task from the PRIMARY deployment is
           RUNNING + gateway port open): write status=running, substatus=
           gateway_healthy, store task_arn, return.
        2. DDB status is no longer "provisioning" (external state change --
           admin stop, re-provision, reaper, etc.): return silently.
        3. PRIMARY deployment rolloutState=FAILED (circuit breaker tripped --
           the provision will never succeed): write status=error, return.
        4. asyncio.CancelledError (backend shutdown): propagate.

        No fixed timeout. The container can take 10s or 10 minutes to become
        healthy -- the poller keeps going until one of the above happens.
        (The SFN caller — ``wait_for_running`` — is itself bounded by the
        state machine's own per-attempt task timeout; this loop has no
        opinion about that.)

        Forced-redeploy paths (resize, forceNewDeployment) have an OLD healthy
        task while the NEW deployment rolls out. The poller filters tasks to
        only those from the PRIMARY deployment so an old task can't cause a
        premature status=running write that masks a failing new rollout.

        Spot capacity fallback: a FARGATE_SPOT service with no Spot capacity
        available places no tasks, reports no failures, and never trips the
        circuit breaker — it would sit in provisioning forever. If ECS has
        placed zero tasks (any lifecycle state, so a PENDING task pulling the
        multi-GB image does NOT trigger this) for SPOT_PLACEMENT_FALLBACK_S
        **while desiredCount > 0**, flip the service to on-demand once and
        keep polling. The desiredCount gate matters: a service sitting at
        desiredCount=0 (stopped, or racing a wake/stop) trivially has zero
        placed tasks forever, and that is not a capacity drought -- flipping
        on that signature permanently triples the container's compute cost
        for no reason (no revert path, see `_fallback_service_to_on_demand`).
        The flip only runs when the service is empty, so it can never kill a
        live task.

        ``claim_id`` / ``generation`` / ``attempt_token``: when ``claim_id``
        is given (the SFN runner's foreground call via ``wait_for_running``),
        the two terminal writes below (running / error) go through the
        FENCED helpers (``update_container_leased`` /
        ``release_provision_to_error``) instead of a bare ``update_fields`` —
        this is the L2 fix: SFN becomes the sole, fenced writer of the
        running/error transition, and a straggler from a superseded attempt
        is rejected instead of racing its write against the winner's. When
        ``claim_id`` is None (every OTHER caller — wake-from-stopped,
        resize — neither of which holds or needs a provision lease), the
        writes are the original bare ``update_fields`` calls, UNCHANGED.
        """
        no_placement_since: float | None = None
        spot_fallback_done = False
        while True:
            try:
                container = await container_repo.get_by_owner_id(user_id)
                if not container or container.get("status") != "provisioning":
                    # External state change or row gone -- nothing to do.
                    return

                service_name = container["service_name"]

                if claim_id is not None:
                    # Pure lease-refresh checkpoint (fields=None), FIRST in
                    # every iteration so no waiting path can skip it — the
                    # no-primary-deployment retry `continue` below bypassed a
                    # later placement. The boot window can run ~12 minutes
                    # with no other leased write, which let the lease read
                    # stale while this poller was alive — and /recover's
                    # active-lease stand-down keys on exactly that freshness.
                    # A dead runner stops heartbeating within one tick, so
                    # the 300s crash-recovery TTL is unchanged; a takeover
                    # surfaces here as ProvisionSuperseded (handler below)
                    # one tick sooner than the next real write would have.
                    await container_repo.update_container_leased(
                        user_id,
                        claim_id,
                        None,
                        generation=generation,
                        attempt_token=attempt_token,
                    )

                # One describe_services per iteration covers both concerns:
                # 1. primary deployment id -> startedBy filter for list_tasks
                # 2. primary deployment rolloutState -> failure detection
                primary = await self._get_primary_deployment(service_name)
                if primary is None:
                    # Transient state -- no PRIMARY deployment visible. Retry.
                    await asyncio.sleep(interval_s)
                    continue

                if primary.get("rolloutState") == "FAILED":
                    if claim_id is not None:
                        await container_repo.release_provision_to_error(
                            user_id,
                            claim_id,
                            last_error="Deployment circuit breaker tripped (rolloutState=FAILED)",
                            retryable=False,
                            generation=generation,
                            attempt_token=attempt_token,
                        )
                    else:
                        await container_repo.update_fields(
                            user_id,
                            {
                                "status": "error",
                                "substatus": "deployment_failed",
                            },
                        )
                    logger.error(
                        "Deployment circuit breaker tripped for container %s (user=%s); marking error",
                        service_name,
                        user_id,
                    )
                    return

                deployment_id = primary.get("id")
                task_arn, ip, placed_count = await self._poll_running_task(service_name, deployment_id)
                if placed_count > 0:
                    no_placement_since = None
                elif not spot_fallback_done:
                    # desiredCount rides along on the same describe_services
                    # call `_get_primary_deployment` already made this tick --
                    # no extra API round trip needed to gate on it.
                    desired_count = primary.get("desiredCount", 0)
                    if desired_count <= 0:
                        # Service isn't asking ECS to place anything right
                        # now (desiredCount=0 -- stopped, or racing a
                        # wake/stop). Zero placed tasks here is the expected
                        # steady state, not a Spot capacity drought, so don't
                        # accrue time toward the flip.
                        no_placement_since = None
                    else:
                        now = time.monotonic()
                        if no_placement_since is None:
                            no_placement_since = now
                        elif now - no_placement_since >= SPOT_PLACEMENT_FALLBACK_S:
                            elapsed_s = now - no_placement_since
                            spot_fallback_done = True
                            self._fallback_service_to_on_demand(
                                service_name,
                                user_id,
                                elapsed_s=elapsed_s,
                                desired_count=desired_count,
                                placed_count=placed_count,
                            )
                # to_thread: the 5s TCP connect must not stall the backend
                # event loop — this poller runs detached in-process for
                # wake/resize transitions.
                if task_arn and ip and await asyncio.to_thread(self.is_healthy, ip):
                    running_fields = {
                        "status": "running",
                        "substatus": "gateway_healthy",
                        "task_arn": task_arn,
                        # A completed provision (either path) has applied the
                        # current task def + EFS config — the wake fast-path
                        # marker is spent. The DIRECT scale-up path never
                        # reaches here with the marker set: the wake verb
                        # routes marked rows through the SFN executor.
                        "needs_provision": False,
                    }
                    if claim_id is not None:
                        # Terminal SUCCESS: atomically RELEASE the lease with
                        # the running write, so "provisioning row carries a
                        # claim" stays a reliable active-SFN-work signal —
                        # residual claims from completed provisions would
                        # make the boot reconcile misclassify a later
                        # interrupted wake/resize as SFN-owned and skip
                        # resuming its poller.
                        await container_repo.update_container_leased(
                            user_id,
                            claim_id,
                            running_fields,
                            generation=generation,
                            attempt_token=attempt_token,
                            release_lease=True,
                        )
                    else:
                        await container_repo.update_fields(user_id, running_fields)
                    logger.info(
                        "Transitioned container %s to running (user=%s, task=%s)",
                        service_name,
                        user_id,
                        task_arn.split("/")[-1],
                    )
                    # THE ROLL IS CONFIRMED — the live task is healthy, so it
                    # holds whatever token its definition carries and any
                    # superseded row nothing can present is finally safe to
                    # delete. This is the sole confirmation point for BOTH
                    # paths: the detached poller, and the SFN runner's
                    # foreground wait_for_running, which just delegates here.
                    # Never raises; a missed call only leaves a stale row.
                    from core.billing.bedrock_gate_tokens import (
                        expire_superseded_gate_tokens,
                    )

                    await expire_superseded_gate_tokens(user_id)

                    return
                if task_arn and ip and container.get("substatus") != "task_running":
                    # Task is RUNNING with an IP but the gateway port isn't
                    # open yet — image pulled, OpenClaw still booting. This
                    # window is minutes long; surface it so the frontend
                    # stepper can render a real phase for it instead of a
                    # time-based guess. Guarded on the fetched row so it
                    # writes once, not once per poll iteration.
                    if claim_id is not None:
                        await container_repo.update_container_leased(
                            user_id,
                            claim_id,
                            {"substatus": "task_running"},
                            generation=generation,
                            attempt_token=attempt_token,
                        )
                    else:
                        await container_repo.update_fields(
                            user_id,
                            {"substatus": "task_running"},
                        )

            except asyncio.CancelledError:
                raise
            except ProvisionSuperseded:
                # Fenced write lost the race (straggler / takeover) — the
                # SFN caller (wait_for_running -> provision_runner) must stop
                # entirely, same as any other checkpoint. Non-fenced callers
                # (claim_id is None) never hit this branch.
                raise
            except Exception:
                logger.exception(
                    "Unexpected error in _await_running_transition for %s; will retry",
                    user_id,
                )

            await asyncio.sleep(interval_s)

    def _fallback_service_to_on_demand(
        self,
        service_name: str,
        user_id: str,
        *,
        elapsed_s: float,
        desired_count: int,
        placed_count: int,
    ) -> None:
        """Flip a service that has placed nothing for SPOT_PLACEMENT_FALLBACK_S
        onto on-demand FARGATE.

        Only called by _await_running_transition once desiredCount > 0 (the
        service actually wants a task running) AND zero tasks have been
        placed for SPOT_PLACEMENT_FALLBACK_S, so forceNewDeployment cannot
        terminate a live task (there are none). The service stays on-demand
        until its next full reprovision recreates it on Spot — deliberately
        no revert path; capacity droughts are rare and a container quietly
        paying on-demand beats machinery to migrate it back.

        This is a response to a PLACEMENT TIMEOUT, not a diagnosed root
        cause. A Spot capacity drought is the common case, but the same
        signature (desiredCount>0, zero placed, no rollout failure) can also
        show up for a stuck image pull, an EFS mount failure, or subnet/ENI
        exhaustion — we don't attempt to disambiguate those here, only to
        stop asserting a cause we never actually observed. The log line
        below best-effort includes ECS's own most recent service event as a
        free diagnostic hint.

        Failure here is non-fatal: the poller keeps polling either way, and
        the container simply stays pending until capacity appears or an
        operator intervenes.
        """
        latest_event = ""
        try:
            resp = self._ecs.describe_services(cluster=self._cluster, services=[service_name])
            events = resp.get("services", [{}])[0].get("events", [])
            if events:
                latest_event = f" Latest ECS event: {events[0].get('message', '')!r}."
        except Exception:
            # Best-effort diagnostic only -- never let it block the flip.
            pass

        try:
            self._ecs.update_service(
                cluster=self._cluster,
                service=service_name,
                capacityProviderStrategy=ON_DEMAND_CAPACITY_STRATEGY,
                forceNewDeployment=True,
            )
            put_metric(
                "container.spot_fallback",
                dimensions={"trigger": "placement_timeout"},
            )
            logger.warning(
                "Flipped %s to on-demand after %.0fs with zero placed tasks "
                "(desiredCount=%d, placed=%d, user=%s); this responds to a "
                "placement timeout, not a diagnosed Spot capacity shortage.%s",
                service_name,
                elapsed_s,
                desired_count,
                placed_count,
                user_id,
                latest_event,
            )
        except Exception:
            logger.exception(
                "Failed to flip %s to on-demand after a placement timeout (user=%s)",
                service_name,
                user_id,
            )

    async def _get_primary_deployment(self, service_name: str) -> dict | None:
        """Return the PRIMARY deployment dict for the service, or None if
        unavailable. Primary is the deployment currently being rolled out or
        most-recently-succeeded; there is at most one at any time.

        Returns None on transient ECS errors so the caller retries on the
        next poll tick rather than flipping to error on a describe_services
        hiccup.
        """
        try:
            resp = self._ecs.describe_services(
                cluster=self._cluster,
                services=[service_name],
            )
        except Exception:
            logger.warning("describe_services failed for %s; will retry next tick", service_name)
            return None

        services = resp.get("services", [])
        if not services:
            return None
        for deployment in services[0].get("deployments", []):
            if deployment.get("status") == "PRIMARY":
                return deployment
        return None

    async def _poll_running_task(
        self, service_name: str, deployment_id: str | None
    ) -> tuple[str | None, str | None, int]:
        """Find a RUNNING task from the given deployment and return
        (task_arn, ip, placed_count).

        Lists ALL running tasks for the service, then filters in-code to
        tasks whose `startedBy` matches the primary deployment's id. ECS
        tags service-launched tasks with startedBy=ecs-svc/<deployment-id>.
        We can't filter via the ListTasks API because ECS rejects startedBy
        combined with serviceName (InvalidParameterException).

        Returns (None, None, placed_count) when no qualifying task has an IP
        yet. placed_count is the number of tasks ECS has placed for the
        service in ANY lifecycle state (PENDING tasks pulling the image count;
        desiredStatus=RUNNING includes them). Zero means ECS has placed
        nothing at all — the Spot capacity-unavailable signature, since that
        condition produces no tasks, no failures, and no rollout state change.
        """
        if not deployment_id:
            return None, None, 0

        list_resp = self._ecs.list_tasks(
            cluster=self._cluster,
            serviceName=service_name,
            desiredStatus="RUNNING",
        )
        task_arns = list_resp.get("taskArns", [])
        if not task_arns:
            return None, None, 0

        desc_resp = self._ecs.describe_tasks(
            cluster=self._cluster,
            tasks=task_arns,
        )
        for task in desc_resp.get("tasks", []):
            if task.get("startedBy") != deployment_id:
                continue
            if task.get("lastStatus") != "RUNNING":
                continue
            private_ip = _task_private_ip(task)
            if private_ip:
                task_arn = task.get("taskArn") or task_arns[0]
                return task_arn, private_ip, len(task_arns)
        return None, None, len(task_arns)

    async def write_user_configs(
        self,
        user_id: str,
        gateway_token: str,
        *,
        provider_choice: str,
        byo_provider: str | None = None,
        hook_token: str | None = None,
        owner_type: str = "personal",
        aip_arns: dict[str, str] | None = None,
        deployed_tag: str | None = None,
    ) -> None:
        """Write OpenClaw config files + pre-paired device trust store to EFS.

        ``deployed_tag`` is the tag of the image that will READ this config,
        for fork-level-gated keys. Provisioning passes the base family's image
        (the replacement task is cloned from it); a regen of a live container
        leaves it ``None`` and the owner's registered task definition is used.

        Writes three files to the user's EFS workspace:

        - `openclaw.json` — the gateway config (provider block driven by
          ``provider_choice``: chatgpt_oauth / byo_key / bedrock_claude).
          ``agents.list`` is now seeded from the owner's live Paperclip
          agent list so /chat and /teams operate on the same agent
          population. If the owner has no Paperclip company yet or
          Paperclip is unreachable, ``fetch_paperclip_agents_for_owner``
          returns ``[]`` and ``build_openclaw_config_dict`` falls back to
          the legacy ``main`` agent so provision still succeeds.
        - `devices/.node-device-key.pem` — the node role's private key, read
          by the in-container agent for loopback authentication
        - `devices/paired.json` — the merged trust store (node + operators)

        Also ensures the operator Ed25519 private key is present in the
        container's DynamoDB row (KMS-encrypted).

        Callers that only need to refresh the device trust store (e.g. after
        a config patch that updated openclaw.json in-place) should call
        :meth:`ensure_device_identities` instead to avoid overwriting the
        openclaw.json that was just patched.

        Args:
            user_id: Container OWNER id (org_id for orgs, user_id for
                personal users). The parameter name is preserved from the
                pre-flat-fee API surface — pre-cutover containers were
                strictly per-user, but post-cutover everything is
                per-owner. Treated as ``owner_id`` internally.
            gateway_token: Auth token for the OpenClaw gateway HTTP API.
            provider_choice: One of ``"chatgpt_oauth"``, ``"byo_key"``,
                ``"bedrock_claude"``, ``"bedrock_trial"``. Drives which
                provider block lands in openclaw.json (both Bedrock modes emit
                the same provider shape; ``bedrock_trial`` emits a one-model
                catalog and the free-tier lockdown).
            byo_provider: ``"openai"`` or ``"anthropic"``. Required when
                ``provider_choice == "byo_key"`` so the right model block is
                emitted; ignored otherwise.
            hook_token: Optional per-user OpenClaw webhook auth token. Not
                interpolated into the rendered openclaw.json (it carries the
                literal ``${OPENCLAW_HOOK_TOKEN}`` placeholder which OpenClaw
                expands at boot from the per-user ECS task-def env var). The
                arg is threaded through for signature symmetry with the env
                injection in :meth:`_register_task_definition` and to leave
                a hook for the rotate flow to land here without rewiring.
        """
        # Treat the inbound parameter as owner_id internally. Naming
        # mismatch with the API surface is intentional — preserves
        # callers but makes the multi-tenant intent clear.
        owner_id = user_id

        # Lazy imports to avoid pulling these into module load (they
        # transitively touch httpx + DDB clients we don't want at import
        # time for unit tests).
        import httpx

        from core.repositories.paperclip_repo import PaperclipRepo
        from core.services.paperclip_admin_client import PaperclipAdminClient
        from core.services.paperclip_openclaw_pairing import (
            fetch_paperclip_agents_for_owner,
        )

        # Match the construction pattern used elsewhere
        # (routers/webhooks.py, routers/paperclip_proxy.py): create an
        # httpx.AsyncClient bound to PAPERCLIP_INTERNAL_URL, hand it to
        # PaperclipAdminClient, and aclose() the underlying client when
        # done. PaperclipAdminClient itself has no aclose() — the http
        # client owns the connection pool lifecycle. Short table name
        # only; core.dynamodb.get_table prepends the env prefix.
        http = httpx.AsyncClient(
            base_url=settings.PAPERCLIP_INTERNAL_URL,
            timeout=15.0,
        )
        # {paperclip_agent_id: plaintext key} for EVERY agent in the owner's
        # company — each agent authenticates to Paperclip as ITSELF (not as
        # Friday) via its own per-agent identity file. Empty when Paperclip is
        # unreachable; the next reprovision self-heals.
        paperclip_agent_keys: dict[str, str] = {}
        paperclip_company_id: str | None = None
        starter_agent_ids: dict[str, str] = {}
        try:
            repo = PaperclipRepo(table_name="paperclip-companies")
            admin_client = PaperclipAdminClient(http_client=http)
            paperclip_agents = await fetch_paperclip_agents_for_owner(
                owner_id=owner_id,
                repo=repo,
                admin_client=admin_client,
            )
            owner_row = await repo.resolve_owner_row(owner_id)
            if owner_row is not None:
                starter_agent_ids = dict(owner_row.starter_agent_ids or {})
            # Mint (or read) a per-agent Paperclip API key for EACH agent so
            # actions in Paperclip's audit log attribute to the acting agent,
            # not to Friday or the global admin service account. Self-healing:
            # ensure_paperclip_api_key reads the stored key or mints + stores
            # one. Best-effort PER AGENT — one agent's mint failure (returns
            # None) is logged + skipped so the rest still get keys; if Paperclip
            # is unreachable the whole map is empty and the next reprovision
            # picks it up.
            if paperclip_agents:
                from core.services.paperclip_api_key import (
                    ensure_paperclip_api_key,
                )
                from core.services.paperclip_openclaw_pairing import (
                    resolve_company_id_for_owner,
                )

                paperclip_company_id = await resolve_company_id_for_owner(
                    owner_id=owner_id,
                    repo=repo,
                )
                for agent in paperclip_agents:
                    agent_id = agent.get("id")
                    if not agent_id:
                        continue
                    key = await ensure_paperclip_api_key(
                        owner_id=owner_id,
                        paperclip_agent_id=agent_id,
                        admin_client=admin_client,
                    )
                    if key:
                        paperclip_agent_keys[agent_id] = key
                    else:
                        logger.warning(
                            "write_user_configs: paperclip key mint skipped for owner=%s agent=%s",
                            owner_id,
                            agent_id,
                        )
        finally:
            await http.aclose()

        # Re-inject the owner's connected integration MCP servers (Stripe, …)
        # so this full openclaw.json overwrite doesn't drop them on a
        # reprovision. Best-effort: a Secrets-Manager hiccup while building the
        # blocks must not fail the whole provision — fall back to no integration
        # servers and let the next reconcile/reprovision self-heal.
        try:
            from core.services.credentials.mcp_server import (
                build_connected_integration_servers,
            )

            integration_mcp_servers = await build_connected_integration_servers(owner_id)
        except Exception:  # noqa: BLE001 — best-effort.
            logger.exception(
                "build_connected_integration_servers failed for %s; provisioning without them",
                owner_id,
            )
            integration_mcp_servers = {}

        # Carry the user's own on-disk MCP servers across this full overwrite
        # (the manual MCP panel writes them only into openclaw.json), merged with
        # the connected integration servers.
        integration_mcp_servers = await _provision_mcp_servers(owner_id, integration_mcp_servers)

        # Same staleness class as the MCP servers above: rebuild the owner's
        # provisioned provider env from Secrets Manager so this full overwrite
        # does not drop it.
        from core.services.provisioning_landing import build_provisioned_env

        provisioned_env = await build_provisioned_env(owner_id)

        # Carry the user's configured channel bot accounts across this full
        # overwrite. A reprovision rewrites openclaw.json wholesale, so without
        # this every Telegram/Slack/Discord bot token + pairing is dropped.
        channel_accounts, channel_bindings = await _provision_channel_accounts(owner_id)

        # The container's XDG dirs must be writable by the access-point user
        # (uid 1000) before anything in the container tries to use them. Done
        # here rather than only at first provision so it self-heals on every
        # config write — existing containers were created with a root-owned
        # XDG_CONFIG_HOME, which silently made the managed Chromium unable to
        # start at all (see Workspace.ensure_xdg_dirs).
        get_workspace().ensure_xdg_dirs(owner_id)

        # The image this config will run on, for fork-level-gated keys.
        if deployed_tag is None:
            deployed_tag = await self._deployed_image_tag(owner_id)

        config_path = Path(settings.EFS_MOUNT_PATH) / owner_id / "openclaw.json"
        await write_openclaw_config(
            config_path=config_path,
            gateway_token=gateway_token,
            provider_choice=provider_choice,
            user_id=owner_id,
            byo_provider=byo_provider,
            paperclip_agents=paperclip_agents,
            hook_token=hook_token,
            owner_type=owner_type,
            integration_mcp_servers=integration_mcp_servers,
            provisioned_env=provisioned_env,
            aip_arns=aip_arns,
            channel_accounts=channel_accounts,
            channel_bindings=channel_bindings,
            deployed_tag=deployed_tag,
        )

        # Seed the workspace role contract for each starter agent the
        # authoritative row names.
        from core.services.starter_roster import pre_stage_starter_roster
        from core.services.friday_identity import pre_stage_paperclip_identities

        if starter_agent_ids:
            try:
                await pre_stage_starter_roster(
                    owner_id=owner_id,
                    paperclip_agents=paperclip_agents,
                    starter_agent_ids=starter_agent_ids,
                    workspace=get_workspace(),
                )
            except Exception as exc:  # noqa: BLE001 — best-effort.
                logger.warning(
                    "write_user_configs: pre_stage_starter_roster failed for owner=%s: %s",
                    owner_id,
                    exc,
                )

        # Stage each agent's per-agent Paperclip identity file
        # (paperclip-claimed-api-key.json) into its own workspace so it can
        # authenticate to Paperclip AS ITSELF via the bundled `paperclip` skill.
        # Best-effort — failure here MUST NOT block provision; the next
        # reprovision re-stages (write-if-missing is idempotent).
        try:
            await pre_stage_paperclip_identities(
                owner_id=owner_id,
                paperclip_agents=paperclip_agents,
                agent_keys=paperclip_agent_keys,
                company_id=paperclip_company_id,
                api_url=settings.PAPERCLIP_INTERNAL_URL or None,
                workspace=get_workspace(),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort.
            put_metric("paperclip_identity.seed.error")
            logger.warning(
                "write_user_configs: pre_stage_paperclip_identities failed for owner=%s: %s",
                owner_id,
                exc,
            )

        # Stage the container-global credentials/oauth.json after the config
        # write, so it's present for any reprovision / config-rewrite path that
        # reaches write_user_configs WITHOUT the provision pre-task (debug
        # PATCH, the reapply script). For a fresh provision the pre-task already
        # wrote it; this overwrite is idempotent. One shared file authenticates
        # every agent (OpenClaw imports it per-agent on boot), so there is no
        # per-agent fan-out. Best-effort + loud: a transient failure self-heals
        # on the next reprovision rather than blocking the config write.
        if provider_choice == "chatgpt_oauth":
            from core.services.oauth_service import resolve_token_owner

            try:
                await self._pre_stage_oauth_for_user(
                    owner_id,
                    token_owner_id=await resolve_token_owner(owner_id=owner_id),
                )
            except Exception as exc:  # noqa: BLE001 — best-effort.
                put_metric("chatgpt_oauth.oauth_json_stage.error")
                logger.warning(
                    "write_user_configs: oauth.json stage failed for owner=%s: %s",
                    owner_id,
                    exc,
                )

        # NOTE: the per-agent Google-account instruction (`gog -a <email>`) is
        # deliberately NOT seeded here. That managed-block edit to IDENTITY.md
        # is user-action-driven — the assign API and the disconnect path own
        # it. IDENTITY.md is a workspace file that survives a config-rewrite
        # reprovision, so a seeded agent stays seeded with no provision-time
        # re-seed; an unassigned agent needs nothing.

        # Refresh the owner-facts file (plan/provider snapshot the bundled
        # lightbulb skill points agents at). The provider this config was JUST
        # rendered with rides along as an override: on the provider-switch
        # cold path this write can land right after the billing-row commit,
        # and an eventually-consistent row re-read could otherwise roll the
        # file back to the OLD provider. Internally best-effort — never
        # blocks the config write.
        from core.services.owner_facts import write_owner_facts

        await write_owner_facts(
            owner_id,
            account_overrides={"provider_choice": provider_choice, "byo_provider": byo_provider},
        )

        await self.ensure_device_identities(owner_id, gateway_token)

    async def ensure_device_identities(self, user_id: str, gateway_token: str) -> None:
        """Idempotently provision node + operator device identities.

        Separated from :meth:`write_user_configs` so callers can refresh the
        device trust store without touching openclaw.json — important for
        the redeploy path that patches openclaw.json in-place and just
        needs the operator entry backfilled after an OpenClaw 4.5 upgrade.

        What this writes:

        - `devices/.node-device-key.pem` (if not already on EFS)
        - `devices/paired.json` (locked merge of the latest node + managed
          operator entries, preserving desktop and other unrelated devices)
        - DynamoDB `containers` row `operator_device_id` + `operator_priv_key_enc`
          (latter is KMS-encrypted with encryption-context bound to owner_id)

        Safe to call repeatedly: reuses existing node PEM and existing
        KMS-encrypted operator seed when present. Only regenerates when the
        ciphertext is missing or undecryptable.
        """
        from core.crypto import kms_secrets
        from core.crypto.operator_device import (
            BACKEND_OPERATOR_SCOPES,
            build_paired_operator_entry,
            generate_operator_device,
            load_operator_device_from_seed,
        )

        workspace = get_workspace()

        # --- Node device identity -------------------------------------------
        # Reuse the existing key if the PEM is already on EFS (so the node's
        # in-container agent keeps the same identity across restarts),
        # otherwise generate fresh.
        try:
            existing_pem = workspace.read_file(user_id, "devices/.node-device-key.pem")
            node_identity = load_node_device_identity(existing_pem)
            logger.info("Reusing existing node device key for user %s", user_id)
        except Exception:
            node_identity = generate_node_device_identity()
            workspace.write_file(
                user_id,
                "devices/.node-device-key.pem",
                node_identity["private_key_pem"],
            )
            logger.info("Generated new node device key for user %s", user_id)

        # --- Operator device identity (OpenClaw 4.5 scoped-auth requirement) -
        # Bind the ciphertext to the owner_id via KMS encryption context — a
        # stolen row can't be replayed against a different container.
        kms_ctx = {"owner_id": user_id, "purpose": "operator-device-seed"}
        existing_container = await container_repo.get_by_owner_id(user_id)
        existing_enc_seed = existing_container.get("operator_priv_key_enc") if existing_container else None

        if existing_enc_seed:
            try:
                seed_bytes = kms_secrets.decrypt_bytes(existing_enc_seed, encryption_context=kms_ctx)
                operator_identity = load_operator_device_from_seed(seed_bytes)
                logger.info("Reusing existing operator device for user %s", user_id)
            except Exception as exc:
                # KMS decrypt failure or corrupted ciphertext — regenerate
                # rather than wedging the container. The old encrypted blob is
                # replaced below before the container boots, so the mismatch
                # never becomes visible to the gateway.
                logger.warning(
                    "Failed to decrypt existing operator device for user %s (%s); regenerating",
                    user_id,
                    exc,
                )
                operator_identity = generate_operator_device()
        else:
            operator_identity = generate_operator_device()
            logger.info("Generated new operator device for user %s", user_id)

        # Always re-encrypt and persist. This covers both "first provision"
        # and "we just regenerated after a decrypt failure" — the DynamoDB row
        # becomes the source of truth alongside the paired.json on EFS.
        enc_seed_b64 = kms_secrets.encrypt_bytes(operator_identity.private_key_seed, encryption_context=kms_ctx)
        await container_repo.update_fields(
            user_id,
            {
                "operator_device_id": operator_identity.device_id,
                "operator_priv_key_enc": enc_seed_b64,
            },
        )

        operator_entry = build_paired_operator_entry(
            operator_identity,
            gateway_token=gateway_token,
            scopes=BACKEND_OPERATOR_SCOPES,
        )

        # Pre-pair each Paperclip agent's device so its relayed operator connect
        # clears the remote-pairing wall, and bind each agent's adapterConfig
        # authToken to this container's gateway_token. Best-effort and enumerated
        # at provision so a reprovision's locked merge re-includes
        # them. Paperclip unavailability must never block container provision.
        paperclip_entries: list[dict] = []
        try:
            import httpx

            from core.repositories.paperclip_repo import PaperclipRepo
            from core.services.paperclip_admin_client import PaperclipAdminClient
            from core.services.paperclip_openclaw_pairing import (
                register_paperclip_agent_devices,
            )

            http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
            try:
                paperclip_entries = await register_paperclip_agent_devices(
                    owner_id=user_id,
                    gateway_token=gateway_token,
                    repo=PaperclipRepo(table_name="paperclip-companies"),
                    admin_client=PaperclipAdminClient(http_client=http),
                )
            finally:
                await http.aclose()
        except Exception as exc:  # noqa: BLE001 — pairing must not block provision
            logger.warning(
                "ensure_device_identities: Paperclip device pairing failed for owner=%s: %s",
                user_id,
                exc,
            )

        from core.services.desktop_pair import EfsWorkspaceAdapter

        device_ids = [
            node_identity["device_id"],
            operator_entry["deviceId"],
            *(entry["deviceId"] for entry in paperclip_entries),
        ]
        if len(device_ids) != len(set(device_ids)):
            raise RuntimeError("managed device trust identities collide")

        desired_entries = [
            _build_node_paired_entry(node_identity["device_id"], node_identity["public_key_b64"]),
            operator_entry,
            *paperclip_entries,
        ]
        old_operator_id = (existing_container or {}).get("operator_device_id")

        def _merge_managed_devices(paired: dict) -> bool:
            changed = False
            if old_operator_id and old_operator_id != operator_entry["deviceId"]:
                old_operator = paired.get(old_operator_id)
                if isinstance(old_operator, dict) and old_operator.get("displayName") == "lightbulb-backend":
                    paired.pop(old_operator_id)
                    changed = True
            for entry in desired_entries:
                device_id = entry["deviceId"]
                current = paired.get(device_id)
                if current is not None and (
                    not isinstance(current, dict) or current.get("publicKey") != entry["publicKey"]
                ):
                    raise RuntimeError("managed device trust identities collide")
                wanted = {**current, **entry} if isinstance(current, dict) else entry
                if isinstance(current, dict) and entry.get("role") == "node":
                    # A stable node key keeps its gateway-issued role token.
                    wanted["tokens"] = current.get("tokens", entry.get("tokens"))
                    wanted["createdAtMs"] = current.get("createdAtMs", entry.get("createdAtMs"))
                if current != wanted:
                    paired[device_id] = wanted
                    changed = True
            return changed

        adapter = EfsWorkspaceAdapter(
            owner_id=user_id,
            workspace=workspace,
            container_repo=container_repo,
            gateway_ws_url=settings.WS_GATEWAY_URL,
        )
        await adapter.mutate_paired_json(_merge_managed_devices)
