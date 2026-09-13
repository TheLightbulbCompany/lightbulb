# Chat theme system: three planes, two axes

Status: design, awaiting review
Date: 2026-09-13
Branch: `design/chat-page-color`
Scope: the visual token system for `/chat` (both shells), plus two user controls
Explicitly out of scope: shell consolidation, panel pruning, any layout or
information-architecture change (see *Out of scope* below)

## 1. Why

`/chat` reads as flat and undifferentiated. Three findings locate the cause
precisely, and only the first is about whiteness.

**The planes are not separated.** `globals.css:135` overrides the brand palette
to neutral inside `.workspace-shell`, setting `--cream: #ffffff` and
`--cream-deep: #fafafa`. `.workspace-rail` (`workspace.css:333`) and
`.room-directory` (`messaging.css:182`) *both* paint `var(--cream-deep)`. So the
icon rail and the channel sidebar are the same color, and that shared plane sits
2% away from the message pane. What should be three depths is two, at a
separation the eye cannot resolve.

**Agent identity carries no color.** `--presence-agent` is hardwired to
`--info`, so Friday, Atlas and every marketplace agent render as identical blue
squares with identical blue dots. For a product whose premise is a team of
distinct agents, the interface communicates the opposite.

**The two shells disagree.** `.workspace-shell` exists only on desktop
(`WorkspaceShell.tsx:451`). The mobile shell renders `.app-shell` /
`.cream-sidebar` and therefore falls through to the brand `:root` — so mobile
`/chat` is cream today while desktop `/chat` is white. Any theme work has to
cover both or the divergence gets worse.

## 2. Decision

Light stays the default. Dark is a toggle. Focus is a second, independent
toggle. Both controls live at the bottom of the rail.

The load-bearing idea, taken from Slack's focus mode: **color splits into three
layers that vary independently.**

| Layer | What it is | Varies with |
|---|---|---|
| Ladder | rail / sidebar / pane / raised / edge | theme |
| Hue | the brand tint carried by the chrome | focus |
| Semantic | unread, mention, presence, run state | nothing |

Slack can strip its aubergine in focus mode and lose nothing, because the hue
layer is decorative and the semantic layer is untouched. Structuring the tokens
this way makes focus mode nearly free — it is the hue layer set to neutral — and
it guarantees that no theme can ever make an unread badge or a failed run
ambiguous.

## 3. The four states

Two booleans, four combinations, all required to be legible.

| | Full color | Focus |
|---|---|---|
| **Light** (default) | Cool-biased greys, agent identity colors | Neutral greys, agents desaturated |
| **Dark** | Navy-biased planes, gold brand accent | Neutral dark, gold drops out |

Light + focus is deliberately close to what ships today. That is the point: it
is the quiet mode, chosen by the user, not the default everyone lands on.

## 4. Token values

Dark is biased toward the brand `--midnight #0F1A2B` rather than Slack's
aubergine, so it reads as Isol8. The pane is the *lightest* plane in both
themes — reading happens there, so it gets the calmest ground.

### Ladder

| Token | Light | Dark | Light focus | Dark focus |
|---|---|---|---|---|
| `--plane-rail` | `#D9DEE7` | `#0C121A` | `#DEDEDE` | `#101012` |
| `--plane-sidebar` | `#EBEEF3` | `#121B26` | `#EFEFEF` | `#171719` |
| `--plane-pane` | `#FFFFFF` | `#18222F` | `#FFFFFF` | `#1E1E21` |
| `--plane-raised` | `#F5F7FA` | `#212D3C` | `#F7F7F7` | `#27272B` |
| `--plane-edge` | `#DCE1E9` | `#2A3646` | `#E2E2E2` | `#323236` |

Light rail→sidebar→pane is a ~7% ladder where today's is 2% and shared.

### Type

| Token | Light | Dark |
|---|---|---|
| `--fg` | `#161C24` | `#E9EEF4` |
| `--fg-soft` | `#4D5866` | `#A7B4C4` |
| `--fg-mute` | `#6E7A88` | `#7D8B9C` |

### Semantic (never varies with focus)

| Token | Light | Dark | Means |
|---|---|---|---|
| `--link` | `#1E52CC` | `#8FB0FF` | links, `@mentions`, Send |
| `--alert` | `#B4402C` | `#E4705E` | unread counts, the New divider |
| `--live` | `#1F8A5B` | `#4BC08A` | human online, run succeeded |
| `--brand` | `#9A6B14` | `#F0C34E` | logo tile, trial bar, mentions-you stripe |

`--brand` is the hue layer's one participant in content; under focus it resolves
to `--fg-soft` in both themes.

### Agent identity

Three seeded colors drawn from the §06 state palette, assigned by stable hash of
`agent_id`, applied to the **avatar tile only**. Presence dots stay uniform —
presence and identity mean different things and must not compete.

| | Light | Dark | Focus (both) |
|---|---|---|---|
| a | `#8A5A12` | `#F0C34E` | `--fg-mute` |
| b | `#1E52CC` | `#7FA8F0` | `--fg-mute` |
| c | `#1C7350` | `#63C09A` | `--fg-mute` |

### Contrast

Measured against the pane each color sits on. Floor is 4.5:1 for text, 3:1
non-text, matching the bars already documented in `globals.css`.

| Role | Dark | Light |
|---|---|---|
| Body text | 13.8:1 | 16.4:1 |
| Secondary | 7.6:1 | 8.1:1 |
| Metadata | 4.6:1 | 4.7:1 |
| Links / mentions | 7.5:1 | 7.7:1 |
| Brand accent | 9.6:1 | 5.3:1 |
| Alert | 5.9:1 | 6.1:1 |
| Live | 7.9:1 | 4.6:1 |

Metadata (4.6 / 4.7) and light Live (4.6) are the tightest values; they clear AA
but leave little headroom, so they are the values to re-measure if any ladder
step moves.

## 5. Where the tokens live

Replace the `:root:has(.workspace-shell), .workspace-shell` block in
`globals.css` with four blocks scoped to attributes on the **document root**:

```
:root[data-chat-theme="light"]                              { … }
:root[data-chat-theme="dark"]                               { … }
:root[data-chat-theme="light"][data-chat-accent="focus"]    { … }
:root[data-chat-theme="dark"][data-chat-accent="focus"]     { … }
```

Root-scoped rather than `:has(.workspace-shell)` for two reasons: it covers both
shells (mobile has no `.workspace-shell`), and it reaches Radix portals rendered
under `<body>` without needing `:has()` at all — which is what the existing
block's comment says it was fighting.

The existing shadcn surface contract (`--background`, `--card`, `--muted`, …)
stays and is repointed at the new ladder, so the ~15 primitives in
`components/ui/` need no edits. `.dark` is left alone: `/admin` owns it.

## 6. Controls

**Desktop.** Two icon buttons at the bottom of `.workspace-rail`, inserted after
`workspace-rail__spacer` and before the existing Help / Settings / `UserButton`
group — moon/sun for theme, a target glyph for focus. Both are
`aria-pressed` toggles with `title` and `aria-label`. The rail bottom then
carries five items; acceptable, and it is where Slack puts the same control.

**Mobile.** No rail exists. The two toggles go in the `ChatLayout` sidebar
footer (`.sidebar-footer`, `ChatLayout.tsx:343` / `ChatLayout.css:735`), which is
the nearest equivalent bottom-left slot.

**Persistence.** `localStorage`, keys `isol8:chat-theme` and
`isol8:chat-accent`. Both read by a small inline script in `app/chat/layout.tsx`
that stamps the attributes on `<html>` **before first paint** — without it, dark
users get a white flash on every load, since the route is
`force-dynamic` and the server cannot know the preference. The script must be
inline and synchronous for that reason.

**Reduced motion.** The ladder transition is 220ms `--ease-out`; suppressed
under `prefers-reduced-motion: reduce`.

## 7. Component changes

Small and mostly mechanical — everything already reads through variables.

1. `workspace.css` — `.workspace-rail` moves from `--cream-deep` to
   `--plane-rail`. This is the change that creates the third plane.
2. `messaging.css` — `.room-directory` moves from `--muted` to
   `--plane-sidebar`; `.room-composer` and `.room-message-actions` move to
   `--plane-raised`.
3. `ParticipantAvatar.tsx` — avatar tile takes the hashed identity color;
   presence dot logic is untouched.
4. `WorkspaceHeader.tsx` — the two rail toggles.
5. `ChatLayout.tsx` — the same two toggles in the sidebar footer.
6. `app/chat/layout.tsx` — the pre-paint stamping script.
7. New `useChatTheme` hook owning both booleans, persistence, and the attribute
   writes.

## 8. Verification

- `scripts/brand-tokens-scan.mjs` must pass — every value above lands as a token;
  no hardcoded color enters `src/`.
- Unit test: `useChatTheme` round-trips both axes through `localStorage` and
  stamps both attributes.
- Unit test: the four token blocks each define every token named in §4, so no
  combination can inherit a value from another theme.
- Manual: all four states at desktop and mobile widths, confirming rail,
  sidebar and pane remain three resolvable depths in each.

## 9. Out of scope

Named because they came up and are deliberately *not* in this branch:

- Consolidating the two shells (dockview desktop / `ChatLayout` mobile).
- The five orphaned dark panels reachable only by `?panel=` URL surgery, already
  flagged for a keep-or-delete decision in `docs/audit-2026-05-04/SUMMARY.md`.
- Any change to panel count, navigation, or information architecture.

This branch changes what `/chat` looks like, not what it contains.

## 10. Risks

**The mobile/desktop divergence is pre-existing and this makes it visible.**
Mobile is cream today; after this change both shells share one ladder. That is
the fix, but it means mobile users see a larger change than desktop users do.

**Brand cream leaves the product surface entirely.** Light mode is white-based,
not cream. Cream survives on marketing routes only. That is a real identity
decision and the reason to reject this spec if it is the wrong one.

**Mention treatment changes.** The current blue-background `@mention` becomes a
brand-stripe treatment, because on a three-plane dark ground the blue fill
competes with the blue agent presence dots. Reversible in isolation.
