"""Paperclip-relevant agent metadata that lives inside OpenClaw's
``AgentEntry.params`` bag.

OpenClaw's ``AgentEntrySchema`` is ``.strict()`` so we can't add
``role`` / ``title`` / ``capabilities`` to it. But it has
``params: z.record(z.string(), z.unknown()).optional()`` as a
documented free-form extension point (see ``openclaw/src/config/
zod-schema.agent-runtime.ts:1002`` + the ``config-misc.test.ts``
case "accepts documented agents.list[].params overrides").

We put the Paperclip-relevant fields under ``params.paperclip``. This
means:

  * They round-trip through the marketplace slice (which carries the
    full agent entry verbatim).
  * They survive chokidar reload + container re-provisions (they're
    persisted in openclaw.json).
  * They stay isolated from other integration metadata that might
    appear in ``params`` (e.g. ``params.cacheRetention`` from upstream
    OpenClaw's own documented overrides).

This module is the SINGLE place that knows the shape of
``params.paperclip``. Every caller — create-agent interceptor,
update-agent interceptor, marketplace deploy — goes through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Paperclip's createAgentSchema length caps. Source:
# paperclip/packages/shared/src/validators/agent.ts (the title /
# capabilities z.string() chains use these in min/max). Mirrored here
# so derive_from_listing produces values that can never trip a 422.
# Keep in sync with upstream — if the schema tightens, re-pin here.
PAPERCLIP_TITLE_MAX_CHARS: Final[int] = 80
PAPERCLIP_CAPABILITIES_MAX_CHARS: Final[int] = 1024

# Mirrors paperclip/packages/shared/src/constants.ts AGENT_ROLES.
# If Paperclip upstream changes this list, the test in
# tests/unit/services/test_paperclip_meta.py will fail and force a
# manual re-sync (we keep the constant inline because importing from
# the gitignored paperclip clone is fragile).
PAPERCLIP_AGENT_ROLES: Final[tuple[str, ...]] = (
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
class PaperclipMeta:
    """Paperclip-relevant agent metadata, lifted from
    ``agent_entry.params.paperclip`` and ready to thread into
    ``PaperclipAdminClient.create_agent`` / ``update_agent``.

    Field meanings match Paperclip's ``createAgentSchema``:
      * ``role`` — one of ``PAPERCLIP_AGENT_ROLES``; defaults to
        ``"general"`` for newly-created agents.
      * ``title`` — short one-liner (e.g. "Restaurant booking
        specialist"). Rendered in Paperclip's roster UI.
      * ``capabilities`` — longer freeform description of what the
        agent does. This is what Friday reads when orchestrating.
      * ``icon`` — Paperclip-side icon picker value; opaque string
        from Paperclip's ``AGENT_ICON_NAMES`` enum. Not validated
        here because the icon list churns more often than roles.
    """

    role: str = "general"
    title: str | None = None
    capabilities: str | None = None
    icon: str | None = None


def read_from_agent_entry(entry: dict) -> PaperclipMeta:
    """Lift the Paperclip meta out of an agent entry.

    Tolerant on read — an unknown ``role`` value is coerced to
    ``"general"`` rather than raising, because the entry comes from
    openclaw.json (potentially hand-edited) and we don't want a typo
    to crash the chat path. Writes are strict (see ``write_to_agent_entry``).
    """
    params = entry.get("params") or {}
    if not isinstance(params, dict):
        return PaperclipMeta()
    pc = params.get("paperclip") or {}
    if not isinstance(pc, dict):
        return PaperclipMeta()
    role = pc.get("role")
    if role not in PAPERCLIP_AGENT_ROLES:
        role = "general"
    title = pc.get("title")
    capabilities = pc.get("capabilities")
    icon = pc.get("icon")
    return PaperclipMeta(
        role=role,
        title=title if isinstance(title, str) and title.strip() else None,
        capabilities=capabilities if isinstance(capabilities, str) and capabilities.strip() else None,
        icon=icon if isinstance(icon, str) and icon.strip() else None,
    )


def write_to_agent_entry(entry: dict, meta: PaperclipMeta) -> dict:
    """Write the Paperclip meta into ``entry["params"]["paperclip"]``.

    Strict on write: an unknown role raises ``ValueError`` to keep bad
    data out of openclaw.json. ``None`` values for ``title`` /
    ``capabilities`` / ``icon`` are interpreted as "no change" — we
    DON'T overwrite an existing value with null. To clear a field,
    pass an empty string (translated to absent at write time).

    Mutates ``entry`` in place AND returns it (so call sites can chain).
    """
    if meta.role not in PAPERCLIP_AGENT_ROLES:
        raise ValueError(f"unknown Paperclip role: {meta.role!r}")

    params = entry.get("params")
    if not isinstance(params, dict):
        params = {}
        entry["params"] = params
    pc = params.get("paperclip")
    if not isinstance(pc, dict):
        pc = {}
        params["paperclip"] = pc

    pc["role"] = meta.role
    if meta.title is not None:
        if meta.title.strip():
            pc["title"] = meta.title
        else:
            pc.pop("title", None)
    if meta.capabilities is not None:
        if meta.capabilities.strip():
            pc["capabilities"] = meta.capabilities
        else:
            pc.pop("capabilities", None)
    if meta.icon is not None:
        if meta.icon.strip():
            pc["icon"] = meta.icon
        else:
            pc.pop("icon", None)
    return entry


def derive_from_listing(*, name: str | None, description_md: str | None) -> PaperclipMeta:
    """Last-resort derivation when the marketplace slice's agent entry
    has no ``params.paperclip`` block — typical for listings published
    before this feature shipped. Maps:

      * ``listing.name`` → ``title`` (truncated to 80 chars to match
        Paperclip's ``title`` length cap)
      * ``listing.description_md`` → ``capabilities`` (truncated to
        the schema-safe 1024 char ceiling)
      * ``role`` defaults to ``"general"`` — no signal in listing data
        to pick a more specific role.

    Returns ``PaperclipMeta`` even when both inputs are None (caller
    gets defaults). Never raises.
    """
    title = (name or "").strip()[:PAPERCLIP_TITLE_MAX_CHARS] or None
    caps = (description_md or "").strip()[:PAPERCLIP_CAPABILITIES_MAX_CHARS] or None
    return PaperclipMeta(role="general", title=title, capabilities=caps, icon=None)


# Public helpers (intentionally narrow API). Don't reach into
# ``entry["params"]["paperclip"]`` from outside this module — use
# read_from_agent_entry / write_to_agent_entry so the format stays
# consistent if we evolve the sub-block later.
__all__ = [
    "PAPERCLIP_AGENT_ROLES",
    "PAPERCLIP_CAPABILITIES_MAX_CHARS",
    "PAPERCLIP_TITLE_MAX_CHARS",
    "PaperclipMeta",
    "derive_from_listing",
    "read_from_agent_entry",
    "write_to_agent_entry",
]
