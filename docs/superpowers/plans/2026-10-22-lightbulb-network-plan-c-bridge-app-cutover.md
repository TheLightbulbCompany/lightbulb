# lightbulb-connect, Plan C: the bridge, the app, and the cutover (monorepo)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The monorepo stops being a hub, and the shared-sessions product keeps working the same (owner, O1). A Lightbulb workspace joins each linked person's network as one cloud member: the bridge. Friday, cloud agents, hosted MCP callers and webhook agents send and receive through it. A web or phone Allow becomes a signed `decision`; this covers Claude permission prompts too (O2). The desktop app attaches to the engine and carries its shared sessions to the bridge over one engine stream per Mac. The hub's transport is deleted in the same PR as the switch, and the release train is ordered and has a rollback. Acceptance: the design checklist (1–10), the bridge checklist (1–17) and the app checklist (1–23), run on the cutover build on the owner's Mac and phone (Task 24).

**Architecture:** The engine is the public repo `lightbulb-connect`. It is pinned by tag in `network-version.json`, and that one file feeds four places: the desktop bundle (the binary as a resource), the installer (version and sha256), `apps/bridge/` (a git-tag crate dependency) and the app's protocol check.

The bridge is a Rust ECS task on the platform cluster. It runs one embedded engine instance (Plan B Task 11b) per (workspace, person) link. Keys are in Secrets Manager, state is snapshotted to S3, and only the VPC can reach it.

The relay is `iroh-relay` behind the existing internal ALB, published through a CloudFront VPC origin (the `bv-` pattern), with QUIC address discovery off.

The backend keeps the workspace's session model, renamed `workspace_sessions`. Only its writer changes: the bridge writes it from each Mac's control stream, in place of the hub socket.

Rollout:
- **Phase 1** (Tasks 1–12) is additive. It merges behind `NETWORK_ENABLED=false`.
- **Phase 2** (Tasks 13–21) is ONE PR, with one reviewed commit per task. Task 22 is the ordered release train that ships it.
- **Task 23** retires the WebSocket door last, in a separate PR, after a week of soak.

**Tech Stack:** Python 3 / FastAPI / DynamoDB / PyJWT `EdDSA` (backend, existing); Rust 2021, `tokio`, `axum`, `aws-sdk-{secretsmanager,s3,dynamodb}` and the `lightbulb-connect` crate (bridge); Tauri 2 (desktop); Next.js 16 and React 19 (frontend); Expo (mobile); AWS CDK (`apps/infra`); `iroh-relay` (version = Plan B's `iroh` pin).

**Specs, in binding order:**
1. `docs/superpowers/specs/2026-10-22-lightbulb-network-architecture.md`.
2. `…-bridge-spec.md`, `…-app-spec.md`, `…-onboarding-and-hosted-mcp-spec.md` and `…-p2p-design.md`.
3. The engine contracts in `docs/superpowers/plans/2026-10-22-lightbulb-network-plan-b-network.md`.

The coordinator's rulings in `.superpowers/sdd/2026-10-22-lightbulb-network-plan-b-network/plan-c-rulings.md` bind over the specs where they differ.

## Still needed from the engine (for the coordinator)

Plan C uses the names below as written here. Each one is a guess until Plan B states it.

1. **A control channel with input.** Every O1 feature that goes from the backend to the Mac needs a bridge→Mac path: teammate typing, the presence band, pick-up and resume requests, and watch on/off. Plan C uses ONE stream per Mac, on the reserved name `<short>/app`. The app sends `share.grant {session: "<own short>/app", viewer: <own bridge key>, input: true, expires_at}`, and the engine delivers viewer→owner `Stream` frames to the owner as `stream` events. Plan B today refuses `input: true` (`unsupported`, Plan D) and drops viewer→owner frames.
2. **Presence before the first envelope.** After `cloud.sessions.set`, a `send` from the new name must not be refused `policy` because the peer has not yet seen the presence. The engine should either flush presence first or carry it with the envelope.
3. **An exported framing call.** `lightbulb_connect::frame::framed(label: &str, request_id: &str, body: &str) -> String` (defang included), as public crate API. The bridge frames webhook-bound text with it.
4. **The permission-hold op.** Plan C assumes `hold.file {kind: "permission", target_session, title, text}` → `{id}` (app-only, person-only). The answer arrives as `hold_closed {id, by, verdict}`, and the bridge sees `hold_notice.kind = "permission"`.
5. **`status.claim_live: bool`**, which the app reads (ruling A on `ui.claim`).
6. **The proof-refresh client.** At 20 h of a proof's 24 h life, the engine calls `POST {issuer}/proof/refresh {proof, at, sig}`, where `sig` is the computer key over `canonical({proof, at})`. It stores the returned `{proof}` with `identity.proof`.
7. **The admission prompt for a cloud candidate**, rendered from the issuer-signed proof: `"<proof.workspace>" (a Lightbulb workspace) wants to join your network. It will see this network's directory: who is on it, their sessions, folders and what they are doing.` Buttons: `Decline`, `Allow`.
8. **Payload shapes to confirm:**
   - `receipt_changed {request_id, from, state, reason?}`.
   - `member_removed {member}`, also fired on the instance when its own key is removed.
   - `snapshot {to: <path>}` → `{ok}`.
   - `person_id` in the directory record.
9. **A question about the binding cloud inbound policy.** The rule is "own person accept, every other sender refused". It also refuses a teammate's reply to a message Friday sent them. Plan C applies the rule as written. A narrow exemption would restore replies: a `reply_to` that names an envelope this instance sent within 7 days. Decide in Plan B Task 11b.

## Engine names Plan C consumes (provided by Plan B)

| Need | Name in Plan B | Plan B task |
|---|---|---|
| Embedding | `embed::start(home, EmbedOpts{kind: Cloud, relay, issuer}) -> Instance`; `Instance::call`, `::events`, `::shutdown` | 11b |
| Cloud sessions | `cloud.sessions.set {sessions:[{name, agent, title}]}`; `send {from:"<short>/<name>", …}` → `{request_id, state, thread, tier}` | 11b |
| Inbound on a cloud instance | event `message {to_session, id}`; `inbox {session}` (marks read) | 11b |
| Bridge join and leave in process | `team.join {ticket, name, kind:"cloud", proof}`, `team.leave {}`, `team.unlink {}` admitted on a cloud instance; the admit sets `person_id = proof.sub` | 11b, 13 (coordinator addendum) |
| Cloud inbound policy | own `person_id` computer members accept; others refused `policy` | 11b (addendum) |
| Snapshot | `snapshot {}` op (`VACUUM INTO`), final snapshot on `shutdown` | 11b (addendum) |
| Holds on the bridge | event `hold_notice {hold_id, holding_member, holding_computer, target_session, from, label, kind, created_at}`; op `decision.send {hold_id, holding_member, verdict, scope}`; event `decision_result {hold_id, result: "applied"\|"not_open"}` | 14 (+ addendum) |
| Receipts and removal | events `receipt_changed`, `member_removed` | 8, 2 (addendum) |
| Directory | `directory {query?}` record, plus `person_id` | 7 (addendum) |
| Streams | `share.grant {session, viewer, input, expires_at}` → `{grant_id}`; `stream.open {session}` → `{stream_id}`; `stream.send {stream_id, data_b64}`; `stream.close`; events `stream_opened`, `stream`, `stream_closed`; a cloud viewer always needs a grant | 11 |
| Holds on the Mac | `holds {}` → `{holds:[{id:"H<n>", …}]}`; `hold.get {id}` (plus `target_birth_micros`); `hold.answer {id, verdict, scope}`; events `held`, `hold_closed {id, by}` | 9 |
| Claim | `ui.claim {}` → `{lease_s: 20}`, heartbeat every 7 s | 5 |
| Team | `team.invite {mode: "reusable"\|"new"\|"once"\|"open"}` → `{ticket, line, app_link, expires_at}`; `team.join_requests`, `team.admit {request, verdict}`, `team.get`, `team.remove` | 3, 6 |
| Policy | `policy.get`, `policy.set {person_id, scope, value}` / `{agents_send_without_asking}` | 9 |
| Proofs | `Proof {iss, kid, iat, exp, sub, name, computer, kind, workspace?}`; `ISSUER_KEYS: [(&str,[u8;32]); 3]` (`p1`, `p2`, `dev`); `identity.proof {proof}` | 13 (+ addendum) |
| Device routes | `POST {issuer}/device/start {member_key, computer}` → `{device_code, user_code, verification_uri_complete, interval}`; `POST {issuer}/device/token {device_code}` → `200 {"pending":true}`, then `200 {"proof"}` once, or `410` | 13 |
| Labels | `<App>` = the declared `agent`, sanitised (printable, ≤ 32 chars) | 13 (+ addendum) |
| Relay list | `{issuer}/relays.json` and `{issuer}/relays.json.minisig` | 12 |

## Global Constraints

- **Clean-room.** Do not open anything named reason-network or reasonmachines. Do not open the contents of `apps/desktop/src-tauri/src/local_sessions/hooked.rs`: Task 21a deletes it by path, and Task 17 finds its hooks by the command string `local-session-hook`, which comes from `main.rs`.
- **Hosts (exact).**
  - Product: `thelightbulb.company` (dev `dev.thelightbulb.company`).
  - Issuer: `https://thelightbulb.company/connect` (dev `https://dev.thelightbulb.company/connect`).
  - Relay: `relay.thelightbulb.company` (dev `relay-dev.thelightbulb.company`).
  - Install: `https://thelightbulb.company/install`. Device page: `https://thelightbulb.company/device`. Hosted MCP: `https://api.thelightbulb.company/mcp`.
  - Join line: `Join Lightbulb Connect: https://thelightbulb.company/j/#<ticket>`.
  - Before a new host's DNS exists, check that no company holds its slug, then add the slug to `RESERVED_SLUGS`.
- **Ports.**
  - Relay task: TCP 80 only, plain HTTP behind the ALB. QUIC discovery is off, so there is no UDP 7842 and no ACME. Metrics on 9090, VPC only.
  - Bridge: TCP 8080, in private subnets, ingress from the backend SG only.
  - Backend internal door: a new ALB listener on 8082, ingress from the bridge SG only.
- **Flags.** `NETWORK_ENABLED` (backend, set in the CDK env; default `false` in both envs) and `NEXT_PUBLIC_NETWORK_ENABLED` (frontend). While they are off: every `/api/v1/network/*` route answers 404, `/device` and `/j/` are not served, and `/install` serves today's app installer. Task 22 turns both on.
- **New env vars (all set by CDK).**
  - Backend: `NETWORK_ENABLED`, `BRIDGE_URL=http://bridge.lightbulb-{env}.local:8080`, `BRIDGE_INTERNAL_TOKEN` (secret), `NETWORK_ISSUER_KID` (`p1` in prod, `dev` in dev), `NETWORK_ISSUER_KEY_SECRET` (a secret NAME, read at call time, never an env value), `NETWORK_ISSUER_URL`, `NETWORK_AUDIT_TABLE`.
  - Bridge: `BACKEND_INTERNAL_URL=http://<ALB DNS>:8082`, `BRIDGE_INTERNAL_TOKEN` (secret), `OWNER_STATE_BUCKET`, `CONTAINERS_TABLE`, `LIGHTBULB_ENV`, `NETWORK_RELAY_URL`, `NETWORK_ISSUER_URL`.
- **Secrets and keys.**
  - Issuer keys: `lightbulb/{env}/network/issuer_key_<kid>` (Ed25519 seed, base64). Prod has `p1` (active) and `p2` (standby); dev has `dev`. All three public halves are compiled into the engine, so a release engine also verifies dev proofs (accepted risk, recorded). Only the backend task role reads them.
  - Each link's key: `lightbulb/{env}/network/bridge/{owner}/{person}`.
  - Internal token: `lightbulb/{env}/bridge_internal_token`.
  - Bridge state: `s3://$OWNER_STATE_BUCKET/bridge-state/<owner>/<person>/state.db`, never under `<owner>/*`.
- **Row keys.** The messaging table (`pk`/`sk`) holds:

  | pk / sk | Holds | Expiry |
  |---|---|---|
  | `OWNER#<owner>` / `NETLINK#<person>` | `{bridge_key, team_id?, state: waiting\|joined\|refused\|removed, linked_at}` | none |
  | `OWNER#<owner>` / `NETTHREAD#<thread>` | `{origin: run\|mailbox\|webhook, ref, session_name}` | TTL 7 d |
  | `NETMAIL#<owner>#<session_name>` / `MSG#<sent_at>#<request_id>` | one mailbox item | TTL 7 d, or read + 24 h |
  | `NETHOLD#<owner>#<person>` / `HOLD#<holding_member>#<hold_id>` | one open hold | TTL 7 d |

  There are no `COMPUTER#` rows. The lease lives in the CONTAINERS table as `owner_id = "__bridge__#lease"` `{holder, expires_at}`, so its renewals fire no messaging stream. Audit rows go to a new table, `lightbulb-{env}-network-audit` (`pk = OWNER#<owner>|ISSUER`, `sk = <ts>#<kind>#<id>`, TTL 30 d past the item's expiry). It gets one row per minted proof and one per relayed decision.
- **Who the bridge is.**
  - A `kind: cloud` member with a `kind=bridge` proof and `person_id = proof.sub`.
  - A person has one link at a time. Relinking replaces the old link after a confirm.
  - The bridge never mints links, never admits, never removes and never governs.
  - It relays `decision` with scope `once | person | own_cloud` only, never `cloud_of`.
  - It reads only these: mail addressed to its sessions, hold notices for its person, the directory, and streams granted to it.
- **Session names on a bridge.**
  - One namespace per bridge, `[a-z0-9-]{1,32}`; the first to register a name keeps it.
  - Workspace agents use `<agent-slug>`. Hosted callers use `<client-slug>-via-<person-name-slug>`, where the client is the registered OAuth/DCR client name or the agent key's name. Webhook agents use `<webhook-slug>`.
  - `agent` is what renders as `<App>`: the agent's display name, the client's registered name, or the webhook's name.
  - A rename makes a new session id and a new thread. This is documented and accepted.
  - One principal per session; two principals never share a name. Request-id dedupe is per session.
- **Requester binding.** Friday sends through the link of the person who wrote the run's `triggerMessageId`, read from the run ledger, never from the runtime payload. A run with no human trigger (nightly CEO, heartbeats, ambient `room_event`) sends through the link of the TARGET session's person, as that bridge's `<agent-slug>` session. No link means no send.
- **No phone text preview (owner).** A hold notice that reaches the workspace carries only the sender, the target, the computer and ids. The backend drops any `preview` key. Phone and web copy: `<label> wants to message your <Agent> session on <computer>.`
- **Person-only ops in the app.** These ops run only after a native AppKit sheet press, or from a notification action handled in Rust; the webview alone never reaches one: hold answer, join admit, policy set, invite, remove, send as the person, connect and disconnect workspace, sign-in, share grant. The mod socket's `/v1/share` refuses any caller with an agent ancestor.
- **Internal doors are VPC-only.** `/api/v1/internal/bridge/*` is served only on the backend's 8082 listener (an ALB rule plus the SG). The backend also refuses those paths unless the ALB-set `X-Forwarded-Port` is `8082`. The bridge listens only in private subnets.
- **Free path.** No copy on the free path says plan, trial, workspace, upgrade or a price. The free path is the CLI, the installer, `/j/`, `/device` and the OS dialogs. Workspace copy says "a Lightbulb workspace" (a trial counts), never "paid".
- **Logging.** Log ids, sizes, states and timings only. Never log a body, key, proof, ticket, webhook URL, bearer key or stream byte.
- **Public repo.** Plan C writes nothing into `lightbulb-connect` except the three issuer PUBLIC keys (owner, O3).
- **Unsigned engine releases (owner).** `install.sh` checks the sha256 it pins. The desktop build signs the bundled copy as a nested binary with the app's Developer ID.
- **Deploy model.**
  - Dev deploys on merge. Prod deploys on `gh workflow run deploy.yml --ref main`.
  - A change under `apps/infra/lib/` runs `cd apps/infra && npx cdk synth` and commits the regenerated `deploy.yml` in the same PR.
  - A Hermes change needs a pin-flip PR, then `scripts/roll_hermes_runtimes.py` and `scripts/reapply_hermes_skills_dirs.py`.
  - The desktop app ships on a `desktop-build.yml` dispatch.
  - Mobile OTA ships on merge, so the Phase 2 PR keeps its mobile commits behind `EXPO_PUBLIC_NETWORK_ENABLED`. Task 22 flips it after the prod dispatch.
- Ponytail at every step. Run `/simplify` after every task, before review.

## Review Focus

1. **A send or a decision for someone it is not.** Friday's requester comes from the ledger. A phone Allow needs the signed-in user to own the link that received the hold. The bridge never signs `cloud_of`. Tests: Task 14 `requester_comes_from_the_ledger_not_the_payload`; Task 11 `decision_route_refuses_another_persons_hold` and `bridge_refuses_cloud_of_scope`.
2. **A stranger waking a cloud agent, or a forged internal call.** A sender to `…/friday` who is not its person is refused. Nothing reaches `/internal/bridge/*` from the public edge. Tests: Task 8 `stranger_to_friday_is_refused_and_wakes_nothing`; Task 6 `internal_bridge_paths_refuse_the_public_listener`.
3. **Person power laundered through the app.** Three things must change nothing: a webview call without a sheet press, a stale notification click, and an agent calling `lightbulb share`. Tests: Task 15c `every_person_only_command_needs_a_sheet_press`; Task 15b `stale_click_answers_nothing`; Task 15c `mod_socket_share_from_an_agent_shell_is_refused`.
4. **Two bridges with one key.** A rolling deploy or a stray task must never run two live instances. Tests: Task 12 `bridge_service_stops_before_it_starts`; Task 5 `second_bridge_waits_without_the_lease`.
5. **A reply reaching the wrong place.** Two Friday messages answered in turn each reach their own run. A late reply lands in its trigger's room thread. An unknown thread never lands in another person's mailbox or DM. Tests: Task 8 `two_threads_route_to_their_own_runs`, `late_reply_posts_to_the_trigger_thread`, `unknown_thread_goes_to_the_persons_dm_only`.

## Owner items still open

- **O3 (Tasks 3, 4).** Generate the issuer keypairs `p1`, `p2` and `dev`, and sign `relays.json` with the owner's minisign key, through the owner's 600 file. The public halves go to the engine repo's `ISSUER_KEYS`.
- **O4 (Tasks 1, 12).** Make the engine repo public, or store a read token as `NETWORK_REPO_TOKEN` in the monorepo's Actions secrets.

---

## Phase 1 — additive, behind `NETWORK_ENABLED=false`

### Task 1: Pin the engine

**Files:**
- Create `network-version.json`, `scripts/check_network_pin.py` and `scripts/tests/test_check_network_pin.py`.
- Modify `apps/desktop/src-tauri/scripts/vendor-sidecars.sh`: fetch the pinned `lightbulb-connect-aarch64-apple-darwin`, check its sha256, and place it at `src-tauri/bin/lightbulb-connect`.
- Modify `apps/desktop/src-tauri/tauri.conf.json`: add `bin/lightbulb-connect` to `resources`.
- Modify `.github/workflows/desktop-build.yml`: codesign the resource as nested code.
- Modify `.github/workflows/frontend-ci.yml` and `desktop-core.yml`: run the pin check.

**Interfaces:**
- `network-version.json` = `{"tag":"v<x.y.z>","macos_arm64_sha256":"<hex>","protocol":<int>,"min_protocol":<int>,"notes":"Pin of TheLightbulbCompany/lightbulb-connect. Bump tag and sha together; network/install.sh, the desktop bundle, apps/bridge/Cargo.toml and the app's protocol range read it."}`.
- `check_network_pin.py` exits 1 unless the pin agrees with `apps/frontend/public/network/install.sh` (`VERSION`, `SHA256`) and with `apps/bridge/Cargo.toml` (`tag`, once that file exists).

- [ ] **Step 1: Failing tests** — `test_pin_and_installer_must_agree`, `test_bridge_manifest_tag_must_match`, `test_missing_bridge_manifest_is_skipped`.
- [ ] **Step 2:** `uv run pytest scripts/tests/test_check_network_pin.py` — FAIL. **Step 3:** Implement.
- [ ] **Step 4:** PASS. `bash apps/desktop/src-tauri/scripts/vendor-sidecars.sh aarch64-apple-darwin` places the binary and refuses a wrong sha.
- [ ] **Step 5:** Commit `chore(network): pin the engine by release tag`.

### Task 2: Installer, `/j/`, `/device`, issuer rewrites (dark)

**Files:**
- Create `apps/frontend/public/network/install.sh` (the new installer; `/install` keeps pointing at today's script until Task 22) and `apps/frontend/scripts/network-install.test.sh`.
- Create `apps/frontend/src/app/j/layout.tsx`, a bare root with no `PostHogProvider`, no `VercelAnalytics` and no Clerk.
- Create `apps/frontend/src/app/j/page.tsx`, `apps/frontend/src/app/j/__tests__/page.test.tsx` and `apps/frontend/src/__tests__/networkRoutes.test.ts`.
- Modify `apps/frontend/next.config.ts`.
- Modify `apps/frontend/src/middleware.ts`: `/j/`, `/device` and `/network/*` answer 404 while `NEXT_PUBLIC_NETWORK_ENABLED` is off; `/j/` bypasses Clerk.

**Interfaces:**
- `install.sh` carries `VERSION=<tag>` and `SHA256=<hex>`, and works in this order:
  1. Download `https://github.com/TheLightbulbCompany/lightbulb-connect/releases/download/$VERSION/lightbulb-connect-aarch64-apple-darwin` with `curl`, which adds no quarantine flag.
  2. Check `shasum -a 256`.
  3. Install to `~/.lightbulb/connect/bin/lightbulb-connect` (0755) and link `~/.local/bin/lightbulb`.
  4. Run `lightbulb-connect setup`, then `lightbulb-connect daemon --install`.

  Flags are `--no-join` and `--join '<link>'`. It never uses sudo, and a re-run upgrades. Texts follow onboarding A2 and its "Failure modes", except that `Verifying the signature...` reads `Verifying the download...` until releases are signed.
- `/j/` is static. It reads `location.hash` only to echo the full link into the two commands. Copy per onboarding A5a/A5b. The test renders the ROUTE with its layout, and asserts no fetch and no `<script>` from posthog, vercel or clerk.
- Rewrites (afterFiles; they work only when the flag is on):
  - `/network/device/:path*` and `/network/proof/:path*` → `https://api[-dev].thelightbulb.company/api/v1/network/…`.
  - `/network/relays.json(.minisig)` → `/network-relays/<env>/relays.json(.minisig)`.
- Redirect `/device` → `/auth/connect-machine?mode=network`. The person TYPES the code there; nothing in the query fills it in.

- [ ] **Step 1: Failing tests** — `j_route_loads_no_analytics_or_clerk`, `j_page_echoes_the_full_link_into_both_commands`, `network_routes_404_while_flag_off`, `device_redirect_carries_no_code`, `install_sh_refuses_a_wrong_sha_and_writes_nothing`, `install_sh_refuses_intel_with_the_exact_text`, `install_sh_output_has_no_plan_trial_workspace_or_price`.
- [ ] **Step 2:** `pnpm --filter @lightbulb/frontend test`; `sh apps/frontend/scripts/network-install.test.sh` — FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(network): engine installer, bare join page, device and issuer routes behind a flag`.

### Task 3: Issuer — device flow, proofs, refresh, audit

**Files:**
- Create `apps/backend/core/services/network_issuer.py`, `apps/backend/routers/network.py` and `apps/backend/core/repositories/network_audit_repo.py`.
- Create the tests `apps/backend/tests/unit/services/test_network_issuer.py` and `apps/backend/tests/unit/routers/test_network_device.py`.
- Modify `apps/backend/core/services/device_login.py`: rows with `purpose: "network"` carry `member_key` and `computer_name`.
- Modify `apps/backend/core/config.py`, and `apps/backend/main.py` (mount the router when `NETWORK_ENABLED`).
- Modify `apps/frontend/src/app/auth/connect-machine/page.tsx` for `mode=network`:
  - The person types the code into a box. The page then shows the computer name and the key short (the first 12 characters of `member_key`).
  - Copy: `Sign in to verify your name on the Lightbulb network. This does not create a workspace or start a plan.`
  - A workspace is optional.

**Interfaces:**
- `network_issuer.mint(sub: str, name: str, computer: str, kind: Literal["computer","bridge"], workspace: str | None, now: int) -> str`:
  - Returns a compact JWS, ≤ 4 KB, with header `{"alg":"EdDSA","kid":NETWORK_ISSUER_KID}` and payload `{iss, kid, iat, exp: iat+86400, sub, name, computer, kind, workspace?}`.
  - `computer` is the 52-char lowercase base32 member key. The hostname is display only.
  - `workspace` is the workspace's display name, ≤ 64 chars and sanitised. The engine renders it in the admission prompt.
  - Each mint writes one audit row `{kind: "proof", sub, computer, proof_kind, exp}`.
  - The key is read from `NETWORK_ISSUER_KEY_SECRET` and cached for 5 minutes.
- Routes under `/api/v1/network`:
  - `POST /device/start {member_key, computer}` (no auth) → `{device_code, user_code, verification_uri_complete: "<origin>/device", interval: 2}`. The URI carries no code.
  - `POST /device/describe {user_code}` (Clerk) → `{computer_name, key_short}`.
  - `POST /device/approve {user_code}` (Clerk; no org needed) → mints a `kind=computer` proof for the approver.
  - `POST /device/token {device_code}` (no auth) → `200 {"pending": true}`, then `200 {"proof"}` exactly once, or `410`.
  - `POST /proof/refresh {proof, at, sig}` (no auth) → `{proof}`. It refuses unless all hold: `sig` verifies against `proof.computer`; `|now - at| ≤ 300`; the old proof's `exp` is at most 7 days past (grace); the Clerk user still exists. It reads the workspace name again and drops it if the person has left.

- [ ] **Step 1: Failing tests** — `proof_verifies_with_the_public_half_and_lasts_24h`, `proof_names_the_key_not_the_hostname`, `start_uri_carries_no_code`, `approve_without_a_workspace_mints_a_proof`, `describe_shows_name_and_key_short`, `token_is_pending_then_proof_once_then_410`, `device_code_stored_only_as_sha256`, `refresh_requires_the_computer_signature`, `refresh_refuses_after_7_days_of_grace`, `refresh_drops_workspace_after_leaving`, `every_mint_writes_an_audit_row`, `routes_log_no_proof` (caplog has no `eyJ`), `network_routes_404_when_disabled`.
- [ ] **Step 2:** `cd apps/backend && uv run pytest tests/unit/services/test_network_issuer.py tests/unit/routers/test_network_device.py -v` — FAIL. **Step 3:** Implement (keys come from the owner, O3). **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(network): sign-in issuer — typed device code, proofs, refresh, audit`.

### Task 4: Relay

**Files:**
- Create `apps/infra/lib/stacks/relay-stack.ts`, `apps/infra/relay/Dockerfile`, `apps/infra/relay/config.toml` and `apps/infra/test/relay-stack.test.ts`.
- Create `apps/frontend/public/network-relays/prod/relays.json(.minisig)`, `apps/frontend/public/network-relays/dev/relays.json(.minisig)` and `docs/runbooks/network-relay.md`.
- Modify `apps/infra/lib/lightbulb-stage.ts`.
- Modify `apps/infra/lib/stacks/network-stack.ts`: expose the ALB's HTTPS listener for one host rule.
- Modify `apps/backend/core/services/company_identity.py`: add `relay` and `relay-dev` to `RESERVED_SLUGS`.
- Regenerate `.github/workflows/deploy.yml`.

**Interfaces:**
- Image: `cargo install iroh-relay --version =<iroh pin> --features server --locked`, then run `iroh-relay --config-path /etc/iroh-relay/config.toml`. linux/arm64.
- Config:
  - `enable_relay = true`, `http_bind_addr = "[::]:80"`, `enable_quic_addr_discovery = false`, `enable_metrics = true`.
  - No `[tls]` table: TLS ends at the ALB, and iroh-relay serves the relay over plain HTTP when `tls` is unset.
  - `access = "everyone"`. Our servers cannot know who the free members are, so there is no member allowlist. Abuse is bounded by `[limits] accept_conn_limit = 10.0`, `accept_conn_burst = 50` and `[limits.client.rx] bytes_per_second = 1048576`, `max_burst_bytes = 4194304`.
- Stack:
  - One Fargate service on the platform cluster: `desiredCount: 1`, `minHealthyPercent: 0`, `maxHealthyPercent: 100`.
  - Its target group sits on the EXISTING internal ALB's HTTPS listener, with a host-header rule `relay[-dev].thelightbulb.company` (ACM wildcard cert).
  - The ALB is internal, so the public door is a CloudFront distribution with a VPC origin to that ALB, forwarding WebSockets (the `bv-` pattern). Route53 gets an alias for the env's relay host.
  - Alarms: relay `BytesTransferred` on CloudFront, and 5xx.
- `relays.json`, one per env: `{"v":1,"relays":["https://relay[-dev].thelightbulb.company"],"expires_at":<now+90 d>}`. The owner signs it (O3); the runbook carries a calendar item to re-sign it every 90 days.

- [ ] **Step 1: Failing tests** — `relay_has_no_nlb_and_no_udp`, `relay_rule_is_host_header_on_the_existing_alb`, `relay_deploy_stops_before_it_starts`, `relay_metrics_are_not_routed`, `relay_host_is_relay_dev_in_dev`; backend `test_relay_slugs_are_reserved`. Before adding the slugs, check the company-slugs table for `relay` and `relay-dev`. If either is taken, stop and ask the owner.
- [ ] **Step 2:** `cd apps/infra && npx jest test/relay-stack.test.ts` — FAIL. **Step 3:** Implement; `npx cdk synth`; commit `deploy.yml`.
- [ ] **Step 4:** PASS. After the dev deploy, a debug engine with `relay = "https://relay-dev.thelightbulb.company"` must pass Plan B's `messages_flow_via_relay_when_direct_is_blocked` against it. A relay connection left idle for 15 minutes must still be up; this checks the CloudFront and ALB idle timeouts against iroh's keepalive.
- [ ] **Step 5:** Commit `feat(infra): iroh relay behind the existing ALB via a CloudFront VPC origin`.

### Task 5: Bridge skeleton — instances, keys, snapshots, lease

**Files:**
- Create in `apps/bridge/`: `Cargo.toml` (with `lightbulb-connect = { git = "https://github.com/TheLightbulbCompany/lightbulb-connect", tag = "<pin>" }`), `Cargo.lock`, `Dockerfile`, `src/main.rs`, `src/instances.rs`, `src/store.rs`, `src/lease.rs`, `src/http.rs` and `tests/instances.rs`.
- Create `.github/workflows/bridge.yml`: runs `cargo test` on PRs touching `apps/bridge/**`, on `vars.CI_RUNNER`.

**Interfaces:**
- `Link { owner: String, person: String }`.
- `Instances::boot(cfg) -> Result<Instances>`:
  1. `GET {BACKEND_INTERNAL_URL}/api/v1/internal/bridge/links` → `[Link]`.
  2. For each link: read the seed from Secrets Manager and write it as `identity.key` in a fresh temp home.
  3. Restore `state.db` from S3 when a snapshot exists.
  4. `embed::start(home, EmbedOpts { kind: Cloud, relay: NETWORK_RELAY_URL, issuer: NETWORK_ISSUER_URL })`.
- `Instances::get`, `::add`, `::remove`.
- `store::snapshot(link)` calls `snapshot {to: <tmp>}`, then `PutObject` to `bridge-state/<owner>/<person>/state.db`. It runs after every instance event that writes state, debounced to 1 s, and once more on SIGTERM.
- `lease.rs`:
  - Takes the lease with a conditional put on the containers-table row `__bridge__#lease` (`attribute_not_exists(owner_id) OR expires_at < :now`, `{holder: <task id>, expires_at: now+30}`).
  - Renews it every 10 s and deletes it on SIGTERM.
  - Without the lease, the task starts no instance and retries every 10 s. If a renewal fails, it stops all instances and goes back to waiting.
- `http::router()` serves `GET /health`. Every other route needs the `x-lightbulb-bridge-internal` header (constant-time compare). Tasks 7, 8, 11 and 18 add routes.
- Memory ceiling: in Step 4, measure one idle instance's RSS. Write `links per 1 GiB = floor(800 MiB / RSS)` in `apps/bridge/README.md`.

- [ ] **Step 1: Failing tests** — `boot_restores_each_link_from_its_snapshot`, `missing_snapshot_starts_an_empty_instance`, `snapshot_is_debounced_and_flushed_on_sigterm`, `second_bridge_waits_without_the_lease`, `lost_lease_stops_every_instance`, `internal_routes_refuse_without_the_token`, `bridge_logs_carry_no_bodies` (send a marker; no log line contains it).
- [ ] **Step 2:** `cd apps/bridge && cargo test` — FAIL. **Step 3:** Implement. **Step 4:** PASS; record the RSS.
- [ ] **Step 5:** Commit `feat(bridge): embedded engine instances with keys, snapshots and a lease`.

### Task 6: The VPC-only internal door

**Files:**
- Modify `apps/infra/lib/stacks/network-stack.ts`: add ALB listener 8082 (HTTP), forwarding to the backend target group, with SG ingress from the bridge SG only.
- Create `apps/backend/core/middleware/internal_bridge_guard.py`: `/api/v1/internal/bridge/*` needs `X-Forwarded-Port: 8082` AND the token. Mount it in `apps/backend/main.py`.
- Tests: `apps/backend/tests/unit/middleware/test_internal_bridge_guard.py` and `apps/infra/test/network-stack.test.ts`.

- [ ] **Step 1: Failing tests** — `internal_bridge_paths_refuse_the_public_listener` (port 443 with a valid token → 404), `internal_bridge_paths_need_the_token`, `listener_8082_admits_only_the_bridge_sg`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement; `npx cdk synth`; commit `deploy.yml`. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(infra): a VPC-only door for bridge-to-backend calls`.

### Task 7: Linking a workspace

**Files:**
- Create `apps/backend/core/services/network_links.py`, `apps/backend/tests/unit/services/test_network_links.py`, `apps/bridge/src/link.rs` and `apps/bridge/tests/link.rs`.
- Modify `apps/backend/routers/network.py`.
- Modify `apps/backend/routers/webhooks.py`: `organizationMembership.deleted` unlinks that person's link.
- Modify `apps/backend/core/services/workspace_wipe.py` and `account_wipe.py`.

**Interfaces:**
- `POST /api/v1/network/workspace {join_line, replace?: bool}` (Clerk; org required, else `workspace_required`):
  1. Refuse a line whose host is not `thelightbulb.company` (400 `bad_join_line`).
  2. If the person already has a link in another workspace and `replace` is not true, answer 409 `already_linked {workspace}`. The app then confirms `Your network is connected to <old>. Connect it to <new> instead?` and the old link is unlinked first.
  3. Create the key secret.
  4. Mint a `kind=bridge` proof: `sub` = the user, `name` = `Lightbulb (<Workspace>)`, `computer` = the bridge key, `workspace` = the workspace name.
  5. Call bridge `POST /bridge/link {owner, person, ticket, proof}`.
  6. Write `NETLINK#<person>` with `state: waiting` and answer `{state}`.

  Copy while it waits: `Waiting for your network's founder or an admin to allow "Lightbulb (<Workspace>)".`
- `DELETE /api/v1/network/workspace` (Clerk) and the membership-deleted webhook both unlink:
  1. They call bridge `POST /bridge/unlink {owner, person}`.
  2. The instance runs `team.unlink`, then `team.leave`, takes a final snapshot and shuts down.
  3. The backend deletes the secret, the snapshot, and that person's `NETLINK`, `NETMAIL`, `NETTHREAD` and `NETHOLD` rows.
- `GET /api/v1/network/bridge` (Clerk) → `{bridge_key, team_id, state}`.
- Internal routes (port 8082): `GET /internal/bridge/links`; `POST /internal/bridge/link-state {owner, person, state, team_id?}`; `POST /internal/bridge/proof {owner, person}` → `{proof}`.
- Bridge `link.rs`:
  - `link(owner, person, ticket, proof)` = `Instances::add`, then `team.join {ticket, name: "Lightbulb (<Workspace>)", kind: "cloud", proof}`.
  - It posts link-state with the outcome. On a `member_removed` that names its own key (the CLI `unlink`, or an admin), it posts `removed`.
  - It renews its proof daily, at 20 h, through `/internal/bridge/proof` and `identity.proof`. Roster growth is about 1.5 MB a year per bridge; compaction is Plan D.
- Wipes: the workspace wipe unlinks every link of the owner; the account wipe unlinks that person's link.

- [ ] **Step 1: Failing tests** — `connect_refuses_a_join_line_from_another_host`, `connect_mints_a_bridge_proof_with_the_workspace_name`, `second_workspace_needs_replace_and_unlinks_the_old`, `membership_deleted_unlinks_the_person`, `member_removed_marks_the_link_removed`, `unlink_deletes_secret_snapshot_and_rows`, `workspace_wipe_removes_every_network_row`; bridge `join_is_kind_cloud_with_the_proof`, `proof_is_renewed_daily`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(network): link one workspace per person through the bridge`.

### Task 8: Bridge send, inbound, routing, mailbox

**Files:**
- Create `apps/bridge/src/mail.rs`, `apps/bridge/tests/mail.rs`, `apps/backend/core/services/network_mail.py` and `apps/backend/tests/unit/services/test_network_mail.py`.
- Modify `apps/backend/routers/network.py`.
- Modify `apps/infra/lambda/messaging-projector/index.py`: skip `NET*` items before any processing. Test: `apps/infra/lambda/messaging-projector/test_index.py`.

**Interfaces:**
- Bridge `POST /bridge/send {owner, person, session_name, agent, to, text, request_id, reply_to?}` → `{request_id, state, thread, tier}`. A new name is declared first with `cloud.sessions.set`; the first principal to register a name keeps it, and a name owned by another principal answers 409 `name_taken`. Then it calls `send`.
- Bridge `GET /bridge/directory?owner&person` → `{sessions:[record]}`.
- Bridge `GET /bridge/receipt?owner&person&session_name&request_id` → the receipt. Receipt changes reach the backend on `receipt_changed`, through `POST /internal/bridge/receipt`.
- Bridge inbound, on each `message` event:
  1. Read `inbox {session}`.
  2. Frame each record with `lightbulb_connect::frame::framed` (still-needed item 3).
  3. `POST /internal/bridge/inbound {owner, person, session_name, from, label, request_id, thread, reply_to?, sent_at, text}`.
  4. Mark the record read only after a 2xx.

  A stranger's message never reaches this point, because the engine's cloud inbound policy refuses it.
- `network_mail.record_send(owner, thread, origin, ref, session_name)`.
- `network_mail.route_inbound(body)`:

  | Inbound | Goes to |
  |---|---|
  | known thread, `origin: run` | the run's room thread (the ledger's `triggerMessageId`), posted as the agent; it wakes a turn only if the room's listen mode would. If the run has ended, the same rule applies; there is no special path. |
  | known thread, `origin: mailbox` | that session's mailbox |
  | known thread, `origin: webhook` | Task 10 |
  | unknown thread, to an agent session | that agent's DM with `person` |
  | unknown thread, to a hosted-caller session | that session's mailbox |

  An inbound message never goes to anyone else's mailbox or DM.
- `network_mail.check_inbox(owner, session_name, wait_secs ≤ 25)` returns on the first unread message or at the deadline. `unread_count(owner, session_name)`.

- [ ] **Step 1: Failing tests** — bridge `send_declares_then_sends`, `name_of_another_principal_is_taken`, `inbound_is_marked_read_only_after_2xx`, `stranger_to_friday_is_refused_and_wakes_nothing` (engine fixture: a computer that is not the person's sends → `refused policy`, and there is no inbound post); backend `two_threads_route_to_their_own_runs`, `late_reply_posts_to_the_trigger_thread`, `unknown_thread_goes_to_the_persons_dm_only`, `mailbox_expires_seven_days_or_a_day_after_read`, `check_inbox_caps_wait_at_25`; projector `net_rows_are_skipped`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(bridge): cloud-session sends, inbound routing by thread, hosted mailbox`.

### Task 9: Hosted MCP network tools (module, not yet wired)

**Files:** Create `apps/backend/core/services/hosted_network.py` and `apps/backend/tests/unit/services/test_hosted_network.py`.

**Interfaces** — the network subset, identical to the engine's:
- `find_sessions(ctx, query="", limit=20)` returns the caller person's link directory. With no joined link it answers `Connect your Mac first: Lightbulb app, Settings, Network, Connect my workspace.`
- `send_network(ctx, request_id, text, session_id, reply_to=None, files=None, notify=None) -> {request_id, state, tier, note}`:
  - `request_id` must match `[A-Za-z0-9._-]{1,128}`.
  - `files` and `notify` → `unsupported`.
  - `*`, `person/*` and `repo:` → `group_send_refused`.
  - The session name comes from the verified OAuth client or key (see Global Constraints).
  - It records the thread with `origin: mailbox`.
- `message_receipt(ctx, request_id)` is keyed by (the calling principal's session, request_id). Another principal's id → not found.
- `check_inbox(ctx, wait_secs=0)`: the wait is capped at 25 s. Records are `{id, request_id, thread, from, label, text, sent_at}`.

- [ ] **Step 1: Failing tests** — `network_subset_matches_the_engine` (signatures against a fixture of Plan B Task 10's MCP schema), `body_claiming_a_session_is_sent_from_the_callers_own_session`, `group_targets_are_refused`, `files_and_notify_are_unsupported`, `request_id_accepts_non_uuid`, `receipt_of_another_principal_is_not_found`, `no_link_says_connect_your_mac`, `wait_over_25_is_capped`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(network): hosted MCP network tools over the bridge`.

### Task 10: Webhook agents through the bridge

**Files:** Modify `apps/backend/core/services/webhook_agents.py` and `apps/bridge/src/mail.rs`. Test: `apps/backend/tests/unit/services/test_webhook_agents.py`.

**Interfaces:**
- Webhook agents are per workspace. Each one is declared on the link of the member who registered it (the `user_id` of `webhook_agents.register`), as `<webhook-slug>`, with `agent` set to its name. If that member has no link, Settings shows `Not reachable from the network until you connect your Mac.`
- `webhook_agents.deliver_from_network(owner: str, agent_id: str, inbound: dict) -> tuple[bool, str]` makes ONE POST per admitted message, with today's retries. The `text` arrives already framed by the bridge, so Python does not defang it.
- Replies come through hosted `send_network`, with the agent's `lbk_` key, from the same link.
- Task 21b deletes `webhook_agents.defang`.

- [ ] **Step 1: Failing tests** — `one_post_per_admitted_message`, `text_arrives_framed_by_the_bridge`, `url_and_key_never_in_logs_or_responses`, `reply_uses_the_registering_members_link`, `unlinked_registrant_reads_not_reachable`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(network): webhook agents as sessions of the bridge`.

### Task 11: Holds — notices, Needs-you, decisions, permission prompts

**Files:**
- Create `apps/backend/core/services/network_holds.py`, `apps/backend/tests/unit/services/test_network_holds.py`, `apps/bridge/src/holds.rs` and `apps/bridge/tests/holds.rs`.
- Modify `apps/backend/routers/network.py`.
- Modify `apps/backend/core/services/activity.py`: add the `network_hold` item kind.

**Interfaces:**
- Bridge:
  - On `hold_notice` → `POST /internal/bridge/hold {owner, person, hold_id, holding_member, holding_computer, target_session, from, label, kind, created_at}`.
  - `POST /bridge/decide {owner, person, hold_id, holding_member, verdict, scope}` → `decision.send`. Any scope other than `once|person|own_cloud` → 400 `bad_scope`.
  - On `decision_result` → `POST /internal/bridge/hold-result {hold_id, result}`.
  - There is no resend on boot: the engine's outbox and the snapshot carry decisions.
- `network_holds.on_notice(body)`:
  - Writes the `NETHOLD` row, dropping any `preview`.
  - Writes one `activity.py` item.
  - Sends one push: `send_push_to_user(person, title=<copy>, body="Open Lightbulb to answer.", data={kind:"network_hold", holdId})`.
  - Copy for `kind: message`: `<label> wants to message your <Agent> session on <computer>.` Copy for `kind: permission`: `<Agent> on <computer> asks permission: <title>.` The title is the prompt's tool name, at most 60 chars, never the command text.
- `GET /api/v1/network/holds` (Clerk) → the person's open holds.
- `POST /api/v1/network/holds/{hold_id}/decision {holding_member, verdict, scope}` (Clerk):
  - 404 unless the row is under `NETHOLD#<owner>#<ctx.user_id>`.
  - `own_cloud` is accepted only when `from` is a session of this link's bridge.
  - A permission hold takes `once` only.
  - Writes an audit row, then calls `/bridge/decide`.
- Closing items: result `not_open` → `Answered on your Mac.` A message hold also closes when `receipt_changed` for that request leaves `held`.
- Known limit: when a third party's hold is answered on the Mac, it stays open on the phone until it is tapped or until its 7-day expiry.

- [ ] **Step 1: Failing tests** — `decision_route_refuses_another_persons_hold`, `bridge_refuses_cloud_of_scope`, `own_cloud_only_for_this_bridges_session`, `permission_hold_takes_once_only`, `hold_notice_preview_is_dropped`, `push_names_sender_agent_computer_and_no_body`, `not_open_reads_answered_on_your_mac`, `receipt_leaving_held_closes_the_item`, `every_decision_writes_an_audit_row`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(network): hold notices to Needs-you, phone/web decisions, permission prompts`.

### Task 12: Bridge, audit table and flags in CDK

**Files:**
- Create `apps/infra/lib/stacks/bridge-stack.ts` and `apps/infra/test/bridge-stack.test.ts`.
- Modify `apps/infra/lib/lightbulb-stage.ts`.
- Modify `apps/infra/lib/stacks/database-stack.ts`: add `lightbulb-{env}-network-audit`, with TTL attribute `expiresAt`.
- Modify `apps/infra/lib/stacks/service-stack.ts`:
  - Backend env, with `NETWORK_ENABLED=false`.
  - IAM: create, read and delete `lightbulb/{env}/network/bridge/*`; read `lightbulb/{env}/network/issuer_key_*`; delete in S3 on `bridge-state/*`; write to the audit table.
- Modify `apps/infra/lib/stacks/auth-stack.ts`: add `bridge_internal_token`.
- Regenerate `.github/workflows/deploy.yml`.

**Interfaces:**
- One Fargate service: `desiredCount: 1`, `minHealthyPercent: 0`, `maxHealthyPercent: 100`; linux/arm64, 0.5 vCPU / 1 GiB, private subnets.
- Cloud Map name `bridge` in `lightbulb-{env}.local`.
- Image: a `DockerImageAsset` of `apps/bridge/Dockerfile`, with build arg `NETWORK_REPO_TOKEN` while O4 is open.
- The task role can do these and nothing else: `GetSecretValue` on `lightbulb/{env}/network/bridge/*`; S3 get and put on `bridge-state/*`; a conditional put and delete in the containers table on `__bridge__#lease`.
- SG: ingress 8080 from the backend SG. Egress 443 and 8082 to the ALB; 443 and UDP to the internet (relays, peers).
- A deploy is a short outage (stop before start; outboxes retry). The runbook says so.

- [ ] **Step 1: Failing tests** — `bridge_service_stops_before_it_starts`, `bridge_has_no_public_listener`, `bridge_role_reads_only_bridge_secrets`, `issuer_keys_readable_only_by_the_backend_role`, `network_enabled_is_false_in_both_envs`, `audit_table_has_ttl`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement; `npx cdk synth`; commit `deploy.yml`. **Step 4:** PASS. After the dev deploy, the bridge's `GET /health` answers the backend task.
- [ ] **Step 5:** Commit `feat(infra): bridge service, network audit table, network flags`.

---

## Phase 2 — the switch (ONE PR, branch `feat/network-cutover` off `origin/main`, one commit per task)

### Task 13: The hosted MCP gains the network tools

**Files:**
- Modify `apps/backend/core/services/hosted_mcp.py`:
  - `send_message` with `session_id` → `hosted_network.send_network`. With `room_id`, `person_id` or `agent_id` it stays in chat as today, and that path keeps the UUID `request_id`.
  - Register `check_inbox`.
  - `find_sessions`, and `message_receipt` for session requests, go to `hosted_network`.
  - `_person_room` uses `messaging.person_dm` directly, not `native_session_tools`.
- Tests: `test_hosted_mcp_session_caller.py`, `test_hosted_mcp_unread.py`, `test_hosted_mcp_auth.py`.

- [ ] **Step 1: Failing tests** — `session_send_goes_to_the_bridge`, `room_send_stays_in_chat`, `check_inbox_is_listed`, `unread_note_counts_mailbox_items`, `person_room_needs_no_native_session_tools`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat(mcp): hosted session tools ride the bridge`.

### Task 14: Friday's tool on the network

**Files:**
- Modify `apps/infra/hermes/plugins/lightbulb/__init__.py` and `plugin.yaml`: rename `lightbulb_local_sessions` to `lightbulb_sessions`. The actions (`resources`, `read`, `send`, `receipt`) and the ask-once rules stay. It authenticates with the hook token alone.
- Modify `apps/infra/hermes/bridge/server.py` and `turn.py`: drop `X-Lightbulb-Local-Session-Token` and `localSessionToken`; pass `runId` only.
- Modify `apps/infra/hermes/skills-src/lightbulb/references/` and `MAINTENANCE.md`.
- Modify `apps/backend/routers/network.py`: add `/api/v1/internal/network/{owner}/sessions|send|receipt` with hook-token auth. The requester comes from the run ledger (`OWNER#<owner>/RUN#<runId>` → `triggerMessageId` → that message's author). A run with no human trigger follows the rule in Global Constraints.
- Modify `apps/backend/core/services/openclaw_work.py`: stop minting grants.
- Tests: `apps/infra/hermes/tests/test_plugin_sessions.py`, `apps/backend/tests/unit/services/test_hermes_skill_tool_names.py`, `apps/backend/tests/unit/routers/test_internal_network.py`.

- [ ] **Step 1: Failing tests** — `requester_comes_from_the_ledger_not_the_payload` (a body that names another member is ignored), `no_human_run_sends_through_the_targets_link`, `requester_without_a_link_gets_connect_your_mac`, `friday_session_is_the_agent_slug`, `refused_is_final_for_the_run`, `tool_names_match_the_skill`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat(hermes): Friday reaches sessions through the bridge`.

### Task 15a: App — engine client, install, attach

**Files:** Create `apps/desktop/src-tauri/src/network/mod.rs`, `client.rs`, `install.rs` and `apps/desktop/src-tauri/tests/network_client.rs`. Modify `apps/desktop/src-tauri/src/lib.rs`.

**Interfaces:**
- `client::Engine::connect(sock) -> Result<Engine>`, `Engine::call(op) -> Result<Value>`, `Engine::subscribe(topics) -> Receiver<Value>`.
  - It reconnects every 2 s, and marks the engine `down` after 3 s of silence.
  - It checks the `ping` protocol against the `network-version.json` range `[min_protocol, protocol]`: `ok`, `engine_too_old`, `engine_too_new` or `down`.
  - While `ok`, it sends `ui.claim {}` every 7 s; a second app instance renews the same lease. It reads `status.claim_live` (still-needed item 5).
- `install::ensure()`:
  - When the bundled engine's version is ≥ the installed one, the bundled engine wins. The app then copies `Contents/Resources/bin/lightbulb-connect` to the canonical path, writes `app-install.json {path, version, installed_at}`, and runs `lightbulb-connect setup` and `daemon --install`.
  - Otherwise it leaves the curl-installed binary in place, and Settings shows `A newer Lightbulb engine (<v>) is installed; this app was built for <v>.`
  - When the engine is down: `launchctl kickstart gui/<uid>/<engine's launchd label>`, with the label read from `lightbulb-connect daemon --status`. When no launchd job exists: `daemon --install`.

- [ ] **Step 1: Failing tests** — `attach_to_a_running_daemon_starts_no_second`, `bundled_wins_only_when_not_older`, `newer_protocol_takes_no_claim`, `claim_heartbeats_every_7s`, `install_writes_app_install_json_and_runs_engine_setup`.
- [ ] **Step 2:** `cd apps/desktop && cargo test -p lightbulb-desktop network::` — FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(desktop): attach to the engine`.

### Task 15b: App — holds, notifications, permission prompts

**Files:**
- Create `apps/desktop/src-tauri/src/network/holds.rs` and `apps/desktop/src-tauri/src/network/permission.rs`.
- Modify `apps/desktop/src-tauri/src/notify.rs`: a hold kind, with `userInfo {hold_id, session_id, birth_micros}`.
- Modify `apps/desktop/src-tauri/src/holder/`: a Claude permission prompt in a held session files `hold.file {kind:"permission", …}`, and `hold_closed.verdict` is applied to Claude through the holder's existing input path.
- Test: `apps/desktop/src-tauri/tests/network_holds.rs`.

**Interfaces:**
- On `held`, the app posts a local notification: `<label> wants to message your <Agent>…` plus the first 140 chars.
  - Actions: **Allow once** (`once`), **Allow <name> always** (`person`), **Decline**. A cloud sender also gets **Always allow** (`own_cloud` or `cloud_of`).
  - The Dock badge counts open holds. The ids of the last 200 notified holds are kept in `notified.json`.
  - On `hold_closed`, the notification is withdrawn.
- An action is answered in Rust. The app first calls `hold.get`, which must show the hold still open, with the same `session_id` and `target_birth_micros`; only then does it call `hold.answer {id, verdict, scope}`. Otherwise it shows `That request is no longer open.`
- `permission::file(session, title, text) -> id`; `permission::apply(id, verdict)`.

- [ ] **Step 1: Failing tests** — `stale_click_answers_nothing`, `click_checks_birth_micros`, `notification_offers_once_person_decline`, `permission_prompt_is_filed_and_answer_reaches_claude`, `withdrawn_on_hold_closed`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(desktop): holds and permission prompts answered in Rust`.

### Task 15c: App — commands, capabilities, sheets, mod socket guard

**Files:**
- Create `apps/desktop/src-tauri/src/network/commands.rs` and `apps/desktop/src-tauri/src/network/sheet.rs`.
- Modify `apps/desktop/src-tauri/capabilities/`: bind the network commands to the main window and the app's own origins.
- Modify `apps/desktop/src-tauri/src/local_sessions/mod_socket.rs`: `/v1/share` refuses a caller with an agent ancestor. It uses the same `caller` classification as the engine, either through `lightbulb-connect caller --pid <pid>` or through a shared copy of the rule.
- Test: `apps/desktop/src-tauri/tests/network_commands.rs`.

**Interfaces:**
- Read commands, which need no sheet: `network_status`, `network_sessions`, `network_receipt`, `network_holds`, `network_policy_get`, `network_team_members`, `network_join_requests`.
- Person-only commands. Each runs only after `sheet::confirm(title, text) -> bool`, an AppKit sheet whose text comes from the engine or from a fixed string, never from the page:
  - `network_send`, `network_hold_request`, `network_policy_set`, `network_team_invite`, `network_team_remove`, `network_join_answer`.
  - `network_connect_agents`, `network_install_engine`, `network_replace_engine`, `network_signin`, `share_set`.
  - `network_connect_workspace` (mints `team.invite {mode:"once"}` and posts its line to `/api/v1/network/workspace`) and `network_disconnect_workspace`.

- [ ] **Step 1: Failing tests** — `every_person_only_command_needs_a_sheet_press` (with the sheet mocked to cancel, no command makes an engine write), `sheet_text_is_not_page_supplied`, `mod_socket_share_from_an_agent_shell_is_refused`, `commands_unavailable_off_origin`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(desktop): network commands behind native sheets`.

### Task 16: `NetworkSessions`, settings, Your computers, tray

**Files:**
- Create `apps/frontend/src/components/network/NetworkSessions.tsx` (no `next/*` imports), `NetworkSettings.tsx`, `adapter.ts` and `__tests__/NetworkSessions.test.tsx`.
- Create `apps/desktop/network-ui/index.html`, `main.tsx` and `build.mjs` (esbuild into `apps/desktop/src-tauri/network-dist/`).
- Modify `apps/frontend/src/components/workspace/WorkspaceSidebar.tsx`.
- Modify `apps/desktop/src-tauri/tauri.conf.json`: the bundled page for the free path.
- Modify `apps/desktop/src-tauri/src/tray.rs`: a menu-bar online view, showing `<n> online` and one line per online person with their computers, from `team.get` `online`.
- Modify `apps/frontend/src/components/desktop/DesktopSettings.tsx`.

**Interfaces:**
- `NetworkSessions({adapter})`:
  - Groups sessions by person, then by computer.
  - Each row shows the agent icon, the folder, busy or idle, `doing`, and a tier tag. Offline rows are dimmed and show `last seen`.
  - While the engine is down it shows `The engine is not running. Start it`, never an empty list.
  - The banner (focused window only) answers through `network_hold_request`.
  - The message box sends through `network_send`, and the receipt line walks the seven states.
- `NetworkSettings`:
  - The "agents send without asking" switch. Without a link it is disabled and reads `Applies to agents from your Lightbulb workspace`.
  - The `people` and `cloud_of` entries, each removable. Entries set by the bridge are marked `from your phone`.
  - `folders_off` and `share_paths`.
  - **Your computers**: the reusable join line (`team.invite {mode:"reusable"}`) with a Copy button, and the text `On your other Mac, sign in, then paste this into a terminal or an agent.`
  - Join requests, with Allow and Decline.
  - "Sign in to verify your name".
  - For members: Connect workspace and Disconnect workspace.
  - For free people: `Add a Lightbulb workspace to bring Friday and cloud agents in`, with no button that starts checkout.

- [ ] **Step 1: Failing tests** — `free_surface_has_no_friday_rooms_or_checkout`, `down_shows_start_not_an_empty_list`, `your_computers_shows_the_reusable_join_line`, `switch_disabled_without_a_link`, `receipt_line_renders_all_seven_states`; tray `tray_lists_online_people`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; `node apps/desktop/network-ui/build.mjs` writes `network-dist/index.html`.
- [ ] **Step 5:** Commit `feat(desktop): network sessions, settings, your computers, tray online view`.

### Task 17: Remove the hub's leftovers on the Mac (first run)

**Files:** Create `apps/desktop/src-tauri/src/network/cleanup.rs` and `apps/desktop/src-tauri/tests/network_cleanup.rs`. Modify `lib.rs`: run the cleanup once, before `install::ensure`, and record it in `app-install.json.cleaned_at`.

**Interfaces:** `cleanup::run(home) -> Report`.
- It removes:
  - Hook entries whose command contains `local-session-hook`: in `~/.claude/settings.json`, in `~/.codex/hooks.json` (with their `~/.codex/config.toml` trust keys), and in the Cursor and Grok hook files.
  - MCP entries whose command is the app binary with `session-mcp`.
  - The hub's `lightbulb-session-sharing` skill folders in `~/.claude/skills`, `~/.codex/skills` and `~/.cursor/skills`. Engine `setup` rewrites its own copies afterwards.
  - `~/Library/LaunchAgents/company.thelightbulb.serve.plist`, after `launchctl bootout`.
- It keeps the `lightbulb@lightbulb` Claude plugin: its mod and `/lightbulb:share` talk to the app's mod socket, which stays.
- It backs up each file it edits as `<file>.lightbulb-bak`, and never follows a leaf symlink.

- [ ] **Step 1: Failing tests** — `removes_only_local_session_hook_entries`, `removes_session_mcp_entry`, `removes_serve_launch_agent`, `keeps_the_lightbulb_plugin`, `runs_once`, `backs_up_edited_files`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(desktop): remove the hub's hooks, MCP entry and service on first run`.

### Task 18: The Mac control channel — shared sessions over one stream (O1)

**Files:**
- Create `apps/desktop/src-tauri/src/network/channel.rs`, `apps/bridge/src/channel.rs` and `apps/bridge/tests/channel.rs`.
- Modify `apps/desktop/src-tauri/src/local_sessions/screen.rs`: send frames to the channel.
- Modify `resume.rs`: run a `resume_want` that arrives on the channel; the guard asks the daemon's `sessions`.
- Modify `mod_socket.rs` (the sharing flags) and the holder's input path (typing that arrives on the channel).

**Interfaces:**
- The grant: the app sends its own bridge `share.grant {session: "<own short>/app", viewer: <bridge key>, input: true, expires_at: now+7 d}`. It re-mints the grant every 6 days while any session is shared (still-needed item 1). The bridge opens `stream.open {session: "<short>/app"}` after the grant.
- Frames are JSON lines inside `data_b64`:

  | Direction | Frames |
  |---|---|
  | app → bridge | `{k:"sessions", sessions:[…shared only…]}`, `{k:"screen", session, …}`, `{k:"events", session, events:[…]}`, `{k:"ended", session, at}`, `{k:"pickup_part", …}` (presigned-PUT results) |
  | bridge → app | `{k:"watch", session, on}`, `{k:"input", session, bytes_b64, by}`, `{k:"band", session, viewers, typing, requests}`, `{k:"pickup_want", session}`, `{k:"resume_want", conversation}` |

  Only sessions the person shares are sent, through "Share every new session" or the per-session Share.
- Bridge routes: `POST /bridge/channel {owner, person, frame}` (backend → Mac) and `POST /internal/bridge/channel {owner, person, frame}` (Mac → backend).

- [ ] **Step 1: Failing tests** — `only_shared_sessions_cross`, `screen_stops_within_1s_of_share_off`, `watch_toggles_screen_frames`, `input_reaches_the_holder_only_for_a_controllable_session`, `resume_want_runs_only_for_an_ended_session`, `grant_is_reminted_before_7_days`, `bridge_without_grant_gets_no_grant`, `codex_pane_shows_a_notice_not_blank`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(sessions): shared sessions ride one engine stream per Mac`.

### Task 19: The workspace session model keeps working, fed by the bridge (O1)

**Files:**
- Rename `apps/backend/core/services/local_sessions.py` to `workspace_sessions.py`, and `core/repositories/local_sessions_repo.py` to `workspace_sessions_repo.py`. The stored row prefixes `LOCALSESSION#` and `LOCALUSER#` stay, because they are stored data.
- In the renamed modules, remove the transport and policy half:
  - Enrollment and registration: `set_enrollment`, `_enrolled`, `register_session`.
  - Command admission: `submit_command` and its helpers.
  - Agent asks: `_file_ask`, `decide_agent_ask` and the rest.
  - Held sends: `announce_held_send`, `release_held`.
  - `allowed_senders`.

  The engine's holds and policy own delivery now.
- Create `apps/backend/core/services/network_channel.py`:
  - `on_frame(owner, person, frame)`: `sessions` → upsert or end rows; `events` → the `EVENT#` mirror; `screen` → `session_screen.forward` on `/ws/<workspaceId>/frames/<roomId>/<deliveryKey>`; `pickup_part` → `session_pickup`.
  - `send_to_mac(owner, person, frame)` → `/bridge/channel`.
- Tests: `apps/backend/tests/unit/services/test_network_channel.py`, plus the renamed suites.

**Importers and the decision for each.** Each keeps working: per O1 the data source changes, the behaviour does not.

| Importer | Calls today | After |
|---|---|---|
| `session_screen.py` | `local_session_relay.publish_resources_changed` | viewer leases send `{k:"watch"}` through `send_to_mac`; only workspace members may call `/bridge/watch` |
| `session_links.py` | `put_link`, `request_join`, `decide_join` | unchanged, over `workspace_sessions` |
| `session_chat.py` | `authorize_session` | unchanged, over `workspace_sessions` |
| `session_presence.py` | presence push | `{k:"band"}` through `send_to_mac` |
| `session_pickup.py`, `routers/desktop_session_pickup.py` | pick-up wants | `{k:"pickup_want"}`; the parts arrive as `pickup_part` |
| `session_resume.py`, `routers/desktop_session_resume.py` | resume wants | `{k:"resume_want"}` |
| `session_browser.py` | `browser_open`, `may_control`, `admit_command`, the relay | the session identity comes from `workspace_sessions`; the CDP path stays on the node relay |
| `activity.py` | `decide_join`, `decide_pickup_request`, `decide_agent_ask`, `get_command` | joins and pick-ups unchanged; agent asks become `network_hold` items (Task 11) |
| `voice_calls.py` | `local_session_grants.mint`, `live_sessions_for_room` | the grant mint goes (the call's agent sends through Task 14's route); `live_sessions_for_room` comes from `workspace_sessions` |
| `desktop_execution.py` | `local_sessions_repo.get_session` | `workspace_sessions_repo.get_session` |
| `desktop_session_start.py`, `routers/desktop_sessions.py` | remote start on the node relay | unchanged (this is not the hub) |
| `routers/teams/session_work.py`, `LocalSessionWork.tsx`, `task_board.py` | session rows | `workspace_sessions` |
| `messaging.py`, `openclaw_work.py` | `PROVIDER_NAMES`, `referenced_sessions`, `live_sessions_for_room`, `get_session` | `workspace_sessions` |
| `main.py` | router mounts | `/local-sessions/device/*` removed (Task 21b) |

- [ ] **Step 1: Failing tests** — `sessions_frame_upserts_and_ends_rows`, `events_frame_extends_the_mirror`, `screen_frame_reaches_the_frames_channel_and_is_not_stored`, `typing_reaches_the_mac_as_input`, `pickup_and_resume_wants_cross_the_channel`, `agent_ask_is_a_network_hold`, `workspace_sees_members_shared_sessions_only`. Every existing session-product suite must also pass with unchanged behaviour.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `refactor(sessions): the workspace session model is fed by the bridge`.

### Task 20: Phone and web Allow UI

**Files:**
- Create `apps/mobile/src/components/network/HoldCard.tsx` and `apps/frontend/src/components/network/HoldItem.tsx`, each with a test.
- Modify the Needs-you renderers on web and mobile. A tap on the mobile push opens the card.
- The mobile surfaces sit behind `EXPO_PUBLIC_NETWORK_ENABLED`; Task 22 flips it.

**Interfaces:**
- Copy as in Task 11.
- A message hold offers **Allow once** (`once`), **Allow <name> always** (`person`) and **Decline**. **Always allow** (`own_cloud`) appears only for this person's own cloud session.
- A permission hold offers Allow once and Decline.
- Results read `Allowed.`, `Declined.` or `Answered on your Mac.` No message text appears anywhere.

- [ ] **Step 1: Failing tests** — `card_shows_no_message_text`, `three_answers_for_a_message_hold`, `always_allow_only_for_own_cloud`, `permission_hold_offers_once_and_decline`, `hidden_while_flag_off`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(network): answer holds from the phone and the web`.

### Task 21: Delete the hub, one commit per area

The audit table goes in the PR description, with path, keep/delete and reason for each file. The test `apps/backend/tests/unit/test_no_hub_left.py` grows with each commit. It runs `rg` over `apps packages`, excluding the WebSocket-door files that Task 23 removes (`apps/infra/lambda/ws-*`, `websocket-authorizer`, `api-stack.ts`).

- [ ] **21a — desktop.**
  - Delete in `local_sessions/`: `transport.rs`, `claude.rs`, `codex.rs`, `hooked.rs`, `enrollment.rs`, `lifecycle.rs`, `native_mcp.rs`, `native_tools.rs`, `plugin.rs`, `approval.rs`, `held_notice.rs`, `presence.rs`, `remote_input.rs`, `snapshot.rs`. (`plugin.rs`: Task 17 keeps the installed plugin, and nothing re-installs it. `approval.rs` is replaced by Task 15b.)
  - Delete `headless.rs` and `examples/local_session_probe.rs`, and the `serve`, `session-mcp` and `local-session-hook` arms in `main.rs`.
  - Remove the `join` path in `cli.rs`. The app binary keeps `claude`, `codex`, `share` and `continue`.
  - In the same commit, rewrite the kept files that mention `transport::`: `local_sessions.rs`, `screen.rs`, `resume.rs`, `mod_socket.rs`, `session_states.rs` (remove the relay flags `READS_*`), `start_links.rs`, `workspace.rs`, `stats.rs`, `session_pickup.rs`, `session_continue.rs`, `session_browser.rs`, `core_boundary.rs`, `claude_export.rs`.
  - Gate pattern: `transport::|local-session-hook|session-mcp`. Run `cd apps/desktop && cargo test`.
  - Commit `refactor(desktop)!: delete the session hub client`.
- [ ] **21b — backend.**
  - Delete `core/services/local_session_relay.py`, `local_session_grants.py` and `native_session_tools.py`.
  - Delete the `/local-sessions/device/*` routes in `routers/messaging.py`, `/local-sessions/enrollment` in `routers/desktop_pair.py`, and the local-session path in `routers/websocket_chat.py`.
  - Delete the pairing half of `device_login.py` and `/auth/device/*` in `routers/desktop_auth.py`.
  - Delete `webhook_agents.defang` and `scripts/load/mac_sim.py` (it simulates the hub).
  - Delete every test file whose subject is gone, and their `conftest.py` stubs.
  - The gate pattern adds `local_session_relay|local_session_grants|local_sessions_repo|native_session_tools|/local-sessions/`. Run `uv run pytest tests/`.
  - Commit `refactor(backend)!: delete the session hub`.
- [ ] **21c — shared, web, mobile and Hermes.**
  - Delete `packages/shared/fixtures/local-session-frames/` and the hub-frame parsers among `packages/shared/src/messaging/localSession*.ts`. Keep what renders `workspace_sessions` data, and rename any file whose name says local-session.
  - Delete `apps/frontend/src/components/desktop/LocalSessions.tsx`, `apps/mobile/src/components/session/SessionApprovalCard.tsx` (Task 20 replaces it) and `apps/infra/hermes/tests/test_native_sessions.py`.
  - The gate pattern adds `localSession|LocalSession`. Run `turbo run test lint`.
  - Commit `refactor(clients)!: delete hub-frame clients`.
- [ ] **21d — docs.** Update `CLAUDE.md`:
  - The Messaging, WebSocket Architecture and Desktop App sections describe the engine, the bridge, the relay and the issuer.
  - The stack table gains the relay and bridge stacks.
  - `workspace_sessions` is named as the session model, with the bridge as its writer.

  Commit `docs: CLAUDE.md after the network cutover`.

### Task 22: The release train (ordered; each step has a check and a rollback)

**Files:** Create `docs/runbooks/network-cutover.md`, containing this table; run ids are filled in during the train.

| # | Step | Check | Rollback |
|---|---|---|---|
| 0 | Before the merge: tag `main` as `hub-final`. Preconditions: Phase 1 is in prod; the engine tag carries every "still needed" item and Plan B's acceptance passed; O3 and O4 are done. | the tag exists; `/network/relays.json` is served | none needed |
| 1 | Merge `feat/network-cutover` (Codex reviewed, all four surfaces). Dev deploys with `NETWORK_ENABLED` still off, so backend and web stay dark. | `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status --interval 60`; `/api/v1/network/*` answers 404 | revert the merge |
| 2 | A PR flips `NETWORK_ENABLED` and `NEXT_PUBLIC_NETWORK_ENABLED` on, and points `/install` at `network/install.sh`. Dev deploys; then run the dev acceptance smoke on a debug engine: link, Friday send, phone Allow. | the smoke rows are recorded | flip the flags off |
| 3 | Prod: when `gh run list … --json status` shows 0 runs in flight, run `gh workflow run deploy.yml --ref main`. | the ECS task-definition image is the merge sha; `/network/device/start` answers | flip the flags off and re-dispatch. If the code itself is bad: revert to `hub-final` and re-dispatch (the ws door still exists until Task 23) |
| 4 | Desktop: `gh workflow run desktop-build.yml --ref main`; auto-update | the DMG's bundled engine sha matches the pin; the first run cleans up (Task 17) | re-publish the previous `desktop-v0.4.*` release as latest |
| 5 | Mobile: flip `EXPO_PUBLIC_NETWORK_ENABLED` in `mobile-ota.yml`; the merge ships the OTA | the phone shows the hold card | flip it back (another OTA) |
| 6 | Hermes, in order: a pin-flip PR for `dev.tag`; dev deploy; on dev, `roll_hermes_runtimes.py` and `reapply_hermes_skills_dirs.py`; a pin-flip PR for `prod.tag`; prod dispatch; on prod, roll and reapply again. | `get-agent-runtime` shows `containerUri` = the new tag; Friday's tool is `lightbulb_sessions` | flip the tags back and roll |
| 7 | The owner links: sign in, Connect my workspace, the founder's Allow | `GET /api/v1/network/bridge` shows `state: joined` | Disconnect workspace |

Between steps 3 and 6, the old runtimes call routes that no longer exist, so Friday's session tool fails with a plain error until the roll finishes. The runbook says so.

- [ ] **Step 1:** Write the runbook. **Step 2:** Run the train, recording each check. **Step 3:** Commit `docs(runbooks): network cutover train`.

### Task 23: Retire the WebSocket door (a separate PR, after a week of soak)

**Files:**
- Modify `apps/infra/lib/stacks/api-stack.ts`: remove the WebSocket API, the `ws-connect`, `ws-disconnect` and `ws-message` integrations, `websocket-authorizer`, and the `ws[-env]` record. Mark `ws-connections` `RemovalPolicy.RETAIN` and detach it.
- Delete `apps/infra/lambda/ws-connect/`, `ws-disconnect/`, `ws-message/` and `websocket-authorizer/`, and the rest of `apps/backend/routers/websocket_chat.py`.
- Remove `gatewayUrl` from `routers/desktop_pair.py`.
- Regenerate `.github/workflows/deploy.yml`.
- `test_no_hub_left` drops its exclusions.
- This PR's description names a follow-up PR that deletes the retained `ws-connections` table once its rows have expired.
- `bv[-env]` and the machine gateway relay stay.

- [ ] **Step 1: Failing tests** — `no_websocket_api_remains`, `bv_door_remains`, `test_no_hub_left` with no exclusions; `deploy-prod-promotion.test.ts` still passes.
- [ ] **Step 2:** FAIL. **Step 3:** Implement; `npx cdk synth`. **Step 4:** PASS.
- [ ] **Step 5:** Commit `refactor(infra)!: retire the local-session WebSocket door`.

### Task 24: Acceptance

**Files:** Create `docs/superpowers/plans/2026-10-22-lightbulb-network-plan-c-acceptance.md`, recording per row: the command or tap, the observed text, the receipt JSON, PASS or FAIL, the build sha and the date.

- [ ] **Step 1: Setup.**
  - The owner's Mac runs the cutover DMG.
  - A second Mac (or a second macOS user) has only the free install line and no account.
  - The owner's phone runs the prod app.
  - The workspace account is **Isol8 Admin**, signed in through a Clerk sign-in ticket (the CLAUDE.md recipe; no password, no OTP).
- [ ] **Step 2: Design rows 1–10.** Show parity item 1 as "sign in, then paste the join line from Your computers". Record parity item 8 (files) as a Plan D gap. Show item 11 with the tray online view.
- [ ] **Step 3: Bridge rows 1–17.**
  - Rows 2–3: the phone shows only the sender, the agent and the computer.
  - Row 15: grep the backend, bridge and relay logs and the messaging table for the unique markers. They appear only in mailbox rows, the Friday room, and the session mirror of sessions the person shared.
  - Add: a stranger's message to `…/friday` is refused. A teammate's reply to Friday follows the answer to still-needed item 9.
- [ ] **Step 4: App rows 1–23.** Row 22: from devtools on `https://thelightbulb.company/chat`, call every person-only `network_*` command. Each opens a sheet, and Cancel changes nothing.
- [ ] **Step 5: O1 continuity**, on web and phone: a shared session's live terminal, its transcript history, a public link, session chat, a pick-up, teammate typing, the presence band, and a resume from the phone. **O2:** answer a Claude permission prompt from the phone.
- [ ] **Step 6: Hosted rows that need no new account.**
  - Claude.ai web; Grok Bot (key and webhook); a DCR script.
  - A "no workspace" sign-in; a revoked key; leaving the workspace.
  - The upgrade path: a free network of two connects the workspace, then disconnects, and the network stays intact.
  - Record the direct-connection rate across all rows (QUIC discovery is off).
- [ ] **Step 7:** Commit `docs(network): plan c acceptance` on a branch; PR; Codex review; merge. A FAIL is fixed forward and its row run again.

## Self-review

- **Coverage of the specs.**
  - Architecture: §1–§2 in Tasks 1, 2 and 15a; §3 in Tasks 3 and 7; §4 (one link per person; a cloud member admitted by the founder or an admin) in Task 7; §5 names and labels in Global Constraints and Tasks 8, 9 and 14; §7 scopes, claim and holds in Tasks 11, 15a, 15b and 20; §8 grants in Task 18; §9 (the MCP subset, `check_inbox` ≤ 25 s) in Tasks 9 and 13; §10, the bridge, in Tasks 5–8 and 12; §11 cutover in Tasks 21–23.
  - Bridge spec: the data rows, the wipes and the membership-deleted unlink in Task 7; `/proof/refresh` in Task 3; the threat-model tests in Tasks 8 and 11.
  - App spec: commands, failure modes and acceptance in Tasks 15a–16 and 24.
  - Onboarding spec: the installer, `/j/` and the typed device code in Tasks 2 and 3.
  - Parity: item 1 through Your computers; item 11 through the tray; item 8 stays in Plan D.
- **Coverage of the rulings.**
  - O1: Tasks 18, 19 and 24. O2: Tasks 11, 15b and 20.
  - A: cloud join, leave and unlink consumed in Task 7; the MCP subset rule in Tasks 9 and 13; framing done in the bridge in Tasks 8 and 10; three issuer keys and the dev kid; one link per person; `<App>` = the sanitised `agent`; `ui.claim {}` with no two-app fields.
  - B: every guess is decided, in Global Constraints or in its task.
  - C: the ledger requester in Task 14; the cloud inbound policy in Task 8; sheets and the mod-socket guard in Task 15c; the bare `/j/` in Task 2; the typed code in Task 3; the VPC door in Task 6; the membership-deleted unlink in Task 7; the admission copy in still-needed item 7; audit rows in Tasks 3 and 11.
  - D: the ordered train with rollback in Task 22; the importers table in Task 19; the Mac cleanup in Task 17; Phase 1 behind flags.
  - E: the relay behind the ALB with QUIC off in Task 4; the lease in the containers table in Task 5; daily proof renewal in Task 7; the 25 s cap; the slug check.
  - G: Task 14 split into 15a, 15b and 15c; Task 21 split per area; Task 10 has Interfaces; no `COMPUTER#` rows; no boot resend.
- **Type consistency with Plan B.** These names are used exactly as in the consumed-names table: `hold.answer {id, verdict, scope}`, `held`/`hold_closed`, `ui.claim {}`, `stream.open {session}`, `stream.send {stream_id, data_b64}`, `team.invite {mode}`, `team.join {ticket, name, kind, proof}`, `cloud.sessions.set`, `inbox {session}`, `decision.send`, `hold_notice`, `decision_result`, `receipt_changed`, `member_removed`. Names Plan B does not state yet are the items under "Still needed". On our side the scope enum is `once|person|own_cloud`.
- **Review Focus.** Each of the five items is pinned to named tests in Tasks 5, 6, 8, 11, 12, 14 and 15b/c, and to acceptance rows.
- **Proportion.** 24 tasks; Tasks 15 and 21 have sub-commits. Phase 1 is 12 additive tasks behind flags. Phase 2 is one PR of 9 tasks (13–21), plus the train. Retiring the door is a separate PR. There are no function bodies.
- **Ponytail cuts stated.**
  - One control stream per Mac: head-of-line blocking across that Mac's sessions; split it per session if that shows.
  - Relay access is open but rate-limited, because we cannot know who the free members are.
  - A single bridge task: a deploy is a short outage.
- **Rulings that could not be applied fully.**
  - "TLS at the existing ALB": that ALB is internal, so the public door is a CloudFront VPC origin in front of it (the `bv-` pattern). There is still no NLB.
  - O1: the features that go from the backend to the Mac depend on still-needed item 1.
  - The binding cloud inbound policy refuses teammates' replies to Friday (still-needed item 9).
