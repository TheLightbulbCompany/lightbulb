"""First-run brief staging — the onboarding aha's OpenClaw-primitive layer.

Writes two files into Friday's agent workspace on EFS BEFORE her container
boots (same pre-boot window friday_identity.py uses; OpenClaw's
writeFileIfMissing honors pre-written files):

  - ``BRIEF.md``      — the user's funnel input, verbatim + identity context.
                        Written write-if-missing: its existence IS the
                        one-shot marker for the whole flow.
  - ``BOOTSTRAP.md``  — the first-run variant, unconditionally overwritten.
                        Ordering note: friday_identity seeds the standard
                        BOOTSTRAP with write_file_if_missing, so whichever
                        of (seed, brief-submit) runs first, the first-run
                        variant wins — no race.

The spec is docs/superpowers/specs/2026-07-12-onboarding-aha-first-run-design.md
(§4.B, §4.D). The visible auto-prompt lives in the frontend
(apps/frontend/src/lib/firstRun.ts) — keep the two in sync.
"""

from __future__ import annotations

from core.containers.workspace import Workspace

_BRIEF_TEMPLATE = """\
# BRIEF.md — Onboarding brief

The human just signed up and told us about the company they want run.

- **Path:** {path}  (new = starting something new, grow = existing business)
- **Their words, verbatim:** {user_input}
{detail_line}\
- **Name:** {display_name}
- **Email:** {email}

Treat their words as the seed, not the ceiling — on the `grow` path the
input is usually their website; fetch it first. Research the person as
well as the business (their public presence tells you who you work for).
"""

_FIRST_RUN_BOOTSTRAP_TEMPLATE = """\
# BOOTSTRAP.md — Your first session is a working session

{first_name} just set up their company and is WATCHING you work: your
chat stream is on the left of their screen, the task board on the right.
A kickoff message (sent on their behalf) opens this conversation. Unlike
a normal first chat, you SHOULD use tools this session — that is the
point. Board writes are what they see, so write to Paperclip early and
often.

Load your Paperclip identity first (see AGENTS.md — the export block for
`paperclip-claimed-api-key.json`), then follow this order strictly:

1. Greet {first_name} by name in ONE line, then start working. Narrate
   briefly as you go — no walls of text between actions.
2. IMMEDIATELY find the issue titled "Set up …" on the board, check out
   the issue, and comment "On it — researching your market now." Do this
   before any research. If no "Set up …" issue exists, create it yourself
   first (title "Set up <company>"), then check it out.
3. Quick pass: read BRIEF.md (this workspace). On the grow path, fetch
   their website once. Then create exactly 3 starter tasks on the board —
   short specific titles, one-line descriptions. Stubs are fine; you will
   enrich them in step 5.
4. Deep research with web_search: their market, competitors, positioning.
   Write findings to `research/market-research.md` in this workspace.
5. As findings land, update each starter task (better description or a
   findings comment). Re-title a stub if research invalidated it.
6. Write `MISSION.md` — what this company is, who it serves, where it's
   headed. Fold its essence into USER.md's Context section and append the
   durable facts to MEMORY.md. This becomes your permanent operating
   context.
7. Close the "Set up …" issue with a summary comment. Then close in chat:
   what you found, what you queued, and invite them to pick a task and
   hit run — "I'll do it while you watch." If they want it running while
   they're away, that's a plan upgrade — say so plainly, don't sell.

Keep the whole session under ~10 minutes of work. If the browser tab
closed mid-run (no one is replying), finish the artifacts anyway — they
are durable and {first_name} will see them on return.

## After this session

This file becomes descriptive, not prescriptive. BRIEF.md stays as the
founding record. Your real working context is IDENTITY.md, SOUL.md,
USER.md, MISSION.md, and MEMORY.md.
"""

_SETUP_ISSUE_DESCRIPTION = """\
Founding setup for {label}, run by Friday during onboarding:

- [ ] Research the business and market (see BRIEF.md in Friday's workspace)
- [ ] Create 3 starter tasks from the research
- [ ] Write the market-research doc and MISSION.md

Created automatically when the owner submitted their onboarding brief.
"""


def render_brief_md(
    *,
    path: str,
    user_input: str,
    detail: str | None,
    display_name: str,
    email: str,
) -> str:
    detail_line = f"- **Extra detail:** {detail}\n" if detail else ""
    return _BRIEF_TEMPLATE.format(
        path=path,
        user_input=user_input,
        detail_line=detail_line,
        display_name=display_name,
        email=email,
    )


def render_first_run_bootstrap(*, first_name: str) -> str:
    return _FIRST_RUN_BOOTSTRAP_TEMPLATE.format(first_name=first_name)


def setup_issue_payload(*, label: str, friday_agent_id: str) -> dict:
    """Body for PaperclipAdminClient.create_issue — raw camelCase keys
    (this call bypasses the Teams router's alias layer)."""
    return {
        "title": f"Set up {label}",
        "description": _SETUP_ISSUE_DESCRIPTION.format(label=label),
        "assigneeAgentId": friday_agent_id,
    }


def stage_first_run_files(
    *,
    workspace: Workspace,
    owner_id: str,
    friday_agent_id: str,
    brief_md: str,
    bootstrap_md: str,
) -> bool:
    """Write BRIEF.md (if missing) + first-run BOOTSTRAP.md (overwrite).

    Returns True on a fresh submit. Returns False when BRIEF.md already
    existed — a replay — in which case NOTHING else is touched (the
    container may already have consumed the bootstrap; the caller must
    also skip issue creation).

    Staging is all-or-nothing on the marker: BRIEF.md is the atomic claim,
    but if the BOOTSTRAP write then fails we roll the claim back by deleting
    BRIEF.md, so a retry re-stages BOTH files rather than seeing the marker
    and short-circuiting to a half-staged workspace (BRIEF present, first-run
    bootstrap missing). A genuine replay — both files already written — still
    returns False on the write_file_if_missing claim and leaves the possibly
    container-evolved BOOTSTRAP untouched.
    """
    brief_rel = f"workspaces/{friday_agent_id}/BRIEF.md"
    wrote = workspace.write_file_if_missing(owner_id, brief_rel, brief_md)
    if not wrote:
        return False
    bootstrap_rel = f"workspaces/{friday_agent_id}/BOOTSTRAP.md"
    try:
        workspace.write_file(owner_id, bootstrap_rel, bootstrap_md)
    except Exception:
        # Undo the marker so this staging isn't recorded as complete — the
        # next submit re-claims BRIEF.md fresh and re-stages both files.
        workspace.delete_file(owner_id, brief_rel)
        raise
    return True
