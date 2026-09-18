# Messaging sidebar IA: group chats, and where the join link lives

**Status:** Join-link placement DECIDED 2026-09-17 (§4). Grouping proposed.
No implementation until the plan is approved.
**Date:** 2026-09-17
**Scope:** the conversation sidebar on web + mobile, and the placement of the
workspace join link.
**Out of scope:** the visual refresh of mobile (see
`2026-09-17-mobile-visual-refresh-design.md`), tool-trace rendering, the
first-run experience.

---

## 1. Why

Two complaints from the owner, both about finding things.

**Group chats are hiding inside Direct messages.** The sidebar's
`Direct messages` group holds both one-to-one `dm` rooms and multi-party
`group-dm` rooms. The owner's objection is about the word, not the code:
*"anytime I hear 'direct message', I think one-on-one."* Slack is the
reference — its Direct messages section really is one-to-one, and a group
conversation reads as its own thing.

**The join link is buried.** Inviting a teammate lives in
`settings/WorkspaceMembersSection.tsx`. Nothing on the messaging page — the
page a new owner actually sits on — tells them where to add people. The owner
wants it next to the company name, top right.

## 2. What exists today

`apps/frontend/src/components/messaging/roomSections.ts` buckets every room:

```
starred → custom sections → channels → direct  (dm AND group-dm)
```

and carries a comment stating the current behaviour is deliberate:

> Direct messages (both `dm` and `group-dm` — the spec has no separate
> "Group chats" section).

So this is a **spec change, not a bug fix.** The mobile app mirrors it —
`apps/mobile/src/app/(app)/(tabs)/home.tsx` hardcodes
`SECTION_ORDER = ["Starred", "Channels", "Direct messages"]` and its
`dms.tsx` tab is documented as "Home's rows restricted to `dm`/`group-dm`".
Both consume the same room `kind` from `@lightbulb/shared`, so one contract
change serves both surfaces.

`RoomDirectory` also renders a `Sessions` group above the room groups, and
`Drafts` below them. The owner's list of what's in the sidebar — "Sessions,
External connections, Channels, Direct messages" — matches this plus the
Connections entry that recently moved onto the workspace rail.

## 3. Decision — sidebar grouping

Split the bucket in two. The display order becomes:

| Group | Holds | Shown when empty |
|---|---|---|
| Starred | any starred room | no |
| *(owner's custom sections)* | whatever the owner put there | no |
| Channels | `channel` | no |
| Direct messages | `dm` only | **yes** — it is the people roster |
| Group chats | `group-dm` | no |

Three properties this must preserve, because each is load-bearing today:

1. **Direct messages stays the roster.** `RoomDirectory` deliberately lists
   *people*, not the rooms that happen to exist (there is a comment saying so:
   "Slack's Direct messages section is the roster"). A person you have never
   messaged still appears. Group chats gets no such treatment — a group chat
   that does not exist cannot be listed, so the group is hidden when empty.
2. **A room the owner filed into a custom section stays there.** Custom
   sections already win over the kind-based buckets in `groupRooms`; adding a
   fifth bucket must not change that precedence.
3. **Starred still wins over everything.** Unchanged.

`Group chats` sits *below* Direct messages rather than above: DMs are the
roster and therefore always present, so putting a sometimes-absent group above
it would make the sidebar's shape jump around as the first group appears and
disappears.

### Naming

"Group chats" is the owner's term and is what ships. Noting for the record
that Slack calls these "group DMs" and hides them inside the DM list, so we
are deliberately diverging from the reference here — the owner's complaint is
precisely that Slack's own treatment is confusing.

## 4. Decision — join link placement

Put a **workspace invite affordance in the messaging surface's top-right,
beside the workspace name.**

**DECIDED 2026-09-17: option (a) — the control opens the existing invite
page.** It invents no new invite primitive and mints no new credential; it is
a shortcut to a surface that already exists and already enforces its own
permissions. No clipboard handling, which matters here: a recent commit
(`fix(orgs): keep join credentials out of request URLs`) shows join
credentials are deliberately treated as sensitive in this codebase, so a
copy-link control would have needed its own justification.

Implementation is therefore small: a button beside the workspace name that
routes to the existing members/invite surface. Do not duplicate any invite
logic into the messaging components.

### Visibility

The control shows only to members who can actually invite. A viewer who
cannot invite must not see a dead button. Mirror whatever permission
`WorkspaceMembersSection` already enforces rather than inventing a second
rule.

## 5. Surfaces to change

| File | Change |
|---|---|
| `apps/frontend/src/components/messaging/roomSections.ts` | add the `group` bucket; update the doc comment that currently asserts the opposite |
| `apps/frontend/src/components/messaging/__tests__/` | the existing grouping tests encode the old behaviour and will fail — that failure is the signal, update them deliberately |
| `apps/frontend/src/components/messaging/sidebarPrefs.ts` | collapse/expand state is per-group; a new group needs a default |
| `apps/mobile/src/app/(app)/(tabs)/home.tsx` | `SECTION_ORDER` gains `Group chats` |
| `apps/mobile/src/app/(app)/(tabs)/dms.tsx` | restrict to `dm`; decide whether group chats get a home on this tab or only on Home |
| web workspace header / `RoomDirectory` heading | the invite control |

## 6. Risks

- **Custom sections interact with this.** An owner who already dragged a
  group chat into a custom section must not see it move. Covered by
  preserving precedence, and worth an explicit test.
- **Sidebar reordering is drag-and-drop** (`SortableSection`,
  `ColumnGrip`). A new fixed group has to behave under reorder the same way
  Channels and Direct messages already do.
- **Mobile's DMs tab currently means "dm + group-dm".** Narrowing it removes
  a place group chats are reachable; Home must still list them or they become
  hard to find on mobile.

## 7. Testing

- `groupRooms` puts `dm` and `group-dm` in different buckets.
- A starred `group-dm` still lands in Starred.
- A `group-dm` with a custom `sectionId` still lands in that section.
- Direct messages renders when empty; Group chats does not.
- The invite control is absent for a member without invite permission.

## 8. Open questions for the owner

Settled: the join link opens the existing invite page (§4).

1. On mobile, should the DMs tab show group chats too, or become strictly
   one-to-one like the sidebar group?
2. Should an existing `group-dm` that an owner has already starred or filed
   be left exactly where it is (recommended), or re-homed into the new group?
