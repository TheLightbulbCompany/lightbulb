# Hosted apps on the free network (owner-approved 2026-10-25)

Replaces item 12 of 2026-10-22-lightbulb-network-p2p-design.md ("hosted assistants … the paid workspace's
bridge"). Modelled on Reason Network's shipped behaviour (research: reason-hosted.md, primary sources): hosted
apps route only through the person's own logged-in computer (final owner scope, 2026-10-25).

## Three kinds of members (unchanged)
1. Computers run the engine; sessions are their coding agents. Free. Peer to peer; n0 relays carry only
   encrypted bytes. No Lightbulb server between computers.
2. A workspace (Friday etc. on Hermes/AgentCore) runs the same engine in the cloud: an ordinary member that
   sleeps and wakes on its doorbell; answers only its members. Paid.
3. Hosted apps (Claude app, ChatGPT, Muse, Grok Bot) cannot run the engine. They use our hosted MCP server,
   which hands each request to the signed-in person's own computer the app is pinned to; it sends peer to peer.

## What a person does
1. Install `lightbulb`; `lightbulb login` (browser; creates a free Lightbulb account if none — Google/GitHub/
   Apple/email via Clerk, no workspace, no card). This computer is now theirs.
2. In the app: add the connector URL `https://api.thelightbulb.company/mcp`, sign in (OAuth). Bots/Muse may use
   OAuth or an `lbk_` key.
3. The app appears on the network as a session `<computer>/<app>-<person>` labelled "Claude via <Name>".
   Tools are the engine's: find_sessions, send_message (reply_to), check_inbox (wait), message_receipt, plus
   `wake_me` for bots.

## Routes
- Hosted apps route ONLY through one of the person's own logged-in computers (Reason's model). Each app
  connection (an OAuth client, or an `lbk_` key) is PINNED at connect to one computer, so its session
  `<computer>/<app>-<person>` and its inbox live in one place. Default: the computer the request came from if
  identifiable, else the most recently connected one; the person changes it on `/auth/network/apps` (phone apps
  such as Muse pick from the list there). Pins are rows in the existing messaging table (`NETPIN#<user>`), no new
  table.
- The pinned computer offline: "<computer> is offline. Open it." No computer online at all: "None of your
  computers is online; open one". Never another computer, never a queue on the server.
- The workspace is not an app route. It stays what it is: its agents join a network via `lightbulb login` +
  `invite --agents`, behind the member gate.
- A logged-in computer's daemon keeps ONE outbound connection to the backend (WebSocket on the existing
  machine-relay door, `bv[-env]`, `/api/v1/gateway/network`): its hello carries its member key's signature over a
  server nonce and its sign-in proof (below). Presence is a TTL row in the existing ws-connections table
  (ConnectionService pattern). Over it the backend sends only the `app.*` ops (app.send, app.inbox peek/ack,
  app.unread, app.receipt, directory). The computer runs them locally; nothing else is exposed.
- The server sees app message text in plaintext in transit (as Reason's does); it stores none of it. Computers
  stay end-to-end encrypted between themselves.

## Accounts
- A free account is a Clerk user with no organisation. Network endpoints (login, hosted MCP network tools,
  wake, the apps page) accept a user without an org; nothing else does (no workspace, trial row, container or
  Paperclip for them).
- `lightbulb login` (device flow: the computer signs a backend challenge with its member key; the person types
  the code shown in their terminal on the confirm page) returns a signed proof, like Reason: a compact EdDSA JWS
  `{iat, exp (30 days), sub: user id, computer: member key}`, signed with a Lightbulb key (private seed in Secrets
  Manager via CDK; public key baked into the engine, `LIGHTBULB_ISSUER_KEY` override for dev and for workspace
  runtimes). The computer stores it (`login.json`, 0600) and renews it in its last week. No key-to-user table.
- Workspace gate: computers show their proof in the network hello; the workspace engine verifies it offline and
  answers only `sub` in `members` (sessions.json); no proof, an unknown user, or a failed verification is
  refused (fail closed when `members` is present).
- Logout deletes the local proof and drops the route link. Proofs are not revocable before exp (30 days);
  removing a person from the workspace is instant via `members`.

## Bots: wake_me
- `wake_me {routine webhook url}`: the bot asks to be woken; the person pastes the webhook key on a Lightbulb
  page (never in chat). On new mail for that app session its pinned computer tells the backend (the session
  name only), which POSTs the webhook once (content-free); otherwise bots poll check_inbox.

## Owner actions
- Enable DCR in Clerk (Reason relies on it; CIMD stays on).
- Key ceremony: after the first deploy creates `lightbulb/{env}/network_proof_key`, bake the prod public key
  (`GET /api/v1/network/issuer`) into the engine's `proof::ISSUER_KEYS`, and roll runtimes with `--respec` so
  each gets `LIGHTBULB_ISSUER_KEY`.

## Not in scope now
- Server-side queueing when no route is online; webhook agents as network addresses (reverted).
