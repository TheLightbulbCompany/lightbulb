# Friday — Dynamic Team Awareness

**Date:** 2026-05-20
**Status:** Approved (scope locked)
**Author:** isol8 platform
**Related:** [`2026-05-08-friday-identity-seeding-design.md`](2026-05-08-friday-identity-seeding-design.md)

## Problem

Friday's seeded `AGENTS.md` (the playbook written into her workspace at provision time, sourced from `_AGENTS_TEMPLATE` in `apps/backend/core/services/friday_identity.py`) contains a hardcoded **"## The in-house roster"** section that names eleven specific specialists — Pulse, Pitch / Pitch-Scout, Scout, Tally, Vera, Atlas, Echo, Ember, Lens, Ora, Thread — with one-line role descriptions for each.

This is wrong in both directions:

- **Lists agents the owner may not have.** Of those eleven, only Friday and Atlas are seeded by `paperclip_provisioning.provision_org`. The rest are marketplace listings the owner may or may not have hired. So Friday "knows about" Pulse even when no Pulse is on the team.
- **Misses agents the owner does have.** Anything the owner hires that isn't on the hardcoded list (a marketplace agent like Stitch, anything Atlas scaffolds, anything custom-created) is invisible to Friday's playbook. The template tells Friday her roster is the list — and Stitch isn't on it.

The template does include the line *"varies per {owner_display}'s installations — always trust `paperclipListAgents` over this list"*, but a hardcoded list right above that instruction overrides the disclaimer in practice. Friday treats the prose as authoritative.

The reported symptom: owner hires Stitch from the marketplace, asks Friday to use Stitch, Friday doesn't recognize the agent.

## Goal

Friday's identity describes **how she operates**, not **who is on her team**. The team is whatever Paperclip says it is, discovered at task time.

## Out of scope

Listed explicitly so the implementation plan doesn't drift:

- **Paperclip-side description threading.** Today the marketplace deploy calls Paperclip's `create_agent` with only `name` + `role`; the listing's `tagline` / `description_md` never travel to Paperclip's `title` / `capabilities` fields. That means `paperclipListAgents` / `paperclipGetAgent` return sparse data. We are deliberately not fixing this here. We will fix it as a follow-up if Friday's behavior is still inadequate after this change.
- **Atlas scaffold path.** Atlas-minted agents have the same sparse-Paperclip-row issue. Same follow-up.
- **Backfill for already-hired agents.** No script. Stitch may remain description-light in Paperclip until the follow-up ships.
- **Retro-fitting existing containers.** Containers already provisioned before this change ships keep their old `AGENTS.md` on EFS. Reprovision (or wait for natural EFS turnover) to get the new template. No migration script.
- **Edits to `IDENTITY.md` / `SOUL.md` / `USER.md` / `BOOTSTRAP.md`.** Only `_AGENTS_TEMPLATE` changes.

## Design

Two edits inside `_AGENTS_TEMPLATE` in `apps/backend/core/services/friday_identity.py`.

### Edit 1 — Roster lookup step (currently lines 314-317 of the live file)

The step today reads:

> 2. **Roster lookup.** Call `paperclipListAgents` to see who's currently on the team. You may also draw on your in-built knowledge of the in-house roster (below). New agents — from the marketplace or scaffolded by Atlas — show up in the roster automatically.

Rewrite so the roster is **only** what Paperclip returns — no "in-built knowledge" fallback:

> 2. **Roster lookup.** Call `paperclipListAgents` to see who's on the team. This is your only source of truth for who exists. For details on any teammate (role, capabilities), call `paperclipGetAgent`. New agents — hired from the marketplace or scaffolded by Atlas — show up here automatically.

### Edit 2 — Delete the "## The in-house roster" section entirely

Lines 333-359 of the live file (the `## The in-house roster` heading, the eleven bullet entries, and the closing "If {owner_display} asks about an agent not in your roster…" paragraph) are removed in full.

The closing instruction "call `paperclipListAgents` first before claiming they don't exist" is preserved in spirit by Edit 1 — Friday is now instructed to call `paperclipListAgents` as step 2 of every task, so the defensive check is no longer needed as a separate paragraph.

## Surrounding sections — left alone

For clarity, the rest of `_AGENTS_TEMPLATE` is untouched:

- Header + Paperclip-MCP intro (lines 266-277)
- "How you work — heartbeats" (lines 279-306)
- Orchestration loop steps 1, 3–6 (lines 308-313, 318-331) — only step 2 wording changes
- Triage — handling bubble-ups (line 361 onward)
- Boundaries, Cross-agent data flow, MCP tool reference

## Effects

- **New containers** provisioned after this lands seed an `AGENTS.md` with no hardcoded roster. Friday discovers the team from Paperclip on every task.
- **Existing containers** are unchanged on EFS. The owner can reprovision (`DELETE /api/v1/debug/provision` then `POST /api/v1/debug/provision`) to pick up the new template, or wait. No automatic migration.
- **Behavioral risk:** with sparse Paperclip rows (current state), Friday's `paperclipListAgents` returns names and roles but minimal capability text. She may have to call `paperclipGetAgent` per teammate, or ask the owner what an agent does. This is acceptable per the scope-down: the follow-up Paperclip-description work makes the data richer.
- **No regression risk** for the Friday + Atlas baseline: both are seeded by `provision_org` with `role="ceo"` / `role="recruiter"`, so they appear in `paperclipListAgents` with enough structure for Friday to delegate "hire a new specialist" to Atlas.

## Testing

- **Unit:** any existing snapshot / golden-file test on `_AGENTS_TEMPLATE` updates to match the new text. If no such test exists, none is added — the template is documentation Friday reads at runtime, not code with a return contract.
- **Manual verification:** reprovision a dev container, open Friday's `AGENTS.md` in the Workspace > Config tab (matches the screenshot path), confirm the in-house-roster block is gone and the new step-2 wording is in place.
- **No backend or frontend changes** — no API surface, no router, no schema. Just a string change in a template constant.

## Rollout

Single PR. Single-file change. Behind no flag. Lands on `main`, ships with the next backend deploy. New containers provisioned from that point forward seed the new template.

## Follow-ups (explicitly deferred)

Filed here so the next session has the context but doesn't pull them into this PR:

1. **Thread listing metadata into Paperclip on marketplace deploy.** Pass listing `tagline` → Paperclip `title` and `description_md` → Paperclip `capabilities` in `create_paperclip_for_marketplace` ([paperclip_agent_lifecycle.py](../../apps/backend/core/services/paperclip_agent_lifecycle.py)). Makes `paperclipListAgents` and `paperclipGetAgent` actually informative.
2. **Same treatment for Atlas-scaffolded hires.** Atlas writes a role spec when scaffolding; thread it into Paperclip the same way.
3. **Verify Paperclip's list/get response payloads include `title` + `capabilities`.** If they don't, either a small upstream Paperclip PR or have Friday loop `paperclipGetAgent` per teammate when she needs detail.
4. **One-time cleanup** for the owner's existing team entries with empty `title` / `capabilities` (Stitch on the reporter's container, primarily).

None of the above is required for this change to be correct. They make Friday *more* useful with the same Friday-side architecture.
