"""Hermes runtime adapter: the second agent-runtime behind the brand-blind port.

Implements the chat-transport (``send_chat`` + SSE→ChatEvent translation),
provisioner (``container_spec``/``write_config``/``write_auth``), identity
seeder (``seed_owner_agents``), and usage-metering facets. The WS-RPC-shaped
members (``sessions``/``call_rpc``/``revoke_provider``/``forward_hook``)
remain deferred stubs — Hermes speaks HTTP+SSE, not the OpenClaw WS protocol,
so those paths are Phase C, not part of the boot+chat spike.

Hermes' chat surface is a two-call HTTP shape, NOT inline streaming:
  1. ``POST /v1/runs`` -> 202 ``{"run_id", "status":"started"}`` — starts the run.
  2. ``GET /v1/runs/{run_id}/events`` -> Server-Sent Events (``data: {json}\\n\\n``,
     no ``id:`` framing, no resume — the server destroys the queue on disconnect).
  3. ``GET /v1/runs/{run_id}`` -> terminal status poll (the recovery path when the
     SSE stream drops mid-run: ``resumable_stream=False``, so re-attaching 404s).
  4. ``POST /v1/runs/{run_id}/approval`` -> answer an in-band approval prompt.
Every call carries ``Authorization: Bearer <API_SERVER_KEY>``; the host is
``http://{container_ip}:8642`` (plain HTTP, intra-VPC).

``_translate_event`` maps each Hermes SSE event onto the internal ChatEvent
shape the frontend already renders (the SAME shape OpenClaw's
``GatewayConnectionPool._transform_agent_event`` + chat-terminal forwarding
produce). That match is over the VISIBLE-CONTENT shape only: Hermes events do
NOT carry the ``runId``/``agent_id``/``session_id`` routing keys the frontend
uses for multi-bubble routing + per-session demux — the routing envelope keys
are added by the chat-routing layer at the ``on_event`` seam.

Import-lightness is load-bearing: registering Hermes means importing
``core.runtime`` imports this module, and the subprocess tripwire in the runtime
tests asserts ``core.containers`` (AWS clients at import) never leaks into that
path. Module-level imports stay stdlib/typing + ``core.runtime.base``; httpx and
``core.containers`` are imported INSIDE methods, lazily, exactly as the OpenClaw
adapter does for ``core.containers``.
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from core.runtime.base import (
    Capabilities,
    ChatEventCallback,
    ChatResult,
    ConfigProfile,
    ContainerNotRunningError,
    ContainerSpec,
    EfsMountLayout,
    Healthcheck,
    OwnerProfile,
    ProviderCreds,
    ProvisionCtx,
    SessionSurface,
)

logger = logging.getLogger(__name__)

# The H1.1-confirmed Hermes capability set. Hermes has no desktop-pairing /
# device exec-approval, no resumable stream, and no durable/structured/per-run
# run model; it does give each agent its own workspace and supports multiple
# agents. Divergence from OpenClaw's all-True set is intentional and verified.
HERMES_CAPABILITIES = Capabilities(
    desktop_pairing=False,
    device_exec_approval=False,
    resumable_stream=False,
    per_agent_workspace=True,
    multi_agent=True,
    durable_runs=False,
    structured_run_context=False,
    per_run_model=False,
)

# Hermes' API server listens on this fixed port inside the container (plain
# HTTP, intra-VPC). Mirrors OpenClaw's GATEWAY_PORT constant.
HERMES_API_PORT = 8642

# Hermes' CDK base task-def family (H1.3). ecs_manager clones this family for the
# per-user service, so the family — NOT this adapter — carries the Hermes image,
# container port 8642, and the static base env (API_SERVER_HOST=0.0.0.0,
# HERMES_SQLITE_JOURNAL_MODE=delete, HERMES_UID=1000). container_spec returns the
# family NAME, mirroring how OpenClaw returns isol8-{env}-openclaw.
_HERMES_BASE_TASK_DEF_FAMILY = "isol8-{env}-hermes"

# Per-user task size. Mirrors OpenClaw's PER_USER_CPU / PER_USER_MEMORY_MIB.
# NOT consumed by the launch path (the CDK base task-def owns cpu/memory) —
# returned only for parity/completeness with OpenClaw's ContainerSpec.
_HERMES_CPU = "512"
_HERMES_MEMORY_MIB = "2048"

# EFS POSIX ownership for the per-owner Hermes home. Hermes runs as UID 1000
# (HERMES_UID=1000 in the CDK base env), same as OpenClaw's node user, so its
# in-container process can read the config/auth files staged below.
_HERMES_EFS_UID = 1000
_HERMES_EFS_GID = 1000

# Timeouts. The SSE read timeout is deliberately generous but finite: a stream
# that idles past it raises ``httpx.ReadTimeout`` (an ``httpx.HTTPError``),
# which the send_chat loop treats as a dropped connection and recovers from via
# the terminal-status poll. The poll retries a bounded number of times.
_POST_TIMEOUT_S = 30.0
_SSE_READ_TIMEOUT_S = 120.0
_POLL_TIMEOUT_S = 30.0
_POLL_MAX_ATTEMPTS = 30
_POLL_INTERVAL_S = 1.0

# Terminal SSE events and their brand-neutral status label.
_TERMINAL_EVENT_STATUS = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}

# Terminal poll statuses (``GET /v1/runs/{id}`` ``status`` field) mapped back
# onto the SSE event name so the poll-recovery path reuses ``_translate_event``
# for its terminal ChatEvent — one translation table, not two.
_POLL_STATUS_TO_EVENT = {
    "completed": "run.completed",
    "failed": "run.failed",
    "cancelled": "run.cancelled",
}

# The single place the spike's auto-approve policy lives.
_AUTO_APPROVE_CHOICE = "always"


def _client_factory() -> "Any":  # returns httpx.AsyncClient
    """Build the HTTP client for Hermes calls. Module-level so tests can swap in
    a ``MockTransport``-backed client; httpx is imported lazily to keep the
    runtime package import-light (the tripwire constraint)."""
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(_POST_TIMEOUT_S))


def _translate_event(event: str, data: Mapping[str, Any]) -> dict | None:
    """Translate ONE Hermes SSE event into the internal ChatEvent shape.

    The output dicts match ``GatewayConnectionPool._transform_agent_event`` and
    the pool's chat-terminal forwarding EXACTLY (connection_pool.py:633-720 for
    chunk/thinking/tool, :1322/:1355/:1366 for done/error/aborted) — but only
    for the VISIBLE-CONTENT shape. These ChatEvents do NOT carry the
    ``runId``/``agent_id``/``session_id`` routing keys the frontend needs for
    multi-bubble routing + per-session demux; the routing envelope keys are
    added by the chat-routing layer at the ``on_event`` seam:

      - ``message.delta``       -> ``{type:"chunk", content}``
      - ``reasoning.available`` -> ``{type:"thinking", content}``
      - ``tool.started``        -> ``{type:"tool_start", tool}``
      - ``tool.completed``      -> ``{type:"tool_end", tool}`` (or ``tool_error``
                                   when the completion carries an ``error``, to
                                   mirror OpenClaw's isError→tool_error split)
      - ``run.completed``       -> ``{type:"done"}``
      - ``run.failed``          -> ``{type:"error", message}``
      - ``run.cancelled``       -> ``{type:"error", message:"Agent run was cancelled"}``

    Returns ``None`` for events with no UI counterpart (unknown events, and any
    known event whose required payload field is empty) — the caller skips them.
    ``approval.request`` is NOT handled here; it is answered in ``send_chat``
    (auto-approve) and emits nothing to the UI. This is deliberately NOT a
    filter over the stream: nothing renderable is dropped.
    """
    if event == "message.delta":
        delta = data.get("delta", "")
        return {"type": "chunk", "content": delta} if delta else None

    if event == "reasoning.available":
        text = data.get("text", "")
        return {"type": "thinking", "content": text} if text else None

    if event == "tool.started":
        tool = data.get("tool", "")
        return {"type": "tool_start", "tool": tool} if tool else None

    if event == "tool.completed":
        tool = data.get("tool", "")
        if not tool:
            return None
        # OpenClaw distinguishes tool_error from tool_end on the result's
        # isError flag; Hermes carries the same signal as a truthy ``error``.
        if data.get("error"):
            return {"type": "tool_error", "tool": tool}
        return {"type": "tool_end", "tool": tool}

    if event == "run.completed":
        # Usage lives on the same terminal frame; H2.5 reads it. This facet only
        # signals turn-end (matching OpenClaw's chat.final -> {type:"done"}).
        return {"type": "done"}

    if event == "run.failed":
        msg = data.get("error") or "Agent run failed"
        return {"type": "error", "message": str(msg)}

    if event == "run.cancelled":
        # Match OpenClaw's aborted shape verbatim (connection_pool.py:1366) so
        # the frontend's cancel handling is identical across runtimes.
        return {"type": "error", "message": "Agent run was cancelled"}

    # Unknown / non-renderable event: pass through silently (do NOT invent
    # filtering — an unrecognized event simply has no UI counterpart yet).
    return None


def _auto_approve_body(data: Mapping[str, Any]) -> dict:
    """Build the body that answers an ``approval.request`` prompt.

    An ``approval.request`` event BLOCKS the Hermes run in the executor until
    answered (choices: once/session/always/deny). The per-user container is the
    user's own sandbox — equivalent trust to OpenClaw's allowCommands — so the
    spike answers every prompt with the broadest grant (``always``) and surfaces
    nothing to the UI. If the request carries an identifier, echo it back so the
    executor can correlate the answer to the prompt. This is the ONE place the
    auto-approve policy lives; H4 revisits whether prompts should reach the UI.
    """
    body: dict[str, Any] = {"choice": _AUTO_APPROVE_CHOICE}
    for key in ("id", "approval_id", "request_id", "call_id"):
        if data.get(key):
            body[key] = data[key]
            break
    return body


async def _iter_sse(response: Any) -> AsyncIterator[tuple[str, dict]]:
    """Parse an httpx streaming ``response`` as Server-Sent Events.

    Yields ``(event_name, data)`` per frame. Hermes frames use ``event:`` +
    ``data:`` lines terminated by a blank line, with no ``id:`` framing. The
    event name comes from the ``event:`` field; if absent it falls back to an
    ``event``/``type`` key inside the JSON payload. Frames whose data is not a
    JSON object are skipped (nothing renderable to translate).
    """
    event_name = ""
    data_lines: list[str] = []

    def _flush() -> tuple[str, dict] | None:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        name = event_name or payload.get("event") or payload.get("type") or ""
        return str(name), payload

    async for line in response.aiter_lines():
        if line == "":
            frame = _flush()
            if frame is not None:
                yield frame
            event_name = ""
            data_lines = []
            continue
        if line.startswith(":"):  # SSE comment / heartbeat
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value.strip()
        elif field == "data":
            data_lines.append(value)

    # Flush a trailing frame not terminated by a blank line.
    frame = _flush()
    if frame is not None:
        yield frame


def _build_hermes_config(provider_choice: str, byo_provider: str | None) -> dict:
    """Build the MINIMAL Hermes ``config.yaml`` body for one owner's provider.

    Returns a plain dict (serialized to YAML by ``write_config``) carrying the one
    block a Hermes daemon needs to boot against the right model provider: a
    ``model`` mapping with the default model ref + provider name, plus a
    ``base_url`` when the provider needs an explicit endpoint. NO secrets are
    emitted — the per-container ``API_SERVER_KEY`` is an env/secret (H3) and
    provider creds are staged separately (``write_auth`` for OAuth, task-def
    secrets for BYO keys, IAM for Bedrock).

    Config-brick caution (an invalid ``config.yaml`` crash-loops the daemon at
    boot): this stays intentionally small and every branch emits a complete
    block. The model refs + the ``model.{default,provider,base_url}`` field names
    are the MINIMAL known-required shape — H4 must validate the exact Hermes
    ``config.yaml`` schema against a real boot and adjust field names / add any
    other required keys before this drives a live container.
    """
    if provider_choice == "chatgpt_oauth":
        # OAuth/Codex path: creds are staged to ~/.hermes/auth.json (write_auth),
        # never into config. Endpoint is the provider default.
        return {"model": {"default": "openai/gpt-5.5", "provider": "openai"}}
    if provider_choice == "byo_key":
        if byo_provider == "openai":
            return {"model": {"default": "openai/gpt-5.5", "provider": "openai"}}
        if byo_provider == "anthropic":
            return {"model": {"default": "anthropic/claude-sonnet-4-6", "provider": "anthropic"}}
        raise ValueError(f"byo_provider must be 'openai' or 'anthropic' for byo_key, got {byo_provider!r}")
    if provider_choice == "bedrock_claude":
        # Bedrock auth is IAM (the per-owner task role) — no creds file — but the
        # runtime endpoint is region-scoped, so it IS pinned in config.
        from core.config import settings

        return {
            "model": {
                "default": "anthropic.claude-sonnet-4-6",
                "provider": "bedrock",
                "base_url": f"https://bedrock-runtime.{settings.AWS_REGION}.amazonaws.com",
            }
        }
    raise ValueError(f"Unknown provider_choice: {provider_choice!r}")


def _write_efs_file(path: Path, content: str, *, mode: int) -> None:
    """Atomically write ``content`` to ``path`` on EFS, chowned to Hermes' UID.

    tmp + ``os.replace`` (crash-safe on POSIX, same filesystem) so a mid-write
    crash never leaves the Hermes daemon a half-written config/auth file; ``chmod``
    to ``mode`` then best-effort ``chown`` to 1000:1000 so the in-container Hermes
    process (UID 1000) can read it. Mirrors the write in
    ``workspace.pre_stage_auth_profile_store``. The chown is best-effort because
    the backend test/dev host can't chown to an arbitrary uid — on the real EFS
    mount (running as root) it succeeds.

    The tmp is created 0o600 up front (via ``os.open`` with an explicit mode,
    not the umask-default 0o644) so a secret file like auth.json — which holds
    live OAuth tokens — is never briefly group/world-readable in the window
    between create and ``chmod``. The ``chmod`` below then sets the final
    per-file mode (config.yaml widens back to 0o644, auth.json stays 0o600).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    try:
        os.chmod(str(tmp), mode)
    except OSError:
        pass
    try:
        os.chown(str(tmp), _HERMES_EFS_UID, _HERMES_EFS_GID)
    except (PermissionError, OSError):
        pass
    os.replace(str(tmp), str(path))
    try:
        os.chown(str(path.parent), _HERMES_EFS_UID, _HERMES_EFS_GID)
    except (PermissionError, OSError):
        pass


# ---------------------------------------------------------------------------
# IdentitySeeder facet: per-agent persona files for the per-run `instructions`
# model.
#
# Hermes has no on-disk IDENTITY/SOUL/USER bootstrap wizard like OpenClaw.
# Instead each agent's persona is a single plain-text blob passed as
# ``instructions`` on ``POST /v1/runs``. We persist that blob per agent to the
# owner's Hermes home on EFS (``~/.hermes/personas/{agent_id}.txt`` in-container)
# so ``send_chat`` reads it at run time. The persona INTENT — Friday = CEO /
# orchestrator, Atlas = in-house recruiter — is REUSED from
# ``friday_identity`` / ``atlas_seed``, but the TEXT is RE-AUTHORED for Hermes:
# OpenClaw's SOUL/IDENTITY/AGENTS markdown is steeped in OpenClaw mechanics (EFS
# files, BOOTSTRAP.md, the curl-based ``paperclip`` skill, heartbeat wakes) that
# do not apply to a Hermes ``/v1/runs`` agent, so copying it verbatim would ship
# false capability claims. Only the role/vibe/boundary intent carries over.
# ---------------------------------------------------------------------------

# Subdir under the owner's Hermes home holding one persona file per agent.
_PERSONA_DIRNAME = "personas"

# One compact provider note per raw provider_choice. This is Hermes' own
# rendering of the raw fact (OpenClaw formats its own ``provider_label``); an
# unknown choice or missing facts contributes nothing.
_PROVIDER_NOTE = {
    "chatgpt_oauth": " You run on the owner's own ChatGPT (OpenAI) account.",
    "bedrock_claude": " You run on Claude via Isol8-managed Bedrock credits.",
    "byo_key": " You run on the owner's own provider API key.",
}

_FRIDAY_PERSONA = """\
You are Friday, the CEO of {owner}'s AI company on Isol8.

You are not a chatbot — you are the entry point and the orchestrator. You break
work into pieces, delegate to the right specialists, coordinate them, and triage
what comes back; as the team grows you stay the CEO rather than doing every task
yourself. You report to {owner}, the human this company exists for.

Your team right now: {roster}. Never assume an agent exists — delegate from the
live roster, and when no one fits, get the right specialist hired.

Tone: sharp, warm, and opinionated — concise when it matters, never a corporate
drone. Be genuinely helpful, not performatively helpful: skip "Great question!"
and just help. Have real opinions. Be resourceful before asking — try to figure
it out, then ask if you are stuck. You have access to {owner}'s work; earn that
trust through competence and treat it with a guest's respect.

Boundaries: private things stay private; ask before acting externally (emails,
anything public); never send half-baked replies; you are not {owner}'s voice in
group settings.{provider}"""

_ATLAS_PERSONA = """\
You are Atlas, the in-house agent-recruiter for {owner}'s AI company on Isol8.

When the team lacks a specialist for a piece of work, you design and scaffold a
new one — you architect the role, its responsibilities, and the tools it needs,
then hand it back to the team. Friday, the CEO, delegates hiring to you and you
report back to her.

Your teammates right now: {roster}.

Tone: sharp, practical, and honest about tradeoffs. Design specialists that
actually do the job asked of them — no aspirational capability claims. Be
resourceful, ask when a requirement is genuinely ambiguous, and keep {owner}
in the loop on what you are building.{provider}"""


def _persona_owner_name(facts: "Any | None", owner_type: str) -> str:
    """The owner display used in a persona. Prefers the shared facts; degrades to
    a brand-blind fallback keyed on owner kind when facts could not be resolved."""
    if facts is not None:
        return facts.display_name
    return "your team" if owner_type == "org" else "the owner"


def _persona_roster_line(agents: "list[Mapping[str, Any]]") -> str:
    """Render the roster as "Name (role), Name (role)" for a persona blurb.

    ``general`` is the shared marketplace/hire role, so it is not a useful
    qualifier; a name that equals its role is not doubled. Empty roster reads
    "just you for now"."""
    parts: list[str] = []
    for agent in agents:
        name = agent.get("name") or agent.get("role") or "an agent"
        role = agent.get("role")
        if role and role != "general" and str(role).lower() != str(name).lower():
            parts.append(f"{name} ({role})")
        else:
            parts.append(str(name))
    return ", ".join(parts) if parts else "just you for now"


def _build_persona(kind: str, *, facts: "Any | None", owner_type: str, agents: "list[Mapping[str, Any]]") -> str:
    """Build ONE agent's persona text (``friday`` or ``atlas``) from the shared
    facts + roster. Pure — no I/O — so it is unit-testable in isolation."""
    template = _FRIDAY_PERSONA if kind == "friday" else _ATLAS_PERSONA
    return template.format(
        owner=_persona_owner_name(facts, owner_type),
        roster=_persona_roster_line(agents),
        provider="" if facts is None else _PROVIDER_NOTE.get(facts.provider_choice, ""),
    )


def _persona_path(owner_id: str, agent_id: str) -> Path:
    """EFS path to ONE agent's persona file (``~/.hermes/personas/{id}.txt``).

    Lazy settings import keeps the module import-light (the tripwire constraint)."""
    from core.config import settings

    return Path(settings.EFS_MOUNT_PATH) / owner_id / _PERSONA_DIRNAME / f"{agent_id}.txt"


def _read_persona(owner_id: str, agent_id: str) -> str | None:
    """Read an agent's persona blob, or ``None`` when unseeded/unreadable.

    Best-effort: a missing file (agent seeded on an older container, or a
    non-seed agent) or any read error yields ``None`` so ``send_chat`` simply
    sends no ``instructions`` rather than failing the turn."""
    try:
        text = _persona_path(owner_id, agent_id).read_text()
    except OSError:
        return None
    text = text.strip()
    return text or None


def _merge_instructions(persona: str | None, context: str | None) -> str | None:
    """Merge an agent's seeded ``persona`` with the caller's per-run ``context``
    into the single ``instructions`` blob for ``POST /v1/runs``.

    INVARIANT — persona FIRST, then context. The persona defines WHO the agent
    is (identity/role/boundaries); the per-run context (Paperclip issue text,
    etc.) is supplementary and must NEVER replace it. Either side may be absent:
    persona-only (a plain webchat turn with no issue context), context-only (an
    unseeded agent), or neither (``None`` → the run carries no instructions).
    Blank/whitespace-only parts are dropped so the merge never emits a stray
    separator.
    """
    parts = [p for p in (persona, context) if p and p.strip()]
    if not parts:
        return None
    return "\n\n".join(part.strip() for part in parts)


# ---------------------------------------------------------------------------
# UsageMeter facet: feed run.completed usage into the platform counters.
#
# THE BILLING TRAP. Hermes' ``run.completed.usage`` reports an ``input_tokens``
# that is CACHE-INCLUSIVE (the full prompt, incl. cached tokens). Metering that
# as uncached input over-bills cache-heavy sessions (cache tokens can be ~99% of
# a long turn). The H1.2 fork patch adds ``uncached_input_tokens``
# (= ``input_tokens - cache_read``) plus ``cache_read_tokens`` /
# ``cache_write_tokens`` so we can meter the NON-cache-inclusive figure as input
# and the cache counts separately — the shape ``record_usage`` already expects
# from OpenClaw's ``sessions.list``. We NEVER meter the raw ``input_tokens``.
# ---------------------------------------------------------------------------


def _resolve_uncached_input(usage: Mapping[str, Any]) -> tuple[int, int, int, bool]:
    """Resolve ``(input_tokens, cache_read, cache_write, patched)`` from a Hermes
    ``run.completed`` usage dict, applying the billing-trap handling.

    ``input_tokens`` here is the NON-cache-inclusive figure to meter — never the
    raw cache-inclusive ``input_tokens`` Hermes reports. Precedence:

      1. Fork patch present (``uncached_input_tokens`` set) → use it verbatim.
         ``patched=True``.
      2. Patch field absent but ``cache_read_tokens`` present → reconstruct
         ``max(0, input_tokens - cache_read)`` ourselves. ``patched=False``.
      3. No cache fields at all (unpatched image / older payload) → best-effort
         fall back to the raw ``input_tokens``. ``patched=False``.

    ``patched=False`` signals the caller to log that the fork patch fields were
    absent so H4 catches an unpatched image on a live boot.
    """
    cache_read = int(usage.get("cache_read_tokens", 0) or 0)
    cache_write = int(usage.get("cache_write_tokens", 0) or 0)
    if usage.get("uncached_input_tokens") is not None:
        return int(usage["uncached_input_tokens"]), cache_read, cache_write, True
    raw_input = int(usage.get("input_tokens", 0) or 0)
    if usage.get("cache_read_tokens") is not None:
        return max(0, raw_input - cache_read), cache_read, cache_write, False
    return raw_input, cache_read, cache_write, False


def _member_from_session_key(session_key: str, owner_id: str) -> str:
    """Best-effort member (Clerk user) attribution from the session key, else the
    owner. Mirrors OpenClaw's resolve-then-fallback-to-owner
    (``connection_pool._resolve_member_from_session``) without the channel-link
    lookups the Hermes routing bridge (H3) will own: the session key is
    ``agent:{agent_id}:{member}[:...]`` and a real member is a Clerk ``user_`` id;
    ``main`` (personal webchat) and anything else attribute to the owner."""
    parts = session_key.split(":")
    if len(parts) >= 3 and parts[0] == "agent" and parts[2].startswith("user_"):
        return parts[2]
    return owner_id


class HermesRuntime:
    kind = "hermes"
    capabilities = HERMES_CAPABILITIES

    @property
    def sessions(self) -> SessionSurface:
        raise NotImplementedError("HermesRuntime.sessions is Phase C")

    async def _resolve(self, owner_id: str) -> tuple[str, str]:
        """Resolve the owner's running container to ``(ip, api_server_key)``.

        Mirrors ``OpenClawRuntime._resolve``: the ECS manager discovers the task
        IP, and the auth material rides the container row. Hermes authenticates
        every call with a per-container ``API_SERVER_KEY``.

        NOTE (H3): the container-row field name for that key is provisioned by
        H3; this reads ``container["api_server_key"]`` as the expected field. If
        H3 lands it under a different key, update this single accessor.
        """
        # Lazy import: core.containers builds AWS clients at import. Same
        # import-lightness reason as the OpenClaw adapter's _get_ecs.
        from core.containers import get_ecs_manager

        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
        key = container.get("api_server_key") if container else None
        if not container or not ip or not key:
            # A container row without an API server key is as unreachable as a
            # cold container — no HTTP call is attempted either way.
            raise ContainerNotRunningError(f"no running container or api_server_key for {owner_id}")
        return ip, key

    async def send_chat(
        self,
        owner_id: str,
        agent_id: str,
        message: str,
        session_key: str,
        on_event: ChatEventCallback,
        context: str | None = None,
    ) -> ChatResult | None:
        """Drive ONE user turn against the owner's Hermes container.

        Starts the run (``POST /v1/runs``), opens the SSE stream, translates each
        event into the internal ChatEvent shape and awaits ``on_event`` per
        event. In-band ``approval.request`` prompts are auto-answered inline so
        the run never blocks. Returns the terminal ``ChatResult`` once the run
        completes/fails/cancels.

        Recovery: Hermes streams are NOT resumable, so if the SSE connection
        drops before a terminal event (network error or read-timeout), this does
        NOT re-attach (a second events GET 404s). It falls back to polling
        ``GET /v1/runs/{run_id}`` until the run is terminal and emits the
        terminal ChatEvent from that. Intermediate deltas are lost on a drop;
        the final answer is not.

        ``session_key`` (``agent:{agent_id}:{user_id}[:{session_id}]``) already
        encodes the agent, so it is passed straight through as Hermes'
        ``session_id`` to scope the run's conversation. Hermes DROPS ``metadata``
        on the run body — any Paperclip/context must ride ``input``/``instructions``
        text.

        Persona + per-run context: the agent's seeded persona
        (``seed_owner_agents`` wrote it to ``personas/{agent_id}.txt``) is read
        here and MERGED with the caller's optional per-run ``context`` (Paperclip
        issue text, etc.) into the ``instructions`` blob — persona FIRST, then
        context, never replacing the persona (``_merge_instructions``). The chat
        bridge owns the context; a plain webchat turn passes none, so the run
        boots on the persona alone.

        ``X-Hermes-Session-Key``: the run also carries the session key as an HTTP
        header (in addition to the body ``session_id``) because the container's
        ``gateway.session_context`` bag that tools read is populated from the
        header, not the body field.
        """
        import httpx

        ip, key = await self._resolve(owner_id)
        base = f"http://{ip}:{HERMES_API_PORT}"
        headers = {
            "Authorization": f"Bearer {key}",
            # Populates the container's gateway.session_context bag (header-fed,
            # not body-fed). Rides every run-scoped call on this headers dict.
            "X-Hermes-Session-Key": session_key,
        }
        # Hermes reads ``input`` (the user turn) + ``session_id`` (conversation
        # scope). ``metadata`` is dropped server-side, so nothing rides it.
        run_body: dict[str, Any] = {"input": message, "session_id": session_key}
        # Boot the agent in-character with its seeded persona, merged persona-
        # first with any per-run context. Best-effort: an unseeded agent with no
        # context simply runs without instructions.
        instructions = _merge_instructions(_read_persona(owner_id, agent_id), context)
        if instructions:
            run_body["instructions"] = instructions

        async with _client_factory() as client:
            resp = await client.post(f"{base}/v1/runs", json=run_body, headers=headers)
            resp.raise_for_status()
            run_id = resp.json()["run_id"]

            saw_terminal = False
            try:
                async with client.stream(
                    "GET",
                    f"{base}/v1/runs/{run_id}/events",
                    headers=headers,
                    timeout=httpx.Timeout(_POST_TIMEOUT_S, read=_SSE_READ_TIMEOUT_S),
                ) as stream:
                    async for event, data in _iter_sse(stream):
                        if event == "approval.request":
                            # Answer inline so the executor unblocks; emit nothing.
                            await client.post(
                                f"{base}/v1/runs/{run_id}/approval",
                                json=_auto_approve_body(data),
                                headers=headers,
                            )
                            continue
                        chat_event = _translate_event(event, data)
                        if chat_event is not None:
                            await on_event(chat_event)
                        if event in _TERMINAL_EVENT_STATUS:
                            # Terminal branch returns inline below, so the
                            # post-loop ``if not saw_terminal`` only runs when the
                            # stream ends WITHOUT a terminal event — no flag flip
                            # is needed here.
                            if event == "run.completed":
                                # Meter usage AFTER the {type:"done"} event above
                                # so the user is never blocked on the counter
                                # write. Best-effort — never breaks the turn.
                                await self._record_usage_from_run(owner_id, session_key, data)
                            return ChatResult(
                                run_id=run_id,
                                status=_TERMINAL_EVENT_STATUS[event],
                                output=data.get("output"),
                            )
            except httpx.HTTPError as exc:
                # Dropped stream mid-run — fall through to the poll recovery.
                logger.warning(
                    "hermes SSE stream dropped for owner=%s run=%s: %s — polling for terminal",
                    owner_id,
                    run_id,
                    exc,
                )

            if not saw_terminal:
                # Stream ended (cleanly or via a caught drop) without a terminal
                # event. Poll the run to resolve its final state.
                return await self._poll_terminal(client, base, run_id, headers, on_event)

        return None

    async def _record_usage_from_run(self, owner_id: str, session_key: str, data: Mapping[str, Any]) -> None:
        """Meter one ``run.completed`` frame's usage into the platform counters.

        Mirrors OpenClaw's usage path (``connection_pool._fetch_and_record_usage``)
        so the DynamoDB counters stay consistent across runtimes: same
        ``record_usage`` sink, same ``input/output/cache_read/cache_write`` shape,
        same owner + per-member attribution. This is ANALYTICS, not the biller —
        for a Bedrock owner the authoritative bill is the AWS-invocation meter
        Lambda; for OAuth/BYO owners it is the owner's own vendor cost recorded at
        spend 0. Either way the token counts must be right.

        Billing trap: ``_resolve_uncached_input`` meters the NON-cache-inclusive
        input (never Hermes' raw cache-inclusive ``input_tokens``); when the H1.2
        fork patch fields are absent it reconstructs / falls back and we log a
        debug so H4 catches an unpatched image.

        Best-effort throughout: wrapped so a usage-recording failure (DDB outage,
        pricing miss, anything) NEVER breaks the chat turn — the ``{type:"done"}``
        was already emitted and the terminal ``ChatResult`` still returns.
        """
        try:
            usage = data.get("usage")
            if not isinstance(usage, Mapping):
                return  # no usage on the frame — nothing to meter
            input_tokens, cache_read, cache_write, patched = _resolve_uncached_input(usage)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            if not patched:
                logger.debug(
                    "hermes usage: fork-patch uncached_input_tokens absent for owner=%s "
                    "(unpatched image?) — metered input=%d reconstructed from "
                    "input_tokens/cache_read_tokens",
                    owner_id,
                    input_tokens,
                )
            # Model rides the terminal frame (or its usage block); fall back to
            # "unknown" like OpenClaw's sessions.list path. No raw_cost override —
            # Hermes reports no estimatedCostUsd, and Bedrock is billed at the AWS
            # invocation level, so record_usage prices from tokens (analytics).
            model = data.get("model") or usage.get("model") or "unknown"
            member_id = _member_from_session_key(session_key, owner_id)

            # Lazy import: keep the module import-light (the tripwire constraint),
            # same posture as core.containers elsewhere in this adapter.
            from core.services.usage_service import record_usage

            await record_usage(
                owner_id=owner_id,
                user_id=member_id,
                model=str(model),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read=cache_read,
                cache_write=cache_write,
            )
        except Exception:  # noqa: BLE001 — best-effort; a usage failure must not break the turn.
            logger.exception("hermes usage: failed to record run.completed usage for owner=%s", owner_id)

    async def _poll_terminal(
        self,
        client: Any,
        base: str,
        run_id: str,
        headers: dict[str, str],
        on_event: ChatEventCallback,
    ) -> ChatResult:
        """Poll ``GET /v1/runs/{run_id}`` until terminal, then emit + return it.

        The recovery path for a dropped SSE stream (Hermes runs are not
        resumable). Emits the terminal ChatEvent (reusing ``_translate_event``
        via ``_POLL_STATUS_TO_EVENT``) exactly once. If the run never reaches a
        terminal state within the retry budget, emits a generic error ChatEvent
        so the UI never hangs.
        """
        import httpx

        for _ in range(_POLL_MAX_ATTEMPTS):
            try:
                resp = await client.get(
                    f"{base}/v1/runs/{run_id}",
                    headers=headers,
                    timeout=httpx.Timeout(_POLL_TIMEOUT_S),
                )
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPError as exc:
                # A single poll attempt failing must not escape send_chat with no
                # terminal event: GET /v1/runs/{id} 404s once its ~3600s status
                # TTL sweeps the run (or on an unknown run_id), and transport
                # errors are transient. Retry within the existing bound; if every
                # attempt errors the loop exhausts and falls through to the same
                # generic terminal error the budget-exhaustion path emits.
                logger.warning(
                    "hermes poll attempt failed for run=%s: %s — retrying",
                    run_id,
                    exc,
                )
                await asyncio.sleep(_POLL_INTERVAL_S)
                continue
            status = body.get("status")
            event_name = _POLL_STATUS_TO_EVENT.get(status)
            if event_name is not None:
                chat_event = _translate_event(event_name, body)
                if chat_event is not None:
                    await on_event(chat_event)
                return ChatResult(run_id=run_id, status=status, output=body.get("output"))
            await asyncio.sleep(_POLL_INTERVAL_S)

        # Never went terminal within budget — surface an error so the UI settles.
        await on_event({"type": "error", "message": "run did not reach a terminal state"})
        return ChatResult(run_id=run_id, status="unknown", output=None)

    async def call_rpc(
        self,
        owner_id: str,
        method: str,
        params: dict | None = None,
        *,
        fresh: bool = False,
        timeout: float | None = None,
        open_timeout: float | None = None,
        recv_timeout: float | None = None,
        rpc_timeout: float | None = None,
    ) -> Any:
        raise NotImplementedError("HermesRuntime.call_rpc is Phase C")

    async def revoke_provider(self, owner_id: str, *, provider: str) -> bool:
        raise NotImplementedError("HermesRuntime.revoke_provider is Phase C")

    async def forward_hook(
        self, owner_id: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        raise NotImplementedError("HermesRuntime.forward_hook is Phase C")

    # --- Provisioner facet: launching / configuring a container -----------

    def container_spec(self, owner_id: str, ctx: ProvisionCtx) -> ContainerSpec:
        """Describe the container to launch for ``owner_id``, brand-blind.

        Facet-consumed vs CDK-owned:
          - ``image`` is the CDK base task-def FAMILY name (``isol8-{env}-hermes``,
            H1.3) the per-user clone inherits its Hermes image + port 8642 + static
            base env from — NOT an image ref this facet injects. Mirrors OpenClaw
            returning ``isol8-{env}-openclaw`` (same ``settings.ENVIRONMENT``
            resolution).
          - ``healthcheck`` (TCP on 8642) IS facet-consumed: ``is_healthy``
            TCP-probes it once H3 threads the owner through the provisioning path.
          - ``efs_mount`` describes the per-owner Hermes home on EFS (the access
            point chroots ``/users/{owner_id}`` to ``~/.hermes`` in-container),
            used to create the mount.
          - ``container_port`` / ``cpu`` / ``memory_mib`` are returned for parity
            with OpenClaw's spec but are NOT consumed by the launch path — the CDK
            base task-def owns them.
          - ``env`` is intentionally EMPTY: Hermes' per-container
            ``API_SERVER_KEY`` is a SECRET injected by H3's provisioning (not
            here), and the static base env (``API_SERVER_HOST`` /
            ``HERMES_SQLITE_JOURNAL_MODE`` / ``HERMES_UID``) lives on the CDK base
            task-def. ``ctx.hook_token`` is therefore not surfaced here.
        """
        # Lazy: core.config is not stdlib and the module keeps its top-level
        # imports to stdlib/typing + core.runtime.base (the tripwire constraint).
        from core.config import settings

        base_family = _HERMES_BASE_TASK_DEF_FAMILY.format(env=settings.ENVIRONMENT or "dev")
        return ContainerSpec(
            image=base_family,
            container_port=HERMES_API_PORT,
            cpu=_HERMES_CPU,
            memory_mib=_HERMES_MEMORY_MIB,
            env={},
            healthcheck=Healthcheck(kind="tcp", port=HERMES_API_PORT, timeout_s=5.0),
            efs_mount=EfsMountLayout(
                mount_root=settings.EFS_MOUNT_PATH,
                owner_subpath=f"/users/{owner_id}",
                posix_uid=_HERMES_EFS_UID,
                posix_gid=_HERMES_EFS_GID,
                root_permissions="0755",
            ),
        )

    async def write_config(self, owner_id: str, profile: ConfigProfile) -> None:
        """Serialize ``profile`` into the owner's Hermes ``config.yaml`` on EFS.

        Writes the minimal model/provider block (keyed to ``profile.provider_choice``)
        to ``Path(EFS_MOUNT_PATH)/{owner_id}/config.yaml`` — chrooted to
        ``~/.hermes/config.yaml`` in-container. NO secrets: ``API_SERVER_KEY`` is
        env/secret (H3), provider creds ride ``write_auth`` / task-def secrets /
        IAM. ``container_auth_token`` is NOT consumed here — Hermes has no shared
        gateway token; it authenticates per-container with ``API_SERVER_KEY``.

        Config-brick caution: emits well-formed YAML with a complete provider
        block; the exact schema + model-ref field names are H4-verified against a
        real boot (see ``_build_hermes_config``).
        """
        # Lazy: yaml is third-party (mirror the httpx deferral); core.config is
        # not stdlib. Keeps the module import-light for the tripwire.
        import yaml

        from core.config import settings

        config = _build_hermes_config(profile.provider_choice, profile.byo_provider)
        body = yaml.safe_dump(config, default_flow_style=False, sort_keys=True)
        config_path = Path(settings.EFS_MOUNT_PATH) / owner_id / "config.yaml"
        _write_efs_file(config_path, body, mode=0o644)

    async def write_auth(self, owner_id: str, creds: ProviderCreds) -> None:
        """Stage ``creds`` onto the owner's Hermes home before launch.

        Like OpenClaw, only OAuth material is staged to the container filesystem —
        Hermes' OAuth (Codex-lineage) path reads ``~/.hermes/auth.json``. BYO keys
        ride task-def secrets and Bedrock uses IAM, so neither is a ``write_auth``.
        Writes to ``Path(EFS_MOUNT_PATH)/{owner_id}/auth.json`` (chrooted to
        ``~/.hermes/auth.json``), owner-only (0600) since it holds live tokens.

        The auth.json SHAPE (Codex-lineage ``{OPENAI_API_KEY, tokens{access_token,
        refresh_token, …}}``) is the MINIMAL documented shape; H4 must confirm the
        exact field names Hermes expects against a real OAuth boot.
        """
        if creds.kind != "oauth":
            raise ValueError(
                "Hermes write_auth stages only 'oauth' credentials to the container "
                f"filesystem; got kind={creds.kind!r} (BYO keys ride task-def secrets, "
                "Bedrock uses IAM)"
            )
        # Lazy: core.config is not stdlib — keep the module import-light.
        from core.config import settings

        secrets = dict(creds.secrets)
        required = {"access_token", "refresh_token"}
        missing = required - secrets.keys()
        if missing:
            raise ValueError(f"oauth creds missing required keys: {sorted(missing)}")

        tokens: dict[str, Any] = {
            "access_token": secrets["access_token"],
            "refresh_token": secrets["refresh_token"],
        }
        for opt in ("id_token", "account_id"):
            if secrets.get(opt):
                tokens[opt] = secrets[opt]
        if secrets.get("expires_at") is not None:
            tokens["expires_at"] = int(secrets["expires_at"])

        auth = {"OPENAI_API_KEY": None, "tokens": tokens}
        auth_path = Path(settings.EFS_MOUNT_PATH) / owner_id / "auth.json"
        _write_efs_file(auth_path, json.dumps(auth, indent=2), mode=0o600)

    # --- IdentitySeeder facet: seeding an owner's agents ------------------

    async def seed_owner_agents(self, owner_id: str, profile: OwnerProfile) -> None:
        """Seed Friday's + Atlas's persona onto the owner's Hermes home on EFS.

        Hermes carries an agent's persona as the per-run ``instructions`` blob
        (there is no on-disk IDENTITY/SOUL/USER wizard), so this writes one
        ``personas/{agent_id}.txt`` per seed agent from the SHARED
        ``profile.facts`` (resolved once at the call site — no Clerk/billing
        re-lookup here) + the Paperclip roster. ``send_chat`` reads that file and
        passes it as ``instructions`` on ``POST /v1/runs``.

        Scope mirrors the OpenClaw seeder: only the Friday (``role == "ceo"``) and
        Atlas (``name == "Atlas"``) seed pair get personas; other roster agents
        (marketplace hires) are left for a later task. Files are written
        atomically (tmp + replace) and overwrite on reprovision — the persona is
        deterministic from the current facts, and Hermes has no in-container
        persona-evolution path to preserve (unlike OpenClaw's write-if-missing).

        Best-effort throughout: an empty roster defers to the next provision, and
        a per-agent write failure is logged + metered but never raises — a seeder
        failure MUST NOT block provisioning (the agent boots with no instructions).
        """
        from core.observability.metrics import put_metric

        agents = list(profile.agents)
        if not agents:
            put_metric("hermes.persona.seed.skipped")
            logger.info(
                "hermes seed_owner_agents: empty roster for owner=%s — deferring to next provision",
                owner_id,
            )
            return

        written = 0
        for agent in agents:
            agent_id = agent.get("id")
            if not agent_id:
                continue
            if agent.get("role") == "ceo":
                kind = "friday"
            elif agent.get("name") == "Atlas":
                kind = "atlas"
            else:
                continue  # spike seeds only the Friday/Atlas seed pair
            try:
                text = _build_persona(
                    kind,
                    facts=profile.facts,
                    owner_type=profile.owner_type,
                    agents=agents,
                )
                _write_efs_file(_persona_path(owner_id, agent_id), text, mode=0o644)
                written += 1
            except Exception as exc:  # noqa: BLE001 — best-effort, must not block provision.
                put_metric("hermes.persona.seed.error")
                logger.warning(
                    "hermes seed_owner_agents: persona write failed owner=%s agent=%s: %s",
                    owner_id,
                    agent_id,
                    exc,
                )

        if written:
            put_metric("hermes.persona.seed.success", value=written)
        logger.info("hermes seed_owner_agents: owner=%s wrote=%d personas", owner_id, written)
