"""Agent role/title/capabilities/icon sent by clients inside ``params.paperclip`` (a wire key old clients
still send on ``agents.create`` / ``agents.update``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: The roles an agent may carry; anything else reads as ``general``.
AGENT_ROLES: Final[tuple[str, ...]] = (
    "ceo",
    "cto",
    "cmo",
    "cfo",
    "security",
    "engineer",
    "designer",
    "pm",
    "qa",
    "devops",
    "researcher",
    "general",
)


@dataclass(frozen=True)
class AgentMeta:
    role: str = "general"
    title: str | None = None
    capabilities: str | None = None
    icon: str | None = None


def read_from_agent_entry(entry: dict) -> AgentMeta:
    """Lift the meta out of an agent entry. Tolerant: an unknown role reads as ``general``, blank strings as None."""
    params = entry.get("params") or {}
    meta = params.get("paperclip") if isinstance(params, dict) else None
    if not isinstance(meta, dict):
        return AgentMeta()
    role = meta.get("role")

    def text(key: str) -> str | None:
        value = meta.get(key)
        return value if isinstance(value, str) and value.strip() else None

    return AgentMeta(
        role=role if role in AGENT_ROLES else "general",
        title=text("title"),
        capabilities=text("capabilities"),
        icon=text("icon"),
    )


def resolve_create_agent_fields(*, role: str | None, meta: AgentMeta | None) -> dict:
    """``{role, title, capabilities, icon}`` for agent create: an explicit non-blank ``role`` wins, then
    ``meta.role``, then ``general``."""
    effective_role = role.strip() if isinstance(role, str) and role.strip() else (meta.role if meta else "general")
    return {
        "role": effective_role,
        "title": meta.title if meta else None,
        "capabilities": meta.capabilities if meta else None,
        "icon": meta.icon if meta else None,
    }
