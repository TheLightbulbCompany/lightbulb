# Workspace shell rehaul

**Branches:** `feat/workspace-shell-rehaul` (#1979) → `feat/workspace-rail-restructure`
(#1980) → `feat/workspace-completion-beats` (#1981), stacked in that order.
**Design source:** `~/Desktop/isol8-chat-previews/` — 13 screens, `index.html` is
the hub, `MAP.html` is the panel-by-panel inventory. Iteration 1 frozen at
`~/Desktop/isol8-chat-previews-v1/`.

## Why

The chat/control surface stacks seven navigation systems in one 244px rail, gives
every agent row four hover actions, and keeps connectors behind Settings. Density
measures around 8/10 against roughly 4/10 for the products it is compared to.
Almost none of the value the platform delivers is on screen: the default landing
state is an empty chat while agents are working and tasks are blocked.

## Decisions (settled, do not re-litigate)

| Question | Decision |
|---|---|
| `dreams`, `skill-proposals` | **Stay in Settings.** Out of scope for the rehaul entirely. |
| `DesktopUpdater` | **Already home** in the sidebar footer. Renders a version label in the browser and the updater UI only under Tauri. No action. |
| Wordmark typeface | **Keep Lora.** Georgia stays the fallback. `.brand-logo` is unchanged. |
| Activity | **Already functional.** A reskin, not a backend feature. See below. |

## Two corrections made while mapping

Both were wrong in the original scope and would have caused duplicated work:

1. **The `browser` PanelKind already exists.** `BrowserDockPanel` renders a noVNC
   live view, singleton-keyed to the container's one Xvfb display, and
   `useBrowserToolAutoOpen` surfaces it when an agent picks up the browser tool.
   Nothing to build.
2. **Activity already has a real feed.** `ActivityPanel` reads `useActivityFeed`
   off the BFF's `/teams/activity`, with `action` / `actorType` / `entityType`
   per event. The gap is display enrichment, not data: the panel's own header
   records that upstream's `agentMap` / `userProfileMap` / `entityNameMap` were
   never ported, so `ActivityRow` degrades to raw entity ids. That is a port,
   not a schema change.

`AgentInspectorPanel` also already carries five tabs including Channels.

## Shipped

- **#1979** Motion foundation, dock chrome, and a width floor for dock groups.
  `groupConstraints` guarded height but not width, while `layoutStorage`
  persists sash positions verbatim — one drag past the edge left a panel too
  narrow to render and every reload restored it.
- **#1980** Session disclosure and Connectors in the rail. Sessions could never
  have animated: `AgentRoster` mounted the element and its content in the same
  commit, so `grid-template-rows` had no start value.
- **#1981** The completion layer, wired to a resolved approval.

## Remaining — UI only

Each row is one PR. Ordered by risk, lowest first.

| # | Work | Notes |
|---|---|---|
| 1 | Agent page: add **Files** tab | `AgentInspectorPanel` already has the tab shell |
| 2 | Routines → own rail destination | Panel exists; this is a nav move |
| 3 | **Activity cleanup** | Port `agentMap` / `entityNameMap` so rows read as names, not ids. Reskin the row to the handoff shape from the preview. |
| 4 | Work: scope switch (Goals / Projects / Tasks) | New chrome over three existing panels |
| 5 | Work: group-by agent and project | Data-driven regroup |
| 6 | Work: create flow | Scope declares the type, so no type picker. Status glyph plus inferred chips. |
| 7 | Rail: one state glyph | Drops the status dot plus published dot |
| 8 | Rail: four row actions → one overflow | **Highest risk.** `ChatLayout.css` is dual-owned with the mobile shell and 13 test files assert on `.agent-item`. Alone in its own PR, mobile explicitly regression-tested. |
| 9 | Layout presets in the header | Needs dockview maximize/split API verified first |
| 10 | Completion beats on the remaining moments | Task done, routine completes, connector reconnects, agent hired |

## Remaining — backend first

Down to two, after the Activity correction.

| Item | What is missing |
|---|---|
| Dashboard → **Today** | A digest read model. "Needs you / working / finished" is not a rearrangement of `teams-dashboard`. |
| **Company brain** | Folder creation and upload endpoints. The EFS file-viewer and `teams-artifacts` cover part of it. |

**Dashboard → Ledger** is probably neither: `UsagePanel` already computes billed
spend with a per-model split, so it may be a reskin. Confirm before scoping.

## Not yet scoped

- **Chat streaming.** The likeliest source of "what we have now is just slow" —
  tokens snapping per chunk rather than settling per word, the thinking block
  staying expanded once the answer starts, the composer jumping. Independent of
  everything above and worth doing early.
- **Empty states**, every screen. A workspace with no agents, no tasks and no
  files is the first thing a new customer sees and none of that copy exists.
- **Provisioning.** Bind to real phases rather than a timer, and survive a step
  failing.
- **Calendar.** Does not exist in any form. Needs a product call on what it is
  for — scheduled routines and due dates, or real external sync — before it can
  be scoped.
- **Mobile** and the **Tauri shell.** Every decision so far is desktop-only.
- **Marketplace** reskin.

## Constraints held throughout

- URL grammar and panel keys unchanged. No deep link breaks.
- Palette, type and radii come from `globals.css`. Density and hierarchy problem,
  not a brand problem.
- Every interaction animates transform, opacity or colour only. Never a property
  that forces layout.
- Everything collapses under `prefers-reduced-motion`.
- No text that does not tell the reader something.

## Done means

`vitest run` green, `eslint` clean on touched files, `tsc --noEmit` adding no new
errors, and every control in `MAP.html`'s button inventory either wired or listed
in the PR body as deliberately deferred.
