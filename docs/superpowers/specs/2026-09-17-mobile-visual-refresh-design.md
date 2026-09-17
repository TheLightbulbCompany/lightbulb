# Mobile visual refresh: depth, size, dark mode and gradients

**Status:** DECISION TAKEN 2026-09-17 (§3: depth + dark mode + gradients +
type scale, all four). No implementation until the plan is approved.
**Date:** 2026-09-17
**Scope:** the visual layer of the Expo iOS app (`apps/mobile/`).
**Out of scope:** mobile file attachments (owner deferred), new features, the
web app.

---

## 1. What the owner said

> *"What we have now in our mobile app is not bad. It's not bad at all. It's
> just a little confusing because there are no gradients. Everything's a little
> small and disorganized... a lot has to do with sizing because our text is
> very small, and it's not very easy to navigate... Even within chats, things
> don't have a gradient, and it's just a little harder to see."*

Approved scope: **targeted fixes, not a full redesign.** Type scale, depth,
and the DM/group split. The structure stays.

References supplied: Slack (sidebar + home), WhatsApp (chat list), Microsoft
Teams (chat list).

## 2. The finding that reframes this

**Every one of the four reference screenshots is a dark interface. Our mobile
app has no dark mode at all.**

`apps/mobile/src/config/theme.ts` says so in its own header:

> The web's `.dark` block has no mobile counterpart yet — dark mode is Phase B,
> keyed off `useColorScheme`.

Meanwhile the web shipped a full theme system three days ago —
`docs/superpowers/specs/2026-09-13-chat-theme-system-design.md`, **status:
implemented** — which diagnosed the *identical* complaint on web and named the
cause precisely:

> The planes are not separated... What should be three depths is two, at a
> separation the eye cannot resolve.

That spec's fix was a **ladder** (rail / sidebar / pane / raised / edge) split
across three independent layers, plus a dark toggle. It also explicitly
flagged that mobile was diverging and that "any theme work has to cover both
or the divergence gets worse."

So the honest reading of "no gradients, hard to see, everything's flat" is
not that mobile needs decorative gradients. **It is that mobile never received
the theme system the web just got**, and that the owner is comparing it
against four dark, depth-separated apps.

## 3. ⚠️ "Gradients" needs an explicit ruling

The Brand Identity Manual, as encoded in `theme.ts`, currently forbids most of
what a literal reading of "add gradients" would mean:

- *"bulb #F0C34E — never type, never a fill"*
- *"a state colour marks text, an icon, a border or a thin rule — never a
  large fill"*
- *"ALL color literals live here — screens import tokens, never raw colors"*
- cards are delineated by an `edge` border, **not** by a surface shift

There are no gradients anywhere in the system today, by design.

Three readings of the request:

| Reading | What ships | Cost |
|---|---|---|
| **A. Depth, not gradients** | port the web's plane ladder — genuinely distinct surface steps, so the list, the chrome and the message pane separate | no brand-manual change; matches what the web already proved |
| **B. Dark mode** | add the `.dark` counterpart to mobile, keyed off `useColorScheme` | the references are all dark; larger job; the web tokens already exist to copy |
| **C. Literal gradients** | decorative gradient fills in the chrome | **requires amending the Brand Identity Manual** |

**RULED 2026-09-17: all three, plus the type-scale bump.** The owner was shown
the brand-manual consequence of C and chose it anyway. That is a legitimate
call and it is recorded here so nobody "fixes" it back later — but it carries
two obligations that are not optional:

1. **The Brand Identity Manual must actually be amended.** Right now
   `theme.ts` and `globals.css` both encode "no large decorative fills" as a
   rule with reasons attached, and `src/__tests__/brandTokens.test.ts` plus
   `scripts/brand-tokens-scan.mjs` enforce the token discipline in CI. Adding
   gradients without changing the stated rule leaves the codebase asserting one
   thing and rendering another, and the next person to read the manual will
   revert it in good faith. **The manual change is part of the work, not a
   follow-up.**
2. **Gradients must not be mobile-only.** `theme.ts`'s entire premise is that
   it mirrors `globals.css` one-for-one, and the web theme spec explicitly
   warned that divergence "gets worse" if theme work covers only one surface. A
   gradient layer invented for mobile alone re-creates exactly the drift that
   spec was written to stop. Define it once, in the shared vocabulary, even if
   mobile is the only surface that consumes it at first.

**Sequencing recommendation stands even so: A and the type bump first.** They
are the cheapest and are what actually fixed this complaint on web — shipping
them first tells us how much of the problem was ever about gradients. B and C
follow. Do not start with C.

## 4. Design — what actually changes

### 4.1 Type scale

The complaint is "text is very small." The fix is a deliberate scale bump in
`theme.ts`'s text styles, not ad-hoc per-screen sizes.

Constraints that must survive:

- `apps/mobile/src/lib/textScale.ts` already handles Dynamic Type — rows stack
  at `fontScale >= 1.35`, and `useScaledHeight` grows fixed boxes. **A larger
  base must not break those thresholds**, and any box sized for N lines must
  still grow.
- Contrast values in `theme.ts` are measured and annotated per token (e.g.
  `mute` at 4.98:1, `muteSoft` at 3.28:1 which clears only the non-text bar).
  Larger type changes which tokens are legal where — text at a larger size has
  a *lower* contrast floor under WCAG, so this is an opportunity to use the
  softer steps correctly, not a licence to ignore the annotations.

### 4.2 Depth

Port the web ladder. Today mobile paints nearly everything `cream #ffffff`
with one step to `#fafafa` and a second to `#ededed` — a 2% separation the
web spec already judged unresolvable by eye. Give the chat list, the chrome
and the message pane genuinely distinct steps, and keep `edge` for hairlines.

The web's token values are the source. Do not invent a second palette —
`theme.ts`'s whole premise is that it mirrors `globals.css` one-for-one, and
that property is worth keeping.

### 4.3 Navigation and grouping

- Split group chats out of Direct messages —
  `SECTION_ORDER` in `app/(app)/(tabs)/home.tsx` and the `dms.tsx` tab. This
  is specified in `2026-09-17-messaging-sidebar-ia-design.md`; **that work
  lands first** and this spec only consumes it.
- Row density and tap targets in `RoomRow.tsx`: the references all give a list
  row a large avatar, a strong name, and a quiet one-line preview. Ours is
  tighter than all three.

### 4.4 Chat surface

`MessageGroup.tsx`, `RunBlock.tsx`, `ToolPill.tsx`. The same readability
problem the web is fixing on `feat/collapse-tool-activity` applies here —
`ToolPill` is mobile's equivalent of the tool trace. Keep the two surfaces
conceptually aligned; do not design a third pattern.

## 5. Sequencing

1. sidebar IA (separate spec) — unblocks 4.3
2. type scale + depth (4.1, 4.2) — one PR, the bulk of the perceived win
3. row density + chat surface (4.3, 4.4)
4. dark mode — ruled in; its own spec
5. gradients — ruled in, LAST, and paired with the Brand Identity Manual
   amendment (§3). Not before A lands, so we can see what depth alone fixed.

## 6. Risks

- **`theme.ts` is consumed by ~100 call sites** (the file notes 98 for `white`
  alone). A token whose meaning changes silently restyles screens nobody
  looked at. Change values, not meanings; add tokens rather than repurposing.
- **`config/__tests__/theme.test.ts` exists** and will likely encode current
  values. Failures there are the signal, not noise.
- **Divergence from web.** The whole point of `theme.ts` mirroring
  `globals.css` is that one brand decision lands in both. Any value invented
  only for mobile re-creates the exact drift the web theme spec warned about.
- **OTA gotcha** (CLAUDE.md): Metro inlines `EXPO_PUBLIC_*` at bundle time and
  `eas update` does not read `eas.json` build env. A visual-only change is
  OTA-safe, but do not bundle env-touching changes into the same release.

## 7. Testing

- `theme.test.ts` updated deliberately, with the new contrast ratios measured
  and annotated the way the existing tokens are.
- Snapshot/row tests for `RoomRow` at default and at `fontScale = 1.35`.
- `pnpm --filter @lightbulb/mobile test`, plus the `expo export` Metro gate
  that `mobile-ci.yml` runs.
- Manual check on a small device — the references are all large phones.

## 8. Open questions for the owner

§3 is settled — all four. Still open:

1. How large is "big enough" for text? A reference beats a number: point at a
   screen and say "this size." Without one, the type bump is a guess, and a
   guess that lands wrong costs another round trip through `theme.ts`'s ~100
   call sites.
2. Where should gradients actually appear? "In the chrome" is not specific
   enough to build. Naming two or three surfaces (the sidebar? the header? the
   active row?) is enough.
3. Does the Brand Identity Manual amendment need anyone else's sign-off, or is
   this decision sufficient?
