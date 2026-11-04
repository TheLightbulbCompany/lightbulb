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

## Apps don't belong to rooms (founder, 4 Nov 2026)

The CLI is hosting for anyone, including a developer on their own laptop, so an app belongs to an **account**, not a room.

- **Owner = a workspace.** `lightbulb login` signs a person in and gives them a workspace to hold their apps: their team's, or a workspace of their own if they have none (the product only makes organisation workspaces, so a solo developer gets a one-member one). The workspace's Stripe Projects setup happens during login or the first `lightbulb app up`, in the browser, the same as for a team; today an app without it stops at `services_account_needed`.
- **A room is optional.** It is only where a request came from when a Lightbulb agent builds the app. `_check_room` (R46: "an app belongs to a room") goes; `app new` no longer needs `--room`.
- **Sharing without rooms:** only me, people I invite, my workspace, anyone with the link. "People in #room" stays as a convenience for teams.
- **Publish gate by caller:** a person running `lightbulb app publish` is the person; an agent may publish only when a person asked in the app's own conversation.
- **Notices** go to the app's conversation when it has one; a developer using only the CLI reads the CLI's output.
- **Canvas** lists apps by workspace, not by room membership.

## Not in this change

- A "New app" box in Canvas with no room (next step, same machinery).
- A live dev-server preview (each update is still a deploy).
- Per-workspace standing approval (Stripe's rule).

## Done when

Two requests sent seconds apart in one room both reach a first version without waiting for each other; each app's page in Canvas shows its own conversation beside its preview; the room shows only the start and ready lines; replying in the app's conversation changes the app.
