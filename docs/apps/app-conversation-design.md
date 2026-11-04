# Each app owns its conversation (design, 4 Nov 2026)

Owner: Apps. Status: for the founder's approval.

## Why

On Emergent each app is its own conversation: you talk to its builder beside a live preview, and two apps build at once. Ours builds inside the room agent's one conversation per room, so the room is a build queue: run 12 waited 17 minutes behind run 11 (4 Nov). The app already owns its record, run state and Canvas page; it does not yet own its conversation.

## What the person sees

1. **Ask.** "@ceo build a booking page" in a room (later: a "New app" box in Canvas).
2. **Handed over at once.** Within seconds the agent answers in the room with one line: "Started Booking. It's being built in its own conversation in Canvas: <link>." The room is free; a second request starts its own app straight away.
3. **The app's page.** Canvas shows the app's conversation beside its preview: the request at the top, the builder's questions, the approval line, "first version", "ready". The person replies there ("make the button green") and the builder answers there.
4. **Back in the room.** One line when it's ready ("Booking is ready in Canvas"), nothing in between.
5. **Several apps.** Each builds in its own conversation at the same time (up to the runtime's 4 concurrent turns per workspace).

## How it works

- **The app's conversation is a hidden room**, like session chat (`session_chat.py`): kind `channel`, `hidden: true`, `app: {app_id}`, a deterministic id from the app id. It never shows in the sidebar; Canvas opens it.
- **Who's in it:** the person who asked, the builder agent, and, when the app is open to a room, anyone who can open the app (seated when they open it, as session chat does).
- **Parallel by construction:** the bridge keys sessions by room id (`agent:<id>:lightbulb:channel:<room>`) with a lock per session, so each app's builder runs in parallel with the room and with other apps. Its files live in that conversation's own work folder.
- **Hand-over, CLI:** a new `lightbulb app start "<request>" --name <name>` (room agent, first action on an app request) creates the app record (state `requested`), the app's conversation, posts the request into it as from the person, wakes the builder there, and answers with the line for the room. Its `next` tells the room agent to post that line and stop. In the app conversation, `lightbulb app new <name>` adopts the reserved app instead of refusing the name. Everything after (`up`, card, `verify`, `publish`) is unchanged and runs there.
- **Lines go to the app's conversation:** the approval line, stopped line and "first version / ready" lines post to the app's conversation; the origin room gets only the start line and the ready line.
- **Canvas:** `AppView` gets the conversation beside the preview, reusing `SessionChat`'s parts (`RoomTimeline`, `RoomComposer`); "Ask the agent to fix it" posts there.
- **Older apps** keep their room; the app conversation is created the first time one is opened in Canvas.

## Not in this change

- A "New app" box in Canvas with no room (next step, same machinery).
- A live dev-server preview (each update is still a deploy).
- Per-workspace standing approval (Stripe's rule).

## Done when

Two requests sent seconds apart in one room both reach a first version without waiting for each other; each app's page in Canvas shows its own conversation beside its preview; the room shows only the start and ready lines; replying in the app's conversation changes the app.
