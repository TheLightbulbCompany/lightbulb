# Message-First Workspace UI Design

**Status:** Approved direction; written specification awaiting review

**Date:** 2026-09-10

**Owner:** Lightbulb UI lane

**Reference:** Sphere's familiar channel-and-DM workspace structure, adapted to Lightbulb's existing visual language and product model

## Summary

Lightbulb's authenticated desktop workspace will become a compact, message-first interface where people and agents share the same rooms, direct-message list, message grammar, mentions, and membership model. The desktop shell will use three regions: a primary navigation sidebar, a central conversation, and a collapsible context panel.

The change is a UI convergence over the existing room, roster, Dockview, deep-link, and workspace systems. It does not introduce a second messaging model or decide which agents onboarding installs. The interface renders the canonical roster and rooms supplied by the backend.

## Goals

- Make messaging the default way to work with both people and agents.
- Present agents as coworkers without hiding that they are agents.
- Replace the current nested agent/session navigation with familiar channels and direct messages.
- Preserve Browser, Inbox, Teams, file viewers, approvals, and future movable layouts through the existing Dockview foundation.
- Keep the shipped layout fixed: rearrangement remains disabled and no layout-editing preference is exposed.
- Preserve current mobile behavior while improving the desktop workspace.
- Use real room, roster, run, unread, typing, membership, and readiness data. Never fabricate presence or placeholder agents.

## Non-goals and ownership boundary

This UI lane does not change:

- onboarding, welcome, first-message, trial, or pricing flows;
- provisioning, Paperclip seeding, marketplace installation, or the starter roster;
- the names, skills, routines, or availability of GStack agents;
- backend room, message, membership, or transport contracts;
- OpenClaw session APIs or container configuration;
- the mobile information architecture.

The concurrent onboarding/workspace effort in PR #2433 owns onboarding and commercial identity. This work consumes its reviewed result and does not overwrite its `WorkspaceShell` changes. Friday, Atlas, Gauge, GStack, and any future agents appear only when the canonical roster exposes them.

## Product model

### One communication grammar

Humans and agents use the same UI primitives:

- avatar and display name;
- direct message;
- channel or group-DM membership;
- mention target;
- chronological message;
- unread and typing/run feedback.

An agent receives a restrained `Agent` label where identity could otherwise be ambiguous, such as a message byline or profile detail. There is no robot-themed container, separate bot rail, or agent-only composer.

### Honest activity state

Agent activity comes from the existing durable roster status plus live `run_active` and `run_ended` frames exposed by `useRosterEntries`:

- running: a subtle 2px amber activity halo;
- paused or needs attention: a static warning/error indicator using the existing semantic tokens;
- idle, stopped, terminated, or unknown: no activity halo.

Reduced-motion mode renders the running halo statically. Human presence is shown only if a canonical human-presence source exists; the UI does not infer it from message recency.

## Desktop information architecture

```text
┌───────────────────────────────────────────────────────────────────────┐
│ 56px global header: workspace identity · search · utilities · profile │
├──────────────┬───────────────────────────────┬────────────────────────┤
│ 248px        │ flexible conversation         │ 320px context          │
│ primary      │ room header                   │ thread / room details  │
│ sidebar      │ message timeline              │ runs / members         │
│              │ composer                      │ approvals / linked work│
└──────────────┴───────────────────────────────┴────────────────────────┘
```

### Global header

The existing 56px `WorkspaceHeader` remains the global utility surface. It keeps search/command palette, file and browser actions, help, profile, and operational banners. Reset-layout is not promoted as a normal navigation action; recovery remains available when a stored layout is invalid.

### Primary sidebar

The desktop `WorkspaceSidebar` becomes the single primary navigation surface. Its order is:

1. workspace identity and compact primary destinations: Home, Activity, Tasks, Browser;
2. Channels, backed by canonical channel rooms;
3. Direct messages, containing people and agents in the same list;
4. More, containing Agents, Money, Automations, Leads, Library, Accounts, and the conditional Domain & Site destinations;
5. Marketplace and settings/profile access at the bottom.

Search remains in the header instead of taking another sidebar row. Activity maps to the existing Inbox destination. All existing company destinations remain reachable through the sidebar or command palette.

Channel and DM rows support the current unread count, selected state, loading/error state, access-removed state, and create-conversation flow already implemented by `RoomDirectory`. Agent management remains available through the existing Agents destination and Marketplace; it is not conflated with opening a conversation.

The legacy `AgentRoster` and nested `SessionList` stop being the primary desktop navigation. Their backend/session capabilities are not deleted by this UI project.

### Center conversation

The center shows one canonical room at a time:

- room identity and membership summary in the header;
- chronological messages and unread boundary;
- typing, streaming, tool, reconnect, retry, and live-run feedback;
- the existing room composer, mentions, drafts, attachments, and send behavior.

Selecting a channel or DM changes the active room without creating another desktop navigation rail. Opening an agent from an agent card or deep link selects that agent's canonical DM when one exists, using the current room-resolution behavior.

### Context panel

On wide desktop, a 320px collapsible panel occupies the right region. Its default room context is composed only from data already available to `RoomPanel`: members and active runs. Selecting a thread, approval, or linked-work item replaces the default context with that detail.

Closing the panel returns focus to the invoking message or control. Context-panel state is local UI state; no new server preference or synchronization contract is added.

## Responsive behavior

- **1180px and wider:** sidebar, conversation, and 320px context panel can be visible together. The context panel is collapsible.
- **860–1179px:** the primary sidebar and conversation remain fixed; context opens as a 320px overlay and is closed by Escape, its close button, or returning to the conversation.
- **859px and narrower:** retain the current mobile messaging composition and navigation. Thread/context detail replaces the conversation with the existing back behavior.

The center column retains a usable composer width before auxiliary content is shown. Dockview auxiliary tabs do not force the message column below its minimum width.

## Visual direction

The interface keeps Lightbulb's current identity rather than copying Slack's purple shell or Sphere's brand styling.

- surfaces: `--cream`, `--cream-deep`, and `--cream-deeper`;
- text and active emphasis: `--midnight`;
- separators: `--edge` and restrained surface shifts instead of floating cards;
- accent: amber for active agent work, existing semantic colors for warning/error/success;
- type: Source Sans 3 for interface and message content, with Lora reserved for selective product/editorial moments;
- density: a 4px spacing rhythm, compact rows, small radii, and visible focus rings;
- motion: short 160–180ms opacity/transform transitions, disabled or made static under reduced motion.

The signature detail is the data-driven activity halo around an agent avatar. It is intentionally quiet and never appears without a real state.

## Component and data architecture

### Reuse the existing messaging system

`MessagingWorkspace` currently owns the complete orchestration for rooms, roster hydration, drafts, mentions, threads, typing, room creation, and live states, then renders `RoomDirectory` beside `RoomPanel`. Desktop needs those two presentational components in different shell regions.

The smallest reusable split is one shell-level messaging provider/context holding the existing orchestration. It has two compositions:

- desktop: `WorkspaceSidebar` consumes the directory state and actions; `ChatPanel` consumes the selected room panel state and actions;
- mobile: `MessagingWorkspace` continues to render the existing directory-plus-room composition inside the same provider.

This is one state owner with two render arrangements, not a new messaging service or duplicate fetch layer. Existing hooks and API contracts remain authoritative.

### One Messages Dockview panel

Dockview remains the underlying desktop workspace engine with drag-and-drop disabled. There is one singleton Messages panel.

`workspaceController.openChatPanel(agentId)` activates that panel and updates its parameters with the requested agent rather than creating `chat:<agentId>` panels. `ChatPanel` already has a parameter subscription path through `useDockPanelParams`; it uses parameter changes to select the requested canonical DM.

Selecting any channel or DM in the primary sidebar also activates the singleton Messages panel and updates the shared selected-room state. Agent shortcuts and `?agent=` links resolve through the same path, so sidebar selection, panel parameters, and the visible conversation cannot drift apart.

This preserves auxiliary Browser, Inbox, Teams/control, approvals, and file-viewer panels without exposing panel rearrangement. It also eliminates the current duplicated shell sidebar plus nested `RoomDirectory` navigation.

### Deep links and stored layouts

- Existing `?agent=` links activate the singleton Messages panel and select the matching canonical DM.
- Existing panel links keep their current behavior.
- Layout recovery remains in place.
- Stored layouts containing multiple legacy `chat:<agentId>` panels are sanitized into one Messages panel. The selected/active legacy chat supplies the initial agent parameter when recoverable; duplicates are discarded.
- An invalid or unavailable agent falls back to the normal room selection behavior and never creates a phantom roster entry.

### Existing capability mapping

| Capability | UI destination after redesign | Existing source retained |
|---|---|---|
| Channels and DMs | Primary sidebar | `RoomDirectory`, room hooks |
| Conversation and composer | Center | `RoomPanel`, `RoomComposer` |
| Threads, members, runs | Right context panel | Existing `RoomPanel` context props |
| Browser | Primary destination / Dockview panel | Existing browser panel/controller |
| Activity | Primary destination | Existing Inbox panel |
| Tasks | Primary destination | Existing Teams/company navigation |
| Agents | More | Existing Teams agents surface |
| Marketplace | Sidebar footer | Existing gallery/marketplace panel |
| Files and approvals | Header/context/Dockview | Existing controllers and panels |

No new frontend dependency, API endpoint, persistence store, or feature flag is required for the first version.

## States and accessibility

The redesign preserves explicit loading, empty, error, reconnecting, retry, access-removed, and provisioning states. Empty channels and empty DM lists give a direct create-conversation action. Unknown roster state is visually neutral.

The primary sidebar uses navigation landmarks and `aria-current`. Channel/DM rows, panel toggles, unread indicators, and agent state have non-color labels. Keyboard users can move from sidebar to room header, timeline, composer, and context panel; Escape closes an overlay context panel. Focus is restored when context closes. Existing focus-visible tokens and reduced-motion behavior remain mandatory.

## Coordination and landing order

1. The active onboarding/workspace PR #2433 lands or publishes its reviewed `WorkspaceShell` result.
2. The UI branch starts from that result and preserves its onboarding/commercial identity changes.
3. Messaging state is split without changing contracts, followed by the shell/sidebar composition and singleton panel behavior.
4. Styling and responsive behavior land after the structural path is covered by tests.
5. The working spec and implementation plan are removed after the feature ships, consistent with repository policy; git history and the PR remain the durable record.

## Verification

Implementation is complete only when all of the following pass:

- focused unit tests for the shared messaging state and desktop/mobile compositions;
- controller tests proving repeated agent opens retarget one Messages panel;
- deep-link and stored-layout migration tests;
- existing room, composer, thread, unread, reconnect, and access-state tests;
- accessibility checks for landmarks, labels, focus restoration, Escape, and reduced motion;
- frontend lint, unit test suite, and production build;
- visual verification at 1440×900, 1100×800, and the existing mobile breakpoint, including long names, unread counts, active runs, empty states, and the right-panel overlay.

## Acceptance criteria

- Desktop has one primary sidebar; `RoomDirectory` is not rendered as a second inner sidebar.
- Channels and direct messages are the dominant navigation model.
- Humans and agents appear in the same DM and message grammar.
- Agent halos reflect real roster/run state and never guess.
- One Messages Dockview panel is retargeted when a different agent is opened.
- Browser, Inbox, Tasks/Teams, Agents, Marketplace, files, approvals, deep links, and layout recovery remain reachable.
- Dockview rearrangement stays disabled and has no user-facing toggle.
- Mobile retains its current composition and behavior.
- No onboarding, pricing, provisioning, starter-agent, marketplace-install, or backend messaging behavior changes are included.

## Execution goal prompt

> Rebuild Lightbulb's authenticated desktop workspace as a Slack-familiar, message-first interface using this specification as the source of truth. Deliver one 248px primary sidebar for destinations, channels, and direct messages; one flexible center conversation; and one collapsible 320px context panel on wide desktop, with the specified overlay and mobile behavior at narrower widths. Present humans and agents through the same room, DM, mention, membership, and message grammar. Use the existing Lightbulb cream/midnight/amber design system and expose an agent activity halo only from real roster and live-run state.
>
> Reuse the existing `MessagingWorkspace`, `RoomDirectory`, `RoomPanel`, room hooks, `useRosterEntries`, Dockview, workspace controller, URL reconciler, and layout recovery. Split messaging orchestration into one shared state owner only where required to render the directory in `WorkspaceSidebar` and the room in `ChatPanel`; keep the current joined mobile composition. Retarget one singleton Messages Dockview panel when a channel, DM, agent shortcut, or `?agent=` link is selected. Preserve Browser, Activity/Inbox, Tasks/Teams, Agents, Marketplace, files, approvals, deep links, and stored-layout recovery. Keep Dockview drag-and-drop disabled and expose no layout-editing toggle.
>
> Stay strictly in the UI lane. Do not change onboarding, pricing, provisioning, Paperclip seeds, starter-agent decisions, GStack packaging, marketplace installation, backend messaging contracts, or first-message behavior. Begin from the reviewed result of PR #2433 and preserve its `WorkspaceShell` work. Add the smallest tests that protect shared state, singleton panel retargeting, deep links/layout migration, responsive behavior, and accessibility. Finish by running the frontend unit suite, lint, production build, and visual checks at the specified desktop and mobile sizes. Do not push or deploy unless explicitly asked.
