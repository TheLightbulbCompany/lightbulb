# A2A phase 2b: the runtime signs in as the workspace

**Status:** draft for the founder's approval (idea approved 3 Nov; this document folds the builder check). Nothing is built.
**Repos:** the CLI engine (after `feat/a2a-phase2`) and the monorepo backend and Hermes bridge (after `feat/a2a-phase2-bridge`).

## 1. Intent

A workspace's runtime (where Friday and the other hosted agents run) already runs the same engine as a laptop and is a member of the same network. The one thing it lacks is a sign-in: a laptop's computer carries a proof from the backend saying "this computer belongs to person X", and the runtime carries none, so a laptop cannot tell whom it may speak for and refuses what it relays. This phase signs the runtime in as its workspace. One rule then covers every sender: the sending computer must be signed in as whoever the authority note says it carries.

## 2. Goals, non-goals, anti-goals

Goals: the backend mints a workspace proof for its own runtime only; the runtime presents it like a laptop; a laptop accepts a hosted note for workspace W from a computer signed in as W; the hold dialog and the record say the ask came via the workspace's agent; the member route (`A2A_DISPATCH_VIA_MEMBER`) can then default on.

Non-goals: per-agent identities (one runtime acts for the whole workspace, so one proof); a new key or issuer; changes to the authority object, the rules document, or the hold machinery; a peer-revocation list on laptops.

Anti-goals: a runtime that can obtain a proof for any key but its own; a laptop trusting a computer that merely claims a workspace; removing the person-link route (it stays as the fallback and for workspaces without a warm runtime).

## 3. What exists (corrected by the builder check)

- Proofs are minted by `network_proof.mint` (`network_proof.py:62-79`, `org` claim at :77); `network_login` mints at sign-in through a browser device-code flow (:190-205) and refreshes on a nonce signed by the computer's own key (:222), carrying `org` forward (:229).
- The CLI's `proof::verify` requires `sub` to start with `user_` (`proof.rs:94`); its `Proof` struct has no `org` field (`proof.rs:56-65`), so the CLI never reads the workspace today. `proof::admitted` admits computers whose proof names a person in `members` (:115-126).
- `own_proof` returns nothing for an embedded instance (`net/mod.rs:1559`, gated on `d.cloud.is_none()`); `embed::start` gives the instance its own home directory (`embed.rs:50-60`), so a `login.json` there is feasible once the gate is lifted.
- The bridge holds the runtime's member key in memory from its boot secret (`bridge/network.py`), so it can sign a nonce with it. There is no route today for a runtime to obtain a proof.
- `authority::bind` accepts a relayed note only when the sender's proof person equals `chain[1].person` with a two-hop chain (`authority.rs:266`). Phase 1 mints a hosted hop as `chain[1] = {agent: <agent id>, session: <run id>, person: <origin person>}` (`a2a_tasks.py ask_session`). `bind` receives only `sender_person` (`authority.rs:244`, called at `mail.rs:1161`).
- `Judge.sender_person` comes from the peer's verified proof `sub` (`net/mod.rs:1348`) and becomes the `person:<sub>` requester with `origin_is_person` true when no note is bound (`mail.rs:1338-1346`). `presence::claimed_user` reports a `user_` sub only (`presence.rs:563`).
- The backend's `revoked_keys` reaches only the runtime (`cloud.sessions.set`); laptops keep no revoked list for a peer's proof.

## 4. The design

**The workspace proof.** Claims `{iat, exp, sub: "org_<workspace id>", org: "org_<workspace id>", computer: <runtime member key>}`, signed by the proof issuer key, 30-day life. A new backend route `POST /api/v1/internal/network/{owner_id}/runtime-proof` (hook token, the same dependency as the other internal routes) takes `{key, nonce, sig}`: the backend checks that `key` is the workspace's recorded runtime member key (`network_member`'s record) and that `sig` is the key's signature over the nonce (the `refresh` pattern), then mints. Any other key is refused. *(My call.)*

**The runtime presents it.** The bridge calls the route at sidecar start and when the runtime re-keys (the contested-key path, `bridge/network.py`), and again before half the proof's life has passed; it writes `login.json` into the engine home. The engine drops the `d.cloud.is_none()` gate in `own_proof`, so an instance shows its proof in every hello. *(My call.)*

**Laptop verification.** `Proof` gains an optional `org`. `proof::verify` accepts a `sub` starting with `user_` or `org_`; an `org_` sub must equal its `org` claim. `proof::admitted` is unchanged (it admits people; the runtime is admitted as a roster member, as today). `presence::claimed_user` keeps reporting only `user_` subjects. *(My call.)*

**Judge.** A workspace sender never becomes a person: `Judge.sender_person` stays `None` for an `org_` proof, and a new `Judge.sender_workspace: Option<&str>` carries the org. Without a bound note such a sender is judged as `member:<short>` (today's behaviour). *(My call.)*

**Binding.** `bind` gains `sender_workspace`. A note is accepted from a workspace sender when `sender_workspace == requester.org`, the chain has exactly two hops, and `chain[1].agent` is set (a hosted agent's hop as phase 1 mints it). The origin check (`chain[0].person == requester.person`, enforced in `verify`) is unchanged. The phase-2 rule for a person relaying (`chain[1].person == sender_person`) stays beside it. *(My call.)*

**Record and dialog.** The hold row and the signed record gain `relayed_by: "org_<id>"` (one nullable column through `ADDED_COLUMNS`). The laptop's dialog line reads "<agent> for <person>, via your workspace's agent". The backend's hold notice is unchanged. *(My call.)*

**Revocation.** A workspace proof lapses at its expiry; the backend stops renewing when the workspace leaves the network or the runtime is retired, and a re-key mints a proof for the new key only. A laptop has no peer-revocation list, so the bound is the 30-day life, the same bound a signed-out laptop's proof already has. *(My call; a shorter life for workspace proofs, such as 7 days, is a one-constant change if you want it.)*

**Default flip.** `A2A_DISPATCH_VIA_MEMBER` stays off until the CLI release that accepts workspace proofs ships; then it defaults on, with the person link as the fallback. An old laptop refuses the relayed note (`bad_request` on bind); the backend counts `refused` as a fallback state, so the ask falls back to the person link without a resend. *(My call.)*

## 5. Compatibility

No wire or protocol change. The CLI gains an optional proof field and one nullable column; old laptops refuse a workspace proof and keep falling back. The backend gains one internal route; the bridge gains one call at start and on re-key.

## 6. Tests (acceptance)

1. Backend: the route mints only for the workspace's recorded runtime key with a valid nonce signature; a wrong key, a bad signature or another workspace's token is refused.
2. CLI: a workspace proof from the debug test issuer verifies; an `org_` sub that differs from its `org` claim is refused; `Judge.sender_person` is `None` and `sender_workspace` is set; `bind` accepts a two-hop hosted note for the sender's workspace and refuses one for another workspace, a one-hop note, and a note whose `chain[1].agent` is missing.
3. End to end in containers (debug builds, test issuer): two laptops plus an embedded runtime signed in as W; a hosted ask for person P relayed by the runtime is held on the target laptop with the dialog naming the agent, P and the workspace; Allow delivers and the record carries `relayed_by`; the same note from a runtime signed in as another workspace is refused.
4. The phase-2 CLI and monorepo suites stay green.

## 7. Size

CLI: about five tasks (proof `org` and the `org_` prefix; the `own_proof` gate and the instance's `login.json`; `Judge` and `bind`; the record column and dialog; tests). Backend and bridge: about four (the runtime-proof route; the bridge fetch, write and renew; renewal stop on leave or re-key; the default flip). No protocol change; one nullable column.

## 8. For the founder

Every open choice above is marked as my call. The one you may want to change is the workspace proof's life: 30 days matches a laptop's today; 7 days narrows the window after a runtime is retired at the cost of more frequent renewal.
