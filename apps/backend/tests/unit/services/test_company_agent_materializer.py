"""Tests for the per-agent company materializer (delegation-preserving).

The materializer places ONE company agent's identity + skills onto the owner's
EFS and registers it in ``openclaw.json``. Its defining property vs the
marketplace ``deploy_from_artifact`` path is that it does NOT clamp the agent
into single-agent containment: company agents must be able to delegate to each
other along the package's reportsTo graph, so ``subagents.allowAgents`` and the
``tools.deny`` gateway clamp must be ABSENT.

Fakes stand in for the real ``Workspace`` (captures EFS writes) and the real
``config_patcher.apply_deploy_aux_mutations`` (captures the agent_entry +
skills_patch), so these are pure unit tests with no disk / container.
"""

import pytest

from core.services.company_agent_materializer import materialize_agent
from core.services.company_package import PlannedAgent

OWNER = "user_owner123"
AGENT_ID = "agent_abc123"


class FakeWorkspace:
    """Records write calls instead of touching EFS.

    Mirrors the real ``Workspace`` write surface: ``write_file`` /
    ``write_file_if_missing`` (text) and ``write_bytes`` (binary), each taking
    ``(user_id, path, content)`` with ``path`` relative to the user's root.
    """

    def __init__(self) -> None:
        self.text_writes: list[tuple[str, str, str]] = []
        self.missing_writes: list[tuple[str, str, str]] = []
        self.byte_writes: list[tuple[str, str, bytes]] = []

    def write_file(self, user_id: str, path: str, content: str) -> None:
        self.text_writes.append((user_id, path, content))

    def write_file_if_missing(self, user_id: str, path: str, content: str) -> bool:
        self.missing_writes.append((user_id, path, content))
        return True

    def write_bytes(self, user_id: str, path: str, data: bytes) -> None:
        self.byte_writes.append((user_id, path, data))


class FakeApplyAux:
    """Records calls matching ``apply_deploy_aux_mutations``'s signature."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(
        self,
        owner_id: str,
        *,
        agent_entry=None,
        plugins_patch=None,
        mcp_patch=None,
        skills_patch=None,
    ) -> None:
        self.calls.append(
            {
                "owner_id": owner_id,
                "agent_entry": agent_entry,
                "plugins_patch": plugins_patch,
                "mcp_patch": mcp_patch,
                "skills_patch": skills_patch,
            }
        )


def _planned(**over) -> PlannedAgent:
    base = dict(
        slug="cto",
        name="Ada",
        role="member",
        title="Chief Technology Officer",
        body_md="# Ada\nYou are the CTO. Delegate freely.\n",
        reports_to_slug="ceo",
        skills=["code-review", "deploy"],
        capabilities="engineering leadership",
    )
    base.update(over)
    return PlannedAgent(**base)


@pytest.mark.asyncio
async def test_writes_agents_md_to_agent_workspace():
    ws = FakeWorkspace()
    aux = FakeApplyAux()
    planned = _planned()

    await materialize_agent(
        owner_id=OWNER,
        new_agent_id=AGENT_ID,
        planned=planned,
        skill_assets=None,
        workspace=ws,
        apply_aux=aux,
    )

    # AGENTS.md is the agent's identity — write-if-missing, at the agent's
    # EFS-relative workspace path. The planner's body is preserved verbatim,
    # with the Paperclip credential rule appended: body_md is model-authored
    # and says nothing about credentials, while Paperclip's wake text still
    # tells the agent to load its key from a file.
    from core.services.paperclip_credential_rule import PAPERCLIP_CREDENTIAL_PROHIBITION

    writes = [w for w in ws.missing_writes if w[1] == f"workspace-{AGENT_ID}/AGENTS.md"]
    assert len(writes) == 1
    body = writes[0][2]
    assert body.startswith(planned.body_md.rstrip("\n"))
    assert PAPERCLIP_CREDENTIAL_PROHIBITION in body


@pytest.mark.asyncio
async def test_agent_entry_core_fields():
    ws = FakeWorkspace()
    aux = FakeApplyAux()
    planned = _planned()

    entry = await materialize_agent(
        owner_id=OWNER,
        new_agent_id=AGENT_ID,
        planned=planned,
        skill_assets=None,
        workspace=ws,
        apply_aux=aux,
    )

    assert entry["id"] == AGENT_ID
    assert entry["workspace"] == f".openclaw/workspace-{AGENT_ID}"
    assert entry["reasoningDefault"] == "stream"
    assert entry["skills"] == planned.skills
    assert entry["name"] == planned.name


@pytest.mark.asyncio
async def test_no_containment_keys_delegation_preserved():
    """The delegation-preserving invariant: NO single-agent containment clamp.

    Marketplace ``deploy_from_artifact`` pins ``subagents.allowAgents=[self]``
    and unions ``gateway``/``sessions_send`` into ``tools.deny``. Company agents
    must delegate to each other, so those keys must never appear.
    """
    ws = FakeWorkspace()
    aux = FakeApplyAux()

    entry = await materialize_agent(
        owner_id=OWNER,
        new_agent_id=AGENT_ID,
        planned=_planned(),
        skill_assets=None,
        workspace=ws,
        apply_aux=aux,
    )

    # No allowAgents containment.
    assert "allowAgents" not in entry.get("subagents", {})
    # No gateway/sessions_send deny clamp.
    assert "gateway" not in entry.get("tools", {}).get("deny", [])
    assert "sessions_send" not in entry.get("tools", {}).get("deny", [])
    # The merged entry the config-patcher receives is the same object.
    assert aux.calls[0]["agent_entry"] == entry


@pytest.mark.asyncio
async def test_skills_bind_via_agent_entry_not_skills_patch():
    """The real skill binding rides in ``agent_entry["skills"]`` (which lands in
    ``agents.list[].skills``), NOT the top-level ``skills_patch`` block.

    ``skills_patch`` must stay ``None``: the real
    ``apply_deploy_aux_mutations`` treats it as a dict
    (``skills_patch.get("allowBundled")``) and would AttributeError on a list.
    """
    ws = FakeWorkspace()
    aux = FakeApplyAux()
    planned = _planned(skills=["alpha", "beta"])

    entry = await materialize_agent(
        owner_id=OWNER,
        new_agent_id=AGENT_ID,
        planned=planned,
        skill_assets=None,
        workspace=ws,
        apply_aux=aux,
    )

    assert len(aux.calls) == 1
    call = aux.calls[0]
    assert call["owner_id"] == OWNER
    # The binding is threaded through the agent entry, not the skills block.
    assert entry["skills"] == ["alpha", "beta"]
    assert call["agent_entry"]["skills"] == ["alpha", "beta"]
    # Company deploy carries no mcp/skills-block mutations through here.
    assert call["skills_patch"] is None
    assert call["mcp_patch"] is None
    # The ONE plugin mutation it does carry: the credential plugin. It must
    # ship with the credential rule appended to the body, because that rule
    # claims PAPERCLIP_* is already in the environment and this is what puts
    # it there for an owner whose on-disk config predates the plugin.
    assert call["plugins_patch"] == {"entries": {"paperclip-exec-creds": {"enabled": True}}}


@pytest.mark.asyncio
async def test_skill_assets_written_under_agent_workspace():
    ws = FakeWorkspace()
    aux = FakeApplyAux()
    assets = {
        "skills/code-review/SKILL.md": b"# Code Review\n",
        "skills/code-review/helper.py": b"print('hi')\n",
    }

    await materialize_agent(
        owner_id=OWNER,
        new_agent_id=AGENT_ID,
        planned=_planned(),
        skill_assets=assets,
        workspace=ws,
        apply_aux=aux,
    )

    written = {(uid, path): data for uid, path, data in ws.byte_writes}
    assert written[(OWNER, f"workspace-{AGENT_ID}/skills/code-review/SKILL.md")] == b"# Code Review\n"
    assert written[(OWNER, f"workspace-{AGENT_ID}/skills/code-review/helper.py")] == b"print('hi')\n"


@pytest.mark.asyncio
async def test_none_skill_assets_is_valid():
    ws = FakeWorkspace()
    aux = FakeApplyAux()

    entry = await materialize_agent(
        owner_id=OWNER,
        new_agent_id=AGENT_ID,
        planned=_planned(),
        skill_assets=None,
        workspace=ws,
        apply_aux=aux,
    )

    assert ws.byte_writes == []
    # AGENTS.md + entry merge still happen.
    assert len(ws.missing_writes) == 1
    assert len(aux.calls) == 1
    assert entry["id"] == AGENT_ID


@pytest.mark.asyncio
async def test_skills_patch_is_always_none_regardless_of_skills():
    """``skills_patch`` is unconditionally ``None`` — whether the agent has
    skills or not — because the binding lives in ``agent_entry["skills"]``.
    """
    ws = FakeWorkspace()
    aux = FakeApplyAux()

    # Skilled agent: still None.
    entry_skilled = await materialize_agent(
        owner_id=OWNER,
        new_agent_id=AGENT_ID,
        planned=_planned(skills=["gamma"]),
        skill_assets=None,
        workspace=ws,
        apply_aux=aux,
    )
    assert entry_skilled["skills"] == ["gamma"]
    assert aux.calls[0]["skills_patch"] is None

    # Skill-less agent: also None, and an empty entry skills list.
    entry_bare = await materialize_agent(
        owner_id=OWNER,
        new_agent_id="agent_bare",
        planned=_planned(skills=[]),
        skill_assets={},
        workspace=ws,
        apply_aux=aux,
    )
    assert entry_bare["skills"] == []
    assert aux.calls[1]["skills_patch"] is None


@pytest.mark.asyncio
async def test_returns_the_merged_agent_entry():
    ws = FakeWorkspace()
    aux = FakeApplyAux()

    entry = await materialize_agent(
        owner_id=OWNER,
        new_agent_id=AGENT_ID,
        planned=_planned(),
        skill_assets=None,
        workspace=ws,
        apply_aux=aux,
    )

    assert isinstance(entry, dict)
    assert entry is aux.calls[0]["agent_entry"] or entry == aux.calls[0]["agent_entry"]
