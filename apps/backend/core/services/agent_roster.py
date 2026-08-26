"""The agent roster's two on-disk shapes, in one place.

OpenClaw 2026.8.1 moved the roster from ``agents.list`` (an array of entries,
each carrying its own ``id``) to ``agents.entries`` (a MAP keyed by agent id,
with ``id`` stripped from the value). Upstream's own migration is the
authoritative translation -- ``src/config/legacy.roster.ts`` at
``v2026.8.1-isol8.1``::

    const entries = Object.fromEntries(
      legacyList.map((value) => {
        const { id, ...config } = value;
        return [id, config];
      }),
    );
    const { list: _list, ...rest } = agents;
    agents = { ...rest, entries };

Two properties of 8.1 make getting this wrong silent rather than loud:

* ``readAgentRosterProperty`` (src/agents/agent-scope-config.ts) returns
  ``entries`` whenever the key is present and only falls back to ``list``
  otherwise -- so a stale ``list`` alongside ``entries`` is simply ignored.
  Never leave both populated.
* ``AgentsSchema`` (src/config/zod-schema.agents.ts) is ``.strict()`` over
  ``{ownership, defaults, entries}``. ``list`` is no longer a config key at
  all; it is re-attached AFTER validation as a non-enumerable projection
  (src/config/agent-list-projection.ts), which means it never survives
  JSON serialization either.

Boot ``doctor --fix`` rewrites every container's file into the keyed shape, so
from the first 8.1 boot onward the roster we read back is ``entries`` -- while
anything still reading or writing ``list`` addresses a dead key and degrades
quietly (empty roster, "agent not registered", a model pin that never lands).
"""

from __future__ import annotations

from typing import Any

__all__ = ["read_agent_roster", "roster_entries", "write_agent_roster"]


def read_agent_roster(agents_obj: dict[str, Any]) -> tuple[str, list[dict]]:
    """Read the roster out of an ``agents`` object as a flat list of entries.

    Returns ``(shape, entries)`` where shape is ``"entries"`` or ``"list"``.
    In the keyed shape the map key is re-injected as ``id`` so every caller
    sees one entry shape regardless of what is on disk.

    Mirrors upstream's preference order: ``entries`` wins whenever present.

    NOTE: in the keyed shape the returned dicts are COPIES, so mutating one
    does not write through to ``agents_obj``. Pair a read-modify-write with
    :func:`write_agent_roster`.
    """
    entries = agents_obj.get("entries")
    if isinstance(entries, dict):
        roster: list[dict] = []
        for agent_id, value in entries.items():
            if isinstance(value, dict):
                roster.append({**value, "id": agent_id})
        return "entries", roster
    agents_list = agents_obj.get("list")
    return "list", agents_list if isinstance(agents_list, list) else []


def write_agent_roster(agents_obj: dict[str, Any], shape: str, roster: list[dict]) -> None:
    """Write the roster back in the shape it was read in.

    In the keyed shape the id is the KEY and is omitted from the value --
    upstream's ``AgentEntrySchema.omit({ id: true })``, and the vendored JSON
    schema sets ``additionalProperties: false`` on the entry, so leaving an
    ``id`` inside the value is a hard schema rejection. ``default`` stays
    legal there (the entries wrapper re-adds it) and is what marks the CEO on
    a multi-agent roster.
    """
    if shape != "entries":
        agents_obj["list"] = roster
        return
    keyed: dict[str, dict] = {}
    for entry in roster:
        if not isinstance(entry, dict):
            continue
        agent_id = entry.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        keyed[agent_id] = {k: v for k, v in entry.items() if k != "id"}
    agents_obj["entries"] = keyed
    # Never leave both shapes populated: upstream prefers ``entries`` and
    # ignores ``list``, so a stale ``list`` is a silent divergence trap.
    agents_obj.pop("list", None)


def roster_entries(config: dict[str, Any] | None) -> list[dict]:
    """The agent entries of a whole ``openclaw.json``, from either shape.

    Read-only convenience over :func:`read_agent_roster` for the many callers
    that just want "the agents in this owner's config". Tolerates a missing /
    non-dict ``agents`` block and the legacy flat-list shape (``agents`` as a
    bare array), which hand-edited configs still carry.
    """
    if not isinstance(config, dict):
        return []
    agents_obj = config.get("agents")
    if isinstance(agents_obj, list):
        return [e for e in agents_obj if isinstance(e, dict)]
    if not isinstance(agents_obj, dict):
        return []
    _shape, roster = read_agent_roster(agents_obj)
    return [e for e in roster if isinstance(e, dict)]
