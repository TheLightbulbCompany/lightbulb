# Shared Native Sessions — Phase Tracker

**Outcome:** A person or agent can continue a shared Claude Code or Codex session from Lightbulb Chat or a phone, see its real transcript and delivery state, and receive a reply in the right conversation. New sessions started in Lightbulb and sessions started directly in either native app both participate.

**Current approach:** Discover and verify native sessions on the paired Mac. Do not implement a Happy-style managed wrapper now. Happy is useful prior art for session identity, ordered history, reconnects, and phone controls, but its wrapper does not establish control of arbitrary independently started sessions. Reconsider a managed launch path only after a measured discovery failure that cannot be fixed cleanly.

**Sharing target (not yet verified in the product):** Sessions discovered on a paired Mac are shared with its workspace by default, with a way for the owner to pause sharing. Visibility and permission to send are separate: keep the existing `teamControl` and per-person allow rules until the two-way design defines the new control policy. At that design gate, distinguish a Lightbulb-started session from an independently discovered one only where the distinction changes a user-visible action or authority; do not add a second session model by default.

## Phase 0 — Restore trustworthy discovery and control

**UX:** An existing shared session shows every public event in order. Its composer is enabled only when the Mac verifies native runtime and remote input. A blocked state explains the actual cause.

- [x] [#3487](https://github.com/TheLightbulbCompany/lightbulb/pull/3487): discover current Codex managed-daemon sessions and use the resumed turn's checkout.
- [x] [#3489](https://github.com/TheLightbulbCompany/lightbulb/pull/3489): safely cross a complete private Codex compaction row without dropping later public output; damaged or public oversized rows stay blocked.
- [ ] Signed production Mac build from merge `11d3eb3a4` succeeds: [run 36749161612](https://github.com/TheLightbulbCompany/lightbulb/actions/runs/36749161612).
- [ ] Install the released build and verify the exact screenshot session drains its 194 queued rows and regains the composer.
- [ ] From iPhone, send one follow-up to that session and see one applied receipt and one native answer. Verify a separately phone-started Codex session appears and accepts input. Test Claude Code discovery too.

## Phase 1 — Make room @session mentions dependable

**UX:** A person selects a live session in Chat, sends one ordinary room message, sees delivery state, and can open the session's answer from the room. The native prompt names the verified sender.

- [x] Human room @session dispatch and receipt shipped in [#3459](https://github.com/TheLightbulbCompany/lightbulb/pull/3459).
- [ ] Repair the roomless-command attribution path so a teammate's verified identity survives into Claude Code/Codex; preserve room authorization and idempotency.
- [ ] Accept on Mac and iPhone with owner and teammate, both providers, busy/offline/denied cases, and one matching answer link. Do not call this phase complete from CI alone.

## Phase 2 — Complete the two-way conversation

**UX:** A Chat-origin message receives a full answer in its thread from the native session, and the native agent can deliberately address a person, agent, room, or another shared session without a prose suffix or skill guess. The room and session both show provenance and delivery state.

- [ ] Design session identity, source-message/thread binding, room access, mention dispatch, loop limit, late replies, and revocation. Include the distinction between independently discovered and Lightbulb-started sessions only if it affects these rules.
- [ ] Upgrade the current silent 300-character system notice into an idempotent, threaded native-session reply.
- [ ] Add a session-bound Lightbulb tool for Claude Code and Codex to list accessible sessions, send, and check receipt; never trust model-supplied actor or room authority or let a model approve a permission prompt.
- [ ] Test Chat → Claude/Codex → Chat, native Codex ↔ Claude, duplicate/retry/reconnect, cross-room denial, and loop prevention on real apps before production acceptance.

## Phase 3 — Control an active native session

**Current limit:** The backend advertises queue-only send for verified native sessions (`steer=false`, `interrupt=false`). Codex uses its CLI queue command. A queued follow-up is not an active-turn steer, and the model/permission footer is read-only.

**UX:** The composer distinguishes **Queue for next turn** from **Steer this turn** when the provider actually supports both. A permitted person can stop an active run and see a precise result. Model, effort, and permission choices show their effective value and when a change takes effect; unsupported changes are explained rather than presented as working controls.

- [ ] Verify Claude Code and Codex native control APIs and lifecycle behavior for steer, interrupt, and changing model/effort/permission during a live session. Keep queue-only as the explicit fallback where the verified provider does not support an action.
- [ ] Design owner/teammate authority for each action, exact approval prompts, revocation, and concurrent turns. Keep a teammate message from granting itself stronger permissions.
- [ ] Implement only supported controls behind advertised capabilities, with request IDs, generation/turn fences, and visible receipts.
- [ ] Accept idle/busy/approval/offline/resume cases from Mac and iPhone; prove a steer affects the active turn while a queue waits for the next one.

## Phase 4 — Make remote start and settings simple

**UX:** Chats has a separate New session action. A person writes the first message, optionally chooses Mac/folder/tool/model/effort/permission, and lands in the actual session with a working composer. Opening Terminal is shown as progress, not success.

- [ ] Land the backend/Mac contract for Mac-advertised choices and explicit safe launch defaults; a Mac's global YOLO setting must not silently become the phone launch setting.
- [ ] Finish [#3488](https://github.com/TheLightbulbCompany/lightbulb/pull/3488) after that contract, including All/Sessions/Channels/Direct filters and prompt-first start.
- [ ] Correlate start request to the exact discovered native session and return its real ID/readiness. Do not guess by newest provider session or retry an uncertain launch into a duplicate.
- [ ] Accept from iPhone, web, and Mac with both providers, chosen settings, app restart, concurrent starts, CLI exit, and unavailable Mac.

## Phase 5 — Team and device acceptance

- [ ] Verify new paired Macs share discovered sessions by default; if current enrollment starts off, change the default and preserve the owner's pause-sharing control.
- [ ] Exercise owner and teammate on separate phones or phone plus simulator, including ask-to-join/control, exact human approval, revocation, and a session started directly in each native app.
- [ ] Verify the released Mac build, iPhone TestFlight/OTA version, backend production SHA, and delivery receipts. Record any physical-device limitation explicitly.
- [ ] Update the Supademo session beat only after the two-way behavior used in the video is proven.

**Release rule:** Each phase gets a scoped implementation plan, Codex review, CI/CD, production promotion, and product testing before its checkbox is closed. Do not use local integration or E2E suites; use CI and manual Mac/iPhone or simulator acceptance. Delete this working plan once the feature train is complete; the PRs remain the durable record.
