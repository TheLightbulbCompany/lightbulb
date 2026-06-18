"""Classify detected hits into buyer-facing requirement dispositions.

Each detected tool/env/host collapses to a canonical provider/tool and is
classified against the platform's integration rail:

  - bundled image tool/skill, or keyless          -> INCLUDED
  - registry provider (providers.yaml) connectable -> CONNECT
  - channel tool (slack/discord/...)               -> CONNECT (via Channels)
  - gog / Google (internal + Bedrock-only)         -> UNAVAILABLE (gated)
  - referenced tool/skill with no implementation   -> UNAVAILABLE (phantom)
  - registry provider but wiring mismatch          -> BYO + reason
  - anything else needing a key                    -> BYO
"""

from __future__ import annotations

from core.containers.config import CHANNEL_PROVIDER_IDS
from core.services.marketplace.requirements import Disposition, Requirement
from core.services.marketplace.requirements_detect import Hit, identify_service

# Core tools + plugins baked into the image. OpenClaw exposes no per-tool
# requirements registry for these, so we maintain the list (verified against the
# extended image's bundled set + Atlas seed).
BUNDLED_TOOLS = frozenset(
    {
        "llm-task",
        "fast-io",
        "summarize",
        "lobster",
        "memory-core",
        "message",
        "capability-evolver",
    }
)
# The backend-provisionable channel providers (Channels tab). Imported from the
# single source of truth so this can't drift — e.g. listing whatsapp here would
# tell a buyer to "connect" a channel the backend never provisions.
CHANNELS = frozenset(CHANNEL_PROVIDER_IDS)
# Tool/provider names that resolve to gated Google Workspace access.
GATED_PROVIDERS = frozenset({"gog", "google"})

_GATED_REASON = "internal + Bedrock-only on this platform"


def _providers() -> dict:
    from core.services.credentials import registry

    return registry.all_providers()  # {key: Provider}


def _kind_for_provider(prov) -> str:
    w = prov.wiring
    if w.mcp_server:
        return "mcp"
    if w.materializer:
        return "cli"
    return "skill"


def _provider_for_tool(name: str):
    """Return ``(key, Provider)`` if a registry provider owns this tool name."""
    for key, prov in _providers().items():
        mcp = prov.wiring.mcp_server
        if key == name or (mcp and mcp.key == name):
            return key, prov
    return None


def classify_hits(
    hits: list[Hit],
    *,
    bundled_mcps: set[str],
    bundled_skills: set[str],
    config_skills: dict,
    config_mcps: dict,
) -> list[Requirement]:
    by_key: dict[str, Requirement] = {}

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
            if name in BUNDLED_TOOLS:
                r = _ensure(name, name, "bundled", Disposition.INCLUDED)
            elif name in CHANNELS:
                r = _ensure(name, name.capitalize(), "channel", Disposition.CONNECT)
            elif name in GATED_PROVIDERS:
                r = _ensure(name, "Google Sheets/Drive", "cli", Disposition.UNAVAILABLE, reason=_GATED_REASON)
            else:
                prov = _provider_for_tool(name)
                if prov is not None:
                    key, spec = prov
                    gated = spec.internal or spec.requires_bedrock
                    r = _ensure(
                        key,
                        spec.display_name,
                        _kind_for_provider(spec),
                        Disposition.UNAVAILABLE if gated else Disposition.CONNECT,
                        reason=_GATED_REASON if gated else None,
                    )
                elif name in config_mcps:
                    # The agent's own MCP server, declared in its openclaw.json
                    # and shipped in the workspace tarball — not a phantom.
                    r = _ensure(name, name, "mcp", Disposition.INCLUDED)
                elif name in config_skills or name in bundled_skills:
                    r = _ensure(name, name, "skill", Disposition.BYO)
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
            if prov is not None and not (prov.internal or prov.requires_bedrock):
                # Connectable by default; the wiring-mismatch downgrade to BYO
                # happens in a post-pass so it can't depend on host-vs-env hit
                # order (see _apply_wiring_mismatch).
                r = _ensure(key, display, "external_api", Disposition.CONNECT, env=env)
            elif prov is not None:
                r = _ensure(key, display, "external_api", Disposition.UNAVAILABLE, env=env, reason=_GATED_REASON)
            else:
                r = _ensure(key, display, "external_api", Disposition.BYO, env=env)
            r.detected_via.append(hit.source)

    _apply_wiring_mismatch(by_key)

    for r in by_key.values():
        if r.key in bundled_mcps or r.key in bundled_skills:
            r.bundled = True
        r.detected_via = sorted(set(r.detected_via))
    return list(by_key.values())


def _apply_wiring_mismatch(by_key: dict[str, Requirement]) -> None:
    """Downgrade a registry CONNECT row to BYO when the agent references ONLY env
    vars the integration's MCP won't set (e.g. STRIPE_API_KEY when the tab wires
    STRIPE_SECRET_KEY). Order-independent: evaluated once over each row's full
    accumulated env, so the result is the same regardless of host/env hit order.

    Only applies to MCP-wired providers — the "mismatch" is specifically that
    the MCP is keyed on a particular env var. A CLI/materializer-rail provider
    (no mcp_server) is connectable regardless of how the agent names its env, so
    it stays CONNECT. And if the agent references the wired key at all (even
    alongside a stray off-key), connecting the integration still satisfies it.
    """
    providers = _providers()
    for r in by_key.values():
        if r.disposition != Disposition.CONNECT or r.kind != "external_api":
            continue
        prov = providers.get(r.key)
        mcp = prov.wiring.mcp_server if prov else None
        if mcp is None:
            continue  # non-MCP rail: the env-var name isn't the binding
        mcp_env = set(mcp.env.keys())
        # Downgrade only when the agent uses env vars and NONE is the wired key.
        if r.env and all(e not in mcp_env for e in r.env):
            r.disposition = Disposition.BYO
            r.reason = (
                f"wiring mismatch: the {r.display_name} integration wires {sorted(mcp_env)[0]}; "
                f"this agent references {r.env[0]} directly"
            )
