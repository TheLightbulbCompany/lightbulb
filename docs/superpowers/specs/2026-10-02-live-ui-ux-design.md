# Live UI/UX under Option D (the agent is the voice) — web + iPhone

Status: draft for owner review (2026-10-02). Companion to
`2026-10-02-voice-agent-is-the-voice-design.md` ("D" below). Delete both when shipped.

Starting point (origin/main): web `components/huddle/LiveStrip.tsx` (one "Lightbulb" `BarVisualizer`
button, up to 3 people faces + "+N", `TrackToggle` mic, Leave, a details popover) mounted in
`RoomHeader.tsx` in place of "Go live"; iPhone `components/voice/LiveSheet.tsx` (page sheet: one
88 pt Lightbulb tile with `LevelRing`, a row of people, mute + leave) opened from the room header's
Live button (#3545). This design **removes the Lightbulb tile** and replaces it with one tile per agent.

Owner rulings this design keeps: Live sits in the room header and never floats; LiveKit UI components
where they exist; no hold-to-talk; join and just talk; neutral look, tokens only; the word is "Live",
never "huddle"; nothing from Live is posted to the room timeline.

---

## 0. The data the UI reads (one contract, both clients)

All agent voices publish as the worker's **one** LiveKit participant (D §7; verification §5: two
RoomIO sessions under one identity overwrite each other's `lk.agent.state`). So the client must
**not** use `useVoiceAssistant()` / `lk.agent.state` to label agents — it reads our own attributes on
the worker participant with `useParticipantAttributes({ participant })`
([hook ref](https://docs.livekit.io/reference/components/react/hook/useparticipantattributes/);
returns `{ attributes: Readonly<Record<string,string>> | undefined }`, re-rendering on
`RoomEvent.ParticipantAttributesChanged`,
[attributes guide](https://docs.livekit.io/home/client/state/participant-attributes/)).

| attribute (on the worker) | value | drives |
|---|---|---|
| `lightbulb.speaking_agent` | agent id, or `""` | which tile gets the audio bars |
| `lightbulb.agent.<id>.status` | `joining` \| `speaking` \| `listening` \| `working` \| `has_news` \| `idle` | tile state |
| `lightbulb.agent.<id>.working` | ≤60-char plain phrase ("pulling Q3 revenue") | "working on X" line |
| `lightbulb.queue` | comma-separated agent ids, in answer order | "next" badges |
| `lightbulb.listening_to` | display name of the person the open voice is pinned to | popover line |

Attributes are limited to 64 KiB combined ([guide](https://docs.livekit.io/home/client/state/participant-attributes/));
this is a few hundred bytes. **Names never travel in attributes** — tiles take names and tints from the
room's member list the client already has, so a rename shows up the same way it does in chat.
Short-lived notices (rename done, ambiguous name, voice failed) arrive on a `live.notice` data topic
from the worker, exactly like today's `huddle.error` (sender checked against the worker identity).

The worker's participant itself never renders as a tile (today it does, as "Lightbulb"). Its identity
is still `LIVE_AGENT_IDENTITY = "agent-ceo"`, which under D is a misleading name — see open question 6.

Which agents get tiles: every agent member of the room whose listen mode is `active` or `mentions`
(both answer to their name in chat). `observe` agents never run in chat, so they get no tile; the
popover lists them as "Observing — can't be asked here". See open question 3.

---

## 1. Agent tiles in the header strip

Each agent tile is its avatar (the existing agent tint + single initial from `Identity`/`agentTint`)
with a state treatment. One audio track exists at a time (one GPT-Live session open, D §7), so only the
speaking tile is ever wired to audio.

| state | when | web tile (24 px avatar in a 32 px strip) | text (tooltip / aria / popover) |
|---|---|---|---|
| **speaking** | `speaking_agent == id` | tile widens to avatar + `BarVisualizer` (`barCount=3`, `track` = worker mic track, `state="speaking"`), `--live` ring, `data-speaking` | "Gauge — speaking" |
| **joining** | `status=joining` (voice opening, 1–3 s unknown, D "Unknowns" 1) | avatar + `BarVisualizer` with `state="connecting"` and no track (LiveKit's own stepping sequence), neutral `--fg-soft` bars | "Gauge is joining…" |
| **listening** | holds the floor, the person is talking | avatar with `--live` ring, bars `state="listening"` | "Gauge — listening to Aden" |
| **working** | `status=working` (background Hermes turn) | avatar + a small spinner-free "…" pip (`--fg-soft`) at bottom-right | "Gauge — working: pulling Q3 revenue" |
| **has something** | `status=has_news` (a finished result waiting for the next quiet moment, D "Several agents") | avatar + raised-hand pip (lucide `Hand`, `--link`) | "Gauge has something — tap to hear it" |
| **idle** | in the room, not involved | plain avatar, no ring | "Gauge" |

**Who is talking**, in priority order: (1) the speaking tile is the only one with moving bars and the
`--live` ring; (2) the strip's `aria-live="polite"` region announces "Gauge is speaking" once per
change of speaker (not per word); (3) the expanded popover's top line says it in words. People keep
today's speaking ring (`useIsSpeaking`). One rule for both: **ring = making sound now**.

`BarVisualizer` facts used here (from the installed `@livekit/components-react` 2.9.24
`BarVisualizer.d.ts` and [the component ref](https://docs.livekit.io/reference/components/react/component/barvisualizer/)):
props `state?: AgentState`, `barCount?: number`, `track?: TrackReferenceOrPlaceholder | LocalAudioTrack | RemoteAudioTrack`,
`options?: { minHeight?, maxHeight? }` (percent), optional template `children`; highlighted bars carry
`data-lk-highlighted`, themed by `.lk-audio-bar` / `--lk-fg` / `--lk-va-bg`. `AgentState` includes
`connecting`, `listening`, `thinking`, `speaking`, `idle`. Our CSS keeps styling `.lk-audio-bar` with
tokens only, as `globals.css` does today.

**How many fit before "+N".** The strip is a header element, so it is budgeted, not elastic:

- Agents: **up to 3 tiles**, then a `+N` chip. People: **up to 3 faces**, then `+N` (unchanged).
- Visible agent slots are **promoted by state**, not by roster order: speaking > joining > queued >
  has-something > working > idle (LiveKit's `useVisualStableUpdate` idea — whoever is active is always
  on the first page). So the person never has to open the popover to see who is talking.
- The speaking tile's bars add ~20 px; the slot count does not change.
- Header narrower than 640 px (the web's narrow panel, a docked split): agents collapse to **1 slot**
  (the active one, else the first) + `+N`; people collapse to a count ("3").
- Rooms with ≤3 agents (every starter DM and most channels) never show `+N` for agents.

## 2. People tiles, the mic, Leave

Unchanged from today on purpose: overlapped people faces with the `--live` speaking ring; the mic is
LiveKit's `TrackToggle source={Microphone}` (the installed component renders a `<button>` with
`aria-pressed` and `data-lk-enabled`; props `source`, `showIcon`, `initialState`, `onChange`,
`captureOptions`, `publishOptions`, `onDeviceError` —
[ref](https://docs.livekit.io/reference/components/react/component/tracktoggle/)); Leave is our own
button (not `DisconnectButton`, whose `Disconnected` event reads as a dropped call).

**Agents vs people, visually** — three cues, none of them colour alone:
1. **Group and order**: agents on the left, a 1 px `--plane-edge` divider, then people.
2. **Shape of the face**: agents are their tinted fill + one initial (the convention chat already uses);
   people are photo or neutral initials. Agent tiles do not overlap; people faces do (a crowd vs a row
   of individuals).
3. **Words**: every agent's accessible name ends "(agent)"; the popover groups "Agents" and "People".

## 3. Addressing affordances

The rules the UI must teach (owner, 2026-10-02):
an agent's name starts or switches its turn ("Gauge, …", "hey Gauge", "ask Gauge…"); "Lightbulb, …"
asks the team; several names queue in order; a name that is also a person's needs "hey" or a request;
renames update STT vocabulary and routing.

**Hint copy.** One line, shown in the popover always, and in the strip itself for the first 3 Live
sessions per person (then only in the popover). It names real agents in this room, never a template:

- 1 agent: *Say "Gauge, …" or tap Gauge to talk.*
- 2–3 agents: *Say an agent's name — "Gauge, …" — or tap them. "Lightbulb, …" asks the team.*
- people-only room: no hint (§6).

**Tap a tile = say its name.** Tapping an agent tile is exactly "Gauge, …" with no words yet: it routes
to that agent (worker RPC `talk_start` with `{agentId}` — today's RPC gains a payload), the tile goes to
**joining**, then **listening**. No hold, no second tap. Tapping the tile that already holds the floor
does nothing (no toggle-off; the exchange ends on its own idle rule). Tapping a **has-something** tile
asks it to speak now. Tapping a **working** tile switches the floor to it, like saying its name.

**Feedback that an agent was addressed.** The instant routing decides (D §5, before the voice opens):
the tile enters **joining** (`connecting` bars), the `aria-live` region says "Gauge is joining", and if
another agent held the floor its bars stop at once (we close it concurrently). There is no sound cue
by default — the agent's first word is the cue. If measured startup is > 2 s (D unknown 1), add a short
neutral earcon on joining; decide after measuring, not before.

**Queue.** "Forge and Gauge, what do you think?" → Forge **joining/speaking**, Gauge shows a small
`2` badge (`--fg-soft` on `--plane-raised`), meaning "answers next". Badges come from
`lightbulb.queue`; they renumber as the queue drains. Saying a new name while a queue is pending
replaces the queue (the newest instruction wins; the UI simply follows `lightbulb.queue`). Popover
wording: "Next: Gauge".

**Name collides with a person.** If the room has a person "Nova" and an agent "Nova", a bare "Nova, …"
is treated as talking to the person (D routing does nothing). The UI then shows a 6 s inline chip in
the strip — *"Meant Nova the agent? Say "hey Nova" or tap"* — with the agent tile outlined (`--link`)
for those 6 s, from a `live.notice {kind: "ambiguous_name", agentId}`. The worker sends it only when the
bare name was the first word of an utterance (otherwise every mention of a colleague would nag). The
hint copy for a colliding agent always reads *Say "hey Nova, …"*. Tapping the tile is never ambiguous.

## 4. First-time naming (default-named agents)

Applies once per agent, only while it still has its default role name (CEO, Engineering, Finance,
Growth, Analytics, Customer Operations), on its first voice exchange.

- **Hear**: the agent's first line, in its own voice: *"Hi — I don't have a name yet. What would you
  like to call me?"* If the person answers with a name, the agent confirms in one line (*"Nova it is."*)
  and continues with whatever was asked. If the person ignores it and asks something, the agent just
  answers; it does not ask again in this Live session.
- **See**: while the question is open, the tile's tooltip/popover line reads *"CEO — asking for a name"*.
  When the rename lands, the rename toast (§5) appears and the tile's name changes everywhere (strip,
  popover, chat sidebar, room member list) via the normal member-list refresh.
- **Never** a modal or a form: naming is spoken. The settings field (§5) is the non-voice path.

## 5. Rename by voice, and voice notes

**Rename toast.** On `live.notice {kind:"renamed", agentId, from, to, by}` every person in this Live
sees a toast (web: the existing `sonner` toaster via `lib/notify`; iPhone: a banner inside the sheet,
and a system-style banner at the top of the room screen if the sheet is hidden):

> **CEO is now Nova** · Undo

- Undo is offered only to the person who asked (`by`), for 10 s; it calls the same rename route back
  to `from`, and the agent says nothing about it (no voice line for an undo).
- Others see the toast without Undo.
- The toast is a notice, not a message: nothing is posted to the room. Outside Live, the next time
  someone opens the room, the new name is simply there.
- Renaming to a name that collides with a person in the workspace: the toast adds *"Say 'hey Nova' in
  Live."*

**Personality ("voice notes") confirmation.** "Be more blunt", "don't say 'great question'" →
`live.notice {kind:"voice_note_saved", agentId, text}` → toast *"Nova will remember: be more blunt"*
with **Edit** (opens settings below). No Undo here — editing is the undo.

**Where to view and edit.** Web: `AgentInspectorPanel` → `AgentOverviewTab` gains a **Voice** section:
the voice (read-only name, e.g. "Marin", until "change your voice" ships) and **Voice notes** — a
plain `<textarea>` with a live `n / 1,000` count, Save/Cancel, help text *"How Nova talks in Live and
chat. Nova adds to this when you ask it to change how it talks."* iPhone: `control/agent-detail` gets
the same section as a grouped-list row → a full-screen text editor. Same backend field (D §6); edits
apply to the next voice session and are appended to an open one (D §6, `append_instructions`).

## 6. Errors and edge states

All copy lives in `packages/shared/src/voice/words.ts` (one table for both clients). Existing codes keep
their text; new ones below. No error is posted to the room.

| state | where it shows | copy | action |
|---|---|---|---|
| free minutes used (`voice_limit/free_minutes_used`) | popover opens itself (today's rule: a new error opens details); agent tiles go idle and **lose tap-to-talk** (aria-disabled), people audio continues | "You've used today's free voice minutes. People can keep talking." | Plans (admins) / nothing (members) |
| credits low | same | existing "Add credits to keep calling." → reword **"Add credits to keep talking to agents."** | Add credits |
| workspace waking (`workspace_not_ready`, or first routed turn on a cold runtime) | tile shows **joining** longer; after 3 s the line becomes "Nova is waking up…" | "Nova is waking up — this can take a minute the first time." | none; it resolves itself. Gives up at 90 s (the ingress budget) → "voice failed" row |
| voice failed to open (`live.notice {kind:"voice_failed", agentId}`) | tile returns to idle with an alert pip; popover row | "Nova couldn't join. Try again." | Retry = tap the tile |
| an agent left the room (member removed / mode set to observe) | tile animates out (fade, none under reduced motion); if it held the floor its bars stop; queue entry dropped | aria-live: "Nova left" | none |
| worker not present (today's 45 s "didn't join") | strip shows people only + an alert dot | "Agents couldn't join this Live." | Retry (re-join) |
| people-only room (no agents, `lightbulb:false`) | strip = Live dot + people + mic + Leave. No agent group, no hint, no "+ add an agent" upsell | — | — |
| mic denied / no mic / dropped / idle ended | unchanged from today | unchanged | unchanged |

A failed or waking agent never blocks people talking: Live is a people call first.

## 7. Expanded view (popover / sheet body)

Clicking the strip's summary opens the existing `Popover` (`align="end"`), now:

```
Live in #general                                   (title)
Gauge is speaking · Next: Forge                    (what's happening — one line)

AGENTS
 [G] Gauge (agent)        Speaking
 [F] Forge (agent)        Next · working: drafting the launch post
 [N] Nova (agent)         Has something — tap to hear
 [A] Analytics (agent)    Idle
 Observing: Pulse — can't be asked here

PEOPLE
 (AD) Aden (you)          Speaking
 (PR) Prasiddha

Say an agent's name — "Gauge, …" — or tap them. "Lightbulb, …" asks the team.
```

- Rows are buttons (agent rows = tap-to-talk, same as the tile). People rows are static.
- The "what's happening" line is computed from attributes only: who is speaking / joining, the queue,
  and the count of agents working ("2 agents working"). It is never a summary of what was said.
- **No transcript**, by policy.

**Live captions — recommendation: not now.** LiveKit can forward transcriptions on the
`lk.transcription` text-stream topic, read with `useTranscriptions()`, and the agent side can switch
forwarding off (`text_output=False`) ([docs](https://docs.livekit.io/agents/build/text/)). Captions
would help hard-of-hearing users and noisy rooms, but they (a) put the conversation on screen in a
product whose rule is that Live leaves no trace, (b) invite screenshots that become the transcript we
declined to keep, and (c) for D only cover agent speech unless we also forward our per-person STT. If
we ever add them: off by default, per-viewer toggle, last two lines only, ephemeral (never stored,
never in the timeline), and people's lines included or none at all. Owner decision (open question 2).

## 8. iPhone parity

| web | iPhone |
|---|---|
| header strip (here) | room header **Live** button (#3545) gains a compact indicator: the speaking agent's avatar with a `--live` ring, else the red Live dot |
| strip in another room ("Live in #x — Return") | the existing "Open Live in #x" header action, unchanged |
| agent tiles | sheet **stage**: one 88 pt tile for the agent holding the floor (with `LevelRing` on the worker track, as today's Lightbulb tile) + its name + state line; below it a horizontal row of **56 pt agent tiles** for the rest, each with its state pip, queue badge, or raised hand. When nobody holds the floor the stage shows the last speaker, else the CEO-role agent, captioned "Tap someone or say their name" |
| `+N` | none: the row scrolls horizontally (the sheet has room) |
| people faces | the existing people row, under a "People" caption |
| `TrackToggle` mic | the existing 64 pt round mute (`mic.fill` / `mic.slash.fill`); LiveKit's RN package has no `TrackToggle`, so this stays our control |
| Leave | the existing red `phone.down.fill` |
| popover | the sheet *is* the expanded view: the "what's happening" line sits under the stage tile; errors in the existing error banner |
| rename / voice-note toasts | banner in the sheet; if hidden, a banner on the room screen |
| hint copy | the existing hint line, with real agent names |

**Background, lock screen, Dynamic Island.** `UIBackgroundModes: ["audio"]` is already set and the
audio session is `playAndRecord` / `voiceChat` (`lib/webrtc.ts`), so Live keeps going with the screen
locked. Two additions worth doing, in order:
1. **Now Playing / audio route** (cheap): set Now Playing info to "Live in #general — Gauge speaking"
   so the lock screen shows who is talking and the route picker works.
2. **CallKit** (worth it, not cheap): reporting Live as a VoIP call gives the green status-bar pill,
   the Dynamic Island call indicator with mute/end, correct interruption by real phone calls, and
   lock-screen controls — all system UI, no custom ActivityKit work. A **Live Activity** (ActivityKit)
   showing the speaking agent is possible but needs a widget extension and a native build; skip until
   CallKit is in and people ask for more.

## 9. Accessibility and reduced motion

- Every tile is a real `<button>` (web) / `Pressable accessibilityRole="button"` (iOS) with name +
  state: "Gauge, agent, speaking", "Forge, agent, next, working: drafting the launch post".
  `accessibilityHint` on iOS: "Talks to Gauge. Or say "Gauge"".
- One polite live region announces **changes of floor only** (joining, speaking, left, failed, renamed),
  never per-word or per-bar. Errors use `role="alert"` as today.
- State is never colour-only: speaking = ring **and** bars **and** text; has-something = hand icon;
  queue = a number; working = text in the label.
- Keyboard (web): the strip is one `role="group"`; Tab moves summary → agent tiles → mic → Leave.
  Enter/Space on a tile = tap. No custom shortcuts.
- Touch targets: iOS tiles ≥ 44 pt; web tiles 26 px visual inside a 32 px hit area (header density,
  same as today's icons).
- Dynamic Type: the sheet's names and state lines scale (existing `textScale` rule); the agent row wraps
  to two lines rather than truncating state text.
- **Reduced motion** (web `prefers-reduced-motion`, iOS `useReducedMotion`): no bar animation — the
  speaking tile shows all bars lit, static (today's `vizState` rule); joining shows static dim bars +
  the word "joining"; no `LevelRing` halo; tiles appear/disappear without fade; the Live dot stops
  pulsing (already in CSS).

## 10. Wireframes

Web header strip (here, ~420 px budget). `[X]` = agent tile (tint + initial), `(xx)` = person,
`▮▮▮` = BarVisualizer, `|` = divider.

```
A. Idle — 3 agents, 2 people
┌──────────────────────────────────────────────────────────────┐
│ ● Live  [G] [F] [N] | (AD)(PR)              🎙  ⏻            │
└──────────────────────────────────────────────────────────────┘

B. Gauge speaking, Forge queued
│ ● Live  [G ▮▮▮] [F ²] [N] | (AD)(PR)        🎙  ⏻            │
         ring+bars  next

C. Nova joining (voice opening)
│ ● Live  [N ▯▯▯ joining] [G] [F] | (AD)(PR)  🎙  ⏻            │
           connecting bars, promoted to first slot

D. Background work + raised hand, 6 agents
│ ● Live  [G ▮▮▮] [F …] [N ✋] +3 | (AD)(PR)(SK)+2  🎙  ⏻       │

E. Ambiguous name (6 s)
│ ● Live  [N]⟵outlined  Meant Nova the agent? Say "hey Nova" or tap │

F. Free minutes used
│ ● Live  [G]·[F]·[N] (dimmed) | (AD)(PR) ⚠   🎙  ⏻             │
   popover auto-opens: "You've used today's free voice minutes. People can keep talking."

G. People-only room
│ ● Live  (AD)(PR)(SK)                         🎙  ⏻            │

H. Live elsewhere (unchanged)
│ ● Live in #general  Return                   🎙  ⏻            │

I. Narrow header (<640 px)
│ ● [G ▮▮▮] +2 | 3                             🎙  ⏻            │
```

iPhone sheet (page sheet):

```
┌─────────────────────────────────────┐
│ 🔴 Live · #general            Hide  │
│                                     │
│              (( [G] ))              │  ← LevelRing on the floor holder
│               Gauge                 │
│              Speaking               │
│        Next: Forge · 2 working      │  ← what's happening
│                                     │
│  Agents                             │
│  [F]²    [N]✋    [A]…    [C]   →   │  ← 56 pt, scrolls
│  Forge   Nova    Analyt.  CEO       │
│                                     │
│  People                             │
│  (AD)    (PR)    (SK)               │
│  Aden    Prasid. Sam                │
│                                     │
│  Say an agent's name — "Gauge, …"   │
│  — or tap them.                     │
│                                     │
│          ( 🎙 )        ( ☎︎ )        │  ← mute / leave
└─────────────────────────────────────┘

Joining:   stage shows [N] with static dim ring, "Nova is joining…"
Waking:    "Nova is waking up — this can take a minute the first time."
Renamed:   banner at top of sheet: "CEO is now Nova · Undo"
Error:     existing red-text banner under the title, Dismiss
```

---

## Implementation map (UI only)

- `packages/shared/src/voice/words.ts`: per-agent `agentLiveStatus(attrs, agentId)`, `liveQueue(attrs)`,
  hint builder from member names, new error/notice copy. Shared by both clients.
- Web: `LiveStrip.tsx` — replace `LightbulbBars`/`JoiningBars` with `AgentTile` (one
  `useParticipantAttributes` on the worker, read per id; `BarVisualizer` only on the floor holder);
  popover groups; `live.notice` handler in `HuddleProvider.tsx` next to `huddle.error`; `talkStart(agentId)`.
- iPhone: `LiveSheet.tsx` stage + agent row; `providers/live.tsx` `talkStart(agentId)` + notices;
  agent-detail Voice section.
- Agent settings: `AgentOverviewTab` Voice section (web), `control/agent-detail.tsx` (iOS).

## Open questions for the owner

1. **Rename toast audience**: everyone in Live (proposed) or only the person who asked?
2. **Live captions**: proposed "not now"; if yes, agent-only or people too?
3. **`mentions` and `observe` agents**: proposed tiles for `active` + `mentions`, none for `observe`.
   Or should naming an `observe` agent in Live work anyway?
4. **Tap with no agent in mind**: D says tap = last agent spoken to, else CEO. With per-agent tiles
   there is no generic "talk" button — is "Lightbulb, …" by voice enough for "ask the team", or do we
   want a small "Ask the team" control in the popover?
5. **Earcon on joining**: only if measured startup > 2 s (proposed), or always?
6. **Worker identity**: rename `LIVE_AGENT_IDENTITY` from `agent-ceo` to something neutral
   (`lightbulb-voice`) now that it speaks for every agent? It is a coordinated worker + both-client change.
7. **CallKit** on iPhone: worth a native build for the Dynamic Island / interruption handling?
