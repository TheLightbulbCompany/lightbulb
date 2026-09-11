# Slack-like essentials

Familiar Slack flows, Lightbulb identity, and real agents. This is the full direction; the centered creation dialog is one delivery batch. Never ship fake controls or invent visibility/permission policies the backend does not enforce.

- [x] Images and membership foundation: PRs #2462, #2463, #2464, #2465, #2467 merged; images and members verified in production.
- [ ] Threads and reactions: #2466 and #2468 merged; promotion run 34546951308 in progress at handoff.
- [ ] Notifications: #2469 in review.
- [ ] Current batch: centered channel/message creation, channel rename, member removal.
- [ ] Next: in-room search.
- [ ] Later: own-message edit/delete, mark unread and unread navigation, profiles, pins/shared content, notification preferences, leave/archive. Add backend authorization and persistence wherever required before exposing controls.

Remove this disposable plan when the overall direction ships; PR descriptions remain the durable record.

Mode: Operate. Scope: RoomDirectory creation only.

THESIS: Create channels and messages in the centered popup explicitly requested by the user.

OWN-WORLD: Preserve the approved white Slack-like shell, Source Sans UI type, existing semantic tokens and Radix dialog controls.

STORY: Choose a conversation type, name a channel, select real workspace people and agents, then create through the canonical API. Errors preserve the draft; success selects the resulting room.

FIRST VIEWPORT: A centered 520px dialog above the unchanged sidebar and conversation, with title and close at the top, fields and scrollable roster in the middle, and persistent Cancel/Create actions below. Narrow screens retain 16px gutters.

FORM: User-pinned familiar dialog, a refinement of the existing surface; no concept seed or imagery required.

FINISH: Focused behavior checks and root review, then user-delegated production visual acceptance. No current messaging visual golden or existing component preview was found; the committed dialog screenshot is blank and cannot verify this surface. Existing system tokens are preserved; no DESIGN.md change or new raster is needed.
