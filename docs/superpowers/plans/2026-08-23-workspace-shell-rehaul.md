# Workspace shell rehaul

**Branch:** `feat/workspace-shell-rehaul` (off `origin/main` @ 12a88a9d4)
**Design source:** `~/Desktop/isol8-chat-previews/` (12 screens, `index.html` is the hub,
`MAP.html` is the panel-by-panel inventory). Iteration 1 frozen at
`~/Desktop/isol8-chat-previews-v1/`.

## Why

The chat/control surface stacks seven navigation systems in one 244px rail, gives
every agent row four hover actions, renders session lists with no disclosure
animation, and keeps connectors behind Settings. Density measures around 8/10
against roughly 4/10 for the products it is compared to. The value the platform
delivers is real and almost none of it is on screen: the default landing state is
an empty chat while agents are working and tasks are blocked.

## Shape of the work

Three phases, each independently shippable and independently revertible. The
design survives any of the later ones slipping.

### Phase 1 — Rail and shell (this PR)

The chrome everything else hangs off. No panel internals change.

1. **Rail restructure.** Roster first, then three destinations. One overflow menu
   per agent row replacing four hover actions. One state glyph replacing the
   status dot plus published dot. Real disclosure for sessions using
   `grid-template-rows: 0fr → 1fr` with staggered children.
2. **Persistent workspace bar.** Icon-only, on every screen, carrying the layout
   presets. This is the discovery path for split view, which today is reachable
   only by an undiscoverable drag gesture.
3. **Motion foundation.** Shared tokens and helpers: cross-document view
   transitions with the rail held still by name, sequenced pane swaps, 90ms press
   feedback, and a hard rule that no interaction animates a layout property.
4. **Dock chrome.** Panels become cards on the cream field rather than boxes cut
   into it. Sash gains a real hit area and a minimum-width clamp that announces
   itself instead of clipping content.
5. **Logo.** Align the preview and the app on one declaration.

### Phase 2 — Work and Agent

1. **Work** as one surface at three zoom levels (Goals / Projects / Tasks) with a
   travelling scope indicator, group-by (status / agent / project), and inline
   completion that advances the parent project ring.
2. **Create flow.** The scope declares what New means, so no type picker. One
   title field, a leading status glyph, and inferred chips for agent, project and
   due date.
3. **Agent page.** Overview / Files / Connectors / Channels / Skills / History,
   all scoped to one agent. Extends the existing `AgentInspectorPanel`.
4. **Routines** out of Work into its own destination, with expandable steps.

### Phase 3 — Dashboard

1. **Four readings of one business** behind a scope switch: Today, Company
   (org chart), Activity, Ledger.
2. **Completion beats.** Drawing checkmark, wash, segmented tracker, closing ring.
3. **Company brain.** Folder tree over the existing EFS file surface.

## Carved out — backend work, tracked separately

These are features wearing a UI, not reskins. Each is a separate piece of work and
the design degrades cleanly without it.

| Item | Why it is not UI |
|---|---|
| Dashboard digest | "Needs you / working / finished" is a new read model |
| Activity stream | Agent-to-agent handoffs are not recorded as durable events |
| Browser panel | No `browser` PanelKind, and no frame streaming to the web client |
| Company brain writes | Folder creation and upload do not exist |

## Undecided, needs a product call

- `dreams` and `skill-proposals` have no home in the new structure.
- `DesktopUpdater` needs a home; the account menu is the suggestion.
- Whether the wordmark becomes real Georgia, which means dropping Lora from
  `.brand-logo`'s stack rather than adding Georgia to it.

## Constraints held throughout

- URL grammar and panel keys are unchanged. No deep link breaks.
- Palette, type and radii come from `globals.css`. This is a density and hierarchy
  problem, not a brand problem.
- Every interaction animates transform, opacity or colour only.
- Everything collapses under `prefers-reduced-motion`.
- No text that does not tell the reader something.

## Done means

- `turbo run test --filter=@isol8/frontend` green
- `turbo run lint` green
- `pnpm run build` green
- Every control in `MAP.html`'s button inventory either present and wired, or
  listed in the PR body as deliberately deferred.
