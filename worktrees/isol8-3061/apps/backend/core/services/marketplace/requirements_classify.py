"""Classify detected hits into buyer-facing requirement dispositions.

Each detected tool/env/host collapses to a canonical provider/tool and is
classified against the platform's integration rail:

  - bundled image tool/skill, or keyless          -> INCLUDED
  - registry provider (providers.yaml) connectable -> CONNECT
  - channel tool (slack/discord/...)               -> CONNECT (via Channels)
  - byo-required provider (Google)                 -> BYO (your own OAuth app)
  - registry provider gated off this environment   -> UNAVAILABLE (request access)
  - referenced tool/skill with no implementation   -> UNAVAILABLE (phantom)
  - registry provider but wiring mismatch          -> BYO + reason
  - anything else needing a key                    -> BYO
"""

from __future__ import annotations

from core.constants import CHANNEL_PROVIDER_IDS
from core.services.marketplace.requirements import Disposition, Requirement
from core.services.marketplace.requirements_detect import Hit, identify_service

# Core tools + plugins baked into the image. OpenClaw exposes no per-tool
# requirements registry for these, so we maintain the list (verified against the
# extended image's bundled set + Atlas seed).
BUNDLED_TOOLS = frozenset(
    {
        "summarize",
        "memory-core",
        "message",
        "capability-evolver",
    }
)
# Lobster engine internals + a fictional skill — never buyer-facing dependencies.
# `state` (durable-KV) and `llm-task` are pipeline DSL the runtime ships; `lobster`
# is the runner itself; `fast-io` is a non-functional skill that was scaffolded
# but never wired. They tell a buyer nothing, so they're dropped from the ledger
# entirely rather than listed as "bundled tools."
_SUPPRESS_TOOLS = frozenset({"state", "llm-task", "lobster", "fast-io"})
# The backend-provisionable channel providers (Channels tab). Imported from the
# single source of truth so this can't drift — e.g. listing whatsapp here would
# tell a buyer to "connect" a channel the backend never provisions.
CHANNELS = frozenset(CHANNEL_PROVIDER_IDS)

_BYO_APP_REASON = "requires your own Google OAuth app"
_GATED_REASON = "not available on this environment yet — request access in the Integrations tab"


def _provider_disposition(prov) -> tuple[Disposition, str | None]:
    """How a registry provider is offered to a buyer: gated off this environment
    → UNAVAILABLE (they can only request it), byo-required → BYO (bring your own
    app), otherwise a normal connectable integration.

    The gate check is the ENV half only — this ledger is built once per listing
    version with no buyer in hand, so it cannot consult owner_allowlist. That
    errs toward disclosure: UNAVAILABLE rows are informational and never block a
    hire, whereas a CONNECT row for something the buyer cannot reach sends them
    to a tab with no Connect button on it.
    """
    from core.services.credentials import registry

    if not registry.visible_in_env(prov):
        return Disposition.UNAVAILABLE, _GATED_REASON
    if prov.byo_required:
        return Disposition.BYO, _BYO_APP_REASON
    return Disposition.CONNECT, None


def _providers() -> dict:
    from core.services.credentials import registry

    return registry.all_providers()  # {key: Provider}


def _kind_for_provider(prov) -> str:
    # ``actor`` is the catalog's declared rail. Wiring shape is NOT the
    # signal: hosted-OAuth MCP providers (stripe/posthog/…) wire via the
    # ``mcp_oauth`` materializer with the server under top-level
    # ``mcp_remote``, and github carries an ``mcp_remote`` block while
    # being a CLI integration.
    if prov.actor == "mcp":
        return "mcp"
    if prov.actor == "cli":
        return "cli"
    return "skill"


def _provider_for_tool(name: str):
    """Return ``(key, Provider)`` if a registry provider owns this tool name —
    by provider key, its MCP server key (stdio or hosted remote), OR a CLI
    binary it declares (``requires.bins``, e.g. ``gh`` → github, so a
    ``gh issue list`` pipeline maps to the connectable github provider, not a
    phantom)."""
    for key, prov in _providers().items():
        mcp = prov.wiring.mcp_server
        remote = prov.mcp_remote
        bins = getattr(prov.requires, "bins", None) or []
        if key == name or (mcp and mcp.key == name) or (remote and remote.key == name) or name in bins:
            return key, prov
    return None


def classify_hits(
    hits: list[Hit],
    *,
    bundled_mcps: set[str],
    bundled_skills: set[str],
    config_skills: dict,
    config_mcps: dict,
    workspace_skills: set[str] | None = None,
    allow_bundled_skills: set[str] | None = None,
) -> list[Requirement]:
    by_key: dict[str, Requirement] = {}
    # Skills that actually reach the buyer: workspace skills auto-ship in the
    # tarball, image-bundled (``skills.allowBundled``) skills are already on the
    # buyer's image, and selected ``skills.entries`` ride the slice. A referenced
    # skill that is in NONE of these won't run for the buyer.
    present_skills = set(bundled_skills) | set(workspace_skills or ()) | set(allow_bundled_skills or ())
    _NOT_BUNDLED = "declared but not bundled — won't ship to buyers"

    def _ensure(key, display, kind, disposition, *, env=None, reason=None) -> Requirement:
        r = by_key.get(key)
        if r is None:
            r = Requirement(
                key=key,
                display_name=display,
                kind=kind,
                disposition=disposition,
                env=list(env or []),
                reason=reason,
            )
            by_key[key] = r
        else:
            for e in env or []:
                if e not in r.env:
                    r.env.append(e)
        return r

    for hit in hits:
        if hit.kind == "tool":
            name = hit.value
            if name in _SUPPRESS_TOOLS:
                # Engine plumbing / fictional skill — not a buyer dependency.
                continue
            if name in BUNDLED_TOOLS:
                r = _ensure(name, name, "bundled", Disposition.INCLUDED)
            elif name in CHANNELS:
                r = _ensure(name, name.capitalize(), "channel", Disposition.CONNECT)
            else:
                prov = _provider_for_tool(name)
                if prov is not None:
                    key, spec = prov
                    disp, reason = _provider_disposition(spec)
                    r = _ensure(key, spec.display_name, _kind_for_provider(spec), disp, reason=reason)
                elif name in bundled_mcps:
                    # The agent's own MCP server, bundled into the slice → ships.
                    r = _ensure(name, name, "mcp", Disposition.INCLUDED)
                elif name in config_mcps:
                    # Declared in the agent's config but NOT bundled, so the slice
                    # won't carry it and the buyer never gets it.
                    r = _ensure(name, name, "mcp", Disposition.UNAVAILABLE, reason=_NOT_BUNDLED)
                elif name in present_skills:
                    # Workspace / image-bundled / selected skill — reaches the buyer.
                    r = _ensure(name, name, "skill", Disposition.INCLUDED)
                elif name in config_skills:
                    # An entries skill the seller didn't bundle → won't ship.
                    r = _ensure(name, name, "skill", Disposition.UNAVAILABLE, reason=_NOT_BUNDLED)
                else:
                    r = _ensure(name, name, "skill", Disposition.UNAVAILABLE, reason="no implementation found")
            r.detected_via.append(hit.source)
        elif hit.kind == "cli_pkg":
            # An `npx <pkg>` / `uvx <pkg>` invocation: the package re-resolves
            # from the public registry on the buyer's container on demand, so it
            # rides along for free — no install, no shipping. Disclosed as
            # INCLUDED with a note; any credentials it needs surface separately
            # via the env/host hits. (A private/unpublished package is the
            # buyer/agent's to provide — we can't tell statically.)
            r = _ensure(hit.value, hit.value, "cli", Disposition.INCLUDED)
            r.note = "resolved at runtime via npx/uvx — no install needed"
            r.detected_via.append(hit.source)
        else:  # env / host -> external API
            ident = identify_service(hit)
            if ident is None:
                continue
            key, display = ident
            prov = _providers().get(key)
            env = [hit.value] if hit.kind == "env" else []
            if prov is not None:
                # Connectable (or BYO for a byo-required provider) by default; the
                # wiring-mismatch downgrade of a CONNECT row to BYO happens in a
                # post-pass so it can't depend on host-vs-env hit order (see
                # _apply_wiring_mismatch).
                disp, reason = _provider_disposition(prov)
                r = _ensure(key, display, "external_api", disp, env=env, reason=reason)
            else:
                r = _ensure(key, display, "external_api", Disposition.BYO, env=env)
            r.detected_via.append(hit.source)

    # A bundled registry-provider MCP (in the slice) is hard-gated by the deploy
    # credential check even when no pipeline references it via --tool — so the
    # buyer could 409 on a credential the ledger never disclosed. Seed a row for
    # each so the ledger matches what the deploy gate enforces.
    for name in bundled_mcps:
        if name in by_key:
            continue
        prov = _providers().get(name)
        if prov is None:
            continue
        disp, reason = _provider_disposition(prov)
        r = _ensure(name, prov.display_name, _kind_for_provider(prov), disp, reason=reason)
        r.detected_via.append("bundle")

    _apply_wiring_mismatch(by_key)

    for r in by_key.values():
        if r.key in bundled_mcps or r.key in bundled_skills:
            r.bundled = True
        r.detected_via = sorted(set(r.detected_via))
    # Drop engine-internal / fictional names regardless of how they were
    # surfaced (a workspace `skills/fast-io/` dir would otherwise classify as an
    # included skill).
    return [r for r in by_key.values() if r.key not in _SUPPRESS_TOOLS]


def _apply_wiring_mismatch(by_key: dict[str, Requirement]) -> None:
    """Downgrade a registry CONNECT row to BYO when the agent references env
    vars connecting the integration won't set. Order-independent: evaluated
    once over each row's full accumulated env, so the result is the same
    regardless of host/env hit order.

    The binding is what connecting puts in the agent's ENVIRONMENT, and the
    two MCP shapes differ:
      - stdio ``wiring.mcp_server``: keyed on specific env vars — a row whose
        env references NONE of them is a mismatch (e.g. STRIPE_API_KEY vs a
        wired STRIPE_SECRET_KEY). No live provider uses this rail today, but
        it is still schema-legal.
      - hosted ``mcp_remote`` (actor "mcp", ``mcp_oauth`` materializer):
        authenticates over OAuth and sets NO env vars at all — ANY env
        reference is a mismatch. An agent scripting against
        STRIPE_SECRET_KEY is not satisfied by connecting the hosted Stripe
        MCP; it needs its own pasted key.

    CLI/materializer-rail providers (github/aws/google) stay CONNECT
    regardless of how the agent names its env — the binary reads its own
    auth store, so the env-var name isn't the binding. And if the agent
    references a wired key at all (even alongside a stray off-key),
    connecting the integration still satisfies it.
    """
    providers = _providers()
    for r in by_key.values():
        # Any registry-provider CONNECT row, whether detected via host/env
        # (kind external_api) or via --tool (kind mcp). Non-provider rows
        # (channels) resolve to no provider below and are skipped.
        if r.disposition != Disposition.CONNECT or not r.env:
            continue
        prov = providers.get(r.key)
        if prov is None:
            continue
        wired_env: set[str] = set()
        if prov.wiring.mcp_server is not None:
            wired_env = set(prov.wiring.mcp_server.env.keys())
        elif not (prov.actor == "mcp" and prov.mcp_remote is not None):
            continue  # non-MCP rail: the env-var name isn't the binding
        if any(e in wired_env for e in r.env):
            continue  # references a wired key — connecting satisfies it
        r.disposition = Disposition.BYO
        wires = (
            f"wires {sorted(wired_env)[0]}"
            if wired_env
            else "connects a hosted MCP over OAuth and sets no environment variables"
        )
        r.reason = (
            f"wiring mismatch: the {r.display_name} integration {wires}; this agent references {r.env[0]} directly"
        )
