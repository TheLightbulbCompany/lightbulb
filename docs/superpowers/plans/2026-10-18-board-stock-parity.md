# Board stock parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the fork's own UI for blocked work, recovery, linked identifiers, the Blocked view, the attention desk and interaction audiences into our Teams app.

**Architecture:** Each item ports a fork `ui/src` component into `apps/frontend/src/components/teams/` (adapted to our tokens, `useTeamsApi` and shell links), plus the few Teams-BFF pass-throughs the fork server already serves. Backend first (two tasks), then one frontend task per spec item, each its own PR.

**Tech Stack:** Next.js 16 / React 19, SWR via `useTeamsApi`, react-markdown + remark, Vitest 3 + RTL; FastAPI BFF over `PaperclipAdminClient`.

**Spec:** `docs/superpowers/specs/2026-10-18-board-stock-parity-design.md`

Abbreviations: **F** = `~/Desktop/paperclip-fork.nosync/ui/src` (port FROM here; tag `v2026.1012.0-isol8.4`, identical `ui/src` to the checked-out `isol8.2`), **W** = `apps/frontend/src/components/teams`, **B** = `apps/backend/routers/teams`.

## Global Constraints

- Port from the fork, never newer upstream; a stock feature the fork lacks is skipped and listed in the PR (known: hover quicklook, locked chip for private/404 issues, "Technical details" fold, `detailLine`).
- Colours only through `globals.css` tokens (`src/__tests__/brandTokens.test.ts`).
- Every ported file opens with the header style of `W/issues/ExecutionBlockerNotice.tsx:1-18` ("Ported from upstream … @ v2026.1012.0-isol8.4 (MIT, (c) 2025 Paperclip AI)" + adaptations list).
- Links go through `teamsDetailUrl` / `shellUrlFromPaperclipPath` (`src/lib/shellNav.ts`), never router `Link`; data through `useTeamsApi().read/post/del/mutate`, never react-query.
- Board-only actions are open to every workspace member (owner ruling 2026-10-18): no extra gate.
- Nothing is built into the Teams Inbox page (`W/inbox/InboxPage`, due for deletion).
- Every BFF route with a path param depends on a fence or is in `ALLOWLIST` of `tests/unit/routers/teams/test_teams_company_fences.py` with a reason.
- Do not port fork recovery handlers that have no BFF route (re-issue, reconcile, break-glass, quarantine, workspace isolation).
- Dev acceptance: an agent drives Chrome against dev; screenshots on each PR. Codex reviews.

## Review Focus

1. Bare tokens that look like identifiers but are not (`GPT-4`, `UTF-8`, `ISO-8601`) must stay plain text: linking only for the company's `issuePrefix`. → Task 3 test.
2. Room messages and session transcripts must never be linkified (MarkdownView is shared). → Task 3 test.
3. A dismissed or snoozed desk item must not raise the Activity badge count. → Task 8 test.
4. A dismissal key contains `:` (`attention:abc`); restore must round-trip it through the URL. → Task 2 test.
5. A blocker the viewer cannot read (fetch 404) renders as its identifier, unlinked, not a crash or blank chip. → Task 5 test.

---

### Task 1: BFF — forward the blocked-view query params on `GET /teams/issues`

**Files:**
- Modify: `B/issues.py:81-150` (list route)
- Test: `apps/backend/tests/unit/routers/teams/test_issues.py`

**Interfaces:**
- Produces: `GET /teams/issues?attention=blocked&includeBlockedInboxAttention=true&includeBlockedBy=true&includeLiveDescendantSummary=true&limit=200` forwarded verbatim to upstream `GET /api/companies/{company}/issues`.

- [ ] **Step 1:** Write `test_list_forwards_blocked_view_params`: call the route with those four params; assert the admin client received `attention="blocked"` and the three `include*="true"` values. Add `test_list_drops_unknown_params` asserting an arbitrary `foo=bar` is not forwarded.
- [ ] **Step 2:** Run `cd apps/backend && uv run pytest tests/unit/routers/teams/test_issues.py -k "blocked_view or unknown_params" -v`. Expected: first FAILS.
- [ ] **Step 3:** Add the four names to the route's query allowlist (`attention` limited to `"blocked"`; the `include*` flags booleans) and pass them through the existing admin-client list call.
- [ ] **Step 4:** Re-run; expected PASS.
- [ ] **Step 5:** Commit `feat(teams): forward the blocked-view params on the issue list`.

### Task 2: BFF — attention desk params and inbox dismissals

**Files:**
- Modify: `B/attention.py:210` (feed route), `apps/backend/core/services/paperclip_admin_client.py` (near `list_attention` :1211)
- Test: `apps/backend/tests/unit/routers/teams/test_attention.py`, `tests/unit/routers/teams/test_teams_company_fences.py`

**Interfaces:**
- Produces:
  - `GET /teams/attention` additionally forwards `all`, `activitySince`, `activityUntil`.
  - `GET /teams/inbox-dismissals` → upstream `GET /api/companies/{co}/inbox-dismissals`.
  - `POST /teams/inbox-dismissals` body `{itemKey}` or `{itemKey, kind: "snooze", snoozedUntil}` → upstream POST.
  - `DELETE /teams/inbox-dismissals/{item_key:path}` → upstream `DELETE …/inbox-dismissals/{itemKey}` (URL-encoded).
  - Admin client: `list_inbox_dismissals(*, company_id, session_cookie)`, `create_inbox_dismissal(*, company_id, body, session_cookie)`, `delete_inbox_dismissal(*, company_id, item_key, session_cookie)`.
  - All use the caller's `ctx.session_cookie` (per-user dismissals; see `attention.py:16-24`).

- [ ] **Step 1:** Write tests: `test_feed_forwards_all_and_window`; `test_dismissal_create_snooze_passthrough`; `test_dismissal_key_with_colon_round_trips` (DELETE `/teams/inbox-dismissals/attention%3Aabc` reaches the client as `item_key="attention:abc"`); `test_dismissal_key_rejected_outside_pattern` (`foo:1` → 422; pattern `^(approval|join|run|attention):.+$`).
- [ ] **Step 2:** Run `uv run pytest tests/unit/routers/teams/test_attention.py -v`; expected FAIL.
- [ ] **Step 3:** Implement the three routes and admin-client methods; add `("DELETE", "/teams/inbox-dismissals/{item_key:path}")` to the fence `ALLOWLIST` with reason "company-scoped via ctx.company_id; per-user upstream".
- [ ] **Step 4:** Run the attention tests and `uv run pytest tests/unit/routers/teams/test_teams_company_fences.py -v`; expected PASS.
- [ ] **Step 5:** Commit `feat(teams): attention desk params and inbox dismissals`.

### Task 3: Identifiers become links in task markdown (spec item 4)

**Files:**
- Create: `W/shared/lib/issue-reference.ts` (port of `F/lib/issue-reference.ts`), `W/shared/components/TaskMarkdown.tsx`
- Modify: `W/shared/components/MarkdownView.tsx` (accept extra `remarkPlugins` and an `a` renderer hook), `W/issues/task-chat/TaskChatMarkdown.tsx`, and the task-surface consumers: `IssueDescription`, `CommentThread`, `DocumentRow`, `IssueThreadInteractionCard`, `TaskChatCompactInteractionCard`, `inbox/DecisionCard`, `approvals/ApprovalPayload`, `approvals/ApprovalDetail`
- Test: `W/shared/__tests__/TaskMarkdown.test.tsx`

**Interfaces:**
- Consumes: `read('/settings')` → `{ issuePrefix: string }`; `read('/agents')`.
- Produces: `remarkLinkIssueReferences({ knownPrefixes }: { knownPrefixes: string[] })`; `<TaskMarkdown body={string} />` — MarkdownView plus issue links (status icon via `StatusIcon`, href `teamsDetailUrl("issue", identifier)`) and the existing agent/user chips.

- [ ] **Step 1:** Write tests with `issuePrefix: "ISOA"`: `links bare ISOA-845` (one anchor, href contains `ISOA-845`); `leaves GPT-4, UTF-8, ISO-8601 plain` (no anchors); `does not link inside code spans`; `MarkdownView without TaskMarkdown leaves ISOA-845 plain` (room-message path).
- [ ] **Step 2:** Run `cd apps/frontend && pnpm vitest run src/components/teams/shared/__tests__/TaskMarkdown.test.tsx`; expected FAIL.
- [ ] **Step 3:** Port `issue-reference.ts` unchanged except the header; build `TaskMarkdown`; swap the listed consumers to it. Issue status for the icon loads on hover/focus via `read(hovered ? '/issues/'+id : null)`.
- [ ] **Step 4:** Re-run; expected PASS. Run `pnpm vitest run src/__tests__/brandTokens.test.ts`.
- [ ] **Step 5:** Commit `feat(teams): link task identifiers in task markdown`.

### Task 4: Execution blocker notice at fork parity (spec item 3)

**Files:**
- Modify: `W/issues/ExecutionBlockerNotice.tsx`, `W/shared/types.ts:649` (`ExecutionBlocker` gains `runStatus, runError, canContinue, canRetry, savedMessageCount` per fork `packages/shared/src/types/execution-projection.ts:1`)
- Test: `W/issues/__tests__/ExecutionBlockerNotice.test.tsx` (rewrite)

**Interfaces:**
- Consumes: existing wakeup `POST /teams/agents/{id}/wakeup` (`B/agents.py:812`).

- [ ] **Step 1:** Rewrite tests from `F/components/ExecutionBlockerNotice.tsx`: `says "Model unavailable." for native_provider_model_rejected`; `says "Recovery needed." otherwise, then runError then nextAction`; `shows the saved-message line when savedMessageCount > 0`; `Inspect run links to the run detail`; `Retry shown when canRetry, hidden when requiresInspection && !canRetry`.
- [ ] **Step 2:** Run the file; expected FAIL.
- [ ] **Step 3:** Port the fork body; Inspect run → `teamsDetailUrl("run", runId)`.
- [ ] **Step 4:** Re-run; expected PASS.
- [ ] **Step 5:** Commit `feat(teams): execution blocker notice at fork parity`.

### Task 5: Blocked notice on the task page (spec item 1)

**Files:**
- Create: `W/issues/IssueBlockedNotice.tsx` (port of `F/components/IssueBlockedNotice.tsx`), `W/issues/lib/issue-blockers.ts`, `W/issues/lib/successful-run-handoff.ts`, `W/issues/lib/liveIssueIds.ts` (ports)
- Modify: `W/issues/lib/recovery.ts` (add `deriveActiveRecoveryDisplayState`, `RECOVERY_CHIP_DEFAULT_TONE`, `recoveryChipLabel` from `F/lib/recovery-display.ts`), `W/shared/types.ts` (`Issue` gains `successfulRunHandoff: SuccessfulRunHandoffState | null`, `activeRecoveryAction`; `IssueRelationIssueSummary` :272 gains `priority, assigneeAgentId, assigneeUserId, terminalBlockers, activeRecoveryAction, scheduledRetry` per fork `issue.ts:212,577`), `W/issues/IssueDetailPage.tsx` (mount after `<ExecutionBlockerNotice>` :377)
- Test: `W/issues/__tests__/IssueBlockedNotice.test.tsx`

**Interfaces:**
- Consumes: `useIssueDetail(issueId)`; live ids from `read(teamsQueryKeys.inbox.liveRuns())` as `W/panels/IssuesPanel.tsx:167`; retry-now `POST /teams/issues/{id}/scheduled-retry/retry-now` (`B/issues.py:392`); `RetryErrorBand` from `W/issues/IssueScheduledRetryCard.tsx:312`.
- Produces: `<IssueBlockedNotice issueId issueStatus blockers allBlockers liveIssueIds blockerAttention successfulRunHandoff scheduledRetry agentName />` (fork props, :398).

- [ ] **Step 1:** Write tests: `amber notice lists unresolved blockers as links`; `shows "Ultimately waiting on" for a chain`; `blue "Waiting on live work" when blockerAttention.state is "covered", with "X of N done"`; `handoff shows "This task still needs a next step" and Retry now`; `handoff hidden when a recovery action is active`; `a blocker whose detail 404s renders its identifier unlinked` (Review Focus 5); `renders nothing when status is not blocked and there is no handoff`.
- [ ] **Step 2:** Run the file; expected FAIL.
- [ ] **Step 3:** Port, replacing `IssueLinkQuicklook` with a link + `StatusIcon`, `StatusGlyph` with `StatusIcon`, react-query with `read`.
- [ ] **Step 4:** Re-run; expected PASS.
- [ ] **Step 5:** Commit `feat(teams): blocked notice on the task page`.

### Task 6: Recovery card re-synced to 1012 (spec item 2)

**Files:**
- Modify: `W/issues/IssueRecoveryActionCard.tsx`, `W/issues/lib/recovery.ts`
- Test: `W/issues/__tests__/IssueRecoveryActionCard.test.tsx`

- [ ] **Step 1:** From `git -C ~/Desktop/paperclip-fork.nosync diff v2026.916.1-isol8.2 v2026.1012.0-isol8.4 -- ui/src/components/IssueRecoveryActionCard.tsx ui/src/lib/recovery-display.ts ui/src/lib/recovery-lineage.ts`, add tests: `native_run lane headline`; `workspace-export repair causes hide the matching resolve options`.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Apply the diff, dropping the `useWorkspaceIsolationControls` gate and any handler without a BFF route; bump the header tag.
- [ ] **Step 4:** Re-run; expected PASS.
- [ ] **Step 5:** Commit `feat(teams): recovery card at 1012`.

### Task 7: Blocked view in Work → Tasks (spec item 5)

**Files:**
- Create: `W/issues/BlockedTasksView.tsx` (port of `F/components/BlockedInboxView.tsx`), `W/issues/lib/blockedInbox.ts`, `W/issues/BlockedReasonChip.tsx` (ports)
- Modify: `W/issues/tasksViewMode.ts` (`TasksViewMode = "list" | "board" | "blocked"`; `getSnapshot` accepts it), `W/issues/TasksList.tsx` (`ViewModeToggle` :434-480, body switch :1171), `W/shared/types.ts` (`IssueBlockedInboxAttention/Reason/Severity` per fork `issue.ts:466-520`)
- Test: `W/issues/__tests__/BlockedTasksView.test.tsx`, `W/issues/__tests__/TasksList.test.tsx`

**Interfaces:**
- Consumes: Task 1's `GET /teams/issues?attention=blocked&…&limit=200`; `W/inbox/IssueRow.tsx`.

- [ ] **Step 1:** Write tests: `toggle offers Blocked and persists it`; `rows show reason chip, owner and stopped-age`; `groups Needs decision / Blocked chain stalled / Recovery required / External wait / Owner paused`; `sorts by Most urgent, Most recent, Longest stopped`; `empty state "No work is stopped."`.
- [ ] **Step 2:** Run both files; expected FAIL.
- [ ] **Step 3:** Port; fetch once with `read`.
- [ ] **Step 4:** Re-run; expected PASS.
- [ ] **Step 5:** Commit `feat(teams): Blocked view in Tasks`.

### Task 8: Attention desk replaces "Needs you" (spec item 6)

**Files:**
- Create: `W/inbox/AttentionDesk.tsx` (port of `F/pages/WhatNeedsMe.tsx` body, no page chrome), `W/inbox/AttentionQueueRow.tsx`, `W/inbox/DecisionShelf.tsx`, `W/inbox/lib/attention.ts`, `W/inbox/hooks/useInboxDismissals.ts` (ports; skip `DecisionTriageStrip` and join-request verbs, which are not proxied)
- Modify: `src/components/activity/ForYou.tsx`, `src/components/activity/activityPage.ts` (`filterForYou` "needs-you" :202 renders the desk instead of rows), `W/shared/types.ts:1801` (`AttentionItem` gains `dismissalKey, dismissal, decideBy, snoozedUntil, rank, shelf, decisionVerbs, inlineResolvable, detail, resolverAudience`)
- Test: `W/inbox/__tests__/AttentionDesk.test.tsx`, `src/components/activity/__tests__/ForYou.test.tsx`

**Interfaces:**
- Consumes: Task 2's routes. Desk fetch key `/attention?includeDismissed=true&all=true` (separate from `useAttentionFeed`'s `/attention`, which keeps driving the badge). `all=true` is never combined with `limit`/`cursor`.

- [ ] **Step 1:** Write tests: `shelves Decide now / New today / Earlier`; `row shows eyebrow, whyNow and inline verbs or Open`; `snooze then Snoozed section lists it with Restore`; `dismiss shows an undo toast and moves it to Dismissed`; `idle N days after 30 days`; `badge count ignores dismissed and snoozed items` (Review Focus 3); `ForYou "Needs you" renders the desk, other filters unchanged`.
- [ ] **Step 2:** Run both files; expected FAIL.
- [ ] **Step 3:** Port; mutate both desk and badge keys after a dismissal change.
- [ ] **Step 4:** Re-run; expected PASS.
- [ ] **Step 5:** Commit `feat(activity): attention desk replaces Needs you`.

### Task 9: Decision card check and interaction audience line (spec item 7)

**Files:**
- Create: `W/issues/InteractionAudienceLine.tsx`, `W/issues/lib/interaction-audience.ts` (ports)
- Modify: `W/issues/IssueThreadInteractionCard.tsx` (mount as fork :3682,3809), `W/shared/types.ts:1589` (`IssueThreadInteractionBase` gains `effectiveResolverPolicy, requestedResolverPolicy, effectiveResolverPolicySource, resolverPolicyProvenance, addresseeAgentId, addresseeUserId`), `W/inbox/DecisionCard.tsx` only where it differs from `F/components/DecisionCard.tsx`
- Test: `W/issues/__tests__/InteractionAudienceLine.test.tsx`

- [ ] **Step 1:** Write tests: `"Only <agent> can answer" for an agent addressee`; `board audience wording`; `no line when policy is absent`. Diff `W/inbox/DecisionCard.tsx` against the fork file; add a test for each behaviour found missing.
- [ ] **Step 2:** Run; expected FAIL.
- [ ] **Step 3:** Port and mount.
- [ ] **Step 4:** Re-run; expected PASS.
- [ ] **Step 5:** Commit `feat(teams): interaction audience line`.

### Task 10: Dev acceptance

- [ ] After each PR deploys to dev, drive Chrome against `dev.thelightbulb.company` (Clerk ticket sign-in, `docs/runbooks/local-dev-browser-verification.md`): a blocked task shows its notice above the timeline; `ISOA-` references are links; Tasks → Blocked lists and groups; Activity → Needs you shows the desk and snooze/dismiss/restore round-trip. Screenshots on the PR.
