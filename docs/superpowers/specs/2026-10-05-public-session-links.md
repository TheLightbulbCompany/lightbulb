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
   sign-up and provisioning path. Then `npx lightbulb continue <link>` writes the conversation
   into their Claude Code and opens `claude --resume` on it: their machine, their Claude, their
   plan. Nothing runs on our agents; the original is untouched. Their terminal shows "Continued
   from <name>'s session · Share yours". The copy is made when they press Continue. Secrets are
   redacted server-side on every copy. The sharer sees who took a copy.
5. **Steering requires sign-in plus Ask to steer**, answered in the owner's band; only the owner's
   keys work there (#3750). A person from another workspace becomes a **guest on that one
   session**: no membership, no access to anything else, revoked when the owner removes them or
   the session ends. People only.
6. **Code visibility** is shown on the page and Continue acts on it: public repo (name, branch,
   commit; code fetched); private repo ("Private repository", no name; Continue clones with the
   receiver's own git credentials, else conversation only, and says so); not pushed or no repo
   ("Code not shared", conversation only). Inside the sharer's workspace, Continue keeps Pick
   up's behaviour (uncommitted work included).
7. **Build order:** Continue, then public Watch, then guest Steer.
8. Deferred: see §9.

## 2. Customer flows

### 2.1 Sharer

1. In the session panel (web/desktop `LocalSessionPanel`), **Share**. The first press creates the
   session's one link and copies it. Audience defaults to **Workspace only**.
2. Optionally switch to **Anyone with the link**. One line appears: "Anyone with the link can
   watch your live terminal. A live screen can't be redacted."
3. Send the link anywhere.
4. While shared: the terminal band shows `● Live · Aden, Mira + 3 watching`. A guest's ask shows
   as `Sam Lee (outside your workspace) asked to steer [Allow] [Decline]`; only the owner's keys
   work. The panel's Access section lists who took a copy and when.
5. **Turn off link**: every link page reads "This link was turned off", watchers stop receiving
   frames at once, guests lose typing. Pause (existing) has the same effect while paused.

### 2.2 Receiver

1. Opens `https://flickmeon.ai/s#<token>`. No sign-in. The page shows title, the sharer's first
   name, provider, state, and the code line (§3.3).
2. **Live:** the terminal plays (Phase 2). **Ended:** the transcript, redacted for anyone outside
   the workspace.
3. **Continue in my Claude Code:** if signed out, Clerk sign-up, then the existing onboarding
   creates their workspace (provisioning starts as for any new workspace), then back to the page.
   The page shows `npx lightbulb continue '<link>'` with a copy button.
4. They run it in their own terminal. The CLI opens the browser on a one-click confirm ("Continue
   <title> in Claude Code on this computer?"), which is the moment the copy is made. The CLI
   downloads the redacted copy, gets the code (§3.3), installs the Lightbulb mod if absent, and
   execs `claude --resume … --fork-session`. The band reads "Continued from Prasiddha's session ·
   Share yours".
5. **Ask to steer** (live, signed in, Phase 3): the button becomes "Requested", then a terminal
   input once allowed, or "<Owner> declined" (they may ask again).

A workspace member opening the link gets today's in-app paths (watch, type if allowed, Pick up)
plus Continue; nothing new is offered to them except the CLI.

## 3. Phase 1: Continue

### 3.1 The one link

Replaces the many-links share model (`local_sessions.py:2233-2440`: `SHARE#<linkId>` listing
rows, `SHARELINK#<hash>` lookup rows, per-link expiry and a `snapshotSeq` cap).

- **Session META** gains `shareLink: {tokenHash, tokenEnc, audience: "workspace"|"anyone",
  createdAt, createdBy, pickupByLink}`. `tokenEnc` is a Fernet ciphertext under `ENCRYPTION_KEY`
  so the owner can copy the same link again, exactly as join links do
  (`workspace_join.py:25-35`). Written through `repo.put_session` like every META change.
- **Lookup row** `SHARELINK#<tokenHash>` / `META` → `{ownerId, sessionId}` (same pk scheme as
  `_share_pk`, `local_sessions.py:2237`). No `expiresAt`: a link lives until it is turned off or
  the session's snapshot retention ends (§3.6).
- **Token:** `secrets.token_urlsafe(32)`, in the URL **fragment**, sent only in POST bodies, never
  logged; every failure reads the same (`workspace_join.py:73-85`, `local_sessions.py:2384-2386`).
- **Pick-up:** creating a link turns pick-up on through `session_pickup.set_pickup`
  (`session_pickup.py:328`) if it was off, and records `pickupByLink: true`; turning the link off
  calls `retire` (`session_pickup.py:853`) only when the link turned it on. This keeps the
  author's Mac snapshotting (the `PICKUPWANT` row, `session_pickup.py:300`) so a copy can be made
  after the Mac goes away.
- Existing `SHARE#`/`SHARELINK#` rows (≤30 days old, workspace-only) are deleted by a one-off
  script at cutover; old links read as turned off. `/chat/shared` is replaced by `/s`.

Owner routes (Clerk, owner only, `_owned_session` rules, `local_sessions.py:2289`):

| Route | Does |
|---|---|
| `PUT /messaging/sessions/{id}/link {audience}` | create if absent (audit `share_link_created`) or change audience; returns `{url, audience}` |
| `GET /messaging/sessions/{id}/link` | `{url, audience}` or 404 |
| `DELETE /messaging/sessions/{id}/link` | delete lookup row and `shareLink`, link watcher leases (§4), guests (§5); audit `share_link_revoked` |

These replace the three `share-links` routes (`routers/messaging.py:369-384`).

### 3.2 Opening a link (unauthenticated)

`POST /public/session-links/open {token}` with `get_optional_user` (`auth.py:279`):

1. GetItem the lookup row by hash; load META. Gone, off, `unshared`, or audience `workspace` and
   the caller is not a current member (`_still_a_member`, `session_screen.py:52`) → the one
   "turned off or not for you" answer.
2. Returns `{title, ownerFirstName, provider, state: live|ended|offline, code, watchers, me}`;
   `me` is `{signedIn, member, joinState, canType}`. No owner id, workspace id, session id, cwd or
   path.
3. `POST /public/session-links/events {token, afterSeq, limit}` reads events as
   `list_shared_events` does (`local_sessions.py:2426`) without the `snapshotSeq` cap; for a
   non-member every string field is passed through the redactor (§3.4).

### 3.3 Repo, branch, commit, visibility (desktop)

Today the Mac sends `code {remote, branch, head, snapshot}` only when it captured a bundle
(`snapshot.rs:435-556`, sent at `snapshot.rs:804`), and the bundle is `--not --remotes=origin`
(`snapshot.rs:513-517`), so it is useless to anyone without origin. New, beside it in the
commit body (`CommitRequest`, `session_pickup.py:196`):

```
repo: {remote, branch|null, head, pushed: bool, public: bool} | null
```

- `remote`: `normalize_remote` of `origin` (`session_pickup.rs:85`), e.g. `github.com/o/r`.
- `pushed`: `git for-each-ref --count=1 --contains <head> refs/remotes/origin` is non-empty.
- `public`: an anonymous probe, `git -c credential.helper= ls-remote https://<remote>.git HEAD`
  with `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/usr/bin/false`; exit 0 = public. Cached per
  remote for one hour. Runs through `git_run` (`session_pickup.rs:40`, no shell).
- Sent on every commit, also when the bundle is unchanged, too large or skipped. Stored on the
  `PICKUP` manifest and copied to `pickupSnapshot.repo` (beside `snapshot.rs`'s current `repo`
  field, `session_pickup.py:742`). A Mac without the new `repoVisibility` feature sends nothing.
- The Mac is the owner's own device, so a wrong answer only mis-describes the owner's own repo.

What the page and the copy show, for someone **outside** the workspace:

| `repo` | Page | Copy's `code` |
|---|---|---|
| `public && pushed` | `github.com/o/r · main · a1b2c3d` | `{mode: "clone", remote, commit: head}` |
| `!public && pushed` | "Private repository" | `{mode: "clone", remote, commit: head}` |
| not pushed, null, or older Mac | "Code not shared" | `{mode: "none"}` |

Inside the workspace: the page shows today's repo line (`LocalSessionPanel.tsx:137-138`) and the
copy's code is `{mode: "bundle"}` with the existing bundle URL plus `remote` (Pick up's
behaviour, uncommitted work included).

### 3.4 Redaction

New `core/services/session_redact.py`, one function `redact(text) -> text`, replacing each match
with `[redacted]`. Patterns: the three secret patterns already in
`marketplace_safety._PATTERNS` (`hardcoded-secret`, `aws-secret-key`, `provider-token`,
`marketplace_safety.py:80-115`) applied to text, plus PEM private-key blocks and `.env`-shaped
lines (`NAME=value` where NAME contains KEY, SECRET, TOKEN or PASSWORD). It is applied:

- to every copy: each transcript JSONL line is parsed, every string value redacted, re-serialized
  (the file stays valid JSONL); each sidecar text file the same way, binary sidecars dropped;
- to public event pages (§3.2) for non-members.

The code bundle (members only) is not redacted: it is the workspace's own code, as Pick up hands
it today. Live screen frames are never redacted (ruling 3).

### 3.5 Copies

Rows in the session partition:

- `COPY#<copyId>`: `{userId, member: bool, state: preparing|ready|failed, codeMode, createdAt,
  expiresAt}` (TTL one hour).
- `CONTINUECODE#<codeHash>` / `META` → `{ownerId, sessionId, copyId, userId, expiresAt}`
  (ten minutes).
- Audit `session_copied` with `subjectUserId` (added to `AUDIT_ACTIONS`,
  `local_sessions.py:1721`). The owner's Access section lists these entries (`list_audit`,
  `local_sessions.py:1816`); that is "the sharer sees who took a copy".

Objects: `session-pickup/<owner>/<sid>/<epoch>/copies/<copyId>/` (`transcript.jsonl.gz`,
`sidecars.json.gz`). Under the pick-up prefix on purpose: it stays out of `<owner>/*`, which the
workspace's agent runtime role can read (`session_pickup.py:12-14`), and `retire`/`sweep_once`
(`session_pickup.py:823-903`) already delete everything under a retired epoch. The sweeper also
deletes `copies/` objects older than one hour.

Routes:

- `POST /session-links/copies {token, state}` (`get_human_session`, `auth.py:255`; requires an
  org, i.e. the free workspace). Checks as `request_pickup` (`session_pickup.py:406-424`):
  link on, snapshot `ready`, not expired, epoch current. Writes `COPY` + `CONTINUECODE` + audit,
  starts the redaction in a background task (`run_in_thread`), returns `{code}`. Limits: 5 copies
  per person per session per hour, one redaction per backend task at a time (others queue).
- `POST /public/session-copies/redeem {code}` (no auth; the code is the credential) →
  `{state: "preparing"}` or the ticket: `PickupTicket` (`session_pickup.py:163`) with the copy's
  URLs and a `code` object per §3.3. A `preparing` copy older than five minutes is restarted.

### 3.6 The CLI

- Lives at `apps/cli/` (`@lightbulb/cli` in the pnpm workspace), Node ≥18, no dependencies
  (built-in `fetch`, `zlib`, `child_process`). Its `prepack` copies
  `apps/desktop/src-tauri/mods/` into the package so there is one source of the mod. Published to
  npm by a new `cli-release.yml` on `cli-v*` tags (Linux runner). **The npm name `lightbulb` is
  taken** (see report); the command needs a name we own.
- `continue <link>`:
  1. Parse the fragment token locally. Start a loopback listener on `127.0.0.1:<random>` and open
     `https://flickmeon.ai/s/continue?port=…&state=…#<token>`. That page (signed in; else
     sign-up/onboarding first) shows one confirm button, calls `POST /session-links/copies`, and
     redirects to `http://127.0.0.1:<port>/done?state=…&code=…`.
  2. Poll redeem every 2 s until ready. Download and verify every part (size + sha256), as the
     desktop does (`session_pickup.rs:300-356, 836-902`), refusing URLs outside the bucket's
     `/session-pickup/` path (`session_pickup.rs:111`).
  3. Code: `bundle` → inside a clone of `remote` in the current folder, the desktop's
     `import_code` steps (`session_pickup.rs:433`: fetch, `bundle verify`, `git worktree add` under
     `.claude/worktrees/`). `clone` → if the current folder is a clone of `remote`, `fetch` + `git
     worktree add <commit>`; else `git clone <remote>` into `./<repo-name>` and check out `commit`
     on `continue/<slug>`. Git runs with the person's own credentials. A failure prints "Couldn't
     get the code (<reason>). Continuing with the conversation only." `none` → the current folder.
  4. Mod: if `claude plugin list` lacks `lightbulb-sessions@lightbulb`, write the mod to
     `~/.lightbulb/claude-mods/` and run the same two commands as `plugin.rs:35-41`. Write
     `~/.lightbulb/continued/<newSessionId>.json` `{from: "<first name>", title}`.
  5. Exec the desktop's argv (`session_pickup.rs:624-638`): `claude --resume <transcript>
     --fork-session --session-id <new uuid> --setting-sources user`, in
     `<worktree>/<cwdRelative>` resolved as `workdir` does (`session_pickup.rs:641`). Claude
     writes the fork into `~/.claude/projects/` itself; that is how the conversation lands there.
     The original session and its Mac are never contacted.
- Mod change: on `session.start` (`register.tsx:129`) read
  `$HOME/.lightbulb/continued/<$.session.id()>.json` (`$.env.get`, `$.fs.read`); while the
  session is not itself shared, the `AbovePrompt` band (`register.tsx:147`) shows
  `Continued from <from>'s session · Share yours (flickmeon.ai/desktop)`.

### 3.7 Web

- `/s` (public; not in `middleware.ts:13`'s protected list): lifts the fragment out of the URL
  before analytics reads it (as `/chat/shared` does today), keeps it in `sessionStorage` across
  sign-up, renders §2.2. `/s/continue` is the CLI confirm.
- Onboarding (`onboarding/page.tsx:60`) honours a same-origin `returnTo` after it creates the
  workspace.
- `LocalSessionPanel`: Share popover (copy, audience, warning line, Turn off), Access lists copies.

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
  live one; same 2 MiB/min per-session budget.
- **Presence:** `session_presence.entry` (`:53`) adds signed-in link watchers to `viewers` (by
  `human_display_name`) and a new `anonymousViewers: n`; the mod's `bandRuns` (`state.ts:12`)
  appends `+ n`. The page shows the count only, never names.
- Live session without a terminal (transcript sharing): the page polls §3.2's events.

## 5. Phase 3: guest Steer

- **Ask:** `POST /session-links/join-requests {token}` (`get_human_session`). Link on, audience
  `anyone`, session live. Same write as `request_join` (`local_sessions.py:1934`) with the entry
  marked `guest: true`. Notifies the owner (`_notify_join_request`) and pushes presence; the band
  labels it "(outside your workspace) asked to steer". Members keep the existing route.
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
receive the live terminal frames, unredacted, while holding a lease.
**Cannot:** learn the owner id, workspace id, session id or folder path; take a copy; ask to steer;
type; subscribe to anything under `ws`; reach any other session.
**A signed-in outsider additionally can:** take a redacted copy (and with it the repo remote for a
private repo, see report), ask to steer, and type once allowed.

- **Tokens:** 256-bit link token in the fragment; 128-bit-plus watch keys and continue codes,
  hash-stored where they are credentials, short TTLs (45 s lease, 10 min code, 1 h copy).
- **Revocation:** turning the link off deletes the lookup row, the leases and the guests.
  `forward` publishes only to live leases, so open subscriptions go silent at once, the same
  property room delivery-key rotation relies on. Copies already taken stay with their takers.
- **Abuse limits:** an API Gateway route `ANY /api/v1/public/{proxy+}` with its own throttle,
  as the Slack route has (`api-stack.ts:128`, `:382-407`); at most 20 link watchers per session;
  at most 30 new leases per link per minute (in-process, like `_spent`, `session_screen.py:154`);
  the copy limits in §3.5.

## 7. Reused vs new

| Reused | New |
|---|---|
| Pick-up snapshot stream, manifest, presigned tickets, retire/sweeper | one-link fields, `/link` routes, `/s` page |
| Desktop `argv`, `workdir`, `import_code`, download checks (ported to Node) | `apps/cli`, `cli-release.yml` |
| Join requests, `decide_join`, `remove_sender`, band, `native-session-decisions` | `guest` flag, `guests`, link-scoped join/input routes |
| `watch`/`forward`/`ack_with_watching`, presence | `pub` namespace + API key, `LINKVIEWER`/`PUBWATCH` leases, `anonymousViewers` |
| `marketplace_safety` patterns, join-link token handling | `session_redact.py`, `COPY`/`CONTINUECODE` rows |
| Sign-up, onboarding, `provision_new_workspace` | `returnTo` on onboarding |
| Mod and its install commands | `repo` metadata from the Mac, "Continued from" band |

## 8. Open questions

1. npm name: `lightbulb` is owned by another publisher. Ask for a transfer, or ship as e.g.
   `npx @thelightbulbcompany/cli continue` / `npx lightbulb-cli continue`?
2. Private repos: is it acceptable that a signed-in receiver's CLI receives the repo remote (and
   so its name) in order to clone (see report)?
3. Should Pick up (the desktop button) also hand out a redacted copy, or only Continue?
4. Does a link to an ended session expire with the 7-day snapshot retention
   (`PICKUP_RETENTION_SECONDS`, `local_sessions.py:189`) or need its own limit?
5. Should the separate "Allow pick up" switch go, now that the link implies pick-up?
6. Headless receivers (SSH, no browser): add a paste-the-code fallback to the confirm page?
7. Codex sessions: Watch and Read only (pick-up is Claude-only); confirm.

## 9. Deferred (owner: "we will do that as we get user feedback")

- Per-prompt injection checks on continued or steered sessions.
- Screening guest messages and keystrokes.
- A safety floor for permission modes (e.g. refusing to resume a copy in bypass mode).
