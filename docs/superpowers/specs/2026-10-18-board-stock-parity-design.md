# Board: stock Paperclip parity for blocked work, recovery and the inbox

## Why

A person who opens a blocked task cannot tell what is needed or from whom. On
2026-10-18 the founder could not see that ISOA-837 waited only on "add Gauge to
#GTM Strategy": the reason sat in the third paragraph of the newest of 20
timeline items, as a raw room UUID, spread over four tasks and an approval.

Stock Paperclip (upstream `origin/HEAD`, 2026-10-18) already solves most of
this, and our Teams port dropped it. Owner ruling 2026-10-18: bring our board
in line with stock first; our own improvements come after, on top.

## What stock does that we lack (the scope)

Paths are upstream `paperclip/` read at `origin/HEAD`; ours are under
`apps/frontend/src/components/teams/`.

1. **Blocked notice on the task page** — `ui/src/components/IssueBlockedNotice.tsx`,
   mounted with the other thread notices above the composer
   (`IssueChatThread.tsx` ~6630). Variants: amber "blocked by linked task(s)"
   with linked blocker chips (identifier, title, status, recovery badge, locked
   chip for private ones), "Ultimately waiting on", stalled-in-review,
   parked-blocker and reopen-suppressed wording; blue "Waiting on live work"
   (`blockerAttention.state === "covered"`) with an "X of N done · Y running"
   bar, step list and "This task resumes automatically"; and the
   successful-run handoff ("This task still needs a next step", the four
   options, run link, **Retry now**). We have none of it.
2. **Recovery action card** — `IssueRecoveryActionCard.tsx`: kind title
   ("Task Needs Next Step", "Board decision required"), owner and returns-to,
   source/corrective run, evidence, next action, attempt n of m and timeout,
   and labelled resolve options with descriptions (Try again / Mark task done /
   Send for review; false-positive options board-only). We never read
   `activeRecoveryAction`.
3. **Execution blocker notice wording** — stock `ExecutionBlockerNotice.tsx`
   leads with a plain sentence ("Recovery needed.", "Model unavailable.", …),
   puts `nextAction` under "Technical details" for workspace-repair cases, and
   offers **Inspect run** and **Continue/Retry**. Ours prints `nextAction`
   verbatim with Retry; bring it to stock's wording and buttons.
4. **Identifiers become links in all task markdown** — stock `MarkdownBody`
   runs `remarkLinkIssueReferences` (`lib/issue-reference.ts`: bare `ISOA-123`,
   `issue://`, `/issues/` URLs → status glyph + link + hover quicklook, locked
   chip on 404) and mention chips (`lib/mention-chips.ts`: agent, user,
   project, skill, routine). Ours (`MarkdownView.tsx`) resolves only explicit
   markdown links. Applies to descriptions, comments, approval and decision
   bodies.
5. **Inbox "Blocked" tab** — `BlockedInboxView.tsx` + `lib/blockedInbox.ts`:
   rows with a reason chip ("Pending board decision", "Unassigned blocker",
   "Parked blocker", "Owner paused"), owner identity and stopped-age; grouped
   (Needs decision / Blocked chain stalled / Recovery required / External wait /
   Owner paused); sort Most urgent / Most recent / Longest stopped; empty state
   "No work is stopped."
6. **Attention desk** — `pages/WhatNeedsMe.tsx`, `AttentionQueueRow.tsx`,
   `lib/attention.ts`: shelves "Decide now" (has `decideBy`) / "New today" /
   "Earlier"; per-row eyebrow (source label, linked task id, decide-by),
   `detailLine ?? whyNow`, audience line, inline verbs or **Open**; overflow
   Snooze / Dismiss (undo toast) / Open source; Snoozed and Dismissed sections
   with Restore; "idle N days" aging. Ours (`inbox/AttentionList.tsx`) shows
   `whyNow` and "Open thread", and approval rows offer Approve/Reject with no
   summary.
7. **Decisions and interactions** — confirm our `DecisionCard` matches stock
   `DecisionCard.tsx` (provenance line with linked ids, per-option effect
   preview, stale "N targets changed" from→to list, typed cancel-tree
   confirmation, "Dismiss — no effects", post-execution results), and add
   stock's `InteractionAudienceLine` to issue-thread interaction cards.

## Goals

- A person opening any blocked task sees, without scrolling the timeline, what
  it is waiting on and the stock action for it.
- The inbox separates stopped work (Blocked tab) from decisions (attention
  desk), with stock's grouping, aging and snooze/dismiss.
- No raw task identifier in task markdown stays unlinked.

## Non-goals (this pass)

- Changing agent instructions, approval payload shapes, or adding a typed
  "waiting on a person" record. That is the next pass, on top of parity.
- Upgrading the Paperclip fork to newer upstream (decision models #15473,
  confirmation-card restack #15895, recovery-hold notice #16169, …). Parity is
  with what our fork's server returns; where stock UI needs a field the fork
  lacks, skip that piece and list it.
- Mobile. Web first; the phone follows in its own pass (my call).
- OpenUI cards in task comments (a separate, small change).

## Anti-goals

- A second, home-grown design next to stock's. Where stock has a component,
  port it (adapted to our tokens and shell), do not reinvent it.
- Anything that lets a click take a consequential action the person could not
  see first (stock's effect previews and confirmations stay).

## Constraints

- Our app's look: neutral planes and tokens from `globals.css`
  (`brandTokens.test.ts` forbids colour literals). Ported components use our
  tokens, not stock's palette.
- Data comes through our Teams BFF (`apps/backend/routers/teams/`), which
  passes upstream JSON through; a field stock reads must reach the client
  (check `blockerAttention`, `activeRecoveryAction`, `executionBlocker`,
  attention `shelf`/`decideBy`/`detailLine`/snooze state on our fork).
- Ported-from-upstream modules carry the existing header saying so.
- Codex reviews; dev acceptance is an agent driving Chrome against dev with
  screenshots on the PR (no E2E suite).

## Decided

- **Board-only actions** (e.g. the recovery card's false-positive options):
  every workspace member may use them. Owner ruling 2026-10-18: every member
  already joins Paperclip as a board user, and that stays. No extra gate in the
  BFF or client.
- **Where items 5 and 6 live** (my call; the Teams Inbox page is due for
  deletion under the 2026-10-14 simplification, so nothing is built there):
  - The stock attention desk (item 6) replaces the "Needs you" filter of
    Activity → For you. Mentions, threads and DMs stay as they are; the
    shelves, snooze and dismiss apply to attention items only.
  - The Blocked view (item 5) is a view in Work → Tasks beside the board and
    list, since stopped work is task rows and work lives in Tasks.
- The fork (`v2026.1012.0-isol8.4`) already ships every stock component named
  above and returns `blockerAttention`, `activeRecoveryAction`,
  `executionBlocker`, `decideBy` and the inbox-dismissal routes. It does not
  return `detailLine`; rows fall back to `whyNow` as stock does.

## Left to the builder

- Port vs. adapt per component, and how to split the work into PRs (one per
  numbered item is a reasonable default).
- Where the notices sit in our layout, as long as they appear above the
  timeline on open, not only at its bottom.
- How the BFF exposes any missing field (pass-through vs. small route).
