---
name: Lightbulb
description: A calm, borders-only workspace where an owner watches their AI company work; loud only on incidents.
colors:
  ground: "#ffffff"
  ground-deep: "#fafafa"
  ground-deeper: "#ededed"
  edge: "#e4e4e7"
  edge-soft: "#f0f0f2"
  ink: "#242428"
  ink-soft: "#45454b"
  ink-muted: "#66666e"
  ink-faint: "#85858d"
  brass: "#B68235"
  ok: "#3A6B45"
  info: "#35566F"
  warn: "#96501A"
  err: "#96382B"
typography:
  title:
    fontFamily: "Satoshi, Avenir Next, Segoe UI, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.4
  body:
    fontFamily: "Satoshi, Avenir Next, Segoe UI, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  meta:
    fontFamily: "Satoshi, Avenir Next, Segoe UI, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.4
  ui:
    fontFamily: "Satoshi, Avenir Next, Segoe UI, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.4
  caption:
    fontFamily: "Satoshi, Avenir Next, Segoe UI, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.4
  label-caps:
    fontFamily: "Satoshi, Avenir Next, Segoe UI, sans-serif"
    fontSize: "0.625rem"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0.12em"
  code:
    fontFamily: "Geist Mono, ui-monospace, SFMono-Regular, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  xs: "4px"
  sm: "6px"
  md: "8px"
  lg: "12px"
  xl: "16px"
  full: "999px"
spacing:
  "1": "4px"
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "5": "20px"
  "6": "24px"
  row: "36px"
  touch: "44px"
  bar: "56px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.ground}"
    typography: "{typography.meta}"
    rounded: "{rounded.full}"
    height: "36px"
    padding: "0 16px"
  button-outline:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    typography: "{typography.meta}"
    rounded: "{rounded.full}"
    height: "36px"
    padding: "0 16px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.meta}"
    rounded: "{rounded.full}"
    height: "36px"
    padding: "0 16px"
  button-ghost-hover:
    backgroundColor: "{colors.ground-deep}"
  button-icon:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.full}"
    size: "36px"
  button-icon-sm:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.full}"
    size: "32px"
  reaction-chip:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.meta}"
    rounded: "{rounded.full}"
    height: "32px"
    padding: "0 12px"
  reaction-chip-selected:
    backgroundColor: "{colors.ground-deep}"
    textColor: "{colors.ink}"
  agent-label:
    backgroundColor: "{colors.ground-deep}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.caption}"
    rounded: "{rounded.xs}"
    padding: "0 5px"
  unread-count:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.meta}"
    rounded: "{rounded.xs}"
    padding: "0 4px"
  directory-entry:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    height: "36px"
    padding: "5px 10px"
  directory-entry-active:
    backgroundColor: "{colors.ground-deeper}"
  rail-item:
    backgroundColor: "transparent"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.md}"
    width: "44px"
    height: "40px"
  rail-item-active:
    backgroundColor: "{colors.ground-deeper}"
    textColor: "{colors.ink}"
  composer:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: "12px"
  input:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.xs}"
    padding: "8px 12px"
  search-trigger:
    backgroundColor: "{colors.ground-deep}"
    textColor: "{colors.ink-muted}"
    typography: "{typography.caption}"
    rounded: "{rounded.sm}"
    height: "32px"
    padding: "0 8px"
  message-toolbar:
    backgroundColor: "{colors.ground}"
    rounded: "{rounded.sm}"
    padding: "2px"
  dialog:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "24px"
---

# Design System: Lightbulb

## Overview

**Creative North Star: "The Quiet Office"**

Lightbulb is the operating surface of a company staffed by AI agents, and the messaging workspace is where the owner watches that staff work. The system is built to be calm when healthy and loud only on incidents: one neutral ground, hairline rules instead of shadows, one weight of emphasis (600), and colour reserved for state. Nothing competes with the conversation; the only things that move are an agent actually running and the owner's own cursor.

Density is working-tool density, not marketing density. Rows sit on a 36px rhythm inside a 56px chrome bar, panels are delineated by 1px `edge` rules, and the hover toolbar is the single element allowed to lift off the page. On touch and narrow viewports the same grammar tightens to 12px gutters and grows every control to a 44px hit area; nothing is redesigned, only re-spaced.

This surface runs the **white ground**: inside `.workspace-shell` the brand's cream/midnight tokens are re-pointed to white and graphite (`--cream: #ffffff`, `--midnight: #242428`), with the same token names, so every component reads `var(--background)`, `var(--foreground)`, `var(--border)` and never a literal. The brand root (`:root`) still defines cream `#F5EFE2` / midnight `#0F1A2B` for the rest of the product; this document records the values the messaging surface actually renders.

**Key Characteristics:**
- One neutral ground with two darker steps (`ground-deep`, `ground-deeper`) that carry hover, selection and the sidebar; no coloured fills.
- Borders only. Depth is a 1px `edge` rule; the three exceptions (hover toolbar, Radix popover/dialog, the 860–1179px details overlay) are catalogued, not general.
- State colour marks text, an icon, a 2px rule or a 2px ring, never a fill, and never without its word or icon.
- One typeface (Satoshi) at 400/600 for chrome, rows and message prose alike; no second sans, no serif.
- Brass appears exactly once: the animated halo on a running agent's avatar.
- Focus is one recipe everywhere: `2px solid var(--ring)` outset 2px, keyboard-only.

## Colors

A single achromatic ramp (white to graphite) with one warm accent held in reserve and four state hues that never become surfaces.

### Primary
- **Ink** (`{colors.ink}`): all body copy, the active tab underline, the primary button fill, the unread dot, the focus ring on the light ground. It is `--foreground`, `--primary` and `--ring` at once; there is no separate brand colour on this surface.

### Secondary
- **Brass** (`{colors.brass}`): the running-agent halo, and nothing else here. Brass is 2.94:1 on the light ground, so it is a ring, never type and never a fill.

### Neutral
- **Ground** (`{colors.ground}`): the page, the composer, the message toolbar, popover and dialog surfaces. Cards are delineated by `edge`, not by a surface shift.
- **Ground Deep** (`{colors.ground-deep}`): the directory sidebar and rail background, the search trigger, `muted`/`accent`/`secondary` fills (selected reaction chip, agent label, skeleton bars, run-detail `pre`).
- **Ground Deeper** (`{colors.ground-deeper}`): hover and `aria-current` on directory entries, rail items and nav rows; the one step used for "you are here".
- **Edge** (`{colors.edge}`): every hairline: panel dividers, the tab strip's bottom rule, input borders, the toolbar border, the image thumbnail border.
- **Edge Soft** (`{colors.edge-soft}`): defined for the shell; not used by the messaging surface (observed).
- **Ink Soft** (`{colors.ink-soft}`): secondary text: sidebar nav labels, the inactive-group dock tab.
- **Ink Muted** (`{colors.ink-muted}`): `--muted-foreground`: timestamps, helper text, inactive tabs, group headings, rail icons at rest, placeholders.
- **Ink Faint** (`{colors.ink-faint}`): disabled and non-text only (dock scrollbar).

### State
- **Ok** (`{colors.ok}`): "Completed" work status text + check icon.
- **Info** (`{colors.info}`): "Working"/"Queued" status text, links in message prose (underlined), the reply-count button, and the 2px left rule on a live run.
- **Warn** (`{colors.warn}`): "Paused"/"Needs approval" status text, the uncertain-send status line, the paused-agent halo.
- **Err** (`{colors.err}`): "Failed" status, every inline error line, the attention-agent halo, the destructive button.

### Named Rules
**The Never-Fill Rule.** A state colour marks text, an icon, a 2px rule or a 2px ring. It is never a background. (Badge = border + text; live run = `border-left: 2px solid var(--info)`; halo = `0 0 0 2px`.)

**The Never-Alone Rule.** Every status keeps its word or icon: `WorkStatus` renders icon + label, the halo carries an `sr-only` label, the unread dot sits beside a bold row. Unknown state renders neutral (`ink-muted`), never ok.

**The One-Brass Rule.** Brass is the detail colour, spent on the running-agent halo. It is never body copy on the light ground and never a page fill.

**The No-Literal Rule.** Components read tokens only (`var(--foreground)`, `text-muted-foreground`); `node scripts/brand-tokens-scan.mjs src` must print 0.

## Typography

**Font:** Satoshi (self-hosted variable woff2, `--font-satoshi`; falls back to Avenir Next, Segoe UI, sans-serif). It is `--font-sans` and `--font-body` both, so display, chrome, rows and message prose are one face. The legacy `--font-serif`, `--font-source-sans` and `--font-lora-serif` tokens all alias it. The phone loads the same family (`Satoshi-Regular/Medium/Bold`).
**Mono Font:** Geist Mono (with ui-monospace, SFMono-Regular, monospace); code and run output only.

**Character:** One geometric sans at two weights. Emphasis is 600, never 700 and never lighter than 400 (the phone's semibold is Satoshi Bold, the only static cut the licence allows); hierarchy comes from size and `ink`/`ink-muted` contrast, not from a second face. The second sans (message prose) was retired on 6 Nov 2026: one face is how the product stays calm.

### Hierarchy
- **Title** (600, 18px, 1.4): the room name in the header; the empty-state heading. Also `font-semibold` at 16px for section headings ("Chats", "Room members", "Channel name").
- **Body** (400, 16px, 1.5): message prose, directory entries, composer text. Paragraphs inside a message carry 2px top / 4px bottom margins.
- **Meta** (400, 14px, 1.4): timestamps, helper and error lines, group headings (600, `ink-muted`), the unread count (tabular numerals), listen-mode selects, run details.
- **UI** (400, 13px, 1.4): sidebar "More" nav rows, dock tab strip.
- **Caption** (400, 12px, 1.4): the "Agent" label chip, composer help, the search trigger.
- **Label-Caps** (600, 10px, 0.12em, uppercase): the product's section-label recipe (`label-caps`); defined globally, unused on this surface.
- **Code** (400, 14px, 1.5, Geist Mono): fenced code in messages and the run-details `pre`.

### Named Rules
**The Two-Weight Rule.** Text is 400 or 600. Bold marks "unread" (directory row), "current" (active tab), and headings; nothing else.

**The One-Face Rule.** Message prose is Satoshi at 16px/1.5, the same face as the chrome around it. No surface switches into a second sans or a serif.

## Layout

The desktop shell is a three-column grid: a **56px rail** of global destinations, a **264px directory sidebar** (220px at ≤1000px), and a flexible main column. The main column carries a **56px header bar** (`edge` bottom rule, right-aligned tools, a `min(360px, 40vw)` search trigger) above the dock; when Messages is active the dock's tab strip collapses to 0 and the messaging workspace fills the slot.

The messaging workspace is itself `264px | 1fr`: `RoomDirectory` on `ground-deep` with an `edge` right rule, and `RoomPanel` on `ground`. The panel stacks a 56px room header (`8px 24px`), a tab strip (24px gap, 42px tabs, `edge` bottom rule), an optional 44px-row member list, the scrolling history (`16px 24px`, `overscroll-behavior: contain`), a 20px typing line, and the composer (`margin 0 20px 20px`, `padding 12px`).

**Room details / thread** is a 344px `room-context` column:
- **≥1180px:** a real third grid column with an `edge` left rule.
- **860–1179px:** an absolutely positioned overlay on the right edge, the one place a soft shadow is used to separate it from the conversation.
- **≤859px:** the directory stacks above the panel (max 36% height, `edge` bottom rule) and the context column replaces the conversation full-width.

**Spacing rhythm** is 4px-based: 4 / 8 / 12 / 16 / 20 / 24 for gutters and gaps; 36px is the row height (directory entries, nav rows, send button, dock strip); 56px is the chrome bar; 44px is the touch floor. Observed off-grid values exist (5px/10px directory entry padding, 6px gaps, 2px toolbar padding) but are not a second scale.

**Density and touch (≤859px, and `(hover: none)`/`(pointer: coarse)`):** gutters drop to 12px (header, history, tabs) and 8px (composer, with `max(8px, env(safe-area-inset-bottom))`); every button and `summary` grows to `min-height: 44px`; text inputs are pinned to 16px to stop iOS zoom; the message hover toolbar and the directory row actions become always-visible (the byline reserves 112px on the right for the toolbar); bylines and run controls wrap.

**Layers:** `--z-chrome: 20` (sticky bars), `--z-overlay: 50` (dialogs, menus, the details overlay uses 3 locally), `--z-toast: 60`.

## Elevation & Depth

Flat. Depth is conveyed by 1px `edge` rules and by stepping the ground one shade (`ground-deep` for the sidebar and rail, `ground-deeper` for the row you are on). There are no resting shadows on cards, panels, inputs or the composer; the composer signals focus by turning its border to `ring`, not by glowing.

Three shadows exist and are exhaustive. All are cast in the ground's own ink (`color-mix` on `--foreground`/`--midnight`), never neutral black.

### Shadow Vocabulary
- **Toolbar lift** (`box-shadow: 0 2px 6px color-mix(in srgb, var(--foreground) 10%, transparent)`): the `.room-message-actions` hover toolbar, because it overlaps message text and a border alone does not separate it. It also carries a 1px `edge` border.
- **Pop** (`--shadow-pop`: `0 4px 12px midnight 8%, 0 12px 32px midnight 10%`): Radix dropdown, popover and dialog content (`shadow-md`/`shadow-lg` map here). Owned by the shadcn primitives, inherited by the surface.
- **Overlay edge** (`box-shadow: -16px 0 32px color-mix(in srgb, var(--midnight) 12%, transparent)`): the 344px details overlay at 860–1179px only, where it floats over the conversation.

The agent halo (`0 0 0 2px`) is a ring drawn with `box-shadow`, not an elevation; see Components.

### Named Rules
**The Borders-Only Rule.** Surfaces are flat at rest and separated by `edge`. A shadow appears only on something that genuinely overlaps content (toolbar, popover, dialog, the tablet overlay), and it is drawn in ink, never black.

## Shapes

Small radii, with the pill reserved for controls. The theme declares four steps (4 / 8 / 12 / 16px, plus full); the messaging surface uses:

- **4px** (`xs`): the agent label chip, unread count, inputs and selects, toolbar buttons, skeleton bars, nav rows, listen-mode select.
- **6px** (`sm`, observed): directory entries, the message toolbar, the header search trigger.
- **8px** (`md`): the send button, image thumbnails, the creation dialog, dropdown content.
- **12px** (`lg`, observed as 10px): the composer.
- **Pill** (`full`): every `Button` (`rounded-full`), avatars (`rounded-full`), the halo, the unread dot.

Lines are 1px `edge`; the active tab and the live-run rule are 2px in `currentColor`/`info`. Images are clipped to 112×112 with `object-fit: cover`. Nothing is skewed, clipped diagonally or outlined in more than one colour.

## Components

### Buttons
Pill, semibold, quiet. All variants share `rounded-full`, `text-sm font-semibold`, a 120ms colour transition and `active:scale-[0.97]`.
- **Shape:** pill (`999px`).
- **Primary:** `ink` fill, `ground` text; `h-9 px-4` (36px). Used for "Create channel", "Save changes", the send button (`room-send`, 36×36 at 8px radius, the one non-pill).
- **Outline:** `ground` fill, `edge` border, `ink` text. "Try again", "Cancel", "Check message status", work-link buttons (`room-work-link`, wrapping, `min-height 36px`).
- **Ghost:** transparent, `ink` text; hover `ground-deep`. Directory actions, composer icon buttons, "Back", the reply-count button (`room-reply`, `info` text, 14px, 28px tall).
- **Destructive:** `err` fill, `ground` text. "Delete message" only.
- **Icon / icon-sm:** 36 / 32px squares, ghost by default, `active:scale-95`.
- **Disabled:** `opacity: 0.5`, `pointer-events: none`; filled variants flatten to `ground-deeper` + `ink-muted` rather than fading.
- **Focus:** the global `2px solid var(--ring)` outline, offset 2px, `:focus-visible` only.

### Chips
- **Reaction chip:** a `Button size="sm"` (32px, `px-3`, gap 6px): emoji + count. Unselected = ghost; selected = `secondary` (`ground-deep` fill, `aria-pressed`). Chips wrap with a 4px gap under the message.
- **Agent label:** a 12px `ink-muted` word "Agent" on `ground-deep` with a 1px `edge` border, 4px radius, `0 5px` padding, sitting in the byline after the author name.
- **Unread count:** 14px tabular numerals in a 1px `edge` box, 4px radius; the row itself goes 600. A 6px `ink` dot is the unread marker elsewhere.
- **Work status:** inline 14px icon (14px) + word, coloured per state (`ok`/`info`/`warn`/`err`, else `ink-muted`). No border, no fill.

### Cards / Containers
- **Corner Style:** the composer is the only card-like container (10px observed; the system step is 12px).
- **Background:** `ground`; the directory and rail are `ground-deep`.
- **Shadow Strategy:** none at rest (see Elevation & Depth).
- **Border:** 1px `edge` (`--input` for the composer, turning `ring` on `:focus-within`).
- **Internal Padding:** 12px (composer), 12px 16px (details body), 16px 24px (history), 24px (dialog header/body/footer).

### Inputs / Fields
- **Style:** 1px `edge` stroke, `ground` fill, 4px radius, `8px 12px` padding, `font: inherit`, placeholder in `ink-muted`; `caret-color` is `ink`.
- **Composer textarea:** borderless inside the composer card (the card is the field): `min-height 64px`, `max-height 200px`, vertical resize, `2px 4px` padding, no ring of its own; the card's border carries focus.
- **Focus:** `2px solid var(--ring)` outline offset 2px on inputs/selects; the composer swaps its border colour to `ring` instead.
- **Error:** an `err` 14px line replaces the help line; `aria-invalid` on the field. Selection is `primary` on `primary-foreground` throughout the surface.
- **Checkbox rows** (`room-choice`, `room-member-choice`): 44px / 40px rows, 16px native checkbox in `accent-color: primary`, hover `ground-deep`.

### Navigation
- **Rail** (56px, `ground-deep`, `edge` right rule): 44×40 stacked icon+9px-label buttons, `ink-muted` at rest, `ink` on `ground-deeper` for hover and `aria-current`, 7px radius (observed; the step is 8px), focus outline offset 1px.
- **Directory** (`ground-deep`): 14px 600 `ink-muted` group headings ("Channels", "People"); entries are 36px rows, `5px 10px`, 6px radius, icon or `sm` avatar + truncated name + optional count; hover and `aria-current` fill `ground-deeper`; the row's overflow menu is `opacity: 0` until hover/focus/open (always visible on `hover: none`).
- **Sidebar "More" nav:** 36px rows, 13px `ink-soft`, 4px radius, same `ground-deeper` hover/current treatment, focus outline in `ink`.
- **Room tabs:** a 24px-gap strip with an `edge` bottom rule; each tab is 42px tall, `ink-muted`, with a transparent 2px bottom border that becomes `ink` (and the text 600) under `aria-current="page"`. This is the same underline grammar as the dock tab strip and `PanelTabs`.
- **Header search trigger:** 32px, `ground-deep`, `edge` border, 6px radius, 12px `ink-muted`, a `kbd` hint behind an `edge` left rule; hover to `ground-deeper`/`ink`.

### Message Article (signature)
A flex row: avatar (32px `default`, 24px `sm` in lists) + content, `gap 12px` (8px on narrow), `8px 0` padding, no border between messages. The byline is `font-semibold` name (a button that underlines on hover), optional "Agent" chip, and a 14px `ink-muted` `<time>`; the body is sans prose at 16px/1.5 with `info` underlined links and a `border-l border-border` blockquote. A search hit gets `bg-accent` (`ground-deep`). Below the body, in order: reaction chips, image thumbnails (112px, 8px radius, `edge` border), the `WorkStatus` run line with a `<details>` for output (`ground-deep` `pre`, 200px max), outline work-link buttons, and the ghost reply-count button (`info`).

**Live run variant** (`room-live-run`): the same article with a 2px `info` left rule and 10px left padding; carries a "Thinking" `<details>` and, while running, a steer input + abort controls.

### Hover Toolbar (signature)
`.room-message-actions`: absolutely positioned top-right of the article, `ground` fill, 1px `edge` border, 6px radius, 2px padding, 2px gap of `icon-sm` ghost buttons (4px radius). Hidden (`opacity: 0; pointer-events: none`) until the article is hovered, focused-within, or its menu is open; always visible on touch. It is the one element on the surface with a resting shadow (see Elevation).

### Participant Avatar + Activity Halo (signature)
`Identity` renders a circular monogram/photo (`xs` 20 / `sm` 24 / `default` 32 / `lg` 40px). `ParticipantAvatar` wraps it in an inline pill and reads the agent's live run state onto `data-agent-state`:
- **running:** a 2px ring of `brass` at 68% that breathes to 4px at 20% (`agent-activity-halo`, 1.8s, `--ease-out`, infinite). Only real run state sets this attribute; nothing decorative triggers it.
- **attention:** a static 2px `err` ring.
- **paused:** a static 2px `warn` ring.
Each state ships an `sr-only` label ("Agent working" etc.). Under `prefers-reduced-motion: reduce` the running ring holds static at 2px brass; the messaging surface also zeroes every transition and animation within `.room-panel`, `.room-directory` and the creation dialog.

### Composer (signature)
A `ground` card with a 1px `--input` border (10px radius, `12px` padding, `margin 0 20px 20px`) whose border turns `ring` on `:focus-within`. Inside, top to bottom: the borderless textarea (3 rows, Enter sends, Shift+Enter breaks), an optional "Mentioning @…" meta line, image previews (112px thumbs with a ghost remove button), an actions row (`space-between`: ghost image and `@` buttons on the left, the 36×36 primary send on the right), the optional member picker (`edge` top rule, 44px checkbox rows, 240px scroll), a 12px help line ("Enter to send · Shift+Enter for a new line") that becomes an `err` line on validation error, and a `role="status"` line in `ink-muted` / `warn` (uncertain) / `err` (rejected). On narrow viewports it hugs the safe-area bottom with 8px margins.

### Dialog
The creation dialog: `min(520px, 100vw - 32px)`, `ground`, 8px radius, header `24px 48px 16px 24px`, scrolling body `0 24px 16px`, footer `16px 24px` behind an `edge` top rule. Uses the Radix `shadow-lg` pop shadow.

## Do's and Don'ts

### Do:
- **Do** read every colour through a token (`var(--foreground)`, `text-muted-foreground`, `text-err`); the scanner must print 0.
- **Do** separate surfaces with a 1px `edge` rule or one ground step (`ground-deep`, `ground-deeper`); never with a shadow at rest.
- **Do** mark state with text, an icon, a 2px rule or a 2px ring, and always pair the colour with its word or icon.
- **Do** keep emphasis at 600 and hierarchy at 18 / 16 / 14 / 13 / 12px on the 1.4–1.5 line-height band; message prose stays Satoshi at 16px/1.5.
- **Do** use the one focus recipe: `outline: 2px solid var(--ring); outline-offset: 2px` on `:focus-visible` only.
- **Do** give every control a 44px hit area at ≤859px and on coarse pointers, pin inputs to 16px there, and make hover-revealed toolbars always visible on touch.
- **Do** animate only under real state (the running halo) with `--dur-fast/base/slow` and `--ease-out`, and hold a static ring under reduced motion.
- **Do** mark "you are here" with `ground-deeper` + `ink` text on rows, and a 2px `currentColor` underline + 600 text on tabs.

### Don't:
- **Don't** fill anything with a state colour or with brass; brass is the halo ring only and is never type on the light ground.
- **Don't** use the `bulb` yellow on this surface: it is 1.45:1 on the ground and cannot be a ring, type or fill.
- **Don't** add a resting shadow to a card, panel, input or the composer; the toolbar, popover/dialog and the 860–1179px overlay are the complete list.
- **Don't** switch message bodies or headings into a second face, or drop below 400 / above 600 weight.
- **Don't** hand-type radii off the 4 / 8 / 12 / 16 / full scale; the 6, 7 and 10px values on this surface are observed drift, not a second scale.
- **Don't** add a second tab strip inside a panel; views inside a page are a `select`.
- **Don't** trigger the activity halo from anything but a live agent run, and don't render a status without its label.
- **Don't** use `:focus` for focus styling or carry a component-local ring; the global `:focus-visible` outline is the only recipe.
