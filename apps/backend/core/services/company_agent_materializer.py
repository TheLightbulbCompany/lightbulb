"""Materialize ONE company agent onto the owner's EFS + ``openclaw.json``.

Given a ``PlannedAgent`` (from ``company_package``), this writes the agent's
identity (``AGENTS.md``) and any skill files onto the owner's EFS workspace,
then deep-merges an ``agents.list`` entry into ``openclaw.json`` via the
config-patcher. It returns the agent entry it wrote so the caller (the deploy
service, Task 4) can track what landed.

Delegation-preserving deviation from marketplace ``catalog_service``:
``deploy_from_artifact`` isolates a bought third-party agent by pinning
``subagents.allowAgents=[self]`` and unioning ``gateway`` / ``sessions_send``
into ``tools.deny``. Company agents are peers in one owner-authored org and
must be able to delegate to each other along the package's ``reportsTo`` graph,
so those containment keys are deliberately ABSENT here.

Dependencies are injected (``workspace`` + ``apply_aux``) so the unit is
testable without EFS or a running container.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from core.services.company_package import PlannedAgent


class _WorkspaceWriter(Protocol):
    """The subset of ``core.containers.workspace.Workspace`` this uses.

    Real signatures (see ``workspace.py``): ``write_file_if_missing`` writes a
    text file only if absent (O_EXCL) and returns a bool; ``write_bytes``
    writes binary data. Both take ``(user_id, path, content)`` where ``path``
    is relative to the owner's EFS root.
    """

    def write_file_if_missing(self, user_id: str, path: str, content: str) -> bool: ...

    def write_bytes(self, user_id: str, path: str, data: bytes) -> None: ...


# Matches ``config_patcher.apply_deploy_aux_mutations``:
#   async (owner_id, *, plugins_patch, mcp_patch=None, skills_patch=None, agent_entry=None)
_ApplyAux = Callable[..., Awaitable[None]]


async def materialize_agent(
    *,
    owner_id: str,
    new_agent_id: str,
    planned: PlannedAgent,
    skill_assets: dict[str, bytes] | None,
    workspace: _WorkspaceWriter,
    apply_aux: _ApplyAux,
) -> dict:
    """Place one company agent's identity + skills on EFS and register it.

    Args:
        owner_id: the container owner (org_id for orgs, user_id personal) — the
            EFS ``user_id`` all writes are keyed by.
        new_agent_id: the buyer-side OpenClaw agent id (shared with Paperclip's
            minted UUID by the deploy service).
        planned: the parsed source agent (identity body, skills, name, ...).
        skill_assets: optional ``relpath -> bytes`` map of skill files to write
            under the agent's workspace. ``None`` / empty is valid.
        workspace: EFS writer (``get_workspace()`` in production).
        apply_aux: ``config_patcher.apply_deploy_aux_mutations`` (or a fake).

    Returns:
        The ``agent_entry`` dict merged into ``agents.list``.
    """
    # EFS-relative agent workspace root. The container reads it at
    # ``.openclaw/workspaces/{id}`` (the EFS access point chroots
    # ``/users/{owner}`` to ``/home/node/.openclaw``), hence the two forms:
    # this bare form for EFS writes, the ``.openclaw/`` form in the entry.
    ws_root = f"workspaces/{new_agent_id}"

    # 1. Identity. Write-if-missing mirrors the identity-seeding idiom
    #    (Friday/Atlas) — a redeploy retry must not clobber a body the agent
    #    may already have evolved.
    workspace.write_file_if_missing(owner_id, f"{ws_root}/AGENTS.md", planned.body_md)

    # 2. Skill files (bytes — may be non-text). None / empty is a no-op.
    for relpath, data in (skill_assets or {}).items():
        workspace.write_bytes(owner_id, f"{ws_root}/{relpath}", data)

    # 3. Agent entry — the canonical company/paperclip agent shape
    #    (``config._build_agents_list``): id + reasoningDefault + workspace,
    #    plus the optional friendly ``name``. NOTABLY MISSING vs
    #    ``deploy_from_artifact``: no ``subagents.allowAgents`` and no
    #    ``tools.deny`` gateway clamp — company agents delegate to each other.
    #    ``title`` / ``role`` / ``capabilities`` are NOT top-level AgentEntry
    #    keys (they live under ``params.paperclip`` via ``write_to_agent_entry``)
    #    so they are handled by the paperclip-meta sync, not this entry.
    agent_entry: dict = {
        "id": new_agent_id,
        "workspace": f".openclaw/workspaces/{new_agent_id}",
        "reasoningDefault": "stream",
        "skills": list(planned.skills),
    }
    if planned.name:
        agent_entry["name"] = planned.name

    # 4. Merge the entry into ``openclaw.json`` ``agents.list``. Company deploy
    #    carries no plugin/mcp mutations through this path.
    await apply_aux(
        owner_id,
        agent_entry=agent_entry,
        plugins_patch=None,
        mcp_patch=None,
        skills_patch=(list(planned.skills) or None),
    )

    return agent_entry
