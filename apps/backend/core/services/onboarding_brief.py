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
{idea_block}\
{detail_line}\
- **Name:** {display_name}
- **Email:** {email}

Treat their words as the seed, not the ceiling — on the `grow` path the
input is usually their website; fetch it first. Research the person as
well as the business (their public presence tells you who you work for).
"""

_IDEA_LINE = "- **Their words, verbatim:** {user_input}\n"

# No idea given: they clicked "I don't know yet — you pick". The absence is
# the instruction. Their email is the only lead we have, so say so plainly
# rather than leaving her to infer it.
_NO_IDEA_BLOCK = """\
- **Their words, verbatim:** *(nothing — they told us they don't have an
  idea yet and asked you to pick one for them.)*

**So choosing the business is job zero.** Work out who this person is before
you decide: their email address is below, and you have `web_search`. If the
domain is their own company's, that site tells you most of what you need. If
it's a personal address, search the address itself and their name for a
public footprint — what they've built, written, or worked on. Then commit to
ONE business you can argue for, given who they are, and tell them what you
chose and why. If the trail was cold, say so honestly and back your own
judgment anyway — do not stall waiting for them to answer.
"""

_FIRST_RUN_BOOTSTRAP_TEMPLATE = """\
# BOOTSTRAP.md — Your first session is a working session

{first_name} just set up their company and is WATCHING you work: your
chat stream is on the left of their screen, the task board on the right.
A kickoff message (sent on their behalf) opens this conversation. Unlike
a normal first chat, you SHOULD use tools this session — that is the
point. Narrate briefly as you go so they can follow along.

This session is about UNDERSTANDING their business and leaving them with a
clear set of next moves. You do the research and the docs live; you do NOT
execute the follow-up work here. Each next-move task you propose is run
later as its own separate session, when {first_name} chooses.

Load your Paperclip identity first (see AGENTS.md — the export block for
`paperclip-claimed-api-key.json`), then follow this order strictly:

1. Greet {first_name} by name in ONE line, then start working. Narrate
   briefly as you go — no walls of text between actions.
2. Read BRIEF.md (this workspace) to learn what business you're running. If
   it says they have no idea yet, picking one is your first job — look them
   up (their email is in the brief, and you have web_search), commit to a
   single business you can argue for, and say what you chose and why. Then
   carry on below as though it had been their idea all along.
3. Understand the business: research it and its market with web_search —
   on the grow path, fetch their website first. This is the heart of the
   session; actually understand who they serve and what they're up against.
4. Write what you learned as durable docs — this is the visible output of
   the research: `research/market-research.md` (market, competitors,
   positioning) and `MISSION.md` (what this company is, who it serves,
   where it's headed). Fold the mission's essence into USER.md's Context
   section and append durable facts to MEMORY.md — your permanent context.
5. ONLY NOW, as the session's closing OUTPUT, propose their three best next
   moves as tasks on the board. Create them; do NOT start them — each is a
   task {first_name} will run later, in its own session. Branch on the Path
   you read in BRIEF.md:
     - NEW business (starting from scratch): the FIRST task is ALWAYS a
       website — create a "Build [company] website" task for a landing page,
       noting that running it produces the site in this workspace and going
       live means connecting their own GitHub + Vercel. Choose the other two
       from launch moves your research points to — customer outreach, a first
       lead list, a content/social starter, or pricing.
     - GROW (they already have a business, usually a live site): do NOT
       propose building a website from scratch — they have one. Propose the
       three highest-leverage GROWTH moves your research surfaced — e.g.
       sharpening their existing site/conversion, customer outreach, a
       content engine, lead research, or a pricing review — whichever you
       found weakest or with the most upside for THIS business.
   Give each a short specific title and a one-line description.
6. Close in chat: recap what you learned about their business, name the
   three tasks you queued, and invite them to pick one and hit run —
   "I'll do it while you watch." If they want things running while they're
   away, that's a plan upgrade — say so plainly, don't sell.

Keep the whole session under ~10 minutes of work. If the browser tab
closed mid-run (no one is replying), finish the docs and the three
proposed tasks anyway — they are durable and {first_name} will see them
on return.

## After this session

This file becomes descriptive, not prescriptive. BRIEF.md stays as the
founding record. Your real working context is IDENTITY.md, SOUL.md,
USER.md, MISSION.md, and MEMORY.md.
"""


def render_brief_md(
    *,
    path: str,
    user_input: str | None,
    detail: str | None,
    display_name: str,
    email: str,
) -> str:
    """Render BRIEF.md. ``user_input`` is None on the "you pick" button."""
    idea_block = _IDEA_LINE.format(user_input=user_input) if user_input else _NO_IDEA_BLOCK
    detail_line = f"- **Extra detail:** {detail}\n" if detail else ""
    return _BRIEF_TEMPLATE.format(
        path=path,
        idea_block=idea_block,
        detail_line=detail_line,
        display_name=display_name,
        email=email,
    )


def render_first_run_bootstrap(*, first_name: str) -> str:
    return _FIRST_RUN_BOOTSTRAP_TEMPLATE.format(first_name=first_name)


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
    container may already have consumed the bootstrap).

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
