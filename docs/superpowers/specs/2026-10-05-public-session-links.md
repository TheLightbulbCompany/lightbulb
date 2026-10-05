# Public session links

Status: draft, 2026-10-05. Builds on session sharing (`2026-10-02-managed-session-sharing.md`),
the sharing mod (`2026-10-04-session-sharing-mod.md`), pick-up (`2026-09-27-session-pickup-design.md`)
and Ask to pick up (`2026-10-05-ask-to-pick-up.md`).

> **This reverses the 2026-09-25 "no public links" ruling** (owner, 2026-10-05). Today a share
> link opens only for a signed-in member of the session's workspace
> (`local_sessions.py:2381-2399`, `_open_share` compares `caller_owner(auth)` with the link's
> `ownerId`). After this work a link can also be opened by anyone who holds it.

## 1. Rulings (owner, 2026-10-05, binding)

1. **One link per session.** The sharer presses Share once and sends it. The receiver chooses
   what to do. The sharer's only setting is the audience: **Workspace only** (today) or
   **Anyone with the link**. The sharer can turn the link off at any time.
2. The link page depends on session state. **Live:** Watch, Ask to steer, Continue in my Claude
   Code. **Ended:** Read, Continue in my Claude Code.
3. **Watching needs no sign-in.** The band shows the watcher count; signed-in watchers are shown
   by name. Choosing "Anyone" shows one warning line: a live screen can't be redacted.
4. **Continue requires sign-in** and gives the person a free workspace through the existing
   sign-up and provisioning path. Then `lightbulb continue '<link>'` (the desktop app's own CLI;
   Mac only) writes the conversation into their Claude Code and opens `claude --resume` on it: their machine, their Claude, their
   plan. Nothing runs on our agents; the original is untouched. Their terminal shows "Continued
   from <name>'s session · Share yours". The copy is made when they press Continue. Secrets are
   redacted server-side on every copy. The sharer sees who took a copy.
5. **Steering requires sign-in plus Ask to steer**, answered in the owner's band; only the owner's
   keys work there (#3750). A person from another workspace becomes a **guest on that one
   session**: no membership, no access to anything else, revoked when the owner removes them or
   the session ends. People only.
6. **Code visibility** is shown on the page and Continue acts on it: public repo (name, branch,
   commit; code cloned); private repo ("Code is in a private repository"; conversation only);
   not pushed or no repo ("Code not shared", conversation only). **No remote URL, repo name or
   branch of a private repo ever leaves the sharer's Mac towards an outside receiver.** Inside the
   sharer's workspace, Continue keeps Pick up's behaviour (code bundle and uncommitted work
   included).
7. **Build order:** Continue, then public Watch, then guest Steer.
8. Deferred: see §9.

## 2. Customer flows

### 2.1 Sharer

1. In the session panel (web/desktop `LocalSessionPanel`), **Share**. The first press creates the
   session's one link and copies it. Audience defaults to **Workspace only**.
2. Optionally switch to **Anyone with the link**. One line appears: "Anyone with the link can
   watch your live terminal. A live screen can't be redacted."
3. Send the link anywhere.
4. The panel's Access section lists who took a copy and when. (Phase 2 adds the watcher count to
   the band; Phase 3 adds guest asks, §4, §5.)
5. **Turn off link**: every link page reads "This link was turned off" and no new copy can be
   made. Pause (existing) has the same effect while paused.

### 2.2 Receiver

1. Opens `https://flickmeon.ai/s#<token>`. No sign-in. The page shows title, the sharer's first
   name, provider, state, and the code line (§3.3).
2. **Live:** the terminal plays (Phase 2; until then the page polls the events, §3.2).
   **Ended:** the transcript, redacted for anyone outside the workspace.
3. **Continue in my Claude Code** (Claude sessions only): if signed out, Clerk sign-up, then the
   existing onboarding creates their workspace (provisioning starts as for any new workspace),
   then back to the page. A receiver without the app first sees "Get Lightbulb for Mac"
   (`/api/download/desktop`, `apps/frontend/src/app/api/download/desktop/route.ts`); the page then
   shows `lightbulb continue '<link>'` with a copy button.
4. They run it in their own terminal. The CLI opens the browser on a one-click confirm ("Continue
   <title> in Claude Code on this computer?"), which is the moment the copy is made. The CLI
   downloads the redacted copy, gets the code (§3.3), installs the Lightbulb mod if absent, and
   runs `claude --resume … --fork-session`. The band reads "Continued from Prasiddha's session ·
   Share yours".

A workspace member opening the link gets today's in-app paths (watch, type if allowed, Pick up)
plus Continue; nothing new is offered to them except the CLI.

## 3. Phase 1: Continue

### 3.1 The one link

Replaces the many-links share model (`local_sessions.py:2233-2440`: `SHARE#<linkId>` listing
rows, `SHARELINK#<hash>` lookup rows, per-link expiry and a `snapshotSeq` cap).

- **Session META** gains `shareLink: {tokenHash, tokenEnc, audience: "workspace"|"anyone",
  createdAt, createdBy}`. `tokenEnc` is a Fernet ciphertext under `ENCRYPTION_KEY` so the owner
  can copy the same link again, exactly as join links do (`workspace_join.py:25-35`). Written
  through `repo.put_session` like every META change. **`META.shareLink.tokenHash` is the truth**:
  an open whose hash does not match it is refused, whatever lookup row it found.
- **Lookup row** `SHARELINK#<tokenHash>` / `META` → `{ownerId, sessionId}` (same pk scheme as
  `_share_pk`, `local_sessions.py:2237`). No `expiresAt`: a link lives until it is turned off or
  the session's snapshot retention ends (§3.2).
- **Token:** `secrets.token_urlsafe(32)`, in the URL **fragment**, sent only in POST bodies, never
  logged; every failure reads the same (`workspace_join.py:73-85`, `local_sessions.py:2384-2386`).
- **The link is the pick-up permission** (Claude sessions). `PUT /link` on a Claude session calls
  `session_pickup.set_pickup(auth, sid, True)` (`session_pickup.py:328`), which starts the
  author's Mac snapshotting (`PICKUPWANT`, `session_pickup.py:300`); `DELETE /link` calls
  `set_pickup(…, False)`, which `retire`s the snapshot (`session_pickup.py:853`). The separate
  pick-up switch (`PUT /messaging/sessions/{id}/pickup`, `routers/messaging.py:332`) is removed;
  Ask to pick up's Allow creates the link with audience `workspace`. `retire` (also the sweeper's
  expiry path) deletes `shareLink` and the lookup row in its transaction. A Codex session's link
  never calls `set_pickup` (it refuses non-Claude, `session_pickup.py:352`): Watch and Read only.
- Old `SHARE#`/`SHARELINK#` rows carry `expiresAt`, the table's TTL (`local_sessions.py:2341`), so
  they disappear by themselves within 30 days; until then the `tokenHash` check above makes them
  read as turned off. No cutover script. `/chat/shared` is replaced by `/s`.

Owner routes (Clerk, owner only, `_owned_session` rules, `local_sessions.py:2289`):

| Route | Does |
|---|---|
| `PUT /messaging/sessions/{id}/link {audience}` | create if absent (audit `share_link_created`) or change audience; returns `{url, audience}` |
| `GET /messaging/sessions/{id}/link` | `{url, audience}` or 404 |
| `DELETE /messaging/sessions/{id}/link` | delete lookup row and `shareLink`, retire pick-up; audit `share_link_revoked` |

These replace the three `share-links` routes (`routers/messaging.py:369-384`).

### 3.2 Opening a link (unauthenticated)

`POST /public/session-links/open {token}` with `get_optional_user` (`auth.py:279`):

1. GetItem the lookup row by hash; load META. Refused with the one "turned off or not for you"
   answer when: no row; `META.shareLink.tokenHash` differs; `unshared`; the owner's Mac no longer
   shares (`_enrolled`, as `_open_share` does today); the session stopped more than
   `PICKUP_RETENTION_SECONDS` ago (`pickup_stopped_at`, `local_sessions.py:739`); or audience
   `workspace` and the caller is anonymous or not a current member (`_still_a_member`,
   `session_screen.py:52`).
2. Returns `{title, ownerFirstName, provider, state: live|ended|offline, code, me: {signedIn,
   member}}`. No owner id, user id, workspace id, session id, cwd or path. (Phase 2 adds
   `watchers`; Phase 3 adds `joinState`, `canType`.)
3. `POST /public/session-links/events {token, afterSeq, limit}` reads events as
   `list_shared_events` does (`local_sessions.py:2426`) without the `snapshotSeq` cap. For a
   non-member each event drops `actor` and `originRequestId` (`local_sessions.py:398-411`), and
   every string, recursively through `tool.input`/`tool.result`, goes through the redactor (§3.4).

### 3.3 Repo, branch, commit, visibility (desktop)

Today the Mac sends `code {remote, branch, head, snapshot}` only when it captured a bundle
(`snapshot.rs:435-556`, sent at `snapshot.rs:804`), and the bundle is `--not --remotes=origin`
(`snapshot.rs:513-517`), so it is useless to anyone without origin. New, beside it in the
commit body (`CommitRequest`, `session_pickup.py:196`), optional:

```
repo: {remote, branch|null, head, pushed: bool, public: bool} | null
```

- `remote`: `normalize_remote` of `origin` (`session_pickup.rs:85`), e.g. `github.com/o/r`.
- `pushed`: `git for-each-ref --count=1 --contains <head> refs/remotes/origin` is non-empty.
- `public`: an anonymous probe, `git -c credential.helper= ls-remote https://<remote>.git HEAD`
  with `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/usr/bin/false`; exit 0 = public. Cached per
  remote for one hour. Runs through `git_run` (`session_pickup.rs:40`, no shell).
- Sent on every commit, also when the bundle is unchanged, too large or skipped. Stored on the
  `PICKUP` manifest only, as `repoInfo` (`pickupSnapshot.repo` is already the remote string,
  `session_pickup.py:742`; the open reads the manifest). An older Mac sends nothing → null.
- The Mac is the owner's own device, so a wrong answer only mis-describes the owner's own repo.

What the page and the copy show, for someone **outside** the workspace:

| `repoInfo` | Page | Copy's `code` |
|---|---|---|
| `public && pushed` | `github.com/o/r · main · a1b2c3d` | `{mode: "clone", remote, commit: head}` |
| `!public && pushed` | "Code is in a private repository" | `{mode: "none"}`; no remote, name or branch in the ticket |
| not pushed, null, or older Mac | "Code not shared" | `{mode: "none"}` |

The private row carries nothing from `repoInfo` but the flag. Inside the workspace: the page
shows today's repo line (`LocalSessionPanel.tsx:137-138`) and the
copy's code is `{mode: "bundle"}` with the existing bundle URL plus `remote` (Pick up's
behaviour, uncommitted work included).

### 3.4 Redaction

New `core/services/session_redact.py`, one function `redact(text) -> text`, replacing each match
with `[redacted]`. Patterns: str versions of the three secret patterns in
`marketplace_safety._PATTERNS` (`hardcoded-secret`, `aws-secret-key`, `provider-token`,
`marketplace_safety.py:80-115`; those are bytes regexes), plus: PEM private-key blocks;
`.env`-shaped lines (`NAME=value` where NAME contains KEY, SECRET, TOKEN or PASSWORD); Stripe
`(sk|rk|pk)_(live|test)_…`; JWTs (`eyJ….….…`); `Authorization: Bearer …`; Google `AIza…`;
credentials in URLs (`://user:pass@`). Home-directory prefixes (`/Users/<name>/`,
`/home/<name>/`) become `~/`. It is applied:

- to every outsider copy, streamed one transcript part at a time (gunzip → per JSONL line: parse,
  redact every string, re-serialize → gzip → upload), never the whole snapshot in memory
  (parts are up to 64 MiB, the snapshot up to 1 GiB, `session_pickup.py:52-53`). The sidecars
  are a tar.gz (`snapshot.rs:319`): text entries are redacted, binary entries dropped, re-tarred;
- to public event pages (§3.2) for non-members.

Member copies are not redacted (ruling below): Pick up and in-workspace Continue hand teammates
the snapshot as it is today, code bundle included. Live screen frames are never redacted
(ruling 3).

### 3.5 Copies

One row per copy, found by the hash of its continue code:

- `SESSIONCOPY#<codeHash>` / `META` → `{ownerId, sessionId, copyId, userId, member: bool,
  challenge, state: preparing|ready|failed, codeMode, createdAt, expiresAt}` (TTL one hour; the
  code is redeemable for its first ten minutes).
- Audit `session_copied` with `subjectUserId` and `detail` = the taker's display name (an
  outsider is not in the member list the panel resolves names from); added to `AUDIT_ACTIONS`
  (`local_sessions.py:1721`). The owner's Access section lists these entries (`list_audit`,
  `local_sessions.py:1816`); that is "the sharer sees who took a copy".

Objects: `session-pickup/<owner>/<sid>/<epoch>/copies/<copyId>/` (`transcript.jsonl.gz` parts,
`sidecars.tar.gz`). Under the pick-up prefix on purpose: it stays out of `<owner>/*`, which the
workspace's agent runtime role can read (`session_pickup.py:12-14`). No new cleanup code:
`sweep_once` already deletes every object the manifest does not name once it is older than
`ABANDONED_UPLOAD_S` (one hour, `session_pickup.py:66, 836-840`), and `retire` deletes the epoch.

Routes:

- `POST /session-links/copies {token, challenge}` (`get_human_session`, `auth.py:255`; requires an
  org, i.e. the free workspace). Checks as `request_pickup` (`session_pickup.py:406-424`):
  link on, Claude session, snapshot `ready`, not expired, epoch current. Writes the copy row +
  audit; a member's copy is `ready` at once, an outsider's starts the redaction in a background
  task (`run_in_thread`, at most one per backend task, behind an `asyncio.Semaphore(1)`). Returns
  `{code}`.
- `POST /public/session-copies/redeem {code, verifier}` (no auth; the code plus the CLI's
  verifier is the credential: `sha256(verifier)` must equal the stored `challenge`, so a code
  left in browser history is useless) → `{state: "preparing"}` or a **copy ticket**:
  `{title, from: <owner first name>, transcript: [Download], sidecars, cwdRelative,
  claudeVersion, code}` with `code` per §3.3 (a private repo's is `{mode: "none"}`). Not a `PickupTicket` (`session_pickup.py:163`):
  that carries `session_id`, `human_user_id`, `conversation_id` and `cwd_name`. A `preparing`
  copy older than five minutes is restarted.

### 3.6 The CLI

No new package. `lightbulb continue '<link>'` is a new subcommand of the existing `lightbulb`
command, which is the desktop app's own binary (`lightbulb-desktop`): the app links
`~/.local/bin/lightbulb` to it (`cli::install`, `cli.rs:584`, run at launch by
`install_at_launch`, `cli.rs:630`), and `main.rs:7-10` hands argv to `cli::cli_args` (`cli.rs:257`,
which matches the link name or `lightbulb-desktop cli …`) then `cli::main` (`cli.rs:526`), whose
`parse` (`cli.rs:274`) today accepts only `claude|codex`. Add `continue` there and a Rust
module beside `session_pickup.rs` that reuses its code directly: `argv` (`:624`), `workdir`
(`:641`), `import_code` (`:433`), `allowed_url` (`:111`) and the part download checks. Nothing is
ported. **`cli.rs` may be owned by another in-flight stream: coordinate before editing.** A
receiver without the app has no command; the page says "Get Lightbulb for Mac" first (§2.2).

- `continue <link>`:
  1. Parse the fragment token locally. Make a random `verifier`. Start a loopback listener on
     `127.0.0.1:<random>` and open
     `https://flickmeon.ai/s?port=…&challenge=<sha256(verifier)>#<token>`. In that mode the `/s`
     page (signed in; else sign-up/onboarding first) shows one confirm button, calls
     `POST /session-links/copies`, and redirects to `http://127.0.0.1:<port>/done?code=…`. The
     page accepts only an integer port 1024-65535 and only the host `127.0.0.1`.
  2. Poll redeem every 2 s until ready. Download and verify every part (size + sha256) with the
     desktop's code (`session_pickup.rs:300-356, 836-902`), refusing URLs outside the bucket's
     `/session-pickup/` path (`allowed_url`).
  3. Code: `bundle` (workspace members only) → `import_code` (`session_pickup.rs:433`: fetch,
     `bundle verify`, `git worktree add` under `.claude/worktrees/`) inside a clone of `remote` in
     the current folder. `clone` (public repos only) → if the current folder is a clone of
     `remote`, `fetch` + `git worktree add <commit>`; else `git clone <remote>` into
     `./<repo-name>` and check out `commit` on `continue/<slug>`. A failure prints "Couldn't get
     the code (<reason>). Continuing with the conversation only." `none` → the current folder.
  4. Mod: if `claude plugin list` lacks `lightbulb-sessions@lightbulb`, install it with the same
     two commands as `plugin.rs:35-41` (the app already bundles the mod, so there is one source).
     Write `~/.lightbulb/continued/<newSessionId>.json` `{from: "<first name>", title}`.
  5. Run the desktop's `argv` (`session_pickup.rs:624-638`): `claude --resume <transcript>
     --fork-session --session-id <new uuid> --setting-sources user`, in
     `<worktree>/<cwdRelative>` resolved by `workdir` (`session_pickup.rs:641`). Claude writes
     the fork into `~/.claude/projects/` itself; that is how the conversation lands there. The
     original session and its Mac are never contacted.
- Mod change: on `session.start` (`register.tsx:129`) read
  `~/.lightbulb/continued/<session id>.json` with `$.fs.read`; while the session is not itself
  shared, the `AbovePrompt` band (`register.tsx:147`) shows
  `Continued from <from>'s session · Share yours (flickmeon.ai/desktop)`. The session id is
  `$.session.id()` (verified in the 2.1.289 mod API). The CLI picks the fork's id itself and passes
  `--session-id <uuid>` with `--resume … --fork-session`, so it can name the marker file before
  Claude starts; check that flag combination on the pinned Claude Code before building. Whether
  `$.fs.read` may read under `~` is unverified; if not, put the marker in the plugin's own
  directory (`$.plugin.root`).

### 3.7 Web

- `/s` (public; not in `middleware.ts:13`'s protected list): lifts the fragment out of the URL
  before analytics reads it (as `/chat/shared` does today), keeps it in `sessionStorage` across
  sign-up (Clerk's modal on the same page, as `/join/[orgId]` does), renders §2.2, and is the CLI
  confirm when `port` and `challenge` are present.
- Onboarding honours a same-origin `returnTo` instead of its fixed `router.replace("/chat")`
  (`onboarding/page.tsx:147`).
- `LocalSessionPanel`: Share popover (copy, audience, warning line, Turn off), Access lists copies.
  The pick-up switch goes.

## 4. Phase 2: public Watch

Members keep watching through `VIEWER#` rows and their own user channel (`session_screen.py:63`,
`forward` at `:197` publishes per viewer with `publish_user`, `live_events.py:138`). A link
watcher has no user channel in the owner's workspace, and AppSync accepts only Clerk OIDC today
(`api-stack.ts:627-640`).

- **AppSync:** add an `API_KEY` provider to the Event API's connection and a new namespace `pub`
  (subscribe `API_KEY`, publish `IAM`). The existing `ws` namespace keeps OIDC-only subscribe
  (`api-stack.ts:651-660`), so the key opens nothing there. `pub`'s `onSubscribe` accepts
  `/pub/<watchKey>` only if a strong read of `PUBWATCH#<watchKey>` / `META` finds an unexpired
  lease. The key is public by design; the lease is the credential. `AppSyncEventsClient` gains an
  `apiKey` mode (`x-api-key` instead of `Authorization`, `appsyncEvents.ts:67`).
- **Lease:** `POST /public/session-links/viewing {token, watchKey?}` (optional auth) every 15 s.
  First call mints `watchKey` (UUID4) and returns it with the realtime endpoint and API key.
  Writes `PUBWATCH#<watchKey>`/`META {ownerId, sessionId, expiresAt}` and
  `LINKVIEWER#<watchKey>` in the session partition `{userId?, since, expiresAt}` (TTL 45 s,
  `VIEWER_TTL_S`), and refreshes the `WATCH#<device>#<sid>` row and nudge exactly as `watch`
  does, so the Mac streams (`ack_with_watching`, `session_screen.py:126`). Response:
  `{watchers, me}`, which is also how a guest learns their join answer (no new channel).
- **Fan-out:** `forward` also reads `LINKVIEWER#` rows and publishes to `/pub/<watchKey>` for each
  live one; same 2 MiB/min per-session budget; at most 20 link watchers per session.
- **Presence:** `session_presence.entry` (`:54`) adds signed-in link watchers to `viewers` (by
  `human_display_name`) and a new `anonymousViewers: n`; the mod's `bandRuns` (`state.ts:12`)
  appends `+ n` (`● Live · Aden, Mira + 3 watching`). The page shows the count only, never names.
- **Turn off link** also deletes the link's `PUBWATCH#`/`LINKVIEWER#` leases, so watchers stop
  receiving frames at once.

## 5. Phase 3: guest Steer

- **Ask:** `POST /session-links/join-requests {token}` (`get_human_session`). Link on, audience
  `anyone`, session live. Same write as `request_join` (`local_sessions.py:1934`) with the entry
  marked `guest: true`. Notifies the owner (`_notify_join_request`) and pushes presence; the band
  reads `Sam Lee (outside your workspace) asked to steer [Allow] [Decline]`. Members keep the
  existing route. The receiver's button becomes "Requested", then a terminal input once allowed,
  or "<Owner> declined" (they may ask again).
- **Allow:** the existing `decide_join` (`:1956`), from the band through
  `native-session-decisions` or from the app. For a `guest` entry it also adds the user to
  `guests: [userId]` on META.
- **Type:** `POST /session-links/input {token, epoch, data}` (`get_human_session`). Resolves the
  session from the link, then requires `user in guests and user in allowed_senders(row)`
  (`:756`). Team control never admits a guest (`may_control`'s team branch, `:761`, is not used
  here). Then the body of `type_into` (`session_screen.py:326`), with `_still_a_member` skipped
  for guests; `fromOwner` is false, so the Mac strips band keys (`remote_input.rs:92`).
- **Revoke:** `remove_sender` (`:1993`) also drops `guests`; session end empties
  `allowed_senders`; turning the link off or Pause clears `guests` and refuses the route (it
  re-reads the link per call). A guest has no row anywhere else: no membership, user stream, room
  or agent path. Agents and other sessions cannot reach these routes (`get_human_session`
  refuses non-session tokens).

## 6. Security

**An anonymous holder of an "Anyone" link can:** read the page model (§3.2); read redacted events;
from Phase 2, receive the live terminal frames, unredacted, while holding a lease.
**Cannot:** learn the owner id, workspace id, session id or the page-model folder path (event
text is redacted best-effort, §3.4); take a copy; ask to steer; type; subscribe to anything under
`ws`; reach any other session.
**A signed-in outsider additionally can:** take a redacted copy (conversation only for a private repo: no remote, name or branch reaches
them); from Phase 3, ask to steer, and type once allowed.

- **Tokens:** 256-bit link token in the fragment; 128-bit-plus watch keys and continue codes,
  hash-stored where they are credentials, short TTLs (45 s lease, 10 min code, 1 h copy). A
  continue code is redeemable only with the CLI's verifier.
- **Revocation:** turning the link off clears `META.shareLink`, deletes the lookup row and retires
  the snapshot (and, from Phase 2/3, the leases and guests). `forward` publishes only to live
  leases, so open subscriptions go silent at once, the same property room delivery-key rotation
  relies on. Copies already taken stay with their takers.
- **Abuse limits:** an API Gateway route `ANY /api/v1/public/{proxy+}` with its own throttle,
  as the Slack route has (`api-stack.ts:128`, `:382-407`); one redaction at a time per backend
  task (§3.5); 20 link watchers per session (§4).

## 7. Reused vs new

| Reused | New |
|---|---|
| Pick-up snapshot stream, manifest, presigned URLs, retire/sweeper (also cleans copies) | one-link fields, `/link` routes, `/s` page |
| `lightbulb` CLI (`cli.rs`) and the desktop's `argv`, `workdir`, `import_code`, download checks, used directly | a `continue` subcommand |
| Join requests, `decide_join`, `remove_sender`, band, `native-session-decisions` | `guest` flag, `guests`, link-scoped join/input routes |
| `watch`/`forward`/`ack_with_watching`, presence | `pub` namespace + API key, `LINKVIEWER`/`PUBWATCH` leases, `anonymousViewers` |
| `marketplace_safety` patterns, join-link token handling | `session_redact.py`, `SESSIONCOPY` row, copy ticket |
| Sign-up, onboarding, `provision_new_workspace` | `returnTo` on onboarding |
| Mod and its install commands | `repoInfo` from the Mac, "Continued from" band |

## 8. Ruled

**Ruled (owner, 2026-10-05):**

1. No npm package. Continue is `lightbulb continue '<link>'` in the existing desktop CLI (§3.6).
   Receivers without the app are told "Get Lightbulb for Mac" first; non-Mac receivers are
   deferred (§9).
2. Private repos stay private: no remote URL, repo name or branch of a private repo leaves the
   sharer's Mac towards an outside receiver. Outside the workspace Continue gives the conversation
   only and the page says "Code is in a private repository". Inside the workspace Pick up is
   unchanged (§3.3).

**Ruled (controller, 2026-10-05):**

3. Pick up stays unredacted: it serves workspace teammates. Only outsider copies and outsider
   event pages are redacted.
4. A link to an ended session lives as long as its snapshot: 7 days (`PICKUP_RETENTION_SECONDS`,
   `local_sessions.py:189`). No separate limit.
5. The separate "Allow pick up" switch goes: the link is the permission (§3.1).
6. A no-browser (device-code) confirm for headless receivers is deferred.
7. Codex sessions get Watch and Read only (pick-up is Claude-only).

## 9. Deferred (owner: "we will do that as we get user feedback")

- Per-prompt injection checks on continued or steered sessions.
- Screening guest messages and keystrokes.
- A safety floor for permission modes (e.g. refusing to resume a copy in bypass mode).
- A paste-the-code confirm for receivers without a browser (ruled above).
- Continue for receivers not on a Mac (no desktop app, so no CLI).
