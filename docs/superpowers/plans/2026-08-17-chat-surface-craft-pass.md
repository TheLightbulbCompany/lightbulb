# Chat Surface Craft Pass — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Isol8 agent surfaces readable and beautiful — plain-language activity narration in place of raw tool names, and the Beautiful UI patterns that genuinely fit, retokenized into the existing editorial system.

**Architecture:** Beautiful UI ships no package, no repo, and no registry — its components exist only as copy-paste text on a marketing page, and **17 of its 20 components accept zero props** (the other three take only `variant`/`fill`/`resettable`). So "adopt the library" is not available at any price. What *is* available, and what this plan does: **lift the markup and CSS, delete the module-level demo data and fake timers, and feed the same JSX from our own state.** Roughly half of each component's line count is canned data; the other half is layout worth having. Nothing enters `package.json`.

The narration layer is separate and larger-leverage: upstream OpenClaw already ships a complete tool→human-label map as generated data, and everything needed to use it is already on our wire. That makes the whole job a frontend PR with zero backend changes.

**Tech Stack:** Next.js 16 App Router, React 19, Tailwind CSS v4 (CSS-first `@theme`), shadcn/ui, SWR, lucide-react, vitest + jsdom, Playwright for render verification.

**Spec:** GitHub issue [#1871](https://github.com/Isol8AI/isol8/issues/1871) (supersedes #751). Research memo and the Beautiful UI source extraction live in the session scratchpad; the load-bearing findings are restated inline below so this plan stands alone.

---

## Global Constraints

- **Base:** `origin/main` (`045c43817`). Branch `feat/chat-surface-craft-pass`, worktree `.claude/worktrees/chat-craft`.
- **No new runtime dependencies.** Nothing added to `apps/frontend/package.json`. No `glimm`, no `liveline`, no `iconoir-react`, no `posthog-js` — all four are imported by Beautiful UI sources and all four are dropped.
- **Never copy Beautiful UI's `@theme` / token block.** Their palette is also `--ink`-led (`--ink`, `--ink-2`, `--ink-3`, `--surface`, `--line`) and we define `--ink: #1a1a1a` in bare `:root`. Pasting their tokens silently repaints our primary text colour app-wide. We retokenize *to* ours; we never import theirs.
- **Icons: `lucide-react` only**, and sparingly. Beautiful UI's "hand-drawn" glyphs were verified to be lucide geometry redrawn (their `run` glyph is `M4 17l6-5-6-5` + `M12 19h8`; lucide's `terminal` is `m4 17 6-6-6-6` + `M12 19h8`), and their SVG contract — `fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"` — *is* lucide's rendering spec. So there is nothing to gain by hand-copying paths. Replacing lucide app-wide is out of scope: it is **104 distinct icons** across 124 files, a design project of its own. File it separately if wanted.
- **Colours come from tokens, never hex.** Every new or touched line uses `var(--cream)`, `--cream-deeper`, `--cream-deepest`, `--edge`, `--edge-soft`, `--forest`, `--forest-soft`, `--forest-tint`, `--gold`, `--ink`, `--ink-soft`, `--mute`, `--mute-soft`, `--warn`, `--danger`. `--mute-soft` is **non-text only** (3.44:1 on cream — clears the 3:1 non-text bar, fails the 4.5:1 text bar). Tertiary text uses `--mute`.
- **Depth strategy is borders, not surface shifts** (`globals.css:105-108`). Cards and panels are delineated by `--edge`; `--cream-deeper` is the hover/selected wash. Do not introduce shadows or elevation tints.
- **Type:** body `--font-dm` (DM Sans), headings `--font-lora` (Lora), code `--font-geist-mono`. Monospace is reserved for content that IS code — literal shell commands, paths, diffs — never for decoration.
- **Both viewports, every task.** Desktop is the dockable workspace shell (`components/workspace/`); mobile is `ChatLayout` via `MobileChatShell` (`app/chat/page.tsx:96`). The breakpoint is exact and shared: `useIsDesktop` is `(min-width: 769px)`, the precise complement of `ChatLayout.css`'s `@media (max-width: 768px)`, so the JS and CSS branches cannot disagree. **Mobile also renders the Teams panels** — `InlineTeamsPanel` reuses the same `TeamsPanelRouter` — so Teams work is mobile work too.
- **Blast radius.** `MessageList` is rendered by four surfaces, not one: `AgentChatWindow` (chat), `WorkspaceDemo.tsx` (**the public landing page**), `teams/panels/RunDetailPanel.tsx` (Paperclip run transcripts, via `runLogToMessages`), and the mobile shell. Every change to it ships to all four. The landing demo must be re-rendered and checked.
- **Render before PR, every component.** Each task produces a side-by-side comparison at `docs/superpowers/plans/renders/<component>.html`, screenshotted at **1280px (desktop)** and **390px (mobile, iPhone-class)**, reviewed before the React is written. Serve with `python3 -m http.server` and view in Chrome; `@playwright/test`'s bundled chromium is unreliable on this machine — use `channel: 'chrome'`.
- **Tests:** `cd apps/frontend && ./node_modules/.bin/vitest run <path>`. Full suite before PR — vitest is RAM-heavy, so run it once at the end, not per task.

---

## The adoption map — all 20 Beautiful UI components

Verdicts are from reading the extracted source, not the marketing page. "Props" is what the component actually accepts.

### Taking (7)

| # | Beautiful UI | Props | What we take | Lands in |
|---|---|---|---|---|
| 1 | **Tool Chips** | none | Row layout, hover-chevron reveal, expand-in-place, `createPortal(…, document.body)` for detail overlays | **new** `chat/ToolActivityTrace.tsx`, replacing `ToolPill`/`ToolUseIndicator` |
| 2 | **Thinking** | `variant`, `onSettled` | Tense flip + settled duration + auto-collapse | `chat/MessageList.tsx:308` `ThinkingBlock` |
| 3 | **Task Rows** | `variant` | Status row vocabulary: running → failed → **retry** → completed, staggered entry, per-row detail drop-down | `teams/panels/RunDetailPanel.tsx`, `RoutinesPanel.tsx` |
| 4 | **Recommendation Card** | none | One primary recommendation + **Alternatives drawer**; picking one promotes it | `chat/HireCard.tsx` |
| 5 | **Code Block** | none | Live copy affordance, per-line streaming render | `chat/MessageList.tsx:115` `CodeBlock`, `chat/FileContentViewer.tsx` |
| 6 | **Selection Actions** | none | Contextual bar beneath selected text | **new** `chat/TranscriptSelectionBar.tsx` |
| 7 | **Insight Cards** | none | Mini-visualization carousel that yields to user input | `teams/panels/DashboardPanel.tsx` |

### Rejecting (13), with the reason

| Beautiful UI | Why not |
|---|---|
| **Prompt Bar** / **Chat** (composer) | 658 lines, `demo = true` by default, imports `glimm` (WebGL shader, no public repo, unrelated maintainer) and an unresolvable `@/components/atoms/Button`. Our `ChatInput.tsx` (598 lines) already has the slash menu, attachments, send/stop, queueing and a model pill. Take only `parseToken` — `/(^|\s)([@/])([\w-]*)$/` — *if* we ever add @-mentions. |
| **Streaming Text** | Has **no text prop**; word-streams a hardcoded `TOKENS` array on its own 55ms timer, and its citation chip is pinned to `SOURCES[0]`. Ours is driven by the real transport and is already correct. We also have no citation-shaped field on the wire, so the citation idea is a parser problem, not a component one — defer. |
| **Loading State** | `useElapsed` counts from **mount**, with no `startedAt` prop, so it resets on every remount and panel re-dock. We stamp a real `startedAt` at `useAgentChat.ts:1554`; elapsed off a real timestamp is five lines and strictly better. |
| **Approval Card** | The best-built of the twenty, but it solves a multi-question form, not approve/deny-a-shell-command. `ExecApprovalCards.tsx` stays. Add a "waiting for your approval" row in the trace that scrolls to it. |
| **Records Table** | An AI spreadsheet grid (columns as AI properties with config popovers). We have no such surface and inventing one is out of scope. |
| **Diff Table** | Per-hunk include/exclude before applying an edit. We have no diff-apply surface; agents apply edits in the container. |
| **Filter Table** | A strict subset of Records Table. |
| **Fine-Tune Card** | Drag-scrub numeric inspector. No surface wants it. |
| **Flowchart** | Draggable cards on a dotted canvas with measured connectors. `OrgChartPanel.tsx` is 49 lines and `LobsterGraphView.tsx` exists; a canvas editor is its own project. |
| **Sidebar Nav** | Direct-selection nav with search — a real fit for `ControlSidebar.tsx` / `WorkspaceSidebar.tsx`, but **PR #1868 is actively rewriting that rail** and touches `WorkspaceSidebar.tsx`. Deliberately deferred to avoid a conflict; revisit after #1868 lands. |
| **Search** | `SearchPanel.tsx` (232 lines) already works. Revisit only if #1868's patterns suggest otherwise. |
| **Context Cards** | Staged reveal of source cards; duplicates what the trace's expand already shows. |
| **Records/Diff/Filter shared popover** | Not applicable without the tables. |

---

## File structure

**Created**
- `apps/frontend/src/lib/toolDisplay/tool-display.json` — vendored upstream data (22 KB, 54 tools). **Done.**
- `apps/frontend/src/lib/toolDisplay/index.ts` — `resolveToolDisplay`, `toolRowLabel`, `summarizeToolRun`. **Done.**
- `apps/frontend/src/lib/__tests__/toolDisplay.test.ts` — 15 tests. **Done, green.**
- `apps/frontend/src/components/chat/ToolActivityTrace.tsx` — the consolidated trace.
- `apps/frontend/src/components/chat/TranscriptSelectionBar.tsx` — selection actions.
- `docs/superpowers/plans/renders/*.html` — one comparison per component (delete with this plan when the work ships).

**Modified**
- `chat/MessageList.tsx` — group consecutive tool blocks; retokenize; `ThinkingBlock`; `CodeBlock`; delete `TOOL_STYLES`, `ToolPill`, `ToolUseIndicator`.
- `chat/StreamingStatusBar.tsx` — drop `deriveStreamingLabel`.
- `hooks/useAgentChat.ts` — one line: carry `startedAt` onto the emitted `tool_use` block.
- `chat/HireCard.tsx`, `chat/FileContentViewer.tsx`, `teams/panels/RunDetailPanel.tsx`, `teams/panels/DashboardPanel.tsx`.

---

## The per-component loop

Every component task runs this loop. It is the process agreed for this work: **nothing is coded before its comparison render is reviewed.**

1. Build `docs/superpowers/plans/renders/<component>.html` — current markup on the left (reproduced class-for-class from `origin/main`, not a strawman), proposed on the right, using the real tokens and fonts.
2. Screenshot at 1280px and 390px. Serve locally and open in Chrome.
3. **Stop and get sign-off on the render.** Do not write React first.
4. Write the failing test.
5. Implement.
6. Run the task's tests.
7. Re-render the real component in the app at both widths and compare against the mock.
8. Commit.

---

## Task 0: Label layer — **COMPLETE**

**Files:** `lib/toolDisplay/{tool-display.json,index.ts}`, `lib/__tests__/toolDisplay.test.ts`

Vendored from fork tag `v2026.7.2-isol8.5` (`a4825b1152bf69c4ea76dcaa412252ca549d4cc6`, 2026-08-17), which matches the `dev.tag` pin in `openclaw-version.json`. Upstream's `scripts/tool-display.ts:65` `ensureCoreToolCoverage()` hard-fails their CI if any tool in `src/agents/tools/*.ts` lacks display metadata, so the map is complete at that version. **Ours is a copy — staleness is on us: re-copy on every `openclaw-version.json` bump.** Add that line to the OpenClaw upgrade checklist.

- [x] Vendored JSON with provenance header
- [x] `resolveToolDisplay(name, args)` → `{title, gerund, past, detail, kind}`
- [x] `mcp__openclaw__*` prefix strip — **the load-bearing case**: without it every `claude-cli` container falls through to the raw-name fallback, which is the exact bug being fixed
- [x] Per-action labels for multiplexed tools (`browser`, `message`)
- [x] Detail resolvers: read line-ranges, shell commands with pipeline collapse, search queries across six arg spellings, URLs
- [x] `summarizeToolRun` counting **distinct targets, not calls**
- [x] 15/15 tests green

---

## Task 1: Tool Activity Trace

**Comparison render: already built and signed off** (`compare.html` → becomes `renders/tool-activity-trace.html`).

**Files:**
- Create: `apps/frontend/src/components/chat/ToolActivityTrace.tsx`
- Modify: `apps/frontend/src/components/chat/MessageList.tsx` (delete `TOOL_STYLES` :352, `ToolPill` :404, `ToolUseIndicator` :479; group in `MessageBlocks` :556)
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (`syncToolStreamMessages` :772 — carry `startedAt`)
- Modify: `apps/frontend/src/components/chat/StreamingStatusBar.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/ToolActivityTrace.test.tsx`

**Interfaces:**
- Consumes: `resolveToolDisplay`, `toolRowLabel`, `summarizeToolRun`, `ToolDisplay` from `@/lib/toolDisplay`.
- Produces: `ToolActivityTrace({ calls, settled })` where `calls: ToolCall[]`, `ToolCall = { id: string; name: string; args: unknown; result: unknown; status: "running"|"done"|"error"; progressText?: string; startedAt?: number; endedAt?: number }`.

**The structural problem to solve first.** During a live run, `syncToolStreamMessages` emits **one `HistoryMessage` per tool call** (`useAgentChat.ts:772`, `ordered` maps entries 1:1). So `MessageBlocks` — where pills are pushed today — only ever sees a single tool block at a time on the live path. Folded history messages *do* carry multiple blocks. Both paths must produce one trace:
- **Live:** change `syncToolStreamMessages` to emit ONE assistant message whose `content` is every entry's blocks in order, rather than one message per entry.
- **History:** group consecutive `tool_use` blocks in `MessageBlocks`; a text block ends the group.

- [ ] **Step 1: Failing test — consecutive calls collapse into one trace**

```tsx
it("renders one trace for consecutive tool calls, not one per call", () => {
  render(<ToolActivityTrace calls={[
    { id: "1", name: "read", args: { path: "a.ts" }, result: "ok", status: "done" },
    { id: "2", name: "bash", args: { command: "npm test" }, result: "ok", status: "done" },
  ]} settled={false} />);
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByText(/Read a\.ts/)).toBeInTheDocument();
  expect(screen.getByText(/npm test/)).toBeInTheDocument();
  expect(screen.queryByText("read")).not.toBeInTheDocument();   // never the raw name
});
```

- [ ] **Step 2: Run it, confirm it fails** — `./node_modules/.bin/vitest run src/components/chat/__tests__/ToolActivityTrace.test.tsx`. Expected: cannot resolve `ToolActivityTrace`.

- [ ] **Step 3: Implement `ToolActivityTrace`** — rows on a `border-left: 1px solid var(--edge)` rule, 13px/1.5 DM Sans, verb in `--ink-soft`, detail in `--mute`, shell commands in `--font-geist-mono` 12px. Duration right-aligned in a fixed 38px slot, `tabular-nums`, `--mute-soft`, **rendered only above 2000ms** so a 40ms read does not jitter the column. Live row carries a single `--forest-soft` dot with a `--forest-tint` halo; no other row carries a mark. Failure rows use `--danger` text plus a 1px `--danger` left segment, with the error inline beneath and never auto-collapsed. Key by `toolCallId`.

- [ ] **Step 4: Failing test — settled folding**

```tsx
it("folds past three calls, stays expanded at three or fewer", () => {
  const call = (i: number) => ({ id: `${i}`, name: "read", args: { path: `f${i}.ts` }, result: "", status: "done" as const });
  const { rerender } = render(<ToolActivityTrace calls={[call(1), call(2), call(3)]} settled />);
  expect(screen.getAllByRole("listitem")).toHaveLength(3);
  rerender(<ToolActivityTrace calls={[1,2,3,4].map(call)} settled />);
  expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  expect(screen.getByText("Read 4 files")).toBeInTheDocument();
});
```

- [ ] **Step 5: Implement folding** — collapsed when `settled && calls.length > 3`. Expanding restores rows; expanding a row shows raw args + output exactly as `ToolPill` does today. **Nothing is destroyed, only folded** — this invariant is what makes the collapsing safe and lets us debug a production agent from a screenshot.

- [ ] **Step 6: Failing test — a stuck row does not lie**

```tsx
it("converts a row with no terminal event to an explicit unknown state", () => {
  // We have a known dropped-RPC failure mode. A gerund left spinning on a dead
  // step is the trust-destroying bug of this genre.
  vi.useFakeTimers();
  render(<ToolActivityTrace calls={[{ id: "1", name: "bash", args: { command: "sleep 999" }, result: null, status: "running", startedAt: Date.now() - 120_000 }]} settled={false} />);
  expect(screen.getByText(/outcome unknown/i)).toBeInTheDocument();
});
```

- [ ] **Step 7: Implement the stall guard**, then run all three tests — expect PASS.

- [ ] **Step 8: Carry `startedAt` onto the block** — in `syncToolStreamMessages`, add `...(e.startedAt ? { startedAt: e.startedAt } : {})` to the emitted `tool_use` block, and `endedAt: e.updatedAt` on terminal. History-loaded transcripts have no timing and must render without durations rather than with wrong ones.

- [ ] **Step 9: Wire into `MessageBlocks` and collapse the live emit** (both paths above), delete `TOOL_STYLES`/`ToolPill`/`ToolUseIndicator`, and strip `deriveStreamingLabel` from `StreamingStatusBar` — leaving only its honest job: showing `Working` when the run is server-active but this tab sees no stream.

- [ ] **Step 10: Verify all four `MessageList` consumers** — chat, mobile `ChatLayout`, `RunDetailPanel`, and `WorkspaceDemo` (landing page). Screenshot the landing demo specifically; a regression there is public.

- [ ] **Step 11: Both viewports** — 1280px and 390px. On mobile the duration column is dropped (not shrunk) and rows wrap to two lines rather than truncating the detail.

- [ ] **Step 12: Commit** — `git commit -m "feat(chat): consolidated plain-language tool activity trace"`

---

## Task 2: Thinking block

**Files:** Modify `chat/MessageList.tsx:308`; Test `chat/__tests__/MessageList.thinking.test.tsx`
**Render:** `renders/thinking.html`

The current block renders the literal string `"Thinking..."` at `:337` — present tense, forever, including in scrollback for reasoning that finished an hour ago.

- [ ] **Step 1: Comparison render + sign-off** (live "Thinking…" expanded vs settled "Thought for 23s" collapsed)
- [ ] **Step 2: Failing test**

```tsx
it("reads past-tense with a duration once settled", () => {
  render(<ThinkingBlock content="reasoning" durationMs={23_000} />);
  expect(screen.getByText(/Thought for 23s/)).toBeInTheDocument();
  expect(screen.queryByText(/Thinking\.\.\./)).not.toBeInTheDocument();
});
```

- [ ] **Step 3: Run it, confirm failure**
- [ ] **Step 4: Implement** — `streaming ? "Thinking…" : durationMs ? \`Thought for ${fmt(durationMs)}\` : "Thought"`. Keep the existing mount-expanded-while-streaming / mount-collapsed-in-history behaviour and the `userToggledRef` escape hatch; only the label and chrome change. Drop the boxed `--cream-deeper` header for an inline chevron + italic label, so a settled thought is one quiet line rather than a full-width bar.
- [ ] **Step 5: Run tests — PASS**
- [ ] **Step 6: Both viewports, then commit**

---

## Task 3: Retokenize `MessageList`

**Files:** Modify `chat/MessageList.tsx`; no new tests (visual — covered by the render check)
**Render:** `renders/palette.html`

`MessageList.tsx` carries **28 hardcoded hex values**, including a Material green/pink tool palette and the assistant's own body text:

| count | hex | is | should be |
|---|---|---|---|
| 8 | `#302d28` | assistant body + pill expansion text | `var(--ink)` / `var(--ink-soft)` |
| 4 | `#a5311f` | error pill text | `var(--danger)` |
| 3+3 | `#fce4ec`, `#f8bbd0` | Material pink error fill/border | `--danger` at low alpha + `--edge` |
| 2 | `#2d8a4e` | Material green running text | `var(--forest)` |
| 1 each | `#e8f5e9`, `#c8e6c9` | Material green fill/border | `--forest-tint` / `--edge-forest` |
| 1 each | `#f8f5f0`, `#f0ebe2`, `#ece7dc`, `#e8e3d9`, `#cdc7ba`, `#b5ae9e` | ad-hoc creams/greys | `--cream*`, `--edge*`, `--mute-soft` |
| 1 | `#c62828` | error dot | `var(--danger)` |

Most of the pink and green disappears with `TOOL_STYLES` in Task 1; this task finishes the rest.

- [ ] **Step 1: Comparison render + sign-off**
- [ ] **Step 2: Replace every hex**, checking each substitution against the token's documented role in `globals.css:43-94`. `--mute-soft` is non-text only.
- [ ] **Step 3: Assert none return** — `grep -c '#[0-9a-fA-F]\{6\}' MessageList.tsx` must be 0.
- [ ] **Step 4: Run the existing MessageList suite** (~50 test files reference this surface) — `./node_modules/.bin/vitest run src/components/chat`
- [ ] **Step 5: Re-render the landing demo** — `WorkspaceDemo.tsx` renders this component publicly.
- [ ] **Step 6: Both viewports, then commit**

---

## Task 4: Hire card → recommendation + alternatives

**Files:** Modify `chat/HireCard.tsx`; Test `chat/__tests__/HireCard.test.tsx`
**Render:** `renders/hire-card.html`

`HireCard` renders `parsed.listings.map(...)` — every suggestion flat, so five recommendations are a wall of five cards in the transcript. Beautiful UI's Recommendation Card is precisely the better shape: one primary, the rest behind an **Alternatives** drawer, and picking one promotes it to primary. Atlas's `recommend_agents` output is exactly this data.

- [ ] **Step 1: Comparison render + sign-off** (5 flat cards vs 1 + "4 alternatives")
- [ ] **Step 2: Failing test**

```tsx
it("shows one recommendation and hides the rest behind a drawer", async () => {
  render(<HireCard suggestion={{ kind: "isol8.hire_suggestion", reason: "r", listings: five }} />);
  expect(screen.getAllByRole("article")).toHaveLength(1);
  await userEvent.click(screen.getByRole("button", { name: /4 alternatives/i }));
  expect(screen.getAllByRole("article")).toHaveLength(5);
});
```

- [ ] **Step 3: Run it, confirm failure**
- [ ] **Step 4: Implement**, preserving the security posture in the file's docstring verbatim — the deploy button is the approval gate and goes through the authenticated marketplace route with the viewer's own session. **Nothing agent-side may mutate the roster.** Promoting an alternative changes only which card is primary; it must not deploy.
- [ ] **Step 5: Run tests — PASS**
- [ ] **Step 6: Both viewports** — the drawer is a bottom sheet on mobile, inline expansion on desktop. Then commit.

---

## Task 5: Code block

**Files:** Modify `chat/MessageList.tsx:115` `CodeBlock`, `chat/FileContentViewer.tsx`; Test `chat/__tests__/CodeBlock.test.tsx`
**Render:** `renders/code-block.html`

- [ ] **Step 1: Comparison render + sign-off**
- [ ] **Step 2: Failing test for the copy affordance** (copies exact source including trailing newline; announces success without a layout shift)
- [ ] **Step 3: Run it, confirm failure**
- [ ] **Step 4: Implement** — take Beautiful UI's copy-button placement and per-line render; keep our Prism highlighting and `resolveHighlightLanguage`. Do **not** take its 2 timers or `SEGMENTS` demo array.
- [ ] **Step 5: Run tests — PASS**
- [ ] **Step 6: Both viewports** — code blocks must scroll inside their own `overflow-x: auto` container; the transcript column must never scroll horizontally on mobile. Then commit.

---

## Task 6: Run status rows

**Files:** Modify `teams/panels/RunDetailPanel.tsx`, `teams/panels/RoutinesPanel.tsx`; Test `teams/panels/__tests__/RunDetailPanel.test.tsx`
**Render:** `renders/task-rows.html`

`RunDetailPanel:23-33` already models the exact state machine Task Rows animates — `running | queued | scheduled_retry` active, `succeeded | done | completed` terminal, `failed | error | cancelled | timed_out` failed — and already maps them to `--forest` / `--danger`. Task Rows' failed→retry→completed sequence maps onto `scheduled_retry` directly.

- [ ] **Step 1: Comparison render + sign-off**
- [ ] **Step 2: Failing test — a retry is a new row, not a mutated one**

```tsx
it("shows a retry as its own row, preserving the failure", () => {
  render(<RunRows runs={[{ id: "1", status: "failed" }, { id: "1r", status: "scheduled_retry", retryOf: "1" }]} />);
  expect(screen.getByText(/failed/i)).toBeInTheDocument();
  expect(screen.getByText(/retry/i)).toBeInTheDocument();
});
```

- [ ] **Step 3: Run it, confirm failure**
- [ ] **Step 4: Implement.** Reuse `LiveRunDot` — do **not** introduce a second liveness vocabulary (per #1868). Drop Task Rows' `TICKS` array and ring-sweep animation: our progress is real, not a 600/900/2400ms script.
- [ ] **Step 5: Run tests — PASS**
- [ ] **Step 6: Both viewports** — this panel renders on mobile through `InlineTeamsPanel`. Then commit.

---

## Task 7: Transcript selection bar

**Files:** Create `chat/TranscriptSelectionBar.tsx`; Modify `chat/MessageList.tsx`; Test `chat/__tests__/TranscriptSelectionBar.test.tsx`
**Render:** `renders/selection-actions.html`

New capability, not in #1871: select text in a transcript and act on it ("ask about this", "copy"). Beautiful UI's own note — *"the global theme owns its surface; this component only composes existing surface, ink, accent, radius and motion tokens"* — is exactly our constraint, which makes it the cleanest lift of the seven.

- [ ] **Step 1: Comparison render + sign-off**
- [ ] **Step 2: Failing test** (bar appears on selection within a message, carries the selected text to `ChatInput`, dismisses on deselect/scroll/Escape)
- [ ] **Step 3: Run it, confirm failure**
- [ ] **Step 4: Implement**, positioning through `createPortal(…, document.body)` — Beautiful UI does this deliberately *"so animated/translated reply wrappers cannot redefine the fixed-position coordinate system."* That is our documented `fixed`-overlay-trapped-by-a-hover-transform bug, already solved. Use it.
- [ ] **Step 5: Run tests — PASS**
- [ ] **Step 6: Both viewports.** Mobile needs care: the OS text-selection callout competes for the same gesture. If it conflicts, ship desktop-only and say so rather than shipping a fighting UI. Then commit.

---

## Task 8: Insight cards

**Files:** Modify `teams/panels/DashboardPanel.tsx`; Test `teams/panels/__tests__/DashboardPanel.test.tsx`
**Render:** `renders/insight-cards.html`

- [ ] **Step 1: Comparison render + sign-off.** If the dashboard has no data worth a carousel, **stop and cut this task** — a carousel of one card is worse than a card.
- [ ] **Step 2: Failing test** (autoplay yields permanently on first interaction; respects `prefers-reduced-motion`)
- [ ] **Step 3: Run it, confirm failure**
- [ ] **Step 4: Implement.** Charts follow the `dataviz` skill's palette rules, not Beautiful UI's `COLORS` array.
- [ ] **Step 5: Run tests — PASS**
- [ ] **Step 6: Both viewports, then commit**

---

## Task 9: Ship

- [ ] **Step 1: Full frontend suite** — `cd apps/frontend && pnpm test` (RAM-heavy; once, at the end)
- [ ] **Step 2: Typecheck** — `pnpm exec tsc --noEmit` (CI typechecks test files too)
- [ ] **Step 3: Lint** — `pnpm run lint`
- [ ] **Step 4: Final renders** at 1280px and 390px of every touched surface, including the landing demo
- [ ] **Step 5: Delete `docs/superpowers/plans/renders/` and this plan** — planning docs are disposable; the PR description is the durable record
- [ ] **Step 6: Add "re-copy `tool-display.json`" to the OpenClaw upgrade checklist**
- [ ] **Step 7: Open the PR** with before/after screenshots at both widths, and a component-by-component note on what was taken from Beautiful UI and what was rejected, with reasons

---

## Open questions

1. **Task 8 (Insight Cards)** is the weakest fit and the most speculative. It is last so it can be cut without disturbing anything.
2. **Sidebar Nav / Search** are deferred behind PR #1868, which is still OPEN and touches `WorkspaceSidebar.tsx` and `globals.css`. If #1868 lands first, revisit both.
3. **Citations** have no field on the wire. Parallel's keyless MCP returns sources as prose inside `tool_result` text, so surfacing them is a parser, not a component. Out of scope here.
4. **The `AgentHead` dead buttons** — copy / regenerate / share render with no `onClick` on every assistant bubble. Not in #1871. Recommend deleting them in Task 3 (one line each); wiring copy and share is trivial, regenerate is real work.
