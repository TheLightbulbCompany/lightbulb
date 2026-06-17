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
BUNDLED_SKILLS = frozenset({"capability-evolver", "skill-vetter"})
CHANNELS = frozenset({"slack", "discord", "telegram", "whatsapp"})
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
                elif name in config_skills or name in bundled_skills:
                    r = _ensure(name, name, "skill", Disposition.BYO)
                else:
                    r = _ensure(name, name, "skill", Disposition.UNAVAILABLE, reason="no implementation found")
            r.detected_via.append(hit.source)
        else:  # env / host -> external API
            ident = identify_service(hit)
            if ident is None:
                continue
            key, display = ident
            prov = _providers().get(key)
            env = [hit.value] if hit.kind == "env" else []
            if prov is not None and not (prov.internal or prov.requires_bedrock):
                disp = Disposition.CONNECT
                reason = None
                mcp = prov.wiring.mcp_server
                mcp_env = set(mcp.env.keys()) if mcp else set()
                # Stripe-style mismatch: the tab wires an MCP keyed on one env
                # var, but the agent references a different one the tab won't set.
                if hit.kind == "env" and hit.value not in mcp_env:
                    disp = Disposition.BYO
                    reason = (
                        f"wiring mismatch: the {display} integration wires an MCP; "
                        f"this agent references {hit.value} directly"
                    )
                r = _ensure(key, display, "external_api", disp, env=env, reason=reason)
            elif prov is not None:
                r = _ensure(key, display, "external_api", Disposition.UNAVAILABLE, env=env, reason=_GATED_REASON)
            else:
                r = _ensure(key, display, "external_api", Disposition.BYO, env=env)
            r.detected_via.append(hit.source)

    for r in by_key.values():
        if r.key in bundled_mcps or r.key in bundled_skills:
            r.bundled = True
        r.detected_via = sorted(set(r.detected_via))
    return list(by_key.values())
