"""Seed each agent's Google-account identity into its IDENTITY.md.

An agent that acts on Gmail / Calendar / Drive must do so as a *specific*
Google account, invoked via ``gog -a <email>``. The block reflects ONLY the
agent's EXPLICIT assignment (``google_accounts_repo.get_assignment``) — there
is no default fallback. An unassigned agent gets no block and uses bare
``gog`` (the keyring's account). Deriving the block from a default would leave
a stale ``gog -a <email>`` instruction behind when that default account is
later disconnected, since an unassigned agent has no row to drive a cleanup.

The instruction lives in IDENTITY.md because OpenClaw auto-loads IDENTITY.md
into the agent's chat context — so the agent reads "use this account" without
us threading it through a tool. But IDENTITY.md is otherwise seeded
write-once (``pre_stage_friday_identity`` / ``pre_stage_atlas`` use
write-if-missing so an evolved persona is never clobbered). To keep the
account instruction *updateable* on reassignment without disturbing the rest
of the file, we carve out a delimited managed block and rewrite only that
region in place:

    <!-- BEGIN gog-account -->
    ...instruction...
    <!-- END gog-account -->

Seeding is **user-action-driven**: the only callers are the assign API (which
seeds one agent when its account changes) and the disconnect path (which
re-seeds affected agents when an account goes away). It is NOT wired into
provisioning — IDENTITY.md is a workspace file that survives a config-rewrite
reprovision, so a seeded agent stays seeded with no provision-time re-seed.
That is also why a missing IDENTITY.md is a no-op here rather than a create:
first-write of IDENTITY.md belongs to the legacy friday/atlas pre-stage, and an
agent without a base file simply isn't seeded yet.
"""

from __future__ import annotations

import logging

from core.containers.workspace import Workspace, WorkspaceError, get_workspace
from core.observability.metrics import put_metric
from core.repositories import google_accounts_repo

logger = logging.getLogger(__name__)

# Markers delimiting the managed region inside IDENTITY.md. HTML comments so
# they render invisibly when the markdown is viewed, yet are trivially
# greppable for the in-place rewrite. The agent never needs to see them.
BEGIN_MARKER = "<!-- BEGIN gog-account -->"
END_MARKER = "<!-- END gog-account -->"


def _render_block(email: str) -> str:
    """The managed-block body for a resolved account.

    Imperative + scoped: the agent must always pass ``-a <email>`` for any
    Google action so it never acts as the wrong account (its own default or,
    worse, another agent's).
    """
    return (
        f"{BEGIN_MARKER}\n"
        "## Google account\n"
        f"Your Google account is `{email}`. For any Gmail / Calendar / Drive "
        f"action, ALWAYS run `gog -a {email} …` — never bare `gog`, and never "
        "another agent's account.\n"
        f"{END_MARKER}"
    )


def _replace_or_append_block(existing: str, block: str) -> str:
    """Return ``existing`` with the managed block updated in place or appended.

    If both markers are present, the region between them (inclusive) is
    replaced — leaving every other byte of IDENTITY.md untouched. Otherwise
    the block is appended to the end, separated by a blank line so it reads
    as its own section.
    """
    begin = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if begin != -1 and end != -1 and end > begin:
        end_close = end + len(END_MARKER)
        return existing[:begin] + block + existing[end_close:]
    # No existing block: append. Normalize trailing whitespace to a single
    # newline so the appended section is cleanly separated and re-runs with
    # the same email are byte-stable.
    return existing.rstrip("\n") + "\n\n" + block + "\n"


def _strip_block(existing: str) -> str:
    """Return ``existing`` with the managed block removed (markers included).

    Used when the agent has no Google account — no account means no
    instruction. Collapses the blank-line separator left behind so removing
    and re-adding the block is content-stable.
    """
    begin = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if begin == -1 or end == -1 or end <= begin:
        return existing
    end_close = end + len(END_MARKER)
    before = existing[:begin].rstrip("\n")
    after = existing[end_close:].lstrip("\n")
    if before and after:
        return before + "\n\n" + after
    return before + after


async def seed_agent_google_identity(
    owner_id: str,
    agent_id: str,
    *,
    workspace: Workspace | None = None,
) -> None:
    """Seed (or clear) the agent's Google-account instruction in IDENTITY.md.

    Reads the agent's EXPLICIT assignment via ``get_assignment`` and rewrites
    the managed block:

    - explicit assignment → block holds the ``gog -a <email>`` instruction.
    - no assignment → block removed entirely (agent uses bare ``gog``).

    Idempotent: re-running with the same account produces byte-identical
    content (and skips the write entirely); re-running with a different
    account replaces the block in place rather than duplicating it.

    If IDENTITY.md doesn't exist yet this is a no-op. First-write of
    IDENTITY.md belongs to the friday/atlas pre-stage; the seeder only ever
    edits an existing file, never creates one — so an agent with no base file
    simply isn't seeded.
    """
    ws = workspace or get_workspace()
    rel_path = f"workspace-{agent_id}/IDENTITY.md"

    email = await google_accounts_repo.get_assignment(owner_id, agent_id)

    try:
        existing = ws.read_file(owner_id, rel_path)
    except WorkspaceError:
        # IDENTITY.md not seeded yet — nothing to edit and we never create it.
        return

    if email is None:
        updated = _strip_block(existing)
    else:
        updated = _replace_or_append_block(existing, _render_block(email))

    if updated == existing:
        return  # No change (block already absent / already current) — skip write.

    ws.write_file(owner_id, rel_path, updated)
    put_metric("google_identity.seed.cleared" if email is None else "google_identity.seed.applied")
